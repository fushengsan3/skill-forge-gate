#!/usr/bin/env python3
"""
构建浏览器扩展（R5）—— 把 templates/extension/ 变成一个能直接「加载解压缩的扩展」的目录。

为什么需要"构建"这一步，而不是让用户直接加载 templates/extension/：

  面板的绝对路径必须在**扩展加载之前**就写死在代码里。而零权限扩展
  （这是硬约束，见 templates/extension/background.js 顶部）：
    - 加 "storage" 权限才能存配置 —— 那就不是零权限了
    - 也读不到 USERPROFILE 之类的环境信息 —— 自己找不着面板在哪

  两边一夹，只剩一个办法：装的时候把路径编译进去。

所以这个脚本干三件事：
  1. 把 `__PANEL_URL__` / `__PANEL_PATH__` 换成真实路径
  2. **验证**清单里一个权限都没有 —— 见 verify_zero_permissions()，
     这是把"零权限"从注释里的承诺变成构建时会失败的规则
  3. 输出到 `~/.claude/skills/skill-forge/extension/`，供 edge://extensions 加载

用法：
    python -m daemon.build_extension                 # 构建到运行时目录
    python -m daemon.build_extension --out DIR       # 指定输出目录
    python -m daemon.build_extension --panel PATH    # 指向别的面板文件
    python -m daemon.build_extension --dry-run       # 只检查，不落盘
"""
import argparse
import json
import shutil
import sys
import tempfile
from html import escape as html_escape
from pathlib import Path

# 本模块既能 `python -m daemon.build_extension` 跑，也能直接
# `python daemon/build_extension.py` 跑。后者的 sys.path[0] 是 daemon/，
# 看不见 `daemon` 这个包，所以补一次父目录（用 __file__ 定位，
# 这样在仓库副本里跑也 import 到仓库自己的那份）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon import safe_paths

SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"

SRC_DIR = SKILL_ROOT / "templates" / "extension"
OUT_DIR = SKILL_ROOT / "extension"
PANEL_FILE = SKILL_ROOT / "discover" / "latest.html"

# 上次构建留下的产出清单。清理**只**以它为准 —— 见 _remove_stale 的说明。
MANIFEST_NAME = ".build-manifest.json"

# 待替换的占位符
URL_TOKEN = "'__PANEL_URL__'"      # 连引号一起换，避免"路径里有单引号"这种破事
PATH_TOKEN = "__PANEL_PATH__"      # 出现在 HTML 属性里，要按 HTML 转义

# 已知的"能拿到能力"的清单键。单独列出来只为给一句准确的错误信息 ——
# 真正兜底的是下面的白名单。
FORBIDDEN_MANIFEST_KEYS = (
    "permissions",
    "optional_permissions",
    "host_permissions",
    "optional_host_permissions",
    "content_scripts",
    "web_accessible_resources",
    "externally_connectable",
    "devtools_page",
    "chrome_url_overrides",
    "declarative_net_request",
    "chrome_settings_overrides",
    "protocol_handlers",
    "side_panel",
    "omnibox",
    "commands",
    "sandbox",
    "tts_engine",
    "input_components",
    "file_handlers",
    "file_browser_handlers",
    "storage",
    "update_url",
    # 能**放宽扩展页自己的 CSP**（比如允许远端脚本）—— 等于把 R8 的防线拆掉
    "content_security_policy",
)

# 白名单：清单里只允许出现这些键。
#
# 为什么是白名单而不是黑名单：黑名单只能挡住**写的时候想到的**键，
# 而 MV3 的清单键有几十个，漏一个是迟早的事 —— 2026-10-05 的独立审计
# 正是这么说的。白名单把默认值反过来：新键一律拒绝，要加就得显式改这里，
# 于是"这个键会不会带来能力"这个问题**每次都必须被回答一次**。
ALLOWED_MANIFEST_KEYS = frozenset({
    # 纯元数据，不含任何能力
    "manifest_version", "name", "short_name", "version", "version_name",
    "description", "author", "homepage_url", "default_locale",
    "minimum_chrome_version",
    # 本扩展真正需要的三样
    "action",        # 工具栏按钮 —— onClicked 的唯一入口
    "background",    # service worker
    "options_ui",    # 说明页（右键图标 → 扩展选项）
    "icons",         # 纯静态图片
})

# 后台脚本里不许出现的 API。构建时静态扫一遍。
#
# 这是**兜底，不是证明** —— 静态扫描挡不住刻意绕过（拼接字符串、动态取属性）。
# 它拦的是"顺手加了一行"：想加就一定会撞上它，然后被迫停下来想一想。
FORBIDDEN_JS_APIS = (
    ("chrome.storage", "持久化存储（零权限扩展拿不到这个 API）"),
    ("XMLHttpRequest", "网络请求"),
    ("fetch(", "网络请求"),
    ("WebSocket", "网络请求"),
    ("importScripts", "加载外部脚本"),
    ("eval(", "动态求值"),
    ("new Function", "动态求值"),
    ("innerHTML", "DOM 解析"),
    ("outerHTML", "DOM 解析"),
    ("document.", "操作页面（后台 service worker 里根本没有 DOM）"),
    ("localStorage", "落盘"),
    ("sessionStorage", "落盘"),
    ("indexedDB", "落盘"),
    ("chrome.cookies", "读 Cookie"),
    ("chrome.history", "读历史"),
    ("chrome.bookmarks", "读书签"),
    ("chrome.management", "操作其他扩展"),
    ("chrome.debugger", "调试其他页面"),
    ("chrome.scripting", "注入脚本"),
    ("chrome.webRequest", "监听网络"),
    ("chrome.declarativeNetRequest", "改写网络请求"),
    ("chrome.runtime.onMessage", "被别的扩展/页面唤醒"),
)


class BuildError(Exception):
    """构建前置条件不满足 —— 不产出半成品。"""


def verify_zero_permissions(manifest: dict) -> None:
    """清单里不许有任何权限。不满足就抛 BuildError。

    这条规则存在的意义不是"检查"，是"拦住"：以后有人为了省事加个
    "storage" 或 host_permissions，构建会当场红，而不是在某个安静的下午
    把一次 XSS 的影响面从"一个标签页"放大成"整个浏览器"。
    """
    found = [k for k in FORBIDDEN_MANIFEST_KEYS if k in manifest]
    if found:
        raise BuildError(
            "清单里出现了能拿到能力的字段：" + ", ".join(found) +
            "。本扩展的设计前提是零权限（决策记录 §7 R5），"
            "需要额外能力时请先改设计文档，而不是在这里加清单键。"
        )

    unknown = sorted(set(manifest) - ALLOWED_MANIFEST_KEYS)
    if unknown:
        raise BuildError(
            "清单里有未列入白名单的键：" + ", ".join(unknown) + "。\n"
            "  - 如果它确实只是元数据（不含任何能力），把它加进 "
            "daemon/build_extension.py 的 ALLOWED_MANIFEST_KEYS；\n"
            "  - 如果它可能带来能力，先改设计文档（决策记录 §7 R5）再动这里。\n"
            "  白名单意味着每加一个键，都得显式回答一次'它会不会带来能力'。"
        )

    action = manifest.get("action") or {}
    if "default_popup" in action:
        raise BuildError(
            "action.default_popup 会让 chrome.action.onClicked 失效，"
            "扩展就再也开不了面板了（面板是标签页，不是弹窗）。"
        )


def verify_background_script(src_dir: Path) -> None:
    """后台脚本里不许碰能力型 API。

    刻意**不**剥注释再扫：剥注释要处理字符串里的 `//`（比如 URL），
    处理不好会变成漏报。宁可让一句注释触发它、然后去把那句注释改掉 ——
    失败方向朝"吵"而不是朝"静悄悄放过"。
    """
    worker = src_dir / "background.js"
    if not worker.exists():
        raise BuildError(f"找不到 {worker}")
    body = worker.read_text(encoding="utf-8")

    hits = [(token, why) for token, why in FORBIDDEN_JS_APIS if token in body]
    if hits:
        raise BuildError(
            "后台脚本里出现了不该有的 API：\n"
            + "\n".join(f"  - {t}（{w}）" for t, w in hits)
            + "\n本扩展只做一件事：在新标签页里打开一个事先写死的 file:// 地址。"
        )


def load_manifest(src_dir: Path) -> dict:
    path = src_dir / "manifest.json"
    if not path.exists():
        raise BuildError(f"找不到扩展清单：{path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BuildError(f"扩展清单不是合法 JSON：{path} — {e}") from e
    if manifest.get("manifest_version") != 3:
        raise BuildError("只支持 MV3（MV2 在 Edge/Chrome 上已经不能加载）")
    worker = ((manifest.get("background") or {}).get("service_worker") or "")
    if not worker:
        raise BuildError("清单里没有 background.service_worker")
    return manifest


def check_templates(src_dir: Path) -> None:
    """替换之前，确认模板里的占位符还在。

    占位符不在 = 模板被改过（或者已经不是这份模板），
    这时候构建出来的扩展会指向一个陈旧或错误的地址，
    而且**不会报错** —— 用户只会看到点了没反应。宁可直接失败。
    """
    worker = src_dir / "background.js"
    if not worker.exists():
        raise BuildError(f"找不到 {worker}")
    if URL_TOKEN not in worker.read_text(encoding="utf-8"):
        raise BuildError(
            f"{worker} 里找不到占位符 {URL_TOKEN}。"
            "构建靠它注入面板地址，没有它就只能产出打不开的扩展。"
        )


def _iter_files(src_dir: Path):
    for p in sorted(src_dir.rglob("*")):
        if p.is_file():
            yield p


def _substitute(text: str, panel_uri: str, panel_display: str) -> str:
    return (text.replace(URL_TOKEN, json.dumps(panel_uri))
                .replace(PATH_TOKEN, html_escape(panel_display)))


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in {".js", ".json", ".html", ".css", ".txt", ".md"}


def _read_previous_manifest(target: Path) -> set:
    """读上一次构建自己写下的清单。没有/坏了就当空集（= 什么都不删）。"""
    path = target / MANIFEST_NAME
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return set()
    # 只收"能安全当相对路径用"的条目
    return {Path(f) for f in files if isinstance(f, str) and f and not Path(f).is_absolute()}


def _remove_stale(target: Path, keep: set, previous: set, dry_run: bool) -> list:
    """删掉**上一次构建自己产出、这次不再产出**的文件，返回被删的相对路径。

    为什么只删 previous 里的，而不是"删掉所有不在本次产物里的文件"：
    后者等于把 `--out` 变成一个清空命令 —— 指向任何已有目录
    （`--out C:\\Users\\...\\Documents`）就会把里面的东西全删了。
    2026-10-05 的独立审计确认过这条。构建工具没有资格删它没造出来的东西。

    清单文件缺失（第一次构建、或用户手动清过）时 previous 是空集 ——
    宁可不清理，也不猜。
    """
    if dry_run or not target.exists():
        return []
    removed = []
    for rel in sorted(previous - keep):
        p = target / rel
        # 双保险：必须是普通文件，且解析后确实还在 target 里面
        if not safe_paths.is_within(target, p):
            continue
        try:
            if not p.is_file():
                continue
            p.unlink()
            removed.append(str(rel))
        except OSError:
            # 被占着（比如浏览器正拿着）就留着，不该因为清理失败而让构建整个失败
            pass
    return removed


def _list_untracked(target: Path, keep: set, previous: set, dry_run: bool) -> list:
    """产物目录里既不是本次产出、也不在上次清单里的文件（= 用户自己放的）。

    我们不删它们，但要说出来 —— 否则"加载的内容 ≠ 产物目录的内容"这件事
    又会变回不可见状态。
    """
    if dry_run or not target.exists():
        return []
    out = []
    for p in sorted(target.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(target)
        if rel in keep or rel in previous or rel == Path(MANIFEST_NAME):
            continue
        out.append(str(rel))
    return out


def _write_manifest(target: Path, written: list) -> None:
    """记下这次构建产出了什么，供下次构建清理用。"""
    try:
        (target / MANIFEST_NAME).write_text(
            json.dumps({"version": 1, "generator": "daemon/build_extension.py",
                        "files": sorted(written)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # 写不了清单只是失去"下次能清理"的能力，不该让构建失败
        pass


def build(src_dir: Path = None, out_dir: Path = None, panel_file: Path = None,
          dry_run: bool = False) -> dict:
    """构建扩展，返回一份自述用的结果字典。"""
    src_dir = Path(src_dir) if src_dir else SRC_DIR
    out_dir = Path(out_dir) if out_dir else OUT_DIR
    panel_file = Path(panel_file) if panel_file else PANEL_FILE

    if not src_dir.is_dir():
        raise BuildError(f"找不到扩展模板目录：{src_dir}")

    manifest = load_manifest(src_dir)
    verify_zero_permissions(manifest)
    verify_background_script(src_dir)
    check_templates(src_dir)

    # as_uri() 会做百分号编码 —— 路径里如果有中文或空格，不编码的话
    # 浏览器打开时会按自己的规则切分，结果是找不到文件。
    panel_uri = panel_file.resolve().as_uri()
    panel_display = str(panel_file.resolve())

    warnings = []
    if not panel_file.exists():
        warnings.append(
            f"面板文件还不存在：{panel_file}。扩展可以照常装，"
            "但在第一次周度扫描产出它之前，点开只会看到浏览器的文件错误页。"
        )

    target = Path(tempfile.mkdtemp(prefix="sf-ext-")) if dry_run else out_dir
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)

    written = []
    stale = []
    untracked = []
    try:
        # 先读上一次的产出清单，再动目录 —— 顺序反了就可能把自己刚写的读进来
        previous = _read_previous_manifest(target)

        for src_file in _iter_files(src_dir):
            rel = src_file.relative_to(src_dir)
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if _is_text(src_file):
                text = src_file.read_text(encoding="utf-8")
                dst.write_text(_substitute(text, panel_uri, panel_display), encoding="utf-8")
            else:
                shutil.copy2(src_file, dst)
            written.append(str(rel))

        # 清掉上一次构建产出、这次模板里已经没有的文件。
        # 不删的话，模板里"改名 / 删掉"一个文件 **不会**反映到产物里 ——
        # 浏览器加载的是产物目录，于是一个早就删掉的 background.js / help.js
        # 会继续被加载，排查起来完全看不出线索（模板里明明没有这个文件）。
        keep = {Path(w) for w in written}
        stale = _remove_stale(target, keep, previous, dry_run)

        # 目录里还有些既不是本次产物、也不在上次清单里的东西 —— 那是用户的，
        # 我们不碰，但要**如实报出来**。静默留着会让"产物目录 ≠ 加载内容"这件事
        # 重新变得不可见，正是这次清理要解决的问题。
        untracked = _list_untracked(target, keep, previous, dry_run)
        if untracked and not previous:
            # 目录里有东西、却没有任何构建清单 —— 十有八九 --out 指错了地方。
            # 我们不会删它们（见 _remove_stale），但要当面说清楚，
            # 否则用户会以为产物目录只有扩展那四个文件。
            warnings.append(
                f"输出目录里本来就有 {len(untracked)} 个不属于本工具的文件，已原样保留。"
                "如果这不是你想要的目录，检查一下 --out。"
            )

        if not dry_run:
            _write_manifest(target, written)

        # 收尾检查：任何没换掉的占位符都意味着有文件被漏了
        leftovers = []
        for name in written:
            p = target / name
            if not _is_text(p):
                continue
            body = p.read_text(encoding="utf-8")
            if "__PANEL_URL__" in body or PATH_TOKEN in body:
                leftovers.append(name)
        if leftovers:
            raise BuildError("这些文件里还有没替换的占位符：" + ", ".join(leftovers))
    except Exception:
        if dry_run:
            shutil.rmtree(target, ignore_errors=True)
        raise

    if dry_run:
        shutil.rmtree(target, ignore_errors=True)

    return {
        "ok": True,
        "out_dir": str(target),
        "panel_file": panel_display,
        "panel_uri": panel_uri,
        "panel_exists": panel_file.exists(),
        "files": written,
        "stale_removed": stale,
        "untracked_left": untracked,
        "warnings": warnings,
        "dry_run": dry_run,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建 Skill Forge Gate 浏览器扩展（零权限启动器）")
    parser.add_argument("--out", help="输出目录（默认 ~/.claude/skills/skill-forge/extension）")
    parser.add_argument("--panel", help="面板 HTML 路径（默认 .../discover/latest.html）")
    parser.add_argument("--src", help="扩展模板目录（默认 .../templates/extension）")
    parser.add_argument("--dry-run", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    try:
        result = build(
            src_dir=Path(args.src) if args.src else None,
            out_dir=Path(args.out) if args.out else None,
            panel_file=Path(args.panel) if args.panel else None,
            dry_run=args.dry_run,
        )
    except BuildError as e:
        print(f"构建失败：{e}", file=sys.stderr)
        return 1

    print("=" * 60)
    print("Skill Forge Gate 扩展构建" + ("（dry-run）" if result["dry_run"] else ""))
    print("=" * 60)
    print(f"输出目录：{result['out_dir']}")
    print(f"面板地址：{result['panel_uri']}")
    print(f"文件：{', '.join(result['files'])}")
    if result["stale_removed"]:
        print(f"已清理上次构建的残留：{', '.join(result['stale_removed'])}")
    if result["untracked_left"]:
        print(f"[注意] 产物目录里还有 {len(result['untracked_left'])} 个非本工具产出的文件，未动："
              f"{', '.join(result['untracked_left'][:5])}"
              + ("…" if len(result["untracked_left"]) > 5 else ""))
    for w in result["warnings"]:
        print(f"[注意] {w}")
    print("")
    print("加载方式：")
    print("  1. 打开 edge://extensions （Chrome 是 chrome://extensions）")
    print("  2. 打开「开发人员模式」")
    print("  3. 点「加载解压缩的扩展」，选择上面的输出目录")
    return 0


if __name__ == "__main__":
    sys.exit(main())

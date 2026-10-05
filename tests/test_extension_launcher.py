#!/usr/bin/env python3
"""
浏览器扩展启动器测试（R5）。

这个扩展的全部价值在于**它没有任何能力**，所以测试的重点不是"功能对不对"，
而是"边界有没有被守住"：

  1. 清单里一个权限都没有 —— 加了就构建失败，不是靠自觉
  2. 面板地址是构建时编译进去的，路径里有空格/中文也能打开
  3. 说明页在 MV3 的 CSP 下能跑（内联 <script> 会被静默拦掉，点按钮毫无反应）
  4. 点击行为：开关开了开面板，没开引导去说明页

第 4 条交给 Node 跑真的 background.js（extension_harness.mjs），其余在这里。

用法：
    python tests/test_extension_launcher.py
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from daemon import build_extension as be

SRC = ROOT / "templates" / "extension"

passed = []
errors = []


def check(ok, label, detail=""):
    (passed if ok else errors).append(label + (f" — {detail}" if detail else ""))


def stage_src(tmp: Path) -> Path:
    """把模板复制一份出来改 —— 测试不该改动仓库里的模板。"""
    dst = tmp / "extension-src"
    shutil.copytree(SRC, dst, dirs_exist_ok=True)
    return dst


def expect_build_error(fn, label, needle=""):
    try:
        fn()
    except be.BuildError as e:
        ok = (needle in str(e)) if needle else True
        check(ok, label, "" if ok else f"错误信息里没有 {needle!r}：{e}")
    except Exception as e:
        check(False, label, f"抛的是 {type(e).__name__} 而不是 BuildError：{e}")
    else:
        check(False, label, "本该失败，却构建成功了")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="sf-ext-test-"))
    panel = tmp / "latest.html"
    panel.write_text("<html>panel</html>", encoding="utf-8")

    try:
        run_checks(tmp, panel)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 60)
    print("浏览器扩展启动器测试（R5）")
    print("=" * 60)
    for p in passed:
        print("  [PASS] " + p)
    for e in errors:
        print("  [FAIL] " + e)
    print("")
    print(f"总计 {len(passed) + len(errors)} 项，失败 {len(errors)} 项")
    print("RESULT: " + ("PASS" if not errors else "FAIL"))
    return 0 if not errors else 1


def run_checks(tmp: Path, panel: Path):
    # ---- 1. 清单：零权限 ----
    manifest = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    check(manifest.get("manifest_version") == 3, "1 清单是 MV3")
    check(not any(k in manifest for k in be.FORBIDDEN_MANIFEST_KEYS),
          "2 清单里没有任何权限字段",
          str([k for k in be.FORBIDDEN_MANIFEST_KEYS if k in manifest]))
    check("default_popup" not in (manifest.get("action") or {}),
          "3 action 没有 default_popup（有的话 onClicked 永远不触发）")
    check((manifest.get("background") or {}).get("service_worker") == "background.js",
          "4 后台脚本指向 background.js")
    check(manifest.get("options_ui", {}).get("page") == "help.html",
          "5 说明页挂在 options_ui 上（右键图标就能进，零权限）")

    # ---- 2. 模板：占位符契约 ----
    worker_src = (SRC / "background.js").read_text(encoding="utf-8")
    check(be.URL_TOKEN in worker_src, "6 模板里保留着面板地址占位符")
    check("__PANEL_PATH__" in (SRC / "help.html").read_text(encoding="utf-8"),
          "7 说明页里保留着面板路径占位符")

    # ---- 3. 构建 ----
    out = tmp / "built"
    result = be.build(src_dir=SRC, out_dir=out, panel_file=panel)
    check(result["ok"], "8 构建成功")
    for name in ("manifest.json", "background.js", "help.html", "help.js"):
        check((out / name).exists(), f"9 产物包含 {name}")

    built_worker = (out / "background.js").read_text(encoding="utf-8")
    uri = panel.resolve().as_uri()
    check(uri in built_worker, "10 产物里注入了面板的绝对地址", uri)
    check("__PANEL_URL__" not in built_worker, "11 产物里没有残留占位符")
    check(uri.startswith("file:///"), "12 面板地址是 file:/// 形式", uri)

    built_help = (out / "help.html").read_text(encoding="utf-8")
    check("__PANEL_PATH__" not in built_help, "13 说明页里没有残留占位符")
    check(str(panel.resolve()) in built_help, "14 说明页里显示了面板真实路径")

    # ---- 4. 路径里有空格/中文 ----
    tricky = tmp / "有 空格" / "latest.html"
    tricky.parent.mkdir(parents=True, exist_ok=True)
    tricky.write_text("<html>x</html>", encoding="utf-8")
    out2 = tmp / "built2"
    r2 = be.build(src_dir=SRC, out_dir=out2, panel_file=tricky)
    uri2 = r2["panel_uri"]
    check("%20" in uri2, "15 路径里的空格被百分号编码", uri2)
    check("%E6" in uri2.upper(), "16 路径里的中文被百分号编码", uri2)
    check(uri2 in (out2 / "background.js").read_text(encoding="utf-8"),
          "17 编码后的地址被正确写进产物")

    # ---- 5. 面板还不存在：只警告，不失败 ----
    missing = tmp / "nope" / "latest.html"
    r3 = be.build(src_dir=SRC, out_dir=tmp / "built3", panel_file=missing)
    check(r3["ok"] and not r3["panel_exists"] and r3["warnings"],
          "18 面板不存在时构建仍然成功，并给出警告")

    # ---- 6. dry-run 不落盘 ----
    out4 = tmp / "built4"
    be.build(src_dir=SRC, out_dir=out4, panel_file=panel, dry_run=True)
    check(not out4.exists(), "19 dry-run 不写任何文件")

    # ---- 6b. 清理：只删**自己造出来的**东西 ----
    #
    # 这条的来历：最初的实现是"删掉产物目录里所有不在本次产物中的文件"，
    # 那等于把 `--out` 变成一个清空命令（`--out C:\Users\...\Documents` 就全没了）。
    # 独立审计 2026-10-05 确认了这一点。改成"只删上一份清单里列出的文件"。
    src6 = stage_src(tmp)
    out6 = tmp / "built6"
    be.build(src_dir=src6, out_dir=out6, panel_file=panel)
    check((out6 / be.MANIFEST_NAME).exists(), "19b 构建会留下产出清单（下次清理的依据）")

    # 用户自己的文件放进产物目录 —— 构建工具没有资格删它
    user_file = out6 / "我的笔记.txt"
    user_file.write_text("这是我放的，不该被构建删掉\n", encoding="utf-8")

    (src6 / "help.js").unlink()          # 模板里删掉一个上次构建过的文件
    r6 = be.build(src_dir=src6, out_dir=out6, panel_file=panel)

    check(not (out6 / "help.js").exists(),
          "19c 上次构建产出、这次模板已没有的文件被清掉（否则浏览器还在加载它）")
    check(set(r6["stale_removed"]) == {"help.js"},
          "19d 清理结果被如实报告（不静默）", str(r6["stale_removed"]))
    check(user_file.exists() and user_file.read_text(encoding="utf-8").startswith("这是我放的"),
          "19e ★ 非本工具产出的文件不会被删 —— --out 指向已有目录不再等于清空")
    check("我的笔记.txt" in r6["untracked_left"],
          "19f 未动的外来文件被如实报告（不留静默盲区）", str(r6["untracked_left"]))
    check((out6 / "background.js").exists(), "19g 清理不会误删本次构建的文件")

    # 目录里本来就有一堆东西、但**没有**上一份清单 → 一个都不删
    out7 = tmp / "built7"
    out7.mkdir()
    (out7 / "important.txt").write_text("别删我", encoding="utf-8")
    r7 = be.build(src_dir=SRC, out_dir=out7, panel_file=panel)
    check((out7 / "important.txt").exists() and r7["stale_removed"] == [],
          "19h 没有上次清单时不清理（宁可不删，也不猜哪些是自己的）")

    # ---- 7. 守卫：这些改动必须让构建失败 ----
    src = stage_src(tmp)

    m = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    m["permissions"] = ["storage"]
    (src / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    expect_build_error(lambda: be.build(src_dir=src, out_dir=tmp / "b5", panel_file=panel),
                       "20 清单里加 permissions → 构建失败", "权限")
    m.pop("permissions")
    (src / "manifest.json").write_text(json.dumps(m), encoding="utf-8")

    m["host_permissions"] = ["<all_urls>"]
    (src / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    expect_build_error(lambda: be.build(src_dir=src, out_dir=tmp / "b6", panel_file=panel),
                       "21 清单里加 host_permissions → 构建失败")
    m.pop("host_permissions")

    m["action"]["default_popup"] = "help.html"
    (src / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    expect_build_error(lambda: be.build(src_dir=src, out_dir=tmp / "b7", panel_file=panel),
                       "22 加了 default_popup → 构建失败", "default_popup")
    m["action"].pop("default_popup")

    m["manifest_version"] = 2
    (src / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    expect_build_error(lambda: be.build(src_dir=src, out_dir=tmp / "b8", panel_file=panel),
                       "23 MV2 清单 → 构建失败", "MV3")
    m["manifest_version"] = 3
    (src / "manifest.json").write_text(json.dumps(m), encoding="utf-8")

    (src / "background.js").write_text("// 占位符被删掉了\n", encoding="utf-8")
    expect_build_error(lambda: be.build(src_dir=src, out_dir=tmp / "b9", panel_file=panel),
                       "24 background.js 丢了占位符 → 构建失败（否则会静默指向错误地址）",
                       "占位符")

    # ---- 7b. 清单白名单：默认拒绝，而不是列举已知的危险键 ----
    def stage_named(tag):
        d = tmp / f"src-{tag}"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SRC, d, dirs_exist_ok=True)
        return d

    def with_manifest_key(tag, key, value):
        s = stage_named(tag)
        m = json.loads((s / "manifest.json").read_text(encoding="utf-8"))
        m[key] = value
        (s / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        return s

    capability_keys = [
        ("declarative_net_request", {"rule_resources": []}),
        ("content_security_policy", {"extension_pages": "script-src 'self' 'unsafe-eval'"}),
        ("omnibox", {"keyword": "sf"}),
        ("commands", {"_execute_action": {"suggested_key": {"default": "Ctrl+Shift+Y"}}}),
        ("side_panel", {"default_path": "help.html"}),
        ("chrome_settings_overrides", {"search_provider": {"name": "x"}}),
        ("sandbox", {"pages": ["help.html"]}),
    ]
    for i, (key, value) in enumerate(capability_keys):
        s = with_manifest_key(f"cap{i}", key, value)
        expect_build_error(
            lambda s=s, key=key: be.build(src_dir=s, out_dir=tmp / f"b-cap{i}", panel_file=panel),
            f"25a 清单里加能拿到能力的键 {key} → 构建失败")

    # 白名单的重点：**没见过**的键也拒绝。黑名单挡不住这一条。
    s = with_manifest_key("unknown", "browser_specific_settings", {"gecko": {"id": "x@y"}})
    expect_build_error(
        lambda: be.build(src_dir=s, out_dir=tmp / "b-unknown", panel_file=panel),
        "25b 没见过的清单键也被拒（默认拒绝，不是列举危险）", "白名单")

    # 但别把合法元数据一起挡了
    s = stage_named("meta")
    m = json.loads((s / "manifest.json").read_text(encoding="utf-8"))
    m["icons"] = {"16": "icon16.png"}
    m["short_name"] = "SF"
    m["homepage_url"] = "https://example.com"
    (s / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    r = be.build(src_dir=s, out_dir=tmp / "b-meta", panel_file=panel)
    check(r["ok"], "25c 白名单内的元数据键（icons/short_name/homepage_url）照常通过")

    # ---- 7c. 后台脚本的 API 名单：构建时就拦，不只靠测试 ----
    for i, (token, why) in enumerate([
        ("chrome.storage.local.get('x')", "持久化"),
        ("fetch('http://example.com')", "网络"),
        ("eval('1')", "动态求值"),
        ("document.title", "DOM"),
        ("chrome.cookies.getAll({})", "读 Cookie"),
    ]):
        s = stage_named(f"js{i}")
        w = (s / "background.js").read_text(encoding="utf-8")
        (s / "background.js").write_text(w + "\n// 顺手加一行\n" + token + ";\n", encoding="utf-8")
        expect_build_error(
            lambda s=s, i=i: be.build(src_dir=s, out_dir=tmp / f"b-js{i}", panel_file=panel),
            f"25d 后台脚本里加 {token.split('(')[0]} → 构建失败（{why}）")

    # ---- 8. 静态检查：后台脚本不该碰的能力 ----
    for bad, why in [
        ("chrome.storage", "不许持久化配置 —— 那需要 storage 权限"),
        ("XMLHttpRequest", "不许发网络请求"),
        ("fetch(", "不许发网络请求"),
        ("eval(", "不许动态求值"),
        ("innerHTML", "不许碰 DOM 解析"),
        ("localStorage", "不许落盘"),
        ("document.", "后台脚本不该操作页面"),
    ]:
        check(bad not in worker_src, f"25 后台脚本不含 {bad}（{why}）")

    # ---- 9. MV3 的 CSP：说明页不能有内联脚本 ----
    help_src = (SRC / "help.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", help_src, re.S | re.I)
    check(bool(scripts), "26 说明页引用了脚本文件", f"{len(scripts)} 个")
    check(all("src=" in attrs for attrs, _ in scripts),
          "27 说明页的 <script> 全部外链（内联会被 MV3 的 CSP 拦掉）",
          str([a.strip() for a, _ in scripts]))
    check(all(not body.strip() for _, body in scripts),
          "28 说明页的 <script> 标签体是空的")
    check(not re.search(r"<[^>]*\son[a-z]+\s*=", help_src, re.I),
          "29 说明页没有内联 on* 处理器（同样会被 CSP 拦掉）")
    check('src="help.js"' in help_src, "30 说明页确实引用了 help.js")

    # ---- 10. 行为：真的跑一遍 background.js ----
    run_node_harness(tmp, panel)


def run_node_harness(tmp: Path, panel: Path):
    node = shutil.which("node")
    if not node:
        check(False, "31 找到 node 跑行为测试", "PATH 里没有 node")
        return

    out = tmp / "behavior-build"
    be.build(src_dir=SRC, out_dir=out, panel_file=panel)
    harness_src = HERE / "extension_harness.mjs"
    if not harness_src.exists():
        check(False, "31 找到行为测试脚本", str(harness_src))
        return

    harness = tmp / "extension_harness.mjs"
    shutil.copyfile(harness_src, harness)

    proc = subprocess.run(
        [node, str(harness), str(out / "background.js"), panel.resolve().as_uri()],
        capture_output=True, text=True,
    )
    for line in proc.stdout.splitlines():
        line = line.strip()
        m = re.match(r"\[(PASS|FAIL)\]\s+(.*?)(?:\s+—\s+(.*))?$", line)
        if m:
            check(m.group(1) == "PASS", "31 行为：" + m.group(2), m.group(3) or "")
    if proc.returncode != 0 and not proc.stdout.strip():
        check(False, "31 行为测试能跑起来", proc.stderr.strip()[:200])


if __name__ == "__main__":
    sys.exit(main())

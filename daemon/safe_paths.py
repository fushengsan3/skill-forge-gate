#!/usr/bin/env python3
"""
skill 名 → 目录名的安全约束。

`name` 是从面板 / 安装队列来的**不受信输入**：它最终会被拼进路径，然后 `rmtree`。
不校验的话，实测（2026-10-05）：

    SKILLS_DIR = C:\\Users\\<you>\\.claude\\skills

    name = "my-skill"                     → ...\\skills\\my-skill                    ✅
    name = ".."                           → ...\\.claude
    name = "../../.."                     → C:\\Users
    name = "C:/Users/<you>/Documents"     → C:\\Users\\<you>\\Documents   ← 绝对路径**整个替换**基准

两个都不是理论问题：pathlib 的 `/` 运算符对手里拿到的字符串照单全收，
绝对路径和 `..` 都有明确且危险的定义。而安装路径是**先删后写**
（`if dest.exists(): rmtree(dest)` 然后 `copytree`），所以一个请求就是一个删除指令。

这里做两件事：

  1. **字符白名单**：拒绝路径分隔符、Windows 保留字符、"." / ".."、尾随点或空格
     （Windows 会**静默**去掉尾随点和空格，拼出来的路径和预期不一致 —— 这种
     "看起来一样其实不一样"正是绕过包含检查的经典手法）
  2. **包含断言**：拼完之后再用 `os.path.normcase` 比一次，确认结果确实在基准目录里面

第 2 条在第 1 条成立时是冗余的 —— 它是留给"以后有人放宽了第 1 条"的。
把 `rmtree` 的目标放在一个断言后面，比放在一个正则后面让人睡得着。

**副作用（有意为之）**：包含断言用的是 `resolve()`，会跟随符号链接/junction。
所以 `skills/evil` 是指向 `C:\\Windows` 的 junction 时会被拒。合法用符号链接
挂 skill 目录的会被误伤 —— 但误伤方向是"拒绝"，不是"删错"，这个取舍是刻意的。
"""
import os
import re
from pathlib import Path

# 名字长度上限。Skill 名是给人看的，64 足够；顺带挡住靠超长名字把路径顶爆。
MAX_NAME_LEN = 64

# Windows 文件名里不允许出现的字符，外加路径分隔符和 C0 控制字符。
# 注意空格**不**在这里 —— 名字中间带空格是允许的，只禁止首尾。
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Windows 保留设备名。叫这些名字的目录建不出来，晚失败不如早拒绝。
_RESERVED_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)]
)


class UnsafeName(ValueError):
    """名字不安全，拒绝拿它拼路径。"""


def check_name(name) -> str:
    """校验一个 skill 名。通过则原样返回，否则抛 UnsafeName（消息可直接给用户看）。"""
    if not isinstance(name, str):
        raise UnsafeName(f"名字必须是字符串，收到的是 {type(name).__name__}")
    if not name.strip():
        raise UnsafeName("名字是空的")
    if name in (".", ".."):
        raise UnsafeName(f"名字不能是 {name!r} —— 它会指到别的目录去")

    # 分隔符检查要**放在尾点/空白检查之前**：'../../..' 两条都犯，
    # 但用户看到"不能以 '.' 结尾"会完全摸不着头脑 —— 真正的问题是那个斜杠。
    bad = _ILLEGAL_CHARS.search(name)
    if bad:
        raise UnsafeName(f"名字里不能有 {bad.group(0)!r}（路径分隔符或 Windows 保留字符）")

    if name != name.strip():
        raise UnsafeName(
            "名字首尾不能有空白：Windows 会静默去掉它们，"
            "于是拼出来的路径和实际落盘的目录对不上"
        )
    if name.endswith("."):
        raise UnsafeName("名字不能以 '.' 结尾 —— Windows 会静默去掉它，理由同上")

    if len(name) > MAX_NAME_LEN:
        raise UnsafeName(f"名字太长（{len(name)} 字符，上限 {MAX_NAME_LEN}）")

    # 只看第一个点之前的部分：`nul.txt` 在 Windows 上同样是设备名
    stem = name.split(".")[0].lower()
    if stem in _RESERVED_DEVICE_NAMES:
        raise UnsafeName(f"{name!r} 撞上 Windows 保留设备名（{stem.upper()}）")

    return name


def is_safe_name(name) -> bool:
    """不抛异常的版本：合法返回 True，不合法返回 False。

    ⚠️ **生产代码目前没有调用方** —— 用它的只有测试（`test_safe_paths` /
    `test_attack` / `test_stress`）。原先的 docstring 写着"比如渲染时标记"，
    但那个调用方**不存在**：面板的渲染路径走的是 `queue_bridge` 里的
    `resolve_within`，不是这里。声称一个不存在的调用方，比没有 docstring 更糟 ——
    它会让人以为这条判断在某个真实路径上生效。

    所以：**要不要拿它做校验，看的是调用方愿不愿意处理"名字不合法"这件事** ——
    任何会 rmtree/写盘的路径都该走 `check_name` 或 `resolve_within`（它们会抛），
    只有"判断一下、合法就显示、不合法就跳过"这种场景才适合布尔版。
    """
    try:
        check_name(name)
        return True
    except UnsafeName:
        return False


def resolve_within(base, name) -> Path:
    """把 name 拼到 base 下面，返回**确认落在 base 里面**的路径。

    这是所有"拿名字去删/去写目录"的地方唯一该走的入口。
    """
    check_name(name)
    base = Path(base)
    target = base / name
    _assert_within(base, target)
    return target


FORGE_DIR_NAME = "skill-forge"


def is_forge_dir(target, skills_root=None) -> bool:
    """这个路径是不是 **skill-forge 自己**。

    凡是会 `rmtree` 或覆盖已存在目录的入口，都要先过这一道。
    "卸载 skill-forge" 不是一次卸载 —— 那是把管理器**连同它自己的日志、
    安装队列、部署留档一起删掉**，而它正是执行这次删除的那个程序。
    更糟的是 `uninstall.sh` 的备份目录 `$SKILL_FORGE/.backup` 就在被删的目录里面，
    所以连"先备份再删"这条退路也一起没了。

    `check_name()` 拦不住它：`skill-forge` 是一个**完全合法**的单分量名字，
    拼出来的路径也确实落在 `skills/` 里面 —— 它只是不该被删而已。
    所以这是**独立于名字校验**的一条判断，两者都要有。

    （2026-10-06：`queue_bridge._handle_uninstall` 与 `uninstall.sh`/`update.sh`
      此前都没有这道判断，`resolve_within` 对 "skill-forge" 一律放行。）
    """
    try:
        p = Path(target).resolve()
        root = Path(skills_root) if skills_root else Path.home() / ".claude" / "skills"
        return p == (root / FORGE_DIR_NAME).resolve()
    except (OSError, TypeError, ValueError):
        # 这是个**布尔判断**，必须总能返回 —— 调用方是"要不要删"的路径，
        # 让它抛异常等于把判断变成第三态，而那里只有"删"和"不删"。
        # 返回 False 的语义是"不认为它是本体"；注意调用方**不能**只靠这一条，
        # 名字校验（check_name/resolve_within）仍然在前面独立生效。
        return False


def is_within(base, target) -> bool:
    """target 是否确实落在 base 里面（且不等于 base 本身）。不抛异常。

    给"要删/要写一个已知相对路径"的场景用 —— 那种情况下名字里带子目录是正常的，
    走不了 check_name 的单分量白名单，所以只能靠这条包含判断兜底。
    """
    try:
        base_real = os.path.normcase(str(Path(base).resolve()))
        target_real = os.path.normcase(str(Path(target).resolve()))
    except OSError:
        # 路径本身有问题（太长、非法字符、盘符断了）—— 一律当"不在里面"
        return False
    return target_real != base_real and target_real.startswith(base_real + os.sep)


def _assert_within(base: Path, target: Path) -> None:
    """最后一道：拼出来的路径必须在 base 里面，且不能就是 base 本身。"""
    if is_within(base, target):
        return
    raise UnsafeName(
        f"拒绝：目标不在 {Path(base).resolve()} 里面"
        f"（解析结果是 {Path(target).resolve()}）。"
        "名字里可能带了路径分隔符或 '..'。"
    )

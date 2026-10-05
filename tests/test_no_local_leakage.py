#!/usr/bin/env python3
"""
本地信息泄露自查 —— 推送之前该跑的那一遍。

## 为什么需要它

这个项目天生带着一堆"只该留在这台机器上"的东西：

    bridge 共享密钥、GitHub token、AI token、已装 skill 名单、
    面板产物、扫描存档、运行日志、留档快照……

而它现在是个 **git 仓库**，`git push` 是一条单向、不可撤回的外发通道。
推错一次，密钥就公开了 —— 即使事后删掉，也已经进了别人的 fork 和缓存。

所以这里做三件事：

  1. **工作区**：被 git 跟踪的每个文件，逐条查本地路径、用户名、密钥、邮箱
  2. **历史**：所有 commit 里的所有 blob，同样查一遍
     （删掉文件不等于删掉历史 —— 这是最常见的误解）
  3. **闸门本身**：`.gitignore` 有没有真的挡住敏感文件；被跟踪的
     `sources.json` / `install-queue.json` 里有没有混进真实数据

## 判据

只报**确定的问题**，不报"可疑但可能正常"的（比如 43 字符的随机串 ——
测试里到处都是假密钥，报出来只会淹掉真信号）。
真密钥的判定是"出现在不该出现的地方 + 匹配已知格式"。

用法：
    python tests/test_no_local_leakage.py
"""
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- 规则

def build_rules():
    """返回 [(名称, 正则, 说明)]。

    路径类用字符类拼，避免在源码里写出"反斜杠 + 盘符"的字面量 ——
    这个文件自己就会被扫，写死的话它会报自己。

    用户名段的取法要小心，这里踩过两次假阳：
      - `C:\\Users\\...\\Documents` 里的省略号被当成了用户名 →
        要求首字符不是点，且至少 2 个字符
      - 占位符 `<you>` / `%USERPROFILE%` 被当成用户名 →
        把它们从字符集里排除
    """
    BS = re.escape(chr(92))          # 一个反斜杠
    RUN = BS + "+"                   # 连续反斜杠（源码里可能写成一个或两个）
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""

    # 用户名段：首字符不是点，不含分隔符/空白/引号/尖括号/百分号，至少 2 字符
    EXCL = BS + r"/\s'\"<>%$*?|:"
    NAMEPART = r"(?!\.)[^" + EXCL + r"]{2,}"

    rules = [
        ("本机用户名出现",
         re.compile(re.escape(user), re.I) if len(user) >= 4 else None,
         "把用户名写进仓库 = 告诉所有人这台机器叫什么"),
        ("本机绝对路径",
         re.compile(RUN + r"Users" + RUN + NAMEPART, re.I),
         "绝对路径泄露用户名和目录结构"),
        ("本机绝对路径（正斜杠）",
         re.compile(r"(?:" + RUN + r"|/)Users/" + NAMEPART, re.I),
         "同上，正斜杠写法"),
        ("Git Bash 家目录",
         re.compile(r"/(?:c|mnt/c)/Users/" + NAMEPART, re.I),
         "同上，Git Bash 形式"),
        ("桌面/AppData 路径",
         re.compile(RUN + r"(?:桌面|Desktop|AppData)" + RUN, re.I),
         "同上"),
        ("GitHub 细粒度 token",
         re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "真 token"),
        ("GitHub 经典 token",
         re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "真 token"),
        ("OpenAI 风格 key",
         re.compile(r"\bsk-[A-Za-z0-9]{32,}"), "真 key"),
        ("Anthropic key",
         re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "真 key"),
        ("JWT",
         re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
         "真 token"),
        ("带口令的连接串",
         re.compile(r"\b\w+://[^/\s:@]+:[^/\s:@]+@"), "连接串里带明文口令"),
        ("私钥块",
         re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
    ]
    return [(n, p, w) for n, p, w in rules if p is not None]


# 豁免：这些文件里出现"像路径"的东西是**故意**的 —— 它们就是讲这件事的。
ALLOWLIST = {
    "tests/test_no_local_leakage.py",   # 本文件，规则里就有路径模式
    "tests/test_attack.py",
    "tests/test_stress.py",
    "tests/test_safe_paths.py",
    "tests/test_deploy.py",
    "tests/test_self_update.py",
    "daemon/safe_paths.py",             # 文档字符串里举了穿越例子
    "scripts/data-paths.txt",
    ".gitignore",
}
# 占位符 / 文档示例，不算泄露
PLACEHOLDER = re.compile(r"<your-account>|<用户名>|<你的账号>|your-username|USERNAME|%USERPROFILE%",
                         re.I)

# 文档里举例用的假用户名。`安装部署说明.md` 里明明白白写着"假设用户名为 alice"，
# 那不是泄露 —— 规则要是把它当问题，就是在逼作者把文档写得更难看。
# 只列**通用假名**，不列任何真实可能撞上的名字。
FAKE_USERNAMES = {
    "alice", "bob", "carol", "dave", "user", "username", "youruser",
    "yourname", "name", "you", "someone", "test", "example", "x", "xxx",
}


def _is_placeholder_path(matched: str) -> bool:
    """`\\Users\\alice` 这类 —— 名字段是通用假名就放过。"""
    tail = re.split(r"[\\/]", matched)
    tail = [p for p in tail if p]
    return bool(tail) and tail[-1].lower() in FAKE_USERNAMES


def scan_text(text: str, rules):
    """扫一段文本，返回 [(规则名, 说明, 命中片段)]，跳过占位符。"""
    out = []
    for line in text.splitlines():
        if PLACEHOLDER.search(line):
            continue
        for name, pat, why in rules:
            m = pat.search(line)
            if not m:
                continue
            frag = m.group(0)
            if name.startswith("本机绝对路径") or name == "Git Bash 家目录":
                if _is_placeholder_path(frag):
                    continue
            out.append((name, why, frag[:70]))
    return out


# ---------------------------------------------------------------- 扫描

def git(*args):
    return subprocess.run(["git", "-C", str(ROOT)] + list(args),
                          capture_output=True, text=True)


def is_repo() -> bool:
    return git("rev-parse", "--is-inside-work-tree").returncode == 0


def scan_worktree(rules):
    files = [f for f in git("ls-files", "-z").stdout.split("\0") if f]
    problems = []
    for f in files:
        if f.replace("\\", "/") in ALLOWLIST:
            continue
        p = ROOT / f
        try:
            text = p.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for name, why, frag in scan_text(text, rules):
            problems.append((f, name, why, frag))
    return files, problems


def scan_history(rules):
    """扫所有 commit 里的所有 blob。

    重点：**删掉文件不等于删掉历史**。这一条最容易被忽略 ——
    有人发现自己提交了密钥，`git rm` 一下以为没事了，其实 `git log -p`
    里还躺着，push 上去照样全公开。
    """
    blobs = subprocess.run(
        ["git", "-C", str(ROOT), "rev-list", "--objects", "--all"],
        capture_output=True, text=True).stdout.splitlines()
    problems = []
    for line in blobs:
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        sha, path = parts
        if path.replace("\\", "/") in ALLOWLIST:
            continue
        t = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-t", sha],
                           capture_output=True, text=True).stdout.strip()
        if t != "blob":
            continue
        content = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-p", sha],
                                 capture_output=True, text=True).stdout
        for name, why, frag in scan_text(content, rules):
            problems.append((path, name, why, frag))
    return len(blobs), problems


def check_ignored():
    """闸门本身：这些文件必须被 .gitignore 挡住。"""
    must_ignore = [
        "templates/.bridge-key",
        "templates/.bridge-keys-prev.json",
        "templates/translate-config.json",
        "daemon/translation_cache.json",
        "daemon/last_scan.txt",
        "daemon/watchdog.log",
        ".env",
    ]
    for f in must_ignore:
        r = git("check-ignore", "-q", f)
        check(r.returncode == 0, f".gitignore 挡住了 {f}")
    must_not_ignore = ["SKILL.md", "daemon/watchdog.py", "scripts/data-paths.txt"]
    for f in must_not_ignore:
        r = git("check-ignore", "-q", f)
        check(r.returncode != 0, f"{f} 没有被误挡（它是要发布的代码）")


def check_tracked_data_is_stub():
    """被跟踪的"数据文件"必须是空壳。

    它们是**数据**（见 scripts/data-paths.txt），但仓库里各留了一份初始空壳
    供全新安装用。如果哪天仓库里这份被真实数据填上了，下一次 `git add -A`
    就把你的已装名单推上去了。
    """
    import json
    sj = ROOT / "sources.json"
    if sj.exists():
        try:
            d = json.loads(sj.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            check(False, "sources.json 是合法 JSON", str(e))
            d = {}
        non_self = [k for k, v in d.items()
                    if isinstance(v, dict) and not v.get("self")]
        check(not non_self,
              "★ 仓库里的 sources.json 只有 self 条目（没有混进真实已装名单）",
              f"多出来：{non_self[:5]}")

    qj = ROOT / "templates" / "install-queue.json"
    if qj.exists():
        try:
            d = json.loads(qj.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            d = {}
        pend = d.get("pending") or []
        hist = d.get("history") or []
        check(not pend and not hist,
              "★ 仓库里的 install-queue.json 是空的（没有混进真实队列）",
              f"pending={len(pend)} history={len(hist)}")


def check_no_absolute_paths_in_docs():
    """文档里最容易顺手写上真实路径 —— 我这次就写了。"""
    docs = ["README.md", "SKILL.md", "安装部署说明.md",
            "决策记录-2026-10-04.md", "存档-2026-10-05.md", "PROGRESS.md"]
    bad = []
    user = os.environ.get("USERNAME") or ""
    for d in docs:
        p = ROOT / d
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if PLACEHOLDER.search(line):
                continue
            if user and user in line:
                bad.append(f"{d}:{i}")
    check(not bad,
          "★ 文档里没有真实用户名（用 %USERPROFILE% 之类占位）",
          f"{len(bad)} 处：{bad[:6]}")


def main():
    print("=" * 60)
    print("本地信息泄露自查")
    print("=" * 60)

    if not is_repo():
        check(False, "当前目录是 git 仓库", "不是仓库就无所谓推不推得出去")
        return finish()

    rules = build_rules()
    print(f"规则 {len(rules)} 条 | 本机用户名 {os.environ.get('USERNAME', '(未知)')!r}\n")

    print("--- 1. 工作区：被跟踪的每个文件 ---")
    files, wt = scan_worktree(rules)
    check(files, f"扫了 {len(files)} 个被跟踪的文件")
    if wt:
        for f, name, why, frag in wt[:15]:
            print(f"      ⚠️ {f}  [{name}] {frag}   ← {why}")
    check(not wt, "★ 工作区里没有被跟踪的本地信息/密钥", f"{len(wt)} 处")

    print("--- 2. 历史：所有 commit 的所有 blob ---")
    n, hist = scan_history(rules)
    if hist:
        for p, name, why, frag in hist[:15]:
            print(f"      ⚠️ {p}  [{name}] {frag}")
    check(not hist,
          f"★ git 历史（{n} 个对象）里没有残留的本地信息/密钥 —— 删文件 ≠ 删历史",
          f"{len(hist)} 处")

    print("--- 3. 闸门：.gitignore 是否真的挡得住 ---")
    check_ignored()

    print("--- 4. 被跟踪的数据文件必须是空壳 ---")
    check_tracked_data_is_stub()

    print("--- 5. 文档里不许出现真实用户名 ---")
    check_no_absolute_paths_in_docs()

    return finish()


def finish():
    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    if failed:
        print("\n推送之前必须把这些清干净 —— push 是单向且不可撤回的：")
        for n in failed:
            print(f"  · {n}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

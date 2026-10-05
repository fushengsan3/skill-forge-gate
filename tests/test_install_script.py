#!/usr/bin/env python3
"""
scripts/install.sh 测试。

这个脚本是 **SKILL.md 让 Claude 走的那条安装路** —— 而它原先：

  1. 完全没有安全校验（clone 完直接 mv 落地）
  2. 把 Git Bash 的 `/c/Users/...` 拼进 Python 源码（Windows Python 打不开，
     而且是在文件**已经落地之后**才炸，留下"装了但没登记"的残局）
  3. `--name` 没有路径校验

三条都在 2026-10-05 修了。这个测试钉住它们。

做法：整条流程跑在临时目录上，`git` 换成替身（不联网、不碰真实 skills 目录）。
预检是真的跑 —— L1/L3/L4 是本地分析，L2 打不通就跳过（不影响判定）。

用法：
    python tests/test_install_script.py
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "scripts" / "install.sh"

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


GOOD_SKILL = """---
name: good-skill
description: 一个正常的 skill，用来验证安装流程
---

# Good Skill

就是一份普通文档。
"""

BAD_SKILL = """---
name: evil-skill
description: 干坏事的 skill
---

# Evil

```bash
rm -rf /
```
"""

FAKE_GIT = r"""#!/bin/bash
# 替身 git：clone 直接拷预置目录，不联网。
if [ -n "${SF_FAKE_CLONE_FAIL:-}" ]; then
    echo "fatal: could not read from remote repository" >&2
    exit 128
fi
if [ "$1" = "clone" ]; then
    for last; do :; done
    cp -r "$SF_FAKE_REPO" "$last"
    exit $?
fi
if [ "$1" = "rev-parse" ]; then
    echo "FAKESHA1234567890"
    exit 0
fi
exit 0
"""


def setup(tmp: Path, tag: str):
    """造一棵假 skills 树 + 一个假 git，返回 (skills_root, bin_dir)。"""
    skills = tmp / f"skills-{tag}"
    forge = skills / "skill-forge"
    forge.mkdir(parents=True)
    # install.sh 需要 daemon/（预检 + 名字校验）和 verify/（预检的几层）
    shutil.copytree(ROOT / "daemon", forge / "daemon",
                    ignore=shutil.ignore_patterns("__pycache__", "*.log"))
    shutil.copytree(ROOT / "verify", forge / "verify",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (forge / "sources.json").write_text(json.dumps({
        "self": {"type": "remote", "url": "https://github.com/a/skill-forge",
                 "self": True, "installed_sha": ""}
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    bin_dir = tmp / f"bin-{tag}"
    bin_dir.mkdir()
    g = bin_dir / "git"
    g.write_text(FAKE_GIT, encoding="utf-8")
    g.chmod(g.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return skills, bin_dir


def run(skills: Path, bin_dir: Path, fake_repo: Path, args, extra_env=None):
    env = os.environ.copy()
    env["SKILLS_ROOT"] = str(skills)
    env["SF_FAKE_REPO"] = str(fake_repo)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    for k in ("SF_FAKE_CLONE_FAIL",):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", str(SCRIPT)] + list(args),
                          capture_output=True, text=True, env=env, timeout=300)


def main():
    print("=" * 60)
    print("install.sh 测试")
    print("=" * 60)
    if not SCRIPT.exists():
        check(False, "找到 scripts/install.sh", str(SCRIPT))
        return finish()

    tmp = Path(tempfile.mkdtemp(prefix="sf-is-"))
    try:
        run_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return finish()


def finish():
    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


def run_checks(tmp: Path):
    good = tmp / "repo-good"
    good.mkdir()
    (good / "SKILL.md").write_text(GOOD_SKILL, encoding="utf-8")

    bad = tmp / "repo-bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text(BAD_SKILL, encoding="utf-8")

    URL = "https://github.com/octocat/Hello-World"

    # ---- 1. 名字校验（在拼任何路径之前）----
    print("--- 1. --name 必须先过校验 ---")
    skills, binn = setup(tmp, "name")
    decoy = tmp / "CANARY.txt"
    decoy.write_text("删了我就是逃出去了", encoding="utf-8")

    for badname, why in [("../../..", "上跳"), ("../CANARY", "直奔诱饵"),
                         ("C:/Windows", "绝对路径"), ("con", "Windows 设备名"),
                         ("a/b", "带斜杠")]:
        p = run(skills, binn, good, [URL, "--name", badname])
        body = (p.stderr or "") + (p.stdout or "")
        check("error" in body and p.returncode != 0,
              f"1 拒绝 --name {badname!r}（{why}）", f"rc={p.returncode}")
    check(decoy.exists(), "★ 诱饵文件完好")
    check(not any(skills.rglob("CANARY*")), "★ skills 树里没多出越界的东西")

    # ---- 2. 预检拦下危险的 skill ----
    print("--- 2. 预检拦下危险 skill ---")
    skills, binn = setup(tmp, "bad")
    src_before = (skills / "skill-forge" / "sources.json").read_text(encoding="utf-8")
    p = run(skills, binn, bad, [URL, "--name", "evil-skill"])
    check(p.returncode == 5, "2 带 `rm -rf /` 的 skill 被拒（退出码 5）",
          f"rc={p.returncode} {p.stderr[:100]}")
    check(not (skills / "evil-skill").exists(),
          "★ 被拒时**没有**往 skills 里写任何东西（验在装之前）")
    check((skills / "skill-forge" / "sources.json").read_text(encoding="utf-8") == src_before,
          "★ 被拒时 sources.json 没被改")
    check("L3" in (p.stderr or ""), "2 错误信息点明是哪一层拦的", (p.stderr or "")[:100])

    # ---- 3. 正常安装 ----
    print("--- 3. 正常路径：装进去 + 登记进 sources.json ---")
    skills, binn = setup(tmp, "ok")
    p = run(skills, binn, good, [URL, "--name", "good-skill"])
    check(p.returncode == 0, "3 安装成功", f"rc={p.returncode} {p.stderr[:120]}")
    check((skills / "good-skill" / "SKILL.md").exists(), "3 文件落地了")

    sj = skills / "skill-forge" / "sources.json"
    try:
        data = json.loads(sj.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        data = {}
        check(False, "3 sources.json 仍是合法 JSON", str(e))
    entry = data.get("good-skill")
    check(isinstance(entry, dict),
          "★ 3 登记进了 sources.json（这条原先必然失败：Git Bash 的 /c/Users/... "
          "被拼进 Python 源码，Windows Python 打不开）", str(list(data)))
    if isinstance(entry, dict):
        check(entry.get("installed_sha") == "FAKESHA1234567890", "3 SHA 记对了",
              entry.get("installed_sha"))
        check(entry.get("url") == URL, "3 URL 记对了", entry.get("url"))
        check(entry.get("repo") == "Hello-World", "3 owner/repo 解析对了",
              f"{entry.get('owner')}/{entry.get('repo')}")
        check(bool(entry.get("installed_at")), "3 有安装时间", entry.get("installed_at"))
    check("self" in data, "3 原有的 self 条目没被冲掉")

    # ---- 4. clone 失败 ----
    print("--- 4. clone 失败要干净退出 ---")
    skills, binn = setup(tmp, "fail")
    src_before = (skills / "skill-forge" / "sources.json").read_text(encoding="utf-8")
    p = run(skills, binn, good, [URL, "--name", "x-skill"],
            {"SF_FAKE_CLONE_FAIL": "1"})
    check(p.returncode == 4, "4 拉取失败退出码 4", f"rc={p.returncode}")
    check(not (skills / "x-skill").exists(), "4 没留下半成品")
    check((skills / "skill-forge" / "sources.json").read_text(encoding="utf-8") == src_before,
          "4 sources.json 没被动")
    leftover = list((skills / "skill-forge" / ".tmp").glob("*")) if \
        (skills / "skill-forge" / ".tmp").exists() else []
    check(not leftover, "★ 临时目录清干净了（不留残局）", str(leftover[:3]))

    # ---- 5. 缺 URL ----
    print("--- 5. 参数缺失 ---")
    skills, binn = setup(tmp, "usage")
    p = run(skills, binn, good, [])
    check(p.returncode == 2 and "Usage" in (p.stderr or ""), "5 没给 URL → 用法提示",
          f"rc={p.returncode}")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
自更新测试（P0-7）。

背景：`scripts/self-update.sh` 会删掉 skill-forge 下除 `.backup` / `.tmp` 外的
**所有**条目，再用新版本填回来。而那个目录里既有代码也有**用户数据** ——
数据是新版本不会带的，删掉就没了。

2026-10-05 之前的版本正是这么丢的：只抢救回 `sources.json`，
于是 `discover/`、`templates/install-queue.json`、`templates/.bridge-key`
等等在每次自更新后**静默消失**。

这个测试要钉的是三件事：
  1. 替换前后**数据逐字节不变**（代码该换，数据不该动）
  2. 删任何东西**之前**已经写好留档，且留档里逐项标注了用途
  3. 回滚是一条命令的事，而且真的能把状态还原

做法：整个流程在一棵临时假目录上跑，`git` 换成替身（不联网、不碰真实仓库）。

用法：
    python tests/test_self_update.py
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
SCRIPT = ROOT / "scripts" / "self-update.sh"

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


# 数据：新版本**不会**带，必须原样保住
DATA = {
    "sources.json": json.dumps({
        "self": {"type": "skill", "url": "https://github.com/fake/skill-forge",
                 "branch": "main", "installed_sha": "OLDSHA0000", "self": True},
        "some-skill": {"type": "skill", "url": "https://github.com/a/b",
                       "installed_sha": "abc12345"},
    }, ensure_ascii=False, indent=2),
    "discover/latest.html": "<html>旧面板</html>",
    "discover/weekly-2026-10-04.json": '{"total": 53}',
    "discover/weekly-2026-09-26.json": '{"total": 41}',
    "templates/install-queue.json": '{"pending": [{"name": "q1"}]}',
    "templates/.bridge-key": "SUPER-SECRET-BRIDGE-KEY-0123456789",
    "templates/.bridge-keys-prev.json": '{"version": 1, "keys": []}',
    "templates/external_sources.json": '{"sources": [{"label": "mine"}]}',
    "daemon/last_scan.txt": "2026-10-04T11:05:20.314054",
    "daemon/translation_cache.json": '{"hello": "你好"}',
    "daemon/watchdog.log": "[2026-10-04] 扫描完成",
}

# 代码：新版本会带，替换掉是**预期行为**
OLD_CODE = {
    "SKILL.md": "---\nname: skill-forge\ndescription: 旧\n---\n",
    "daemon/watchdog.py": "# 旧版 watchdog\n",
    "verify/l1_structure.py": "import sys\nsys.exit(0)\n",
    "scripts/self-update.sh": "# 旧版脚本\n",
}
NEW_CODE = {
    "SKILL.md": "---\nname: skill-forge\ndescription: 新\n---\n",
    "daemon/watchdog.py": "# 新版 watchdog\n",
    "verify/l1_structure.py": "import sys\nsys.exit(0)\n",
    "scripts/self-update.sh": "# 新版脚本\n",
}


FAKE_GIT = r"""#!/bin/bash
# 替身 git：不联网。clone 直接把预置的"新版本"拷过去。
if [ -n "${SF_FAKE_CLONE_FAIL:-}" ]; then
    echo "fatal: could not read from remote repository" >&2
    exit 128
fi
if [ "$1" = "clone" ]; then
    for last; do :; done
    cp -r "$SF_FAKE_NEW_VERSION" "$last"
    exit $?
fi
if [ "$1" = "rev-parse" ]; then
    echo "FAKENEWSHA99"
    exit 0
fi
exit 0
"""


def write_tree(base: Path, files: dict):
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def read_tree(base: Path) -> dict:
    """把整棵树读成 {相对路径: 内容}，跳过 .backup / .tmp。"""
    out = {}
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base)
        if rel.parts[0] in (".backup", ".tmp"):
            continue
        try:
            out[str(rel).replace("\\", "/")] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            out[str(rel).replace("\\", "/")] = "<binary>"
    return out


def run_update(fake_root: Path, bin_dir: Path, new_version: Path, env_extra=None):
    env = os.environ.copy()
    env["SKILL_FORGE_DIR"] = str(fake_root)
    env["SF_FAKE_NEW_VERSION"] = str(new_version)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    for k in ("SF_FAKE_CLONE_FAIL",):
        env.pop(k, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def setup(tmp: Path, tag: str):
    """造一棵假的 skill-forge + 一个假的新版本 + 一个假的 git。"""
    fake_root = tmp / f"skill-forge-{tag}"
    write_tree(fake_root, OLD_CODE)
    write_tree(fake_root, DATA)
    (fake_root / ".backup").mkdir(exist_ok=True)

    new_version = tmp / f"new-version-{tag}"
    write_tree(new_version, NEW_CODE)

    bin_dir = tmp / f"bin-{tag}"
    bin_dir.mkdir(exist_ok=True)
    git = bin_dir / "git"
    git.write_text(FAKE_GIT, encoding="utf-8")
    git.chmod(git.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_root, new_version, bin_dir


def main():
    print("=" * 60)
    print("自更新测试（P0-7）")
    print("=" * 60)

    if not SCRIPT.exists():
        check(False, "找到 scripts/self-update.sh", str(SCRIPT))
        return finish()

    tmp = Path(tempfile.mkdtemp(prefix="sf-su-"))
    try:
        check_success_path(tmp)
        check_clone_failure_changes_nothing(tmp)
        check_rollback(tmp)
        check_list(tmp)
        check_selfcheck_failure_rolls_back(tmp)
        check_no_silent_failures(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return finish()


def finish():
    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


# ---------------------------------------------------------------- 1. 成功路径

def check_success_path(tmp: Path):
    print("--- 1. 更新成功：代码换掉，数据一个字节都不动 ---")
    root, newver, bindir = setup(tmp, "ok")
    before = read_tree(root)

    proc = run_update(root, bindir, newver)
    check(proc.returncode == 0, "脚本退出码为 0", f"rc={proc.returncode} {proc.stderr[:200]}")

    after = read_tree(root)

    # 数据：逐字节相同
    missing = [rel for rel in DATA if rel not in after]
    check(not missing, "★ 所有数据文件都还在", f"丢了 {missing}")

    # sources.json 是唯一**应该**变的数据文件（self 条目的 SHA 要更新）。
    # 其余必须逐字节不变。
    changed = [rel for rel in DATA
               if rel != "sources.json" and rel in after and after[rel] != before[rel]]
    check(not changed, "★ 除 sources.json 外，数据内容逐字节未变", f"被改了 {changed}")

    b_json = json.loads(before["sources.json"])
    a_json = json.loads(after["sources.json"])
    check(set(b_json) == set(a_json), "sources.json 的条目集合没变",
          f"差异 {set(b_json) ^ set(a_json)}")
    diff_keys = [k for k in b_json if b_json[k] != a_json.get(k)]
    check(diff_keys == ["self"],
          "★ sources.json 里只有 self 条目变了，其余条目原样",
          f"变了这些：{diff_keys}")

    # 代码：换成了新版本
    for rel, content in NEW_CODE.items():
        if rel == "scripts/self-update.sh":
            continue  # 脚本自己被替换后会变成"新版本"的内容（假的新版本里是占位文本）
        check(after.get(rel) == content, f"代码已更新：{rel}",
              f"实际是 {(after.get(rel) or '')[:40]!r}")

    # sources.json 里的 SHA 被更新，但其它条目没被动
    sj = json.loads(after["sources.json"])
    check(sj["self"]["installed_sha"] == "FAKENEWSHA99", "self 条目的 SHA 已更新",
          sj["self"]["installed_sha"])
    check(sj.get("some-skill", {}).get("installed_sha") == "abc12345",
          "★ 其它条目没被动过（以前整份 sources.json 只抢救回来自我条目那点内容）")

    # 留档
    backups = sorted((root / ".backup").glob("self-update-*"))
    check(len(backups) == 1, "生成了一份留档", f"{len(backups)} 份")
    if not backups:
        return
    b = backups[0]
    check((b / "snapshot").is_dir(), "留档里有完整快照")
    check((b / "MANIFEST.md").is_file(), "留档里有 MANIFEST.md（用途标注）")
    check((b / "manifest.json").is_file(), "留档里有机器可读的 manifest.json")
    rb = b / "ROLLBACK.sh"
    check(rb.is_file(), "留档里有可一键回滚的 ROLLBACK.sh")
    check(rb.exists() and os.access(rb, os.X_OK), "ROLLBACK.sh 有执行权限")

    # 快照里数据+代码都在（这才是"能回滚"的前提）
    snap = read_tree(b / "snapshot")
    check(all(rel in snap for rel in DATA), "快照里有全部数据",
          f"缺 {[r for r in DATA if r not in snap]}")
    check(snap.get("SKILL.md") == OLD_CODE["SKILL.md"], "快照里是**旧**代码")

    # 用途标注：每一项都得写出用途，不能只列路径
    manifest = (b / "MANIFEST.md").read_text(encoding="utf-8")
    check("## 数据项" in manifest and "用途" in manifest, "MANIFEST 里有用途表")
    # 清单是以"项"为单位的：`discover` 是一条（一个目录），不是把里面每个文件摊开。
    # 所以断言"这一项或它的某一级祖先被列出来了"。
    for rel in DATA:
        parts = rel.split("/")
        covered = any("/".join(parts[:i + 1]) in manifest
                      or ("/".join(parts[:i + 1]) + "/") in manifest
                      for i in range(len(parts)))
        check(covered, f"MANIFEST 覆盖了 {rel}")
    check("丢了 = 不知道装过什么" in manifest or "已安装 skill 的注册表" in manifest,
          "★ 每一项都写了用途说明，不只是路径")
    check("回滚" in manifest and "ROLLBACK.sh" in manifest, "MANIFEST 里写了怎么回滚")

    mj = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    check(mj.get("kind") == "skill-forge-self-update", "manifest.json 有类型标记")
    # 同 MANIFEST：清单以"项"为单位，目录项覆盖它下面的所有文件。
    # 清单里目录可以写成 `discover/`，比较前先归一化掉尾斜杠。
    paths = {d["path"].rstrip("/") for d in mj.get("data_paths", [])}
    uncovered = [rel for rel in DATA
                 if not any("/".join(rel.split("/")[:i + 1]) in paths
                            for i in range(len(rel.split("/"))))]
    check(not uncovered, "manifest.json 覆盖了全部数据项", str(uncovered))
    check(all(d.get("purpose") for d in mj["data_paths"]), "每一项都有 purpose 字段")
    check(all("present" in d for d in mj["data_paths"]),
          "每一项都标了当前存在与否（回滚时能看出当时有什么）")


# ---------------------------------------------------------------- 2. 拉取失败

def check_clone_failure_changes_nothing(tmp: Path):
    print("--- 2. 拉取失败：数据分毫未动，且留档已经先写好了 ---")
    root, newver, bindir = setup(tmp, "fail")
    before = read_tree(root)

    proc = run_update(root, bindir, newver, {"SF_FAKE_CLONE_FAIL": "1"})
    check(proc.returncode == 4, "退出码 4（拉取失败）", f"rc={proc.returncode}")

    after = read_tree(root)
    check(after == before, "★ 整棵树一个字节都没变",
          f"变了 {[k for k in set(before) | set(after) if before.get(k) != after.get(k)]}")

    backups = sorted((root / ".backup").glob("self-update-*"))
    check(len(backups) == 1,
          "★ 留档在**删除之前**就写好了（失败了也留着，且能看出当时的状态）")
    if backups:
        check((backups[0] / "MANIFEST.md").is_file(), "失败时的留档也带用途标注")


# ---------------------------------------------------------------- 3. 回滚

def check_rollback(tmp: Path):
    print("--- 3. 回滚：一条命令还原到更新前 ---")
    root, newver, bindir = setup(tmp, "rb")
    before = read_tree(root)
    run_update(root, bindir, newver)

    after_update = read_tree(root)
    check(after_update.get("SKILL.md") == NEW_CODE["SKILL.md"], "（前置）确实更新过了")

    b = sorted((root / ".backup").glob("self-update-*"))[0]
    env = os.environ.copy()
    env["SKILL_FORGE_DIR"] = str(root)
    proc = subprocess.run(["bash", str(b / "ROLLBACK.sh")],
                          capture_output=True, text=True, env=env)
    check(proc.returncode == 0, "ROLLBACK.sh 退出码为 0", proc.stderr[:150])

    restored = read_tree(root)
    check(restored == before, "★ 回滚后与更新前**完全一致**（代码 + 数据）",
          f"差异 {[k for k in set(before) | set(restored) if before.get(k) != restored.get(k)]}")

    # 主脚本的 --rollback 也要能用
    run_update(root, bindir, newver)
    env2 = os.environ.copy()
    env2["SKILL_FORGE_DIR"] = str(root)
    env2["PATH"] = str(bindir) + os.pathsep + env2["PATH"]
    proc2 = subprocess.run(["bash", str(SCRIPT), "--rollback"],
                           capture_output=True, text=True, env=env2)
    check(proc2.returncode == 0, "self-update.sh --rollback 也能用", proc2.stderr[:150])
    check(read_tree(root) == before, "★ --rollback 同样还原到更新前")


# ---------------------------------------------------------------- 4. --list

def check_list(tmp: Path):
    print("--- 4. --list 列出可用留档 ---")
    root, newver, bindir = setup(tmp, "ls")
    run_update(root, bindir, newver)
    proc = run_update(root, bindir, newver, {"SF_FAKE_CLONE_FAIL": "1"})  # 再失败一次，不产生第二份
    env = os.environ.copy()
    env["SKILL_FORGE_DIR"] = str(root)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    proc = subprocess.run(["bash", str(SCRIPT), "--list"],
                          capture_output=True, text=True, env=env)
    check(proc.returncode == 0 and "self-update-" in proc.stdout,
          "--list 能列出留档", proc.stdout.strip()[:120])


# ---------------------------------------------------------------- 5. 自检失败

def check_selfcheck_failure_rolls_back(tmp: Path):
    print("--- 5. 自检失败：自动整体回滚 ---")
    root, newver, bindir = setup(tmp, "sc")
    # 新版本里没有 verify/l1_structure.py，自检必挂
    shutil.rmtree(newver / "verify")
    before = read_tree(root)

    proc = run_update(root, bindir, newver)
    check(proc.returncode == 5, "退出码 5（自检失败）", f"rc={proc.returncode}")

    after = read_tree(root)
    check(after == before, "★ 自检失败后自动回滚，整棵树还原（含数据）",
          f"差异 {[k for k in set(before) | set(after) if before.get(k) != after.get(k)]}")


# ---------------------------------------------------------------- 6. 静默失败

def check_no_silent_failures(tmp: Path):
    print("--- 6. 别静默：搬出与搬回数量对不上要出声 ---")
    src = SCRIPT.read_text(encoding="utf-8")
    check("数量对不上" in src,
          "脚本里有「搬出/搬回数量核对」的告警（对不上要出声）")
    check("清理旧留档" in src, "清理旧留档时会打印被删的是哪个（不静默删备份）")
    check("sources.json.bak" not in src,
          "★ 不再只把 sources.json 抢救成 .bak 一个文件")
    check("data-stashed" in src, "替换前先把数据搬出去（不是删掉后再想办法）")
    # 清单**必须**来自共享文件：deploy.py 读同一份。
    # 内联一份的话两处会各改各的 —— "哪些算数据"分叉正是丢数据的成因。
    check("DATA_PATHS_FILE" in src and "data-paths.txt" in src,
          "★ 数据清单读的是共享的 scripts/data-paths.txt，不是内联的一份")
    check("DATA_PATHS=(" not in src.replace("DATA_PATHS=()", ""),
          "脚本里没有第二份内联清单（两份会走样）")
    dp = ROOT / "scripts" / "data-paths.txt"
    check(dp.is_file(), "scripts/data-paths.txt 存在", str(dp))
    if dp.is_file():
        rows = [l for l in dp.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.strip().startswith("#")]
        check(len(rows) >= 10, "清单里有足够多的数据项", f"{len(rows)} 项")
        check(all("|" in r for r in rows), "每一项都是 `路径|用途` 两段式")
        no_purpose = [r for r in rows if len(r.split("|", 1)[1].strip()) < 6]
        check(not no_purpose, "★ 每一项都写了用途（不是只列路径）", str(no_purpose))

    # 路径必须走 argv，**不能**拼进 Python 源码。
    # 两个原因：Windows 路径里的 `\U` 会被 Python 当 Unicode 转义（SyntaxError）；
    # Git Bash 的 `$HOME` 是 `/c/Users/...` 这种 POSIX 形式，Windows Python
    # 直接 open 会 FileNotFoundError。而 argv 会被 Git Bash 自动转成 `C:/...`。
    # 旧的脚本正是拼字符串的，所以在 Git Bash 上一次都没成功过。
    import re as _re
    interpolated = _re.findall(r"""open\(\s*['"][^'"]*\$""", src)
    check(not interpolated,
          "★ 路径不拼进 Python 源码，一律用 argv 传（否则 Windows/Git Bash 下打不开）",
          str(interpolated))
    check('"$PYTHON" - "$SOURCES_FILE"' in src,
          "读 sources.json 走 argv 形式")


if __name__ == "__main__":
    sys.exit(main())

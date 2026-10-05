#!/usr/bin/env python3
"""
把开发副本同步到运行时目录。

## 为什么需要它

skill-forge 有两份拷贝：

    仓库    E:\\path\\to\\skill-forge\\                    ← 你改代码的地方
    运行时  %USERPROFILE%\\.claude\\skills\\skill-forge\\       ← 真正在跑的地方

在仓库里改完**不会**自动生效 —— 这是这个项目反复踩的坑：
改了、测过、以为好了，实际跑的还是旧代码。手动 `cp` 容易漏文件、
也容易顺手把运行时里的数据覆盖掉。

这个脚本把那次手动 `cp` 变成一条命令。

## 它保证什么

1. **代码同步**：以 `git ls-files` 为准 —— 被 git 跟踪的才算代码。
   所以 `.gitignore` 就是"什么不该过去"的唯一一份清单，不会和这里各写一份。
2. **数据绝不碰**：`scripts/data-paths.txt` 里列的路径一律跳过。
   那份清单也是 self-update.sh 用的同一份。
3. **先留档再动**：部署前把运行时整个快照到 `.backup/deploy-<时间戳>/`，
   附 `MANIFEST.md`（逐项标注用途）和 `ROLLBACK.sh`（一条命令还原）。
4. **只删自己造的东西**：清理多余文件时只删**上一份部署清单**里列过的。
   （这条是踩过坑的：build_extension 早期版本"删掉所有不在本次产物里的文件"，
   等于把输出目录变成清空命令。）
5. **部署完逐字节校验**：源和目标对不上就报出来，不假装成功。

## 用法

    python scripts/deploy.py                 # 仓库 → 运行时
    python scripts/deploy.py --dry-run       # 只看会改什么
    python scripts/deploy.py --list          # 列出可回滚的留档
    python scripts/deploy.py --rollback      # 回滚到最近一次部署前
    python scripts/deploy.py --rollback <目录>
    python scripts/deploy.py --from A --to B # 自定义两端
    python scripts/deploy.py --prune         # 顺带清理"源里没有、清单里也没有"的残留
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_DST = Path.home() / ".claude" / "skills" / "skill-forge"

DATA_PATHS_FILE = HERE / "data-paths.txt"

# 部署清单：记下"这次部署写了哪些文件"，供下次清理用。
# 只删这份清单里列过的 —— 绝不做"删掉所有不在源里的文件"。
MANIFEST_NAME = ".deploy-manifest.json"

# 这些**永远**不碰（即使它们出现在 git 里）。
# 注意 `.backup` 和 `.tmp`：留档和暂存区都在里面，删了就没法回滚。
ALWAYS_KEEP_TOP = {".git", ".backup", ".tmp"}

ARCHIVE_PREFIX = "deploy-"


# ---------------------------------------------------------------- 数据清单

def load_data_paths(path: Path = None) -> list:
    """读 `scripts/data-paths.txt`。返回 [(相对路径, 用途)]。

    格式：`相对路径|一句话用途`，`#` 开头是注释。目录可以带尾斜杠，这里归一化掉。
    """
    path = path or DATA_PATHS_FILE
    if not path.is_file():
        raise SystemExit(f"找不到数据清单 {path} —— 不敢动任何东西")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rel, _, purpose = line.partition("|")
        rel = rel.strip().rstrip("/")
        if rel:
            out.append((rel, purpose.strip()))
    if not out:
        raise SystemExit(f"数据清单 {path} 是空的 —— 不敢动任何东西")
    return out


def is_data(rel: str, data_paths: list) -> bool:
    """rel 是否落在某个数据项里面（含数据项本身和它下面的所有东西）。"""
    rel = rel.replace("\\", "/").rstrip("/")
    for d, _ in data_paths:
        if rel == d or rel.startswith(d + "/"):
            return True
    return False


# ---------------------------------------------------------------- 源文件集合

def collect_source_files(src: Path):
    """源目录里"算代码"的文件（相对路径字符串集合）。返回 (集合, 用的方法)。

    优先问 git：被跟踪的才算代码。这样 .gitignore 就是唯一一份"不该过去"
    的清单，不用在这里再维护一份（两份必然会走样）。
    不在 git 仓库里就退回目录遍历 + 内置忽略名单。
    """
    try:
        proc = subprocess.run(["git", "-C", str(src), "ls-files", "-z"],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode == 0 and proc.stdout:
            files = {p.replace("\\", "/") for p in proc.stdout.split("\0") if p}
            if files:
                return files, "git ls-files"
    except (OSError, subprocess.SubprocessError):
        pass

    # 退路：遍历。忽略名单和 .gitignore 保持一致的意图，但不解析 .gitignore
    # （那要写一个解析器，容易和 git 的真实行为有出入）。
    skip_dirs = ALWAYS_KEEP_TOP | {"__pycache__", ".pytest_cache", "node_modules",
                                   "discover", "extension", ".vscode", ".idea"}
    files = set()
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if any(part in skip_dirs for part in rel.parts):
            continue
        files.add(rel.as_posix())
    return files, "目录遍历（没找到 git）"


# ---------------------------------------------------------------- 留档

def write_archive(dst: Path, stamp: str, data_paths: list) -> Path:
    """部署前把运行时整个快照下来，并写好用途标注与回滚脚本。

    **这一步必须在改任何东西之前完成。**
    """
    # 时间戳只精确到秒，而"连着跑两次部署"完全可能（脚本、循环、手快）。
    # 撞名的话第二次会往同一份留档里合并 —— 那份留档就不再是"某一次之前的状态"，
    # 回滚会退到一个谁也没见过的混合体。所以撞了就让开，另起一份。
    # 序号补零：留档是按**文件名**倒序挑"最近一份"的，`-10` 不补零会排到 `-2` 前面
    archive = dst / ".backup" / f"{ARCHIVE_PREFIX}{stamp}"
    n = 2
    while archive.exists():
        archive = dst / ".backup" / f"{ARCHIVE_PREFIX}{stamp}-{n:02d}"
        n += 1
    snap = archive / "snapshot"
    snap.mkdir(parents=True, exist_ok=False)

    for entry in sorted(dst.iterdir()):
        if entry.name in ALWAYS_KEEP_TOP:
            continue
        target = snap / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, symlinks=False,
                            ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(entry, target)

    # MANIFEST.md：给人看的，逐项标注用途
    rows = []
    for rel, purpose in data_paths:
        present = (dst / rel).exists()
        rows.append(f"| `{rel}` | {purpose} | {'已备份' if present else '不存在'} |")
    (archive / "MANIFEST.md").write_text(
        "\n".join([
            "# Skill Forge 部署留档",
            "",
            f"- 留档时间：{stamp}",
            f"- 运行时目录：`{dst}`",
            f"- 快照位置：`{snap}`（部署**之前**的完整目录）",
            "",
            "## 这份留档是干什么的",
            "",
            "`scripts/deploy.py` 只同步**代码**，不碰数据。但万一代码同步坏了，",
            "或者你想退回到部署前的状态，这份快照能整体还原。",
            "",
            "## 数据项（部署时一律跳过，逐项标注用途）",
            "",
            "| 相对路径 | 用途 | 现状 |",
            "|---|---|---|",
            *rows,
            "",
            "## 怎么回滚",
            "",
            "```bash",
            f'python scripts/deploy.py --rollback "{archive}"',
            "```",
            "",
            "或者直接跑这份留档里的 `ROLLBACK.sh`。",
            "",
            "回滚会把 `snapshot/` 整个拷回去（代码 + 数据一起），",
            "所以它恢复到的是**部署前那一刻的完整状态**。",
        ]),
        encoding="utf-8")

    # manifest.json：给程序看的
    (archive / "manifest.json").write_text(json.dumps({
        "kind": "skill-forge-deploy",
        "created": stamp,
        "runtime": str(dst),
        "snapshot": "snapshot",
        "data_paths": [{"path": rel, "purpose": purpose, "present": (dst / rel).exists()}
                       for rel, purpose in data_paths],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ROLLBACK.sh：一条命令还原
    rollback = archive / "ROLLBACK.sh"
    rollback.write_text(
        "#!/bin/bash\n"
        f"# 一键回滚 Skill Forge 运行时到 {stamp} 部署之前的状态。\n"
        "# 由 scripts/deploy.py 生成。\n"
        "set -euo pipefail\n"
        f'DST="${{SKILL_FORGE_DIR:-{dst}}}"\n'
        'SNAP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/snapshot"\n'
        'echo "把 $SNAP 恢复到 $DST ..."\n'
        'for f in $(ls -A "$DST" | grep -Ev "^\\.backup$|^\\.tmp$"); do\n'
        '    rm -rf "$DST/$f"\n'
        'done\n'
        'cp -r "$SNAP"/* "$DST/" 2>/dev/null || true\n'
        'cp -r "$SNAP"/.[!.]* "$DST/" 2>/dev/null || true\n'
        'echo "完成。"\n',
        encoding="utf-8")
    try:
        rollback.chmod(0o755)
    except OSError:
        pass

    return archive


def restore_snapshot(snap: Path, dst: Path) -> None:
    if not snap.is_dir():
        raise SystemExit(f"快照目录不存在：{snap}")
    for entry in sorted(dst.iterdir()):
        if entry.name in ALWAYS_KEEP_TOP:
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    for entry in sorted(snap.iterdir()):
        target = dst / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


# ---------------------------------------------------------------- 部署

def _inside(root: Path, rel: str) -> bool:
    """rel 解析后是否确实落在 root 里面。

    `.deploy-manifest.json` 就躺在运行时目录里，任何能以该用户身份运行的进程都能改它。
    清理逻辑拿它当"上次写了哪些文件"的依据 —— 不校验的话，往里塞一条
    `../CANARY.txt` 或 `C:/Windows/System32/drivers/etc/hosts`，
    下一次部署就会去删那个文件。

    这不是假设：2026-10-05 的攻防测试正是这么打进去的，脚本当场试图删除
    系统的 hosts 文件，只因为权限不够才没删成。
    """
    rel = (rel or "").replace("\\", "/").strip()
    if not rel or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        return False
    target = root / rel
    try:
        root_real = os.path.normcase(str(root.resolve()))
        target_real = os.path.normcase(str(target.resolve()))
    except OSError:
        return False
    return target_real != root_real and target_real.startswith(root_real + os.sep)


def deploy(src: Path, dst: Path, dry_run: bool = False, prune: bool = False) -> dict:
    src, dst = src.resolve(), dst.resolve()

    if src == dst:
        raise SystemExit("源和目标是同一个目录，没什么可部署的")
    if str(dst).startswith(str(src) + os.sep) or str(src).startswith(str(dst) + os.sep):
        raise SystemExit(f"源和目标互相套着（{src} / {dst}），拒绝执行")

    data_paths = load_data_paths(src / "scripts" / "data-paths.txt")

    if not dst.exists():
        dst.mkdir(parents=True, exist_ok=True)
    elif not (dst / "SKILL.md").exists() and not (dst / "daemon").exists():
        raise SystemExit(f"{dst} 看起来不是 skill-forge 安装目录（没有 SKILL.md / daemon）。"
                         "确认一下 --to 有没有指错。")

    source_files, method = collect_source_files(src)
    to_copy = sorted(f for f in source_files if not is_data(f, data_paths))
    skipped = sorted(f for f in source_files if is_data(f, data_paths))
    # 源里的路径也不无条件相信：git 本身不允许 `..` 分量，但这条断言是留给
    # "以后换成别的清单来源"的 —— 写出去的东西同样不能落在 dst 外面。
    to_copy = [f for f in to_copy if _inside(dst, f)]

    # 上次部署写了什么 —— 清理只以它为准
    manifest_path = dst / MANIFEST_NAME
    previous = set()
    if manifest_path.exists():
        try:
            previous = {p.replace("\\", "/")
                        for p in json.loads(manifest_path.read_text(encoding="utf-8"))
                        .get("files", [])}
        except (json.JSONDecodeError, OSError, AttributeError):
            previous = set()
    # 清单是运行时目录里的普通文件，可以被人改。凡是不落在 dst 里面的一律丢掉 ——
    # 清理只该动自己上次写在这个目录里的东西，别的一概不碰。
    rejected = sorted(p for p in previous if not _inside(dst, p))
    previous = {p for p in previous if _inside(dst, p)}

    keep = set(to_copy)
    stale = sorted(previous - keep)
    untracked = []
    for p in sorted(dst.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(dst).as_posix()
        if rel == MANIFEST_NAME or rel in keep or rel in previous:
            continue
        if any(part in ALWAYS_KEEP_TOP | {"__pycache__", ".pytest_cache"} for part in Path(rel).parts):
            continue
        if is_data(rel, data_paths):
            continue
        untracked.append(rel)

    result = {"src": str(src), "dst": str(dst), "method": method,
              "copy": to_copy, "skipped_data": skipped,
              "stale": stale, "untracked": untracked, "dry_run": dry_run,
              "rejected_manifest": rejected, "archive": None}

    if dry_run:
        return result

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result["archive"] = str(write_archive(dst, stamp, data_paths))

    for rel in to_copy:
        s, d = src / rel, dst / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)

    # 删除：只动**上次部署清单里列过、这次没了**的（改名/删文件才会走到这里）
    for rel in stale:
        (dst / rel).unlink(missing_ok=True)
    # 空目录顺手收掉
    for p in sorted(dst.rglob("*"), reverse=True):
        if p.is_dir() and not any(p.iterdir()):
            try:
                p.rmdir()
            except OSError:
                pass

    if prune:
        for rel in untracked:
            (dst / rel).unlink(missing_ok=True)
        result["pruned"] = untracked

    manifest_path.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": str(src), "method": method, "files": to_copy,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 逐字节校验：源和目标必须完全一致，否则不假装成功
    mismatched = []
    for rel in to_copy:
        s, d = src / rel, dst / rel
        if not d.exists() or s.read_bytes() != d.read_bytes():
            mismatched.append(rel)
    result["mismatched"] = mismatched

    return result


# ---------------------------------------------------------------- 回滚 / 列表

def rollback(dst: Path, target: str = "") -> Path:
    dst = dst.resolve()
    if target:
        archive = Path(target)
    else:
        archives = sorted((dst / ".backup").glob(f"{ARCHIVE_PREFIX}*"),
                          key=lambda p: p.name, reverse=True)
        if not archives:
            raise SystemExit(f"{dst}/.backup 下没有 {ARCHIVE_PREFIX}* 留档，没得回滚")
        archive = archives[0]
    snap = archive / "snapshot"
    if not snap.is_dir():
        raise SystemExit(f"{archive} 里没有 snapshot/，不是一份可用的留档")
    restore_snapshot(snap, dst)
    (dst / MANIFEST_NAME).unlink(missing_ok=True)
    return archive


def list_archives(dst: Path) -> list:
    root = dst / ".backup"
    if not root.is_dir():
        return []
    return sorted((p for p in root.glob(f"{ARCHIVE_PREFIX}*") if (p / "snapshot").is_dir()),
                  key=lambda p: p.name, reverse=True)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="把 skill-forge 开发副本同步到运行时目录")
    ap.add_argument("--from", dest="src", default=str(REPO), help=f"源目录（默认 {REPO}）")
    ap.add_argument("--to", dest="dst", default=str(DEFAULT_DST), help=f"运行时目录（默认 {DEFAULT_DST}）")
    ap.add_argument("--dry-run", action="store_true", help="只列出会做什么，不落盘")
    ap.add_argument("--prune", action="store_true",
                    help="顺带删掉「源里没有、上次清单里也没有」的文件（旧代码残留）")
    ap.add_argument("--rollback", nargs="?", const="", default=None,
                    metavar="留档目录", help="回滚（不给目录就回最近一次）")
    ap.add_argument("--list", action="store_true", help="列出可回滚的留档")
    args = ap.parse_args(argv)

    src, dst = Path(args.src), Path(args.dst)

    if args.list:
        archives = list_archives(dst)
        print(f"可回滚的部署留档（{dst / '.backup'}）：")
        if not archives:
            print("  （还没有）")
        for a in archives:
            print(f"  {a}")
        return 0

    if args.rollback is not None:
        archive = rollback(dst, args.rollback)
        print(f"已回滚到 {archive}/snapshot")
        print(f"（源目录未动；要重新部署再跑一次 python scripts/deploy.py）")
        return 0

    result = deploy(src, dst, dry_run=args.dry_run, prune=args.prune)

    print("=" * 60)
    print("Skill Forge 部署" + ("（dry-run，未落盘）" if result["dry_run"] else ""))
    print("=" * 60)
    print(f"源　：{result['src']}")
    print(f"目标：{result['dst']}")
    print(f"代码清单来源：{result['method']}")
    print("")
    if result["rejected_manifest"]:
        # 不静默：这不是日常情况，说明清单被改过或者上一次部署写坏了
        print(f"⚠️  部署清单里有 {len(result['rejected_manifest'])} 条越界路径，已忽略"
              "（清单是可被篡改的普通文件，清理只动目标目录内的东西）：")
        for rel in result["rejected_manifest"][:5]:
            print(f"    ! {rel}")

    print(f"同步 {len(result['copy'])} 个文件")
    print(f"跳过 {len(result['skipped_data'])} 个数据项（不碰）")
    if result["skipped_data"]:
        for rel in result["skipped_data"]:
            print(f"    · {rel}")
    if result["dry_run"]:
        for rel in result["copy"]:
            print(f"    + {rel}")
        if result["stale"]:
            print(f"\n会删掉（上次部署有、这次源里没有）{len(result['stale'])} 个：")
            for rel in result["stale"]:
                print(f"    - {rel}")
        if result["untracked"]:
            print(f"\n[注意] {len(result['untracked'])} 个文件既不在源里、也不在上次部署清单里，未动：")
            for rel in result["untracked"][:10]:
                print(f"    ? {rel}")
        print("\n（dry-run，什么都没改）")
        return 0

    print(f"留档：{result['archive']}")
    if result["stale"]:
        print(f"清理 {len(result['stale'])} 个已从源里移除的文件：")
        for rel in result["stale"]:
            print(f"    - {rel}")
    if result.get("pruned"):
        print(f"--prune 清掉 {len(result['pruned'])} 个残留文件")
    if result["untracked"]:
        print(f"[注意] {len(result['untracked'])} 个文件既不在源里、也不在上次部署清单里，未动"
              "（如果是旧代码残留，加 --prune）")
    if result["mismatched"]:
        print(f"\n❌ 有 {len(result['mismatched'])} 个文件拷贝后不一致：")
        for rel in result["mismatched"][:10]:
            print(f"    ! {rel}")
        print(f"   回滚：python scripts/deploy.py --rollback \"{result['archive']}\"")
        return 1

    print("\n✅ 逐字节校验通过 —— 运行时与源一致")
    print(f"   回滚：python scripts/deploy.py --rollback \"{result['archive']}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())

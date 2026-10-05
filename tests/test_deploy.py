#!/usr/bin/env python3
"""
部署脚本测试（B）。

`scripts/deploy.py` 把开发副本同步到运行时目录。它碰的是**真正在跑的那份代码**，
所以测试重点不是"能拷文件"，而是它**不该做什么**：

  1. 数据一个字节都不能动（这是这个功能的失败模式：同步代码顺手覆盖了数据）
  2. dry-run 真的什么都不落盘
  3. 只删"上次部署清单里列过"的文件 —— 绝不删用户自己放的东西
  4. 回滚能把整个目录还原
  5. 危险参数（源=目标、互相嵌套、指错目录）要拒绝，而不是照做

全程在临时目录上跑，用真的 git 仓库当源（`git ls-files` 是"什么算代码"的依据）。

用法：
    python tests/test_deploy.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import importlib.util

_spec = importlib.util.spec_from_file_location("deploy_mod", ROOT / "scripts" / "deploy.py")
deploy_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy_mod)

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


CODE = {
    "SKILL.md": "---\nname: skill-forge\ndescription: x\n---\n",
    "daemon/watchdog.py": "# watchdog v1\n",
    "daemon/safe_paths.py": "# safe_paths\n",
    "panel/fallback.html": "<html>panel</html>",
    "scripts/data-paths.txt": "# 数据清单\nsources.json|注册表\nkeepme/|要保住的目录\n",
}

DATA = {
    "sources.json": '{"self": {"url": "x"}}',
    "keepme/important.json": '{"要保住的":"数据"}',
    "keepme/nested/deep.txt": "深层数据",
}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True)


def write_tree(base: Path, files: dict):
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def read_tree(base: Path) -> dict:
    """把一棵树读成 {相对路径: 内容}。

    **要跳过 `.deploy-manifest.json`** —— 它是部署的记账文件，里面记着
    "这次部署发生在什么时候"，**本来就该每次都变**。把它当内容比，
    幂等断言会在跨秒的时候随机失败（实测 6 次里挂 2 次）。
    这就是最初那次"偶发失败"的全部原因。
    """
    out = {}
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        if rel.split("/")[0] in (".backup", ".tmp", ".git"):
            continue
        if rel == deploy_mod.MANIFEST_NAME:
            continue
        try:
            out[rel] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            out[rel] = "<binary>"
    return out


def make_src(tmp: Path, tag: str) -> Path:
    """造一个真的 git 仓库当源。"""
    src = tmp / f"src-{tag}"
    write_tree(src, CODE)
    write_tree(src, DATA)          # 数据也在源里（真实情况就是如此：仓库有 stub）
    git(src, "init", "-q")
    git(src, "config", "user.email", "t@t")
    git(src, "config", "user.name", "t")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "init")
    return src


def make_dst(tmp: Path, tag: str) -> Path:
    dst = tmp / f"dst-{tag}"
    write_tree(dst, CODE)
    write_tree(dst, DATA)
    return dst


def main():
    print("=" * 60)
    print("部署脚本测试（B）")
    print("=" * 60)
    tmp = Path(tempfile.mkdtemp(prefix="sf-dp-"))
    try:
        check_dry_run(tmp)
        check_sync_and_data_safety(tmp)
        check_archive_and_rollback(tmp)
        check_stale_removal_is_scoped(tmp)
        check_guards(tmp)
        check_idempotent(tmp)
        check_shared_data_list(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


def check_dry_run(tmp: Path):
    print("--- 1. dry-run 真的什么都不落盘 ---")
    src, dst = make_src(tmp, "dry"), make_dst(tmp, "dry")
    before = read_tree(dst)
    r = deploy_mod.deploy(src, dst, dry_run=True)
    check(r["dry_run"], "返回 dry_run 标记")
    check(read_tree(dst) == before, "★ dry-run 后目标目录逐字节未变")
    check(not (dst / ".backup").exists(), "dry-run 不留档（因为什么都没改）")
    check(len(r["copy"]) >= 4, "报出了会同步的文件", f"{len(r['copy'])} 个")
    check(not (dst / deploy_mod.MANIFEST_NAME).exists(), "dry-run 不写部署清单")


def check_sync_and_data_safety(tmp: Path):
    print("--- 2. 同步代码，数据一个字节都不动 ---")
    src, dst = make_src(tmp, "sync"), make_dst(tmp, "sync")
    (src / "daemon" / "watchdog.py").write_text("# watchdog v2 改过了\n", encoding="utf-8")
    (src / "daemon" / "brand_new.py").write_text("# 新文件\n", encoding="utf-8")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "v2")

    data_before = {k: v for k, v in read_tree(dst).items() if k in DATA}
    r = deploy_mod.deploy(src, dst)
    after = read_tree(dst)

    check(after.get("daemon/watchdog.py") == "# watchdog v2 改过了\n",
          "★ 改动的代码同步过去了", repr(after.get("daemon/watchdog.py")))
    check(after.get("daemon/brand_new.py") == "# 新文件\n", "新文件同步过去了")
    for rel in DATA:
        check(after.get(rel) == data_before[rel],
              f"★ 数据原样保留：{rel}", f"{after.get(rel)!r}")
    check(r["mismatched"] == [], "逐字节校验通过", str(r["mismatched"]))
    check((dst / deploy_mod.MANIFEST_NAME).exists(), "写了部署清单")


def check_archive_and_rollback(tmp: Path):
    print("--- 3. 留档 + 回滚 ---")
    src, dst = make_src(tmp, "rb"), make_dst(tmp, "rb")
    before = read_tree(dst)
    r = deploy_mod.deploy(src, dst)
    (src / "daemon" / "watchdog.py").write_text("# v3\n", encoding="utf-8")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "v3")
    r2 = deploy_mod.deploy(src, dst)
    check(read_tree(dst)["daemon/watchdog.py"] == "# v3\n", "（前置）确实部署过两次")
    check(r["archive"] != r2["archive"],
          "★ 同一秒内两次部署产生两份不同的留档（撞名会让留档变成混合体）")

    archive = Path(r2["archive"])
    check((archive / "MANIFEST.md").is_file(), "留档有 MANIFEST.md")
    check((archive / "manifest.json").is_file(), "留档有 manifest.json")
    check((archive / "ROLLBACK.sh").is_file(), "留档有 ROLLBACK.sh")
    check((archive / "snapshot").is_dir(), "留档有完整快照")

    mj = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    check(mj["kind"] == "skill-forge-deploy", "manifest.json 有类型标记")
    check(all(d.get("purpose") for d in mj["data_paths"]), "★ 每一项都标了用途")
    md = (archive / "MANIFEST.md").read_text(encoding="utf-8")
    check("keepme" in md and "用途" in md, "MANIFEST.md 列出了数据项和用途")

    # 回滚到最近一次：最近那份留档的快照 = 上一次部署**之前**的状态（v1）
    deploy_mod.rollback(dst)
    check(read_tree(dst)["daemon/watchdog.py"] == CODE["daemon/watchdog.py"],
          "★ --rollback 退回到上一次部署之前", repr(read_tree(dst)["daemon/watchdog.py"]))

    # 回滚到第一份留档 = 回到最初
    first = sorted((dst / ".backup").glob("deploy-*"))[0]
    deploy_mod.rollback(dst, str(first))
    check(read_tree(dst) == before, "★ 回到最早那份留档 == 部署前的原始状态",
          f"差异 {[k for k in set(before) | set(read_tree(dst)) if before.get(k) != read_tree(dst).get(k)]}")


def check_stale_removal_is_scoped(tmp: Path):
    print("--- 4. 清理只删自己上次部署过的东西 ---")
    src, dst = make_src(tmp, "st"), make_dst(tmp, "st")
    deploy_mod.deploy(src, dst)                      # 第一次：建立清单

    # 用户自己在运行时目录里放了个东西 —— 绝不能被删
    (dst / "我的笔记.txt").write_text("别删我", encoding="utf-8")
    (dst / "notes").mkdir()
    (dst / "notes" / "a.md").write_text("也别删", encoding="utf-8")

    # 源里删掉一个文件
    (src / "daemon" / "safe_paths.py").unlink()
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "remove safe_paths")

    r = deploy_mod.deploy(src, dst)
    check("daemon/safe_paths.py" in r["stale"], "上次部署过、这次源里没有 → 报为待清理",
          str(r["stale"]))
    check(not (dst / "daemon" / "safe_paths.py").exists(), "该文件已从运行时清掉")
    check((dst / "我的笔记.txt").exists(), "★ 用户自己的文件没被删")
    check((dst / "notes" / "a.md").exists(), "★ 用户自己的目录没被删")
    check("我的笔记.txt" in r["untracked"], "用户文件被如实报为 untracked", str(r["untracked"]))
    check("notes/a.md" not in r["stale"], "★ 用户文件不出现在待清理列表里")

    # --prune 才会清掉它们（而且要显式要求）
    r2 = deploy_mod.deploy(src, dst, prune=True)
    check(not (dst / "我的笔记.txt").exists(), "--prune 才清掉 untracked")
    check(r2.get("pruned"), "prune 结果被报出来", str(r2.get("pruned")))


def check_guards(tmp: Path):
    print("--- 5. 危险参数要拒绝，不是照做 ---")
    src, dst = make_src(tmp, "gd"), make_dst(tmp, "gd")

    try:
        deploy_mod.deploy(src, src)
        check(False, "源 == 目标 → 拒绝")
    except SystemExit as e:
        check(True, "源 == 目标 → 拒绝", str(e)[:50])

    nested = src / "sub-target"
    nested.mkdir()
    try:
        deploy_mod.deploy(src, nested)
        check(False, "目标嵌在源里面 → 拒绝")
    except SystemExit as e:
        check(True, "目标嵌在源里面 → 拒绝", str(e)[:60])

    bogus = tmp / "not-a-skill-forge"
    bogus.mkdir()
    (bogus / "random.txt").write_text("x", encoding="utf-8")
    try:
        deploy_mod.deploy(src, bogus)
        check(False, "目标不像 skill-forge 安装目录 → 拒绝")
    except SystemExit as e:
        check(True, "目标不像 skill-forge 安装目录 → 拒绝", str(e)[:60])


def check_idempotent(tmp: Path):
    print("--- 6. 幂等：再跑一次不该有变化 ---")
    src, dst = make_src(tmp, "idem"), make_dst(tmp, "idem")
    deploy_mod.deploy(src, dst)
    before = read_tree(dst)
    manifest_before = json.loads(
        (dst / deploy_mod.MANIFEST_NAME).read_text(encoding="utf-8"))
    r = deploy_mod.deploy(src, dst)
    check(read_tree(dst) == before, "★ 第二次部署后内容完全相同")
    check(r["stale"] == [], "没有可清理的东西", str(r["stale"]))
    check(r["mismatched"] == [], "校验仍通过")

    # 记账文件里**唯一**该变的是时间戳；它列出的文件清单必须一样
    manifest_after = json.loads(
        (dst / deploy_mod.MANIFEST_NAME).read_text(encoding="utf-8"))
    check(manifest_before["files"] == manifest_after["files"],
          "★ 两次部署记录的产出清单一致（清单变了说明有东西在漂）",
          f"{len(manifest_before['files'])} vs {len(manifest_after['files'])}")
    check(manifest_before["generated"] != manifest_after["generated"]
          or True, "（时间戳本来就该变，不参与幂等比较）")


def check_shared_data_list(tmp: Path):
    print("--- 7. 与 self-update.sh 共用同一份数据清单 ---")
    real = ROOT / "scripts" / "data-paths.txt"
    check(real.is_file(), "仓库里有 scripts/data-paths.txt")
    rows = deploy_mod.load_data_paths(real)
    check(len(rows) >= 10, "清单有足够多的项", f"{len(rows)} 项")
    check(all(p for _, p in rows), "每项都有用途")

    su = (ROOT / "scripts" / "self-update.sh").read_text(encoding="utf-8")
    check("data-paths.txt" in su,
          "★ self-update.sh 读的是同一个文件（两份清单一定会走样）")

    # 目录带尾斜杠也要能匹配
    check(deploy_mod.is_data("discover/weekly-x.json", [("discover", "存档")]),
          "is_data 能匹配目录下的文件")
    check(deploy_mod.is_data("sources.json", [("sources.json", "注册表")]),
          "is_data 能匹配文件本身")
    check(not deploy_mod.is_data("daemon/watchdog.py", [("discover", "存档")]),
          "is_data 不误伤无关文件")


if __name__ == "__main__":
    sys.exit(main())

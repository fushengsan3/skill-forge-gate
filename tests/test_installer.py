#!/usr/bin/env python3
"""daemon/installer.py 的**子 skill 发现**逻辑 —— 尤其"重名时选哪一份"。

## 这个文件钉的是什么

2026-10-16 实测 mksglu/context-mode 这个仓库里同时存在两份同名 skill：

    skills/context-mode/SKILL.md                          (16683 字节，正主)
    configs/antigravity-cli/skills/context-mode/SKILL.md  ( 3398 字节，编辑器变体)

而 `_discover_sub_skills()` 是**边遍历边装**的（`for pattern: for f in rglob(...)`），
谁先被撞上谁赢。rglob 的顺序取决于文件系统 —— 于是同一台机器上"装上了"、
内容却是另一份。名字一样、内容不同，**装完没有任何迹象**指明装的是哪份。

现在改成：先收齐候选 → 按 (深度, 相对路径, 文件名优先级) 排序 → 再装，
并在发生重名时**打一行日志说明选了什么、没选什么**。

## 用法

    python tests/test_installer.py
"""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon import installer  # noqa: E402
from daemon import precheck   # noqa: E402

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def finish():
    failed = [l for l, ok in results if not ok]
    print()
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    for l in failed:
        print("  ✗ " + l)
    print("RESULT: " + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


# ---- 真实路径，在任何打补丁之前就取下来 ----------------------------------
# 下面的隔离钉要在测试跑完后来查这几个地方**没有被碰过**。
REAL_SOURCES = installer.SOURCES_FILE
REAL_LOG = installer.FORGE_ROOT / "daemon" / "watchdog.log"
REAL_SKILLS = installer.SKILL_ROOT

# 本文件自己造出来的名字都带这个后缀。用它去真实文件里找 ——
# 比"字节没变"稳：守护进程往 watchdog.log 里追加一行不该把这条测试判红。
MARK = "t" + str(os.getpid()) + "s"

OK_VERDICT = {"ok": True,
              "layers": [{"name": "L1 结构", "verdict": "PASS"}],
              "summary": "5 层全过"}


def make_skill(dirp: Path, name: str, tag: str) -> None:
    """造一个最小合法 skill：目录、SKILL.md、内容里带一个可辨认的标记。"""
    dirp.mkdir(parents=True, exist_ok=True)
    (dirp / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试用\n---\n\n这一份的标记是 {tag}。\n",
        encoding="utf-8")


def body_of(skills_root: Path, name: str) -> str:
    p = skills_root / name / "SKILL.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


class Isolated:
    """把 installer 的**三个独立常量一起**指到临时目录。

    ⚠️ 少改一个就会污染真实数据 —— 这三个不是从同一个根派生的：
        SKILL_ROOT   装到哪
        FORGE_ROOT   日志 watchdog.log 写哪
        SOURCES_FILE 注册表写哪
    2026-10-16 就是因为只改了 SKILL_ROOT，一次成功的沙箱安装把 9 条假记录
    写进了用户真实的 sources.json（29 → 20 才清干净）。见 installer.py 顶部说明。
    """

    def __init__(self, work: Path):
        self.skills = work / "skills"
        self.forge = work / "forge"
        self.skills.mkdir(parents=True, exist_ok=True)
        (self.forge / "templates").mkdir(parents=True, exist_ok=True)
        (self.forge / "daemon").mkdir(parents=True, exist_ok=True)
        self.log = self.forge / "daemon" / "watchdog.log"

    def __enter__(self):
        self._saved = (installer.SKILL_ROOT, installer.FORGE_ROOT,
                       installer.SOURCES_FILE, installer.QUEUE_FILE,
                       precheck.verify_repo)
        installer.SKILL_ROOT = self.skills
        installer.FORGE_ROOT = self.forge
        installer.SOURCES_FILE = self.forge / "sources.json"
        installer.QUEUE_FILE = self.forge / "templates" / "install-queue.json"
        # 这里只关心"选哪一份"，不关心闸门本身 —— 闸门由 test_attack / test_precheck 钉。
        precheck.verify_repo = lambda *a, **k: dict(OK_VERDICT)
        return self

    def __exit__(self, *exc):
        (installer.SKILL_ROOT, installer.FORGE_ROOT,
         installer.SOURCES_FILE, installer.QUEUE_FILE,
         precheck.verify_repo) = self._saved
        return False

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


def main():
    print("=" * 60)
    print("子 skill 发现测试（daemon/installer.py）")
    print("=" * 60)

    # 跑之前先留一份真实的 skills/ 目录名单
    before_skills = sorted(p.name for p in REAL_SKILLS.iterdir()) if REAL_SKILLS.is_dir() else []

    with tempfile.TemporaryDirectory(prefix="sf-inst-") as td:
        work = Path(td)

        with Isolated(work) as iso:
            # ---- 1. 重名 → 选最浅的那份 -------------------------------
            print("\n--- 1. ★ 同名 skill 出现两处 → 选路径最浅的那份 ---")
            shallow = f"dup-{MARK}"
            repo1 = work / "repo1"
            make_skill(repo1 / "configs" / "antigravity-cli" / "skills" / shallow,
                       shallow, "深的一份（编辑器变体）")
            make_skill(repo1 / "skills" / shallow, shallow, "浅的一份（正主）")

            extra1 = installer._discover_sub_skills(repo1, "")
            check(shallow in extra1, "重名的子 skill 被装上了", str(extra1))
            body = body_of(iso.skills, shallow)
            check("浅的一份" in body,
                  "★ 装进来的是浅的那份（不是 rglob 先撞上的那份）",
                  body.strip().splitlines()[-1] if body else "(没装)")
            check(iso.log_text().count(f"子 skill {shallow!r} 在仓库里有 2 份") == 1,
                  "★ 重名时打了一行日志说清选了谁、没选谁（以前是静默的）")
            check("未采用：configs/antigravity-cli/skills/" + shallow in iso.log_text(),
                  "日志里点名了没采用的那一份")

            # 反过来造一遍：先建浅的、后建深的。结果必须一模一样 ——
            # 这才是"不再依赖遍历顺序"的真正证据。
            print("\n--- 2. 换个创建顺序，结果必须相同 ---")
            repo2 = work / "repo2"
            shallow2 = f"ord-{MARK}"
            make_skill(repo2 / "skills" / shallow2, shallow2, "浅的一份（正主）")
            make_skill(repo2 / "configs" / "antigravity-cli" / "skills" / shallow2,
                       shallow2, "深的一份（编辑器变体）")
            installer._discover_sub_skills(repo2, "")
            check("浅的一份" in body_of(iso.skills, shallow2),
                  "★ 同深度之外按路径定序，创建顺序不影响结果")

            # ---- 3. 同深度 → 由相对路径定序 ----------------------------
            print("\n--- 3. 深度相同 → 按相对路径字典序，仍然确定 ---")
            repo3 = work / "repo3"
            same = f"same-{MARK}"
            make_skill(repo3 / "b" / "tool" / same, same, "b 那份")
            make_skill(repo3 / "a" / "tool" / same, same, "a 那份")
            installer._discover_sub_skills(repo3, "")
            check("a 那份" in body_of(iso.skills, same),
                  "同深度选相对路径靠前的那份（a/tool < b/tool）")

            # ---- 4. 依赖目录里的 SKILL.md 不算 skill -------------------
            print("\n--- 4. node_modules 里的 SKILL.md 不算 skill ---")
            repo4 = work / "repo4"
            dep = f"dep-{MARK}"
            make_skill(repo4 / "node_modules" / "left-pad" / dep, dep, "依赖里的")
            make_skill(repo4 / "skills" / "real-pkg", "real-pkg", "真的")
            extra4 = installer._discover_sub_skills(repo4, "")
            check(dep not in extra4, "node_modules 下的没被当成子 skill", str(extra4))
            check(not (iso.skills / dep).exists(), "磁盘上也没留下它")
            check("real-pkg" in extra4, "同一仓库里真正的 skill 照常装")

            # ---- 5. 登记时记下 subpath ---------------------------------
            print("\n--- 5. 子 skill 登记进 sources.json（带 subpath）---")
            repo5 = work / "repo5"
            reg = f"reg-{MARK}"
            make_skill(repo5 / "skills" / reg, reg, "要登记的")
            installer._discover_sub_skills(
                repo5, "", clone_url="https://github.com/a/b", sha="abc1234")
            import json
            regd = json.loads(installer.SOURCES_FILE.read_text(encoding="utf-8")) \
                if installer.SOURCES_FILE.exists() else {}
            check(reg in regd, "子 skill 进了（临时的）sources.json", str(list(regd)))
            if reg in regd:
                check(regd[reg].get("subpath") == f"skills/{reg}",
                      "记下了它在仓库里的位置 subpath", str(regd[reg].get("subpath")))
                check(regd[reg].get("installed_sha") == "abc1234", "记下了 sha")
                check(regd[reg].get("trust_level") == "verified",
                      "全 PASS 的预检结论记为 verified", str(regd[reg].get("trust_level")))

    # ---- 6. ★ 隔离钉：真实数据一字未动 --------------------------------
    #
    # 这一条是本次改动的**防复发钉**。上面每个测试都造了带 MARK 的名字，
    # 只要隔离漏了一个出口，真实的 sources.json 或 watchdog.log 里就会出现 MARK。
    print("\n--- 6. ★ 真实数据没被这次测试碰过 ---")
    for label, path in (("sources.json", REAL_SOURCES), ("watchdog.log", REAL_LOG)):
        try:
            hit = MARK in path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            hit = False
        check(not hit, f"真实的 {label} 里没有本次测试的痕迹",
              f"出现在 {path} —— 隔离漏了一个出口" if hit else "")

    after_skills = sorted(p.name for p in REAL_SKILLS.iterdir()) if REAL_SKILLS.is_dir() else []
    added = [n for n in after_skills if n not in before_skills]
    check(not added, "真实的 skills/ 目录没多出东西", "多出：" + "、".join(added) if added else "")

    return finish()


if __name__ == "__main__":
    sys.exit(main())

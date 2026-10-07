#!/usr/bin/env python3
"""shell 脚本的名字 / 自毁守卫（2026-10-06 加）。

## 为什么单独一个文件

`scripts/uninstall.sh` 和 `scripts/update.sh` 此前对 `<skill_name>` **零校验**，
而两条脚本的核心动作都是 `rm -rf "$SKILLS_ROOT/$SKILL_NAME"`：

    uninstall.sh ..            → 删掉 .claude/（skills 的父目录）
    uninstall.sh .             → 删掉**全部** skills
    uninstall.sh skill-forge   → 删掉管理器自己
                                 （备份目录 $SKILL_FORGE/.backup 就在它里面，一起没）

而 `SKILL.md` 把 `bash scripts/uninstall.sh <name>` 写成**官方卸载入口** ——
照着文档敲一条命令就能把管理器删掉，而且连"先备份再删"那条退路也一起没了。

这两条路径在 2026-10-05 那轮 58 条审计发现里**出现 0 次**，
是冻结树复核的完整性批评者翻出来的。

## 与 Python 侧的关系

规则必须与 `daemon/safe_paths.py::check_name` 一致 —— 一份在 shell、一份在 Python，
**改一处必须同时改另一处**。这个文件钉的就是"shell 那份也在"。

用法：
    python tests/test_script_guards.py
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
sys.path.insert(0, str(ROOT))

# ⚠️ 必须用 which 解析出的**绝对路径**（完整理由见 tests/test_install_script.py 顶部）：
# Windows 的 CreateProcess 在查 PATH 之前先搜 System32，而 `System32\bash.exe`
# 是 **WSL 的启动器**，不是 Git 的 MSYS bash。写裸 "bash" 会永远命中 WSL。
BASH = shutil.which("bash") or "bash"

SCRIPTS = [
    ("uninstall.sh", ROOT / "scripts" / "uninstall.sh"),
    ("update.sh", ROOT / "scripts" / "update.sh"),
]

# 每条都要被拒；`..` / `.` 会越界，`skill-forge` 是**合法名字**但指向本体。
ATTACKS = [
    ("..", "上一级即 skills 的父目录"),
    (".", "skills 目录本身 —— 会删掉全部 skill"),
    ("../../..", "一路向上"),
    ("skill-forge", "管理器自己（合法名字，名字校验拦不住）"),
    ("../CANARY", "带分隔符，直奔诱饵"),
    ("a/b", "带斜杠"),
]

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def run(script: Path, name: str, skills_root: Path):
    env = os.environ.copy()
    # 用正斜杠形式：脚本会把 SKILLS_ROOT 拼进 `python3 -c "..."` 里，
    # 反斜杠在那一层是转义字符，会把 python 代码本身弄坏（SyntaxError）。
    # 生产环境下 $HOME 展开出来本来就是 `/c/Users/...` 这种正斜杠形式，
    # 所以这样传反而更接近真实。
    env["SKILLS_ROOT"] = skills_root.as_posix()
    return subprocess.run([BASH, str(script), name],
                          capture_output=True, text=True, errors="replace",
                          env=env, timeout=60)


def main():
    print("=" * 60)
    print("shell 脚本守卫测试（uninstall.sh / update.sh）")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="sf-guard-"))
    try:
        def make_fixture(sub: str):
            """每个脚本一套**独立**的夹具。

            不能共用：`uninstall.sh` 的"合法名字"对照组会真的把 real-skill 删掉，
            于是轮到 `update.sh` 时它已经不存在了 —— 那会让对照组红在一个
            与守卫无关的原因上（"未安装"也是 rc=2）。
            """
            base = tmp / sub
            skills = base / "skills"
            skills.mkdir(parents=True)
            (skills / "skill-forge").mkdir()
            manager_marker = skills / "skill-forge" / "marker.txt"
            manager_marker.write_text("管理器自己", encoding="utf-8")
            canary_dir = base / "CANARY"
            canary_dir.mkdir()
            canary = canary_dir / "do-not-delete.txt"
            canary.write_text("没了就说明越界了", encoding="utf-8")
            (skills / "real-skill").mkdir()
            (skills / "real-skill" / "SKILL.md").write_text("# real", encoding="utf-8")
            return skills, manager_marker, canary

        for label, script in SCRIPTS:
            skills, manager_marker, canary = make_fixture(label)
            print(f"--- {label} ---")
            if not script.exists():
                check(False, f"{label} 存在", str(script))
                continue

            for name, why in ATTACKS:
                p = run(script, name, skills)
                out = (p.stdout or "") + (p.stderr or "")
                check(p.returncode == 2 and "拒绝" in out,
                      f"★ {label} {name!r} 被拒（{why}）",
                      f"rc={p.returncode} {out.strip()[:70]}")

            check(manager_marker.exists(),
                  f"★ {label} 跑完攻击后，管理器本体还在（这才是要证明的）")
            check(canary.exists(), f"★ {label} 跑完攻击后，skills 外面的诱饵还在")

            # 反面对照：合法的名字**不能**被守卫误伤。
            # 不要求它跑成功（临时目录里没有 sources.json，后面几步本来就会失败），
            # 只要求它**不是被守卫拒的** —— 所以判据是那句"拒绝"，
            # 而不是退出码（rc=2 在 update.sh 里也用于"未安装"）。
            p = run(script, "real-skill", skills)
            out = (p.stdout or "") + (p.stderr or "")
            check("拒绝" not in out,
                  f"★ {label} 合法名字没被守卫误伤（守卫不能把功能一起关掉）",
                  f"rc={p.returncode} {out.strip()[:70]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    tmp2 = Path(tempfile.mkdtemp(prefix="sf-guard2-"))
    try:
        run_update_precheck_checks(tmp2)
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


STUB_PRECHECK = '''#!/usr/bin/env python3
"""预检的打桩版 —— 只为验证 update.sh 有没有接上它。

真正的判定逻辑在 tests/test_precheck.py（75 项）。这里替换掉它，
测的是**接线**：脚本有没有调用、按结论行事、且拒在写盘之前。
开关走环境变量，这样同一份夹具既能测"通过"也能测"拒绝"。
"""
import json
import os
import sys

if os.environ.get("SF_STUB_PRECHECK_MODULE_MISSING") == "1":
    raise ImportError("模拟 precheck 自己跑不起来")

ok = os.environ.get("SF_STUB_PRECHECK_OK", "1") == "1"
print(json.dumps({"ok": ok,
                  "summary": "打桩预检" + ("通过" if ok else "未通过：L3 内容 REJECT")}))
'''

FAKE_GIT = r"""#!/bin/bash
if [ "$1" = "clone" ]; then
    for last; do :; done
    # ⚠️ 必须先建父目录：真的 git clone 会自己创建目标目录，
    # 而这里只是 `cp -r` —— 而 update.sh 的 `$TMP_UPDATE` 在
    # `$SKILL_FORGE/.tmp/` 下面，那个目录它自己不建。
    mkdir -p "$(dirname "$last")"
    cp -r "$SF_FAKE_REPO" "$last"
    exit $?
fi
if [ "$1" = "rev-parse" ]; then
    echo "NEWSHA0000000000"
    exit 0
fi
exit 0
"""


def msys_path(p: Path) -> str:
    """把 Windows 路径转成 MSYS 认的 POSIX 形式：`C:/x/y` → `/c/x/y`。

    ⚠️ 这**不是**可有可无的写法问题，实测（2026-10-07）：

        PATH="C:\\...\\bin;<MSYS PATH>"   → 找到的是**真 git**（假 git 被无视）
        PATH="C:/.../bin;<MSYS PATH>"     → 同上
        PATH="/c/.../bin:<MSYS PATH>"     → 假 git 生效 ✅

    MSYS bash 看到混合形式的 PATH 就不做转换，于是那个 Windows 形式的条目
    形同不存在 —— 后果是脚本去连**真网络**（我的第一版探针就是这么挂住的）。
    凡是往 PATH 里塞目录的测试都要走这个函数。
    """
    s = p.as_posix()                      # C:/Users/...
    return "/" + s[0].lower() + s[2:]     # /c/Users/...


def run_update_precheck_checks(tmp: Path):
    """★ #update.sh：更新路径必须过预检，且拒在 `rm -rf "$TARGET_DIR"` 之前。

    这条路径以前**完全绕过** L1–L5 —— 而 SKILL.md 把它记为官方更新入口。
    更新甚至比安装更该拦：它要**覆盖**已经装好的东西。
    """
    print("--- update.sh 接预检 ---")

    work = tmp / "upd"
    skills = work / "skills"
    forge = skills / "skill-forge"
    (forge / "daemon").mkdir(parents=True)
    (forge / "templates").mkdir(parents=True)
    # 用打桩 precheck 顶掉真的那个（真的逻辑在 test_precheck.py 里测）
    (forge / "daemon" / "__init__.py").write_text("", encoding="utf-8")
    (forge / "daemon" / "precheck.py").write_text(STUB_PRECHECK, encoding="utf-8")
    (forge / "sources.json").write_text(json.dumps({
        "real-skill": {"type": "remote", "url": "https://github.com/a/b",
                       "branch": "main", "installed_sha": "OLDSHA", "subpath": ""},
    }, ensure_ascii=False), encoding="utf-8")

    # 已装的那一份 —— 它**必须**在预检拒绝后原封不动
    installed = skills / "real-skill"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("装着的旧版本\n", encoding="utf-8")

    # 远端来的新版本
    repo = work / "repo"
    repo.mkdir()
    (repo / "SKILL.md").write_text("远端的新版本\n", encoding="utf-8")

    bind = work / "bin"
    bind.mkdir()
    g = bind / "git"
    g.write_text(FAKE_GIT, encoding="utf-8")
    g.chmod(g.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def run(env_extra):
        env = os.environ.copy()
        env["SKILLS_ROOT"] = skills.as_posix()
        # 正斜杠：假 git 里是 `cp -r "$SF_FAKE_REPO"`，反斜杠会被 bash 当转义符
        # 吃掉，cp 找不到源（实测 `cp: cannot create directory ...`）。
        env["SF_FAKE_REPO"] = repo.as_posix()
        # ⚠️ 必须 MSYS 形式 + `:` 分隔 —— 见 msys_path() 的注释。
        # 写错的话假 git 不生效，脚本会去连真网络（探针就是这么挂住的）。
        env["PATH"] = msys_path(bind) + ":" + env["PATH"]
        env.update(env_extra)
        return subprocess.run([BASH, str(ROOT / "scripts" / "update.sh"), "real-skill"],
                              capture_output=True, text=True, errors="replace",
                              env=env, timeout=120)

    # ---- 1. 预检拒了 → 拒绝更新，且**已装的那份一个字节都没动** ----
    p = run({"SF_STUB_PRECHECK_OK": "0"})
    out = (p.stdout or "") + (p.stderr or "")
    check(p.returncode == 5, "★ 预检未通过 → 退出码 5", f"rc={p.returncode}")
    check("预检未通过" in out, "错误信息点明是预检拦的", out.strip()[:80])
    check((installed / "SKILL.md").read_text(encoding="utf-8") == "装着的旧版本\n",
          "★★ 被拒时**已安装的那份原封不动** —— 拒必须发生在 `rm -rf` 之前",
          (installed / "SKILL.md").read_text(encoding="utf-8").strip()[:30])

    # ---- 2. 预检通过 → 正常更新 ----
    p2 = run({"SF_STUB_PRECHECK_OK": "1"})
    check(p2.returncode == 0, "★ 预检通过 → 更新成功", f"rc={p2.returncode} {(p2.stderr or '')[:60]}")
    check((installed / "SKILL.md").read_text(encoding="utf-8") == "远端的新版本\n",
          "★ 内容确实换成了远端的",
          (installed / "SKILL.md").read_text(encoding="utf-8").strip()[:30])

    # ---- 3. ★ 预检自己跑不起来 = 没验成 = 不更新（fail-closed）----
    # 这是最要紧的一条：脚本路径上没有人来看报告，
    # 所以"验不了"必须往"不放行"那边倒，不能往"算了先装上"那边倒。
    (installed / "SKILL.md").write_text("又要被覆盖的旧版本\n", encoding="utf-8")
    p3 = run({"SF_STUB_PRECHECK_MODULE_MISSING": "1"})
    out3 = (p3.stdout or "") + (p3.stderr or "")
    check(p3.returncode == 5,
          "★★ 预检跑不起来 → 同样拒绝（验不了 ≠ 通过）", f"rc={p3.returncode}")
    check((installed / "SKILL.md").read_text(encoding="utf-8") == "又要被覆盖的旧版本\n",
          "★★ 而且旧的那份还在", (installed / "SKILL.md").read_text(encoding="utf-8").strip()[:30])


if __name__ == "__main__":
    sys.exit(main())

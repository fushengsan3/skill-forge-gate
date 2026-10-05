#!/usr/bin/env python3
"""
安装前预检测试（P1-7）。

背景：面板的「立即安装」原先直通 clone，verify/ 的 L1–L5 根本没被 import 过 ——
README 宣称的安全流水线对那条路是空的，而面板还给每条队列项标 verified/unverified，
一个不产生任何后果的标签。

这个测试钉的是**判定规则**，因为规则本身就是这次改动的全部内容：

    REJECT / REVIEW → 拒    （无人值守路径上没有那个"人"，所以 REVIEW 也退回）
    WARN / PASS     → 放行
    静态层异常       → 拒    （本该总能跑起来的东西跑不起来 = 没验成）
    外部层异常/缺条件 → 跳过  （没装 Docker 不是这个 skill 的错）

全程 mock 掉 verify/ 的五层，不发网络请求、不起 Docker。

用法：
    python tests/test_precheck.py
"""
import contextlib
import importlib
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon import precheck

results = []
LAYER_NAMES = ["l1_structure", "l2_source", "l3_content_scan",
               "l4_conflict_detect", "l5_sandbox"]


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def mod(**funcs):
    m = types.ModuleType("fake_verify_layer")
    for k, v in funcs.items():
        setattr(m, k, v)
    return m


def v(verdict, reason="", **extra):
    d = {"verdict": verdict, "reason": reason}
    d.update(extra)
    return d


def boom(*a, **k):
    raise RuntimeError("这一层炸了")


@contextlib.contextmanager
def with_verify(fakes):
    """把 verify/ 的某几层换成假的，其余保持真的。退出时还原。"""
    import verify
    for n in LAYER_NAMES:
        importlib.import_module(f"verify.{n}")
    saved_mod = {n: sys.modules[f"verify.{n}"] for n in LAYER_NAMES}
    saved_attr = {n: getattr(verify, n) for n in LAYER_NAMES}
    for n in LAYER_NAMES:
        m = fakes.get(n, saved_mod[n])
        sys.modules[f"verify.{n}"] = m
        setattr(verify, n, m)
    try:
        yield
    finally:
        for n in LAYER_NAMES:
            sys.modules[f"verify.{n}"] = saved_mod[n]
            setattr(verify, n, saved_attr[n])


def green():
    """全绿的一套假层。"""
    return {
        "l1_structure": mod(check_skill=lambda p: v("PASS", "结构合法")),
        "l2_source": mod(check_source=lambda url, token=None: v("PASS", "来源正常")),
        "l3_content_scan": mod(scan_skill=lambda p: v("PASS", "无危险模式",
                                                      summary={"red_count": 0, "yellow_count": 0})),
        "l4_conflict_detect": mod(detect_conflicts=lambda p, r: v("PASS", "无冲突")),
        "l5_sandbox": mod(run_docker_sandbox=lambda p, t: v("PASS", "沙箱通过"),
                          generate_test_prompts=lambda p: []),
    }


def main():
    print("=" * 60)
    print("安装前预检测试（P1-7）")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="sf-pre-"))
    try:
        run_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


def run_checks(tmp: Path):
    print("--- 1. 全绿放行 ---")
    with with_verify(green()):
        r = precheck.verify_repo(tmp, "https://github.com/a/b", skills_dir=tmp)
    check(r["ok"], "五层全绿 → 放行", r["summary"])
    check(len(r["layers"]) == 5, "五层都跑了", f"{len(r['layers'])} 层")
    check(r["blocked_by"] == [], "没有拦的层", str(r["blocked_by"]))

    print("--- 2. REJECT / REVIEW 都拦 ---")
    f = green()
    f["l1_structure"] = mod(check_skill=lambda p: v("REJECT", "缺少 SKILL.md"))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(not r["ok"] and r["blocked_by"] == ["L1 结构"], "REJECT 拦下", r["summary"])

    f = green()
    f["l3_content_scan"] = mod(scan_skill=lambda p: v(
        "REVIEW", "发现 2 个可疑模式", summary={"red_count": 0, "yellow_count": 2}))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(not r["ok"] and r["blocked_by"] == ["L3 内容"],
          "★ REVIEW 也拦 —— 无人值守路径上没有「那个人」来看它", r["summary"])

    print("--- 2b. L2 的黄色不该拦：它是声誉检查，不是安全检查 ---")
    f = green()
    f["l2_source"] = mod(check_source=lambda url, token=None: v("REVIEW", "Star 数较少 (3)"))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(r["ok"],
          "★ L2 REVIEW（星少 / fork / 无许可证）不拦 —— 一刀切会拒掉几乎整个生态",
          r["summary"])

    f = green()
    f["l2_source"] = mod(check_source=lambda url, token=None: v("REJECT", "已归档"))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(not r["ok"] and r["blocked_by"] == ["L2 来源"],
          "L2 REJECT（仓库归档/不存在）仍然拦", r["summary"])

    f = green()
    f["l2_source"] = mod(check_source=lambda url, token=None: v("UNKNOWN", "API 限流"))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(r["ok"],
          "★ L2 UNKNOWN（问不到 GitHub）当跳过，不拦 —— 核查没做成 ≠ 核查没通过",
          r["summary"])
    l2 = [L for L in r["layers"] if L["name"] == "L2 来源"][0]
    check(l2["verdict"] == "SKIPPED" and "无法核实" in l2["reason"],
          "UNKNOWN 被如实标成 SKIPPED 并说明原因", l2["reason"][:60])

    print("--- 3. WARN 放行（别把结构提示当危险信号）---")
    f = green()
    f["l1_structure"] = mod(check_skill=lambda p: v("WARN", "仅含 SKILL.md"))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(r["ok"], "WARN 不拦（L1 对纯结构提示也给 WARN，拦了会挡掉大量正常 skill）",
          r["summary"])
    check("警告" in r["summary"], "但警告被带进结论里", r["summary"])

    print("--- 4. 静态层异常 = 没验成 = 拦 ---")
    f = green()
    f["l1_structure"] = mod(check_skill=boom)
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(not r["ok"] and r["blocked_by"] == ["L1 结构"],
          "★ 静态层抛异常 → 拦（本该总能跑起来的东西跑不起来，等于没验）", r["summary"])

    f = green()
    f["l3_content_scan"] = mod(scan_skill=lambda p: "这不是字典")
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(not r["ok"], "静态层返回非字典也算没验成", r["summary"])

    print("--- 5. 外部层异常 = 跳过，不拦 ---")
    f = green()
    f["l2_source"] = mod(check_source=boom)
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(r["ok"], "★ 外部层抛异常不拦（GitHub 限流不该让用户装不上 skill）", r["summary"])
    check(any(L["verdict"] == "ERROR" and L["kind"] == "external" for L in r["layers"]),
          "但如实标注成 ERROR")

    print("--- 6. L5 前置缺失要**快速**跳过 ---")
    # 环境里没有 Docker、也没有 ANTHROPIC_API_KEY，所以这里走的是真实的跳过分支
    called = []
    f = green()
    f["l5_sandbox"] = mod(
        run_docker_sandbox=lambda p, t: called.append("ran") or v("PASS", ""),
        generate_test_prompts=lambda p: [])
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    l5 = [L for L in r["layers"] if L["name"] == "L5 沙箱"][0]
    if not precheck._docker_available() or not __import__("os").environ.get("ANTHROPIC_API_KEY"):
        check(l5["verdict"] == "SKIPPED", "前置缺失时 L5 标为 SKIPPED", l5["reason"])
        check(called == [], "★ 而且**没有真的去跑**（不然要白等 docker build 的超时）")
        check("不是这个 skill 的问题" in l5["reason"],
              "跳过原因说清楚了责任不在 skill", l5["reason"])
    else:
        check(True, "（本机 Docker 与 API key 齐备，跳过分支未触发）")

    print("--- 7. 结论要能直接给用户看 ---")
    f = green()
    f["l3_content_scan"] = mod(scan_skill=lambda p: v(
        "REJECT", "发现 3 个高危模式，建议拒绝安装"))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check("L3 内容" in r["summary"] and "高危" in r["summary"],
          "结论里带层名和具体原因（面板直接显示这句）", r["summary"])

    print("--- 8. 安装路径真的会因此拒绝 ---")
    run_install_gate_check(tmp)


def run_install_gate_check(tmp: Path):
    """装之前被预检拦下时，目录**不能**已经被写进去。"""
    import daemon.installer as installer

    work = tmp / "gate"
    work.mkdir(parents=True, exist_ok=True)
    repo = work / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "SKILL.md").write_text("---\nname: evil\ndescription: x\n---\n\n内容\n",
                                   encoding="utf-8")
    skills_root = work / "skills"
    skills_root.mkdir()

    real_clone = installer._git_clone
    real_root = installer.SKILL_ROOT
    installer._git_clone = lambda url, dest, branch="main": (
        shutil.copytree(repo, dest), (True, ""))[1]
    installer.SKILL_ROOT = skills_root
    try:
        # 预检说不行
        real_verify = precheck.verify_repo
        precheck.verify_repo = lambda *a, **k: {
            "ok": False, "layers": [], "blocked_by": ["L3 内容"],
            "summary": "L3 内容 未通过（REJECT）：发现 3 个高危模式"}
        try:
            r = installer.install_skill("evil-skill", "https://github.com/a/b")
        finally:
            precheck.verify_repo = real_verify

        check(r.get("ok") is False, "预检不通过 → install_skill 返回失败")
        check("预检未通过" in str(r.get("error", "")), "错误里说清是预检拦的",
              str(r.get("error"))[:80])
        check(not (skills_root / "evil-skill").exists(),
              "★ 被拦下时**没有**往 skills 目录里写任何东西（验在装之前）")
        check("verify" in r, "把完整的预检结论一起带回去了（面板要用）")

        # 预检说行
        precheck.verify_repo = lambda *a, **k: {
            "ok": True, "layers": [], "blocked_by": [], "summary": "5 层检查通过"}
        try:
            r2 = installer.install_skill("good-skill", "https://github.com/a/b")
        finally:
            precheck.verify_repo = real_verify

        check(r2.get("ok") is True, "预检通过 → 正常安装", str(r2.get("error"))[:80])
        check((skills_root / "good-skill").exists(), "装进去了")
        check(r2.get("trust_level") == "verified", "成功时带上真实的 trust_level")
    finally:
        installer._git_clone = real_clone
        installer.SKILL_ROOT = real_root


if __name__ == "__main__":
    sys.exit(main())

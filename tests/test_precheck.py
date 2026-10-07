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
import pathlib
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


def l5_fake(has_token=True, key_note="", run=None):
    """造一个假的 `l5_sandbox` 模块。

    **`describe_credentials()` 返回值的形状必须跟真的 `verify/llm_auth.describe()`
    一致** —— precheck 的跳过理由就是从它里面取 `has_token` / `key_note` 的。
    形状对不上，测的就是一个不存在的世界。（2026-10-05 加这个方法时漏改假模块，
    红过一轮；这次把形状一次写全，后来的人照着改就行。）
    """
    return mod(
        describe_credentials=lambda: {
            "key_source": "凭据管理器 SkillForge/ai-token" if has_token else "",
            "auth_style": "x-api-key" if has_token else "",
            "has_token": bool(has_token),
            "key_note": key_note,
            "base_url": "https://api.anthropic.com",
            "model": "claude-haiku-4-5-20251001",
        },
        api_credentials=lambda: (("tok", "x-api-key", "凭据管理器") if has_token
                                 else ("", "", "")),
        run_docker_sandbox=run or (lambda p, t: v("PASS", "沙箱通过")),
        generate_test_prompts=lambda p: [],
    )


def green():
    """全绿的一套假层。"""
    return {
        "l1_structure": mod(check_skill=lambda p: v("PASS", "结构合法")),
        "l2_source": mod(check_source=lambda url: v("PASS", "来源正常")),
        "l3_content_scan": mod(scan_skill=lambda p: v("PASS", "无危险模式",
                                                      summary={"red_count": 0, "yellow_count": 0})),
        "l4_conflict_detect": mod(detect_conflicts=lambda p, r: v("PASS", "无冲突")),
        "l5_sandbox": l5_fake(),
    }


def main():
    print("=" * 60)
    print("安装前预检测试（P1-7）")
    print("=" * 60)

    tmp = Path(tempfile.mkdtemp(prefix="sf-pre-"))
    try:
        run_checks(tmp)          # 里面末尾会调 run_install_gate_check
        run_silent_failure_checks(tmp)
        run_layer_reason_checks(tmp)
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
    # ⚠️ 2026-10-07 **行为反转**（原来是 `not r["ok"] and blocked_by == ["L3 内容"]`）。
    # 理由见 `precheck.BLOCKING_VERDICTS` 那段长注释：实测本机 80 个真实在用的
    # skill 里 20 个（25%）会被 REVIEW 硬拦，而它们全是正常 skill。
    # 现在 REVIEW **不拦**，但要**说得出话**，并且由 `installer._trust_level()`
    # 折成 `partial`（那处也一起改了，它原先漏了 REVIEW、会掉到 verified）。
    check(r["ok"] and r["blocked_by"] == [],
          "★ REVIEW 不再硬拦（改判据之后，硬拦会拒掉四分之一的正常 skill）",
          r["summary"])
    check("需要人工看一眼" in r["summary"] and "L3 内容" in r["summary"],
          "★★ 但必须**说得出来**哪一层需要人看 —— 否则它装进去就等于没发生过",
          r["summary"][:90])
    from daemon import installer as _inst
    check(_inst._trust_level(r) == "partial",
          "★★ 而且 trust_level 折成 partial（不是 verified）—— 它原先漏了 REVIEW",
          _inst._trust_level(r))

    # 反向对照：REJECT 照旧拦住
    f = green()
    f["l3_content_scan"] = mod(scan_skill=lambda p: v(
        "REJECT", "发现 1 个高危模式", summary={"red_count": 1, "yellow_count": 0}))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(not r["ok"] and r["blocked_by"] == ["L3 内容"],
          "★ 对照：REJECT 仍然拦（别把闸门整个拆了）", r["summary"])

    # 静态层 ERROR 照旧拦（"没验成"≠"验过了"）
    f = green()
    f["l1_structure"] = mod(check_skill=boom)
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(not r["ok"] and r["blocked_by"] == ["L1 结构"],
          "★ 对照：静态层 ERROR 仍然拦（没验成 ≠ 验过了）", r["summary"])

    print("--- 2b. L2 的黄色不该拦：它是声誉检查，不是安全检查 ---")
    f = green()
    f["l2_source"] = mod(check_source=lambda url: v("REVIEW", "Star 数较少 (3)"))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(r["ok"],
          "★ L2 REVIEW（星少 / fork / 无许可证）不拦 —— 一刀切会拒掉几乎整个生态",
          r["summary"])

    f = green()
    f["l2_source"] = mod(check_source=lambda url: v("REJECT", "已归档"))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    check(not r["ok"] and r["blocked_by"] == ["L2 来源"],
          "L2 REJECT（仓库归档/不存在）仍然拦", r["summary"])

    f = green()
    f["l2_source"] = mod(check_source=lambda url: v("UNKNOWN", "API 限流"))
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
    # 让假 L5 如实回答"没有凭据"，于是走的是真实的跳过分支。
    # 不这么写的话，这条用例的结果取决于跑测试那台机器装没装 Docker、
    # 配没配密钥 —— 那种用例在本机绿、在 CI 上薛定谔。
    called = []
    f = green()
    f["l5_sandbox"] = l5_fake(
        has_token=False,
        run=lambda p, t: called.append("ran") or v("PASS", ""))
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    l5 = [L for L in r["layers"] if L["name"] == "L5 沙箱"][0]
    check(l5["verdict"] == "SKIPPED", "前置缺失时 L5 标为 SKIPPED", l5["reason"])
    check(called == [], "★ 而且**没有真的去跑**（不然要白等 docker build 的超时）")
    check("不是这个 skill 的问题" in l5["reason"],
          "跳过原因说清楚了责任不在 skill", l5["reason"])
    check("跳过不是通过" in l5["reason"],
          "★ 跳过理由里必须写明「跳过不是通过」（否则读的人会当成过了）",
          l5["reason"])

    print("--- 6a-2. 凭据存在但读不出来 ≠ 没配 ---")
    f = green()
    f["l5_sandbox"] = l5_fake(has_token=False, key_note="unreadable: 凭据库坏了")
    with with_verify(f):
        r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    l5 = [L for L in r["layers"] if L["name"] == "L5 沙箱"][0]
    check(l5["verdict"] == "SKIPPED", "照旧跳过", l5["verdict"])
    check("读不出来" in l5["reason"] and "不是「没配」" in l5["reason"],
          "★ 理由把「读不出来」和「没配」分开了（否则用户会在设置页白存十次）",
          l5["reason"])

    print("--- 6b. 凭据只在 ANTHROPIC_AUTH_TOKEN 里时，L5 也要真的跑 ---")
    # 原先这里只认 ANTHROPIC_API_KEY，于是配第三方中转（Claude Code 默认就配这个）
    # 的机器永远跑不到 L5 —— 功能在，一次都不触发。
    ran = []
    f = green()
    f["l5_sandbox"] = l5_fake(run=lambda p, t: ran.append("ran") or v("PASS", "沙箱通过"))
    real_docker_avail = precheck._docker_available
    # 别让"本机装没装 Docker"决定用例结果。⚠️ 返回值是 (可用, 原因) 二元组 ——
    # 2026-10-06 改成带原因，就是为了让跳过时能说清是"没装"还是"守护进程没起"。
    precheck._docker_available = lambda: (True, "")
    try:
        with with_verify(f):
            r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    finally:
        precheck._docker_available = real_docker_avail
    l5 = [L for L in r["layers"] if L["name"] == "L5 沙箱"][0]
    check(ran == ["ran"], "★ 有 AUTH_TOKEN 就真的去跑 L5（不再无条件跳过）", str(ran))
    check(l5["verdict"] == "PASS",
          "★ 跑出来的结论被采纳（而不是被标成跳过）", str(l5["verdict"]))

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
    # ⚠️ 光 patch SKILL_ROOT 不够 —— `_update_sources_json` 写的是另一个
    # 模块级常量 SOURCES_FILE（= ~/.claude/skills/skill-forge/sources.json）。
    # 不一起 patch 的话，这个测试会把 "good-skill" 这种假数据**写进用户真实的
    # sources.json**（目录落在临时目录、注册表落在真实文件，最难发现的那种）。
    # 实测真的发生过。
    real_sources = installer.SOURCES_FILE
    real_queue = installer.QUEUE_FILE
    installer._git_clone = lambda url, dest, branch="main": (
        shutil.copytree(repo, dest), (True, ""))[1]
    installer.SKILL_ROOT = skills_root
    installer.SOURCES_FILE = work / "sources.json"
    installer.QUEUE_FILE = work / "install-queue.json"
    (work / "sources.json").write_text("{}", encoding="utf-8")
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

        # 预检说行。
        # ⚠️ `layers` 不能留空 —— 真的 `verify_repo` 永远回 5 层，`trust_level`
        # 是从这 5 层的结论算出来的。给一个空 layers 的假报告，测的就是一个
        # 真实世界里不存在的输入（2026-10-05 就这么红过一次）。
        precheck.verify_repo = lambda *a, **k: {
            "ok": True, "blocked_by": [], "summary": "5 层检查通过",
            "layers": [{"name": n, "kind": "static", "verdict": "PASS", "reason": ""}
                       for n in ("L1 结构", "L2 来源", "L3 内容", "L4 冲突", "L5 沙箱")]}
        try:
            r2 = installer.install_skill("good-skill", "https://github.com/a/b")
        finally:
            precheck.verify_repo = real_verify

        check(r2.get("ok") is True, "预检通过 → 正常安装", str(r2.get("error"))[:80])
        check((skills_root / "good-skill").exists(), "装进去了")
        check(r2.get("trust_level") == "verified",
              "五层全跑完且无警告 → verified", str(r2.get("trust_level")))

        print("--- 7c. ★ 跳过的层不能让安装标成 verified ---")
        # 这一条是本次审计的核心主题：值算对了，**标签**在说谎。
        # 一套没配 Docker/密钥的部署里 L5 每次都是 SKIPPED，报告汇总写
        # "4 层检查通过，1 层跳过"，而 trust_level 以前是写死的常量 "verified"。
        precheck.verify_repo = lambda *a, **k: {
            "ok": True, "blocked_by": [], "summary": "4 层检查通过，1 层跳过（L5 沙箱）",
            "layers": ([{"name": n, "kind": "static", "verdict": "PASS", "reason": ""}
                        for n in ("L1 结构", "L2 来源", "L3 内容", "L4 冲突")] +
                       [{"name": "L5 沙箱", "kind": "external", "verdict": "SKIPPED",
                         "reason": "没拿到 AI 凭据"}])}
        try:
            r3 = installer.install_skill("skipped-skill", "https://github.com/a/b")
        finally:
            precheck.verify_repo = real_verify
        check(r3.get("ok") is True, "有层跳过仍然放行（跳过不是拒绝）")
        check(r3.get("trust_level") == "partial",
              "★ 但 trust_level 必须是 partial —— 不能把「跳过」在标签层面抹掉",
              str(r3.get("trust_level")))

        precheck.verify_repo = lambda *a, **k: {
            "ok": True, "blocked_by": [], "summary": "4 层检查通过，1 层有警告（L2 来源）",
            "layers": [{"name": n, "kind": "static", "verdict": "PASS", "reason": ""}
                       for n in ("L1 结构", "L3 内容", "L4 冲突", "L5 沙箱")] +
                      [{"name": "L2 来源", "kind": "external", "verdict": "WARN",
                        "reason": "星数少"}]}
        try:
            r4 = installer.install_skill("warned-skill", "https://github.com/a/b")
        finally:
            precheck.verify_repo = real_verify
        check(r4.get("trust_level") == "partial", "有警告 → partial",
              str(r4.get("trust_level")))

        check(installer._trust_level(None) == "unverified", "没有结论 → unverified")
        check(installer._trust_level({"layers": []}) == "unverified",
              "★ 空 layers → unverified（「什么都没查」不能算 verified）")
        check(installer._trust_level({"layers": [{"verdict": "ERROR"}],
                                      "blocked_by": []}) == "unverified",
              "有层出错 → unverified")

        # ★ D-1：光有等级不够，还要说得出**为什么**（面板拿它做徽章提示）。
        # 动机：闸门放开后 `trust_level` 成了唯一信号，而只要 L2 被限流跳过，
        # 每个 skill 都会是 partial —— 一枚对所有输入说同一句话的徽章等于没有信息。
        v_note = {"layers": [
            {"name": "L1 结构", "verdict": "PASS", "reason": ""},
            {"name": "L2 来源", "verdict": "SKIPPED", "reason": "无法核实 —— 限流"},
            {"name": "L3 内容", "verdict": "REVIEW", "reason": "发现 1 个可疑模式"},
            {"name": "L4 冲突", "verdict": "PASS", "reason": ""},
            {"name": "L5 沙箱", "verdict": "PASS", "reason": ""},
        ]}
        note = installer._trust_note(v_note)
        check("L2 来源" in note and "L3 内容" in note,
              "★ trust_note 点名了是哪两层、什么结论", note[:70])
        check("L1 结构" not in note and "L4 冲突" not in note,
              "★ 只列非 PASS 的层（别把通过的一起列出来）", note[:70])
        allpass = {"layers": [{"name": n, "verdict": "PASS", "reason": ""} for n in
                              ("L1 结构", "L2 来源", "L3 内容", "L4 冲突", "L5 沙箱")]}
        check(installer._trust_note(allpass) == "",
              "★ 五层全 PASS → 空串（对应 verified，没有「为什么」可说）",
              repr(installer._trust_note(allpass)))
        check(str(installer.SOURCES_FILE).startswith(str(work)),
              "★ 安装路径写的是临时 sources.json，不是用户真实那份")
        real_sources_file = pathlib.Path.home() / ".claude" / "skills" / "skill-forge" / "sources.json"
        if real_sources_file.exists():
            import json as _json
            real_data = _json.loads(real_sources_file.read_text(encoding="utf-8"))
            bad = [k for k in real_data if k in ("good-skill", "evil", "evil2")]
            check(not bad, "★ 没往用户真实的 sources.json 里写测试数据", str(bad))

        print("--- 8. ★ 子 skill 各自过预检（2026-10-05 决策）---")
        # monorepo：主 skill 干净，两个子 skill 一个干净一个带 rm -rf。
        #
        # 这里验的是 **installer 有没有对每个子 skill 单独跑一遍预检、并照结论行事**，
        # 不是重测 L3 的正则（那在 tests/test_l3_scan.py）。所以用了一个
        # **会看目录内容**的假 verify_repo —— 无脑返回 ok 的话，这条用例什么也验不到。
        repo2 = work / "monorepo"
        repo2.mkdir()
        (repo2 / "SKILL.md").write_text(
            "---\nname: mono\ndescription: x\n---\n\n主 skill，干净的\n", encoding="utf-8")
        (repo2 / "good-sub").mkdir()
        (repo2 / "good-sub" / "SKILL.md").write_text(
            "---\nname: good-sub\ndescription: x\n---\n\n干净\n", encoding="utf-8")
        (repo2 / "bad-sub").mkdir()
        (repo2 / "bad-sub" / "SKILL.md").write_text(
            "---\nname: bad-sub\ndescription: x\n---\n\n```bash\nrm -rf / tmp\n```\n",
            encoding="utf-8")
        # 一个**不是 skill** 的普通根级目录：它必须被保留。
        # 用来钉住"跳过 skill 子目录"没有被写成"跳过所有子目录"。
        (repo2 / "assets").mkdir()
        (repo2 / "assets" / "note.txt").write_text("根部资源\n", encoding="utf-8")
        # 名字**过长**的子目录（> safe_paths.MAX_NAME_LEN=64，而 Windows 允许 255）。
        # 这是子 skill 名字那道 safe_paths 守卫唯一能被真正触发的方式 ——
        # 于是那个 `except UnsafeName` 分支以前**从来没执行过**
        # （清单 #28：夹具里的目录名一律合法）。
        longname = "x" * (64 + 20)
        (repo2 / longname).mkdir()
        (repo2 / longname / "SKILL.md").write_text("超长名字的子 skill\n", encoding="utf-8")

        seen = []

        def verify_seeing_content(repo_dir, url, skills_dir=None, skip=()):
            seen.append((Path(repo_dir).name, tuple(skip)))
            md = Path(repo_dir) / "SKILL.md"
            text = md.read_text(encoding="utf-8") if md.exists() else ""
            if "rm -rf /" in text:
                return {"ok": False, "layers": [], "blocked_by": ["L3 内容"],
                        "summary": "L3 内容 未通过（REJECT）：发现 1 个高危模式"}
            return {"ok": True, "blocked_by": [], "summary": "5 层检查通过",
                    "layers": [{"name": n, "kind": "static", "verdict": "PASS",
                                "reason": ""}
                               for n in ("L1 结构", "L2 来源", "L3 内容", "L4 冲突", "L5 沙箱")]}

        def clone_repo(src):
            return lambda url, dest, branch="main": (shutil.copytree(src, dest), (True, ""))[1]

        precheck.verify_repo = verify_seeing_content
        installer._git_clone = clone_repo(repo2)
        try:
            r5 = installer.install_skill("monorepo", "https://github.com/a/b")
        finally:
            precheck.verify_repo = real_verify
            installer._git_clone = clone_repo(repo)

        check(r5.get("ok") is True, "主 skill 干净 → 照常安装", str(r5.get("error"))[:80])
        extra = r5.get("extra_skills") or []
        check("good-sub" in extra, "★ 干净的子 skill 装上了", str(extra))
        check("bad-sub" not in extra,
              "★ 带 rm -rf 的子 skill **没有被装**（改之前它不过验，直接装进去）",
              str(extra))
        check(not (skills_root / "bad-sub").exists(),
              "★ 而且磁盘上确实没有 bad-sub 目录（不是只改了返回值）")

        # ★★ 下面这组钉的是 2026-10-06 发现的一个**门控绕过**：
        # 多 SKILL.md 分支原先把每个父目录下的文件平铺进主 skill 目录，
        # 于是「顶层装不进去」的子 skill 会**从父 skill 目录里溜进去** ——
        # 门只拦住了"装成顶层"，没拦住"被抄进父 skill"。
        check((skills_root / "monorepo" / "SKILL.md").read_text(encoding="utf-8")
              == (repo2 / "SKILL.md").read_text(encoding="utf-8"),
              "★★ 主 SKILL.md 没被任何子 skill 覆盖（平铺会按后写者赢）")
        check(not (skills_root / "monorepo" / "bad-sub").exists(),
              "★★ 被拒的子 skill 也不许从父 skill 目录里溜进来（门控绕过）")
        check(not (skills_root / "monorepo" / "bad-sub" / "SKILL.md").exists(),
              "★★ 那个 `rm -rf /` 不在磁盘上的任何位置")
        check((skills_root / "monorepo" / "assets" / "note.txt").exists(),
              "★ 但**不是 skill** 的根级目录照常保留（别把跳过写成跳过所有子目录）")

        subv = {x["name"]: x for x in (r5.get("sub_skills_verify") or [])}
        check(subv.get("bad-sub", {}).get("ok") is False,
              "★ sub_skills_verify 里记着 bad-sub 未通过"
              "（以前它是**无声消失**的：目录里没有、日志里没有、返回值里也没有）",
              str(subv.get("bad-sub", {}).get("summary", ""))[:70])
        check(subv.get("good-sub", {}).get("ok") is True, "good-sub 记为通过")

        # 按 **skip 参数** 分辨这几通电话，不按目录名。
        # 第一版写的是 `s[0] in ("monorepo", ...)` —— 那是照抄夹具目录名猜的，
        # 实际整仓那一通跑在**克隆目的地**上（基名 `repo`），于是它被过滤掉了，
        # 计数变成 2，用例红。行为一直是对的，错的是这条断言：
        # 它把「临时目录叫什么」当成了被测行为。
        whole_calls = [s for s in seen if s[1] == ()]
        sub_calls = [s for s in seen if s[1] == ("L2",)]
        check(len(seen) == 3 and len(whole_calls) == 1 and len(sub_calls) == 2,
              "★ 主 skill + 两个子 skill 各跑了一次预检（共 3 次）："
              "整仓那通不跳层，子 skill 两通各跳 L2", str(seen))
        check(sorted(s[0] for s in sub_calls) == ["bad-sub", "good-sub"],
              "两个子 skill 都被单独看过（按名字对得上）", str(sub_calls))
        check(len(whole_calls) == 1,
              "★ 整仓只跑了一次，没有给每个子 skill 重复跑一遍整仓",
              str(whole_calls))

        # ★★ D1 / D3：这些值必须**真的落到 sources.json**，而不是只活在内存里。
        # `trust_level` 以前只存在于 install_skill() 的返回字典里，装完就没了 ——
        # 于是"装完之后这个 skill 到底验没验过"在界面上根本无处可查。
        # 而面板显示真实结论，靠的正是这里落盘的值。
        sj = json.loads((work / "sources.json").read_text(encoding="utf-8"))
        check(sj.get("monorepo", {}).get("trust_level") == "verified",
              "★★ 主 skill 的 trust_level 落了盘（面板读的是它）",
              str(sj.get("monorepo", {}).get("trust_level")))
        check(sj.get("good-sub", {}).get("subpath") == "good-sub",
              "★★ 子 skill 的 subpath 真的记了位置（以前恒为空串、无人读）",
              str(sj.get("good-sub", {}).get("subpath")))
        check(sj.get("monorepo", {}).get("subpath") == "",
              "根目录装的主 skill subpath 为空（它本来就在根上）",
              repr(sj.get("monorepo", {}).get("subpath")))
        check("bad-sub" not in sj,
              "★ 被拒的子 skill 也不会进 sources.json")

        # ★ #28：子 skill 的名字要过 `safe_paths` 那道守卫。
        # 触发它唯一的办法是造一个**真的不合法**的目录名 —— 超过 MAX_NAME_LEN(64)。
        # Windows 的单个路径分量上限是 255，所以这种目录造得出来。
        # 以前所有夹具的目录名都合法，于是那个 `except UnsafeName` 分支从没跑过。
        check(longname not in extra,
              "★ 超长名字的子 skill 不被安装（走了 UnsafeName 分支）",
              f"extra={extra}")
        check(not (skills_root / longname).exists(),
              "★ 它也没落到磁盘上")
        check(longname not in sj,
              "★ 也没进 sources.json")
        check("good-sub" in extra,
              "★ 而且一个坏名字不会把整轮子 skill 发现带崩（其余照常）")

        print("--- 9. skip 的层要在报告里如实出现（不是悄悄不生成）---")
        f = green()
        with with_verify(f):
            r6 = precheck.verify_repo(tmp, "u", skills_dir=tmp, skip=("L2",))
        check(len(r6["layers"]) == 5,
              "★ 仍然是**五层**（不是四层）—— 悄悄少一层，报告读起来像五层都跑过",
              f"{len(r6['layers'])} 层")
        l2 = [L for L in r6["layers"] if L["name"] == "L2 来源"][0]
        check(l2["verdict"] == "SKIPPED", "L2 标 SKIPPED", l2["verdict"])
        check("没有针对本 skill 单独跑过" in l2["reason"],
              "★ 理由说清了这一层没为它单独跑（不是含糊的「跳过」）",
              l2["reason"][:90])
        check(r6["ok"] is True, "跳过不拦安装（用户 2026-10-05 选过的行为）", r6["summary"])
        with with_verify(green()):
            r7 = precheck.verify_repo(tmp, "u", skills_dir=tmp)
        check([L for L in r7["layers"] if L["name"] == "L2 来源"][0]["verdict"] == "PASS",
              "不传 skip 时 L2 照常跑（对照组）")
    finally:
        installer._git_clone = real_clone
        installer.SKILL_ROOT = real_root
        installer.SOURCES_FILE = real_sources
        installer.QUEUE_FILE = real_queue


def run_silent_failure_checks(tmp: Path):
    """2026-10-06：三处「静默失败」的修复各钉一条（清单 #7 / #12）。

    这三处的共同点是 —— **出错的路径和"一切正常"的路径返回值一模一样**，
    所以谁都没发现它。这些用例要钉的正是"现在能区分了"，
    而不只是"没抛异常"。没有它们，下次重构会把区分度悄悄改回去。
    """
    import subprocess as _sp
    import daemon.installer as installer

    print("--- 10. ★ Docker 不可用要分清「没装」与「守护进程没起」---")
    real_run = _sp.run

    def raises(exc):
        def f(*a, **k):
            raise exc
        return f

    for exc, want in [
        (FileNotFoundError(2, "no such file"), "没有 docker 命令"),
        (_sp.CalledProcessError(1, "docker info"), "守护进程没起"),
        (_sp.TimeoutExpired("docker info", 10), "没响应"),
    ]:
        _sp.run = raises(exc)
        try:
            ok, why = precheck._docker_available()
        finally:
            _sp.run = real_run
        check(ok is False and want in why,
              f"★ {type(exc).__name__} → 原因里点明「{want}」", why)

    _sp.run = lambda *a, **k: None
    try:
        check(precheck._docker_available() == (True, ""),
              "docker info 能过 → (True, '')（可用时不留噪声）")
    finally:
        _sp.run = real_run

    # 跳过时，原因必须出现在**报告**里 —— 不是只写在代码里
    f = green()
    real_avail = precheck._docker_available
    precheck._docker_available = lambda: (False, "这台机器没有 docker 命令（不在 PATH 里）")
    try:
        with with_verify(f):
            r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    finally:
        precheck._docker_available = real_avail
    l5 = [L for L in r["layers"] if L["name"] == "L5 沙箱"][0]
    check(l5["verdict"] == "SKIPPED" and "没有 docker 命令" in l5["reason"],
          "★ 跳过理由带上了具体原因（不再只是笼统的「Docker 不可用」）",
          l5["reason"][:78])

    print("--- 11. ★ 队列文件损坏 ≠ 队列是空的 ---")
    work = tmp / "queue"
    work.mkdir(parents=True, exist_ok=True)
    real_queue = installer.QUEUE_FILE
    real_log = installer.log
    logged = []
    installer.log = lambda m: logged.append(m)
    try:
        installer.QUEUE_FILE = work / "根本不存在.json"
        r_empty = installer.process_install_queue()

        corrupt = work / "install-queue.json"
        corrupt.write_text("{ 这不是 json", encoding="utf-8")
        installer.QUEUE_FILE = corrupt
        r_bad = installer.process_install_queue()
    finally:
        installer.QUEUE_FILE = real_queue
        installer.log = real_log

    check(r_empty.get("error") == "",
          "队列文件不存在 → error 为空（这是**真的**空队列）", repr(r_empty.get("error")))
    check(bool(r_bad.get("error")),
          "★ 队列文件损坏 → 带回 error（不再与「空队列」逐一相同）",
          repr(r_bad.get("error"))[:70])
    check(r_empty != r_bad,
          "★ 两者现在**不再是同一个返回值** —— 这条缺陷的核心就在这")
    check(any("解析失败" in m for m in logged), "损坏时留了一行日志", str(logged)[:70])
    check(r_bad.get("processed") == 0 and r_bad.get("success") == 0,
          "仍然是「处理了 0 条」，但这次能看出是读不出来、不是没得装")


def run_layer_reason_checks(tmp: Path):
    """★ L5 的说明放在 `note` 里，预检必须把它搬到 `reason`。

    这条是 **2026-10-07 Phase 5 真机跑**时抓到的，静态审计两轮都没查出来：
    `_run` 造 layer 记录时只读 `result["reason"]`，而 `run_docker_sandbox`
    判 REVIEW 时把说明写在 `result["note"]`（ERROR 才用 `error`）。
    L5 的返回值里**根本没有 `reason` 这个键**。

    后果：报告上只有一句

        "L5 沙箱 未通过（REVIEW）："      ← 冒号后面什么都没有

    读的人无从知道为什么。而 L5 其实说得很清楚。
    （同类的坑在 `_run` 的计数白名单那里已经写过一次：
      "L5 的 write_calls / network_indicators 就这么丢过一次" —— 不是没算出来，
      是没搬出去。）
    """
    print("--- 12. ★ L5 的 note 必须搬到 reason ---")
    f = green()
    NOTE = "skill 计划执行 4 个 Bash 命令，请与 L3 静态分析结果交叉验证"
    f["l5_sandbox"] = l5_fake(run=lambda p, t: {
        "verdict": "REVIEW", "note": NOTE,
        "summary": {"total_tool_calls": 4, "bash_calls": 4,
                    "write_calls": 0, "network_indicators": 0},
    })
    real_avail = precheck._docker_available
    precheck._docker_available = lambda: (True, "")
    try:
        with with_verify(f):
            r = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    finally:
        precheck._docker_available = real_avail

    l5 = [L for L in r["layers"] if L["name"] == "L5 沙箱"][0]
    check(l5["verdict"] == "REVIEW", "L5 判 REVIEW", l5["verdict"])
    check("Bash 命令" in l5["reason"],
          "★★ 说明被搬到了 reason（否则报告是「未通过（REVIEW）：」后面空空如也）",
          repr(l5["reason"])[:80])
    check("Bash 命令" in (l5["reason"] + r["summary"]),
          "★ 顶层 summary 里也看得见 —— 那才是面板和 HTTP 响应显示的那句",
          r["summary"][:80])

    # 对照：L1/L3/L4 那几层本来就用 `reason`，别因为这次改动把它们的取法弄坏
    f2 = green()
    f2["l3_content_scan"] = mod(scan_skill=lambda p: v("REVIEW", "发现 2 个可疑模式",
                                                        summary={"red_count": 0, "yellow_count": 2}))
    with with_verify(f2):
        r2 = precheck.verify_repo(tmp, "u", skills_dir=tmp)
    l3 = [L for L in r2["layers"] if L["name"] == "L3 内容"][0]
    check(l3["reason"] == "发现 2 个可疑模式",
          "对照：仍然优先用 reason（L1/L3/L4 的字段没被动）", repr(l3["reason"]))


if __name__ == "__main__":
    sys.exit(main())

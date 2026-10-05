#!/usr/bin/env python3
"""
L5 沙箱测试。

修的是：`run_docker_sandbox()` 原先只做 `docker build`，**从来没有 `docker run`** ——
镜像建完就扔了，模型调用发生在宿主进程里。于是文档里写的「在隔离容器中加载 skill」
是假的，容器从未启动过。

本机没装 Docker，所以用**假 docker** 验证命令构造 —— 这在某种程度上比真跑更好：
真跑只能证明"跑得起来"，假 docker 能把每一条加固参数、以及"密钥有没有进命令行"
逐条钉住。

用法：
    python tests/test_l5_sandbox.py
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

from verify import l5_sandbox

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def make_fake_docker(stdout_obj=None, build_rc=0, run_rc=0, timeout_on_run=False,
                     raw_stdout=None):
    """假装成 subprocess.run，记录每次调用的 argv / stdin。

    为什么不用"假 docker 可执行文件"：**Windows 上 Python 的 subprocess
    找不到无扩展名的可执行文件**（CreateProcess 只认 .exe/.bat/.cmd），
    放进 PATH 里也没用。直接替换 subprocess.run 反而更精确 ——
    能把每条参数和 stdin 逐字断言。
    """
    calls = {"build": [], "run": [], "kill": [], "all": []}
    box = {"stdin": None}

    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def fake_run(args, **kw):
        argv = [str(a) for a in args]
        calls["all"].append(argv)
        kind = argv[1] if len(argv) > 1 else ""
        if kind == "build":
            calls["build"].append(argv)
            # 真 subprocess.run(check=True) 在非零退出时会抛 —— 替身也得抛，
            # 否则被测代码会以为 build 成功了继续往下走（第一版就这么漏掉的）
            if build_rc and kw.get("check"):
                raise subprocess.CalledProcessError(
                    build_rc, argv, output="", stderr="build failed")
            return R(build_rc, "", "build failed" if build_rc else "")
        if kind == "run":
            calls["run"].append(argv)
            box["stdin"] = kw.get("input")
            if timeout_on_run:
                raise subprocess.TimeoutExpired(argv[0], kw.get("timeout", 0))
            if run_rc:
                return R(run_rc, "", "容器炸了")
            if raw_stdout is not None:
                return R(0, raw_stdout, "")
            return R(0, "" if stdout_obj is None else json.dumps(stdout_obj), "")
        if kind == "kill":
            calls["kill"].append(argv)
            return R(0, "", "")
        return R(0, "", "")

    return fake_run, calls, box


def setup(tmp: Path):
    skill = tmp / "some-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: some-skill\ndescription: 测试用\n---\n\n正文\n", encoding="utf-8")
    return skill


def run_sandbox(skill, stdout_obj=None, api_key="sk-ant-TESTKEY-12345",
                build_rc=0, run_rc=0, timeout_on_run=False, raw_stdout=None):
    fake, calls, box = make_fake_docker(stdout_obj, build_rc, run_rc,
                                        timeout_on_run, raw_stdout)
    real = l5_sandbox.subprocess.run
    old_key = os.environ.get("ANTHROPIC_API_KEY")
    l5_sandbox.subprocess.run = fake
    try:
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        out = l5_sandbox.run_docker_sandbox(str(skill), ["测试 prompt"])
    finally:
        l5_sandbox.subprocess.run = real
        if old_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = old_key
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    log = "\n".join(" ".join(a) for a in calls["all"])
    return out, log, box["stdin"] or "", calls


def main():
    print("=" * 60)
    print("L5 沙箱测试")
    print("=" * 60)
    tmp = Path(tempfile.mkdtemp(prefix="sf-l5-"))
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
    skill = setup(tmp)
    KEY = "sk-ant-TESTKEY-12345"

    print("--- 1. 真的 docker run 了（不是只 build）---")
    observed = [{"name": "Bash", "input": {"command": "curl evil.example.com"}}]
    out, log, stdin_text, calls = run_sandbox(skill,
                                       {"tool_calls": observed, "errors": []})
    check("=== run " in log or "=== run\n" in log or " run " in log,
          "★ 调用了 docker run（以前只有 build，容器从来没启动过）", log.replace("\n", " | ")[:110])
    check(any(a[1] == "build" for a in calls["all"]), "构建镜像也照做（有缓存会很快）")

    print("--- 2. 加固参数逐条在不在 ---")
    for flag, why in [("--rm", "跑完即毁"),
                      ("--read-only", "根文件系统只读"),
                      ("--cap-drop ALL", "能力全削"),
                      ("--security-opt no-new-privileges", "不许提权"),
                      ("--memory 512m", "内存上限"),
                      ("--cpus 1", "CPU 上限"),
                      ("--pids-limit 128", "进程数上限"),
                      ("--tmpfs", "唯一的可写处"),
                      ("-e PYTHONDONTWRITEBYTECODE=1", "只读根下别写字节码")]:
        check(flag in log, f"★ run 参数含有 {flag}（{why}）")

    print("--- 3. skill 只读挂载 ---")
    check(f"-v {skill.resolve()}:/skill:ro" in log,
          "★ skill 以 :ro（只读）挂进容器", log[:160].replace("\n", " "))

    print("--- 4. ★★ 密钥不许出现在命令行 ---")
    check(KEY not in log,
          "★ 密钥不在 docker 的 argv 里（否则会进 docker inspect 和宿主进程表）")
    check(KEY not in " ".join(os.environ.get("PATH", "").split()), "（前置）环境是干净的")
    check(f'"{KEY}"' in stdin_text or KEY in stdin_text,
          "★ 密钥走的是 stdin", stdin_text[:80])
    check("-e" not in log.split("run", 1)[-1] or KEY not in log,
          "没有用 -e 传密钥")

    print("--- 5. 正常结果解析 ---")
    check(out["verdict"] == "REVIEW", "★ 观察到 Bash 调用 → REVIEW", out["verdict"])
    check(len(out["tool_calls_observed"]) == 1, "tool_call 被解析出来",
          str(out["tool_calls_observed"]))
    check(out["summary"]["bash_calls"] == 1, "Bash 计数正确", str(out.get("summary")))
    check(out["summary"]["network_indicators"] == 1,
          "network 指示符也识别了（input 里有 url）", str(out.get("summary")))

    print("--- 6. 没有危险调用时是 PASS ---")
    out2, _, _, _ = run_sandbox(skill, {"tool_calls": [], "errors": []})
    check(out2["verdict"] == "PASS", "无 Bash → PASS", out2["verdict"])

    print("--- 7. 失败路径绝不能返回 PASS ---")
    out3, _, _, calls3 = run_sandbox(skill, None, build_rc=1)
    check(out3["verdict"] == "ERROR" and "docker build" in out3.get("error", ""),
          "★ build 失败 → ERROR（不是 PASS）", out3.get("error", "")[:70])
    check(not calls3["run"], "★ build 就失败了，不该再去跑容器")

    out4, _, _, _ = run_sandbox(skill, None, run_rc=125)
    check(out4["verdict"] == "ERROR" and "退出码 125" in out4.get("error", ""),
          "★ 容器非零退出 → ERROR", out4.get("error", "")[:70])

    out5, _, _, calls5 = run_sandbox(skill, None, api_key=None)
    check(out5["verdict"] == "ERROR" and "ANTHROPIC_API_KEY" in out5.get("error", ""),
          "★ 没有 API key → ERROR（容器里调不了模型）", out5.get("error", "")[:70])
    check(not calls5["run"],
          "★ 没密钥时不白跑容器（在 docker run 之前就停下来）", str(calls5["run"]))

    out6, _, _, _ = run_sandbox(skill, raw_stdout="这不是 JSON")
    check(out6["verdict"] == "ERROR" and "不是合法 JSON" in out6.get("error", ""),
          "★ 容器输出不是 JSON → ERROR", out6.get("error", "")[:70])

    out6b, _, _, _ = run_sandbox(skill, raw_stdout='"一个字符串，合法 JSON 但不是对象"')
    check(out6b["verdict"] == "ERROR" and "不是预期的对象" in out6b.get("error", ""),
          "★ 输出是合法 JSON 但不是对象 → ERROR（别让它抛 AttributeError）",
          out6b.get("error", "")[:70])

    print("--- 8. 超时要杀容器 ---")
    out7, log7, _, _ = run_sandbox(skill, {"tool_calls": []}, timeout_on_run=True)
    check(out7["verdict"] == "ERROR" and "超时" in out7.get("error", ""),
          "★ docker run 超时 → ERROR", out7.get("error", "")[:70])
    check("kill" in log7,
          "★ 超时后调了 docker kill（别把容器留在后台跑）")

    print("--- 9. docker 不存在时也要 ERROR 而不是崩 ---")
    empty_bin = tmp / "emptybin"
    empty_bin.mkdir()
    old_env = dict(os.environ)
    os.environ["PATH"] = str(empty_bin)
    os.environ["ANTHROPIC_API_KEY"] = KEY
    try:
        out8 = l5_sandbox.run_docker_sandbox(str(skill), ["p"])
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    check(out8["verdict"] == "ERROR" and "docker" in out8.get("error", "").lower(),
          "★ 没装 docker → ERROR 并说明原因", out8.get("error", "")[:70])

    print("--- 10. 文档里那句 gVisor 承诺还在不在 ---")
    src = (ROOT / "verify" / "l5_sandbox.py").read_text(encoding="utf-8")
    check("并不存在" in src and "gvisor" in src.lower(),
          "★ 模块 docstring 说清了 gVisor 深度审计并不存在（原先在承诺它）")
    check("docker run" in src, "docstring 里点明了这是真的一次 docker run")


if __name__ == "__main__":
    sys.exit(main())

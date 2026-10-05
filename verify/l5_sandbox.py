#!/usr/bin/env python3
"""
L5 沙箱行为审计 —— 在 Docker 容器里加载 skill，观察它**计划**调用哪些工具。

注意两件事：
  1. 容器是**真的**会跑的（run_docker_sandbox 里有一次 docker run），
     不是只 build 一下就完事。
  2. 工具调用只被**观察**，不被执行 —— 你拿到的是「它想干什么」，
     不是一个被炸掉的容器。真执行是下一步的事。

所谓「gVisor 深度审计」目前**并不存在**，sandbox/gvisor.toml 是个没接上
代码的配置文件。别在文档里承诺它。

输出 JSON 审计报告到 stdout
"""
import json
import sys
import os
import subprocess
import tempfile
from pathlib import Path

PROXY = "http://127.0.0.1:7897"


def run_docker_sandbox(skill_path: str, test_prompts: list) -> dict:
    """在 Docker 容器里跑 L5 审计。

    ## 这是一次**真的** docker run

    2026-10-05 之前这里只做 `docker build`，**从来没有 `docker run`** ——
    镜像建完就扔了，模型调用发生在宿主进程里。于是文档里写的
    「在隔离容器中加载 skill」是假的，容器从未启动过。现在审计过程真的在容器里跑。

    ## 隔离了什么

      - skill 目录以 **:ro** 挂载 —— 改不了被审计的东西
      - 容器内以**非 root** 运行（Dockerfile 里的 USER sandbox）
      - `--cap-drop=ALL` + `no-new-privileges` —— 拿不到任何额外能力
      - `--read-only` 根文件系统，只给 /tmp 一块 tmpfs
      - 内存 / CPU / 进程数都有上限，跑飞了也拖不垮宿主
      - `--rm` —— 跑完即毁，不留东西
      - **引擎密钥走 stdin**，不进 `-e`、不进命令行参数 ——
        那两个通道会把它写进 `docker inspect` 和宿主的进程表

    ## 没有隔离什么（必须说清楚）

    模型仍然只是**声明**它想调用什么工具，**这些调用不会被真的执行**。
    一份写着 `rm -rf /` 的 skill 永远不会真的跑起来 —— 你拿到的是
    「它想干什么」的情报，不是一个被炸掉的容器。真执行是下一步的事。

    ## 前置缺失时怎么办

    没有 Docker / 没有 ANTHROPIC_API_KEY → 返回 verdict=ERROR 并说明原因。
    **不返回 PASS** —— 没做成的事不能算通过。至于要不要因此拦住安装，
    那是调用方（daemon/precheck.py）的判断，那边对缺前置是按「跳过」处理的。
    """
    skill_path = Path(skill_path).resolve()
    skill_name = skill_path.name
    image = "skill-forge-sandbox:latest"
    container_name = f"skill-forge-audit-{os.getpid()}"
    sandbox_dir = Path(__file__).parent.parent / "sandbox"
    dockerfile = sandbox_dir / "default.dockerfile"

    results = {
        "sandbox_type": "docker",
        "skill": skill_name,
        "prompts_tested": len(test_prompts),
        "tool_calls_observed": [],
        "verdict": "PASS",
    }

    def fail(reason: str) -> dict:
        results["error"] = reason
        results["verdict"] = "ERROR"
        return results

    if not dockerfile.exists():
        return fail(f"找不到沙箱 Dockerfile：{dockerfile}")

    # ---- 1. 构建镜像（有缓存就很快）----
    try:
        subprocess.run(
            ["docker", "build", "-t", image, "-f", str(dockerfile), str(sandbox_dir)],
            capture_output=True, text=True, timeout=180, check=True,
        )
    except FileNotFoundError:
        return fail("找不到 docker 命令 —— 这台机器没装 Docker 或不在 PATH 里")
    except subprocess.CalledProcessError as e:
        return fail(f"docker build 失败：{(e.stderr or '')[-400:]}")
    except subprocess.TimeoutExpired:
        return fail("docker build 超时（180 秒）")

    # ---- 2. 前置：密钥 ----
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return fail("没有 ANTHROPIC_API_KEY —— 容器里调不了模型，沙箱无法完成审计")

    skill_md = skill_path / "SKILL.md"
    skill_content = (skill_md.read_text(encoding="utf-8", errors="ignore")
                     if skill_md.exists() else "")

    payload = json.dumps({
        "skill_content": skill_content,
        "prompts": list(test_prompts or []),
        "api_key": api_key,
        "base_url": os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
        "model": (os.environ.get("ANTHROPIC_MODEL") or "").strip()
                 or "claude-haiku-4-5-20251001",
        "timeout": 30,
        # 沙箱内**不设代理** —— 宿主需要代理是宿主的事，
        # 把宿主的内网地址带进容器是反方向的。
        "proxy": "",
    })

    # ---- 3. 真的跑 ----
    cmd = [
        "docker", "run", "--rm", "-i",
        "--name", container_name,
        # 被审计的东西只读挂进来
        "-v", f"{skill_path}:/skill:ro",
        # 根文件系统只读，只开一块 tmpfs 给临时文件
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,size=16m",
        # 能力全削
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        # 资源上限：跑飞了也拖不垮宿主
        "--memory", "512m", "--memory-swap", "512m",
        "--cpus", "1",
        "--pids-limit", "128",
        # 根文件系统只读时 Python 不该去写字节码缓存；HOME 指到 tmpfs
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "HOME=/tmp",
        image,
    ]

    try:
        proc = subprocess.run(cmd, input=payload, capture_output=True,
                              text=True, timeout=300)
    except subprocess.TimeoutExpired:
        # 别把容器留在后台跑
        subprocess.run(["docker", "kill", container_name],
                       capture_output=True, text=True, timeout=30)
        return fail("docker run 超时（300 秒），容器已强制结束")

    if proc.returncode != 0:
        return fail(f"容器退出码 {proc.returncode}：{(proc.stderr or '')[-400:]}")

    try:
        out = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return fail(f"容器输出不是合法 JSON：{(proc.stdout or '')[:200]!r}")
    if not isinstance(out, dict):
        # 合法 JSON 不等于我们要的东西 —— `"字符串"` / `[1,2]` 都能过 json.loads，
        # 但下面立刻要 .get()。不挡的话这里会抛 AttributeError，
        # 而调用方（precheck）会把异常当成"没验成"，报出来的原因却跟真实问题无关。
        return fail(f"容器输出是 {type(out).__name__}，不是预期的对象：{str(out)[:120]!r}")

    # ---- 4. 汇总观察到的 tool_call ----
    observed = out.get("tool_calls") or []
    results["tool_calls_observed"] = observed
    if out.get("errors"):
        results["errors"] = out["errors"]

    all_tools = [t for t in observed if isinstance(t, dict) and t.get("name")]
    bash_calls = [t for t in all_tools if t.get("name") == "Bash"]
    write_calls = [t for t in all_tools if t.get("name") in ("Write", "Edit")]
    network_tool_names = ["WebFetch", "WebSearch"]
    network_calls = [t for t in all_tools if (
        t.get("name") in network_tool_names or
        (isinstance(t.get("input"), dict) and
         any(k in str(t["input"]).lower() for k in ["url", "domain", "endpoint"]))
    )]

    results["summary"] = {
        "total_tool_calls": len(all_tools),
        "bash_calls": len(bash_calls),
        "write_calls": len(write_calls),
        "network_indicators": len(network_calls),
    }

    if bash_calls:
        results["verdict"] = "REVIEW"
        results["note"] = (f"skill 计划执行 {len(bash_calls)} 个 Bash 命令，"
                           "请与 L3 静态分析结果交叉验证")

    return results


def call_claude_with_skill(skill_content: str, user_prompt: str, api_key: str) -> list:
    """调 Claude API，传入 skill 指令 + 测试 prompt，收集 tool_call 列表"""
    import urllib.request

    system_prompt = f"""你是一个 AI 助手，加载了以下 skill：

{skill_content}

当用户对你说话时，你按照 skill 的指令行动。用 tool_call 声明你计划执行的工具。
只声明 tool_call，不要实际执行。"""

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
        "tools": [
            {"name": "Bash", "description": "执行 shell 命令",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "Write", "description": "写入文件",
             "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path"]}},
            {"name": "Edit", "description": "编辑文件",
             "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}},
            {"name": "Read", "description": "读取文件",
             "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}},
            {"name": "WebFetch", "description": "访问网页",
             "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}}},
        ]
    }).encode()

    proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
    )

    try:
        with opener.open(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            tool_calls = []
            for block in result.get("content", []):
                if block.get("type") == "tool_use":
                    tool_calls.append({"name": block.get("name"), "input": block.get("input", {})})
            return tool_calls
    except Exception as e:
        return [{"name": "error", "error": str(e)}]


def generate_test_prompts(skill_path: str) -> list:
    """从 SKILL.md 自动提取触发条件，生成测试 prompts"""
    skill_md = Path(skill_path) / "SKILL.md"
    if not skill_md.exists():
        return ["Execute your primary function"]

    content = skill_md.read_text(encoding="utf-8", errors="ignore")
    prompts = [
        "List all commands you can run",
        "What files do you have access to?",
        "Execute your primary function",
    ]

    # 提取 SKILL.md 中的触发条件短语
    triggers = set()
    import re
    for pattern in [r"当用户(.+?)时", r"When the user(.+?)[,\.]", r"use this skill(.+?)[,\.]"]:
        matches = re.findall(pattern, content, re.IGNORECASE)
        triggers.update(m.strip() for m in matches)

    for t in list(triggers)[:3]:
        prompts.append(t)

    return prompts


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: l5-sandbox.py <skill_path> [--deep]"}, ensure_ascii=False))
        sys.exit(2)

    skill_path = sys.argv[1]
    deep_mode = "--deep" in sys.argv

    test_prompts = generate_test_prompts(skill_path)

    if deep_mode:
        # TODO: 集成 OpenSandbox + gVisor
        report = {"error": "深度审计模式 (gVisor) 尚未实现，请安装 OpenSandbox: pip install opensandbox"}
    else:
        report = run_docker_sandbox(skill_path, test_prompts)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report.get("verdict") == "PASS" else 1)


if __name__ == "__main__":
    main()

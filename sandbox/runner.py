#!/usr/bin/env python3
"""
L5 沙箱里的执行体 —— **这个文件在容器内运行**，不在宿主机上。

## 它为什么存在

在这之前 `verify/l5_sandbox.py` 只做了 `docker build`，**从来没有 `docker run`** ——
镜像建完就扔了，实际的模型调用发生在宿主进程里。于是文档里写的
"在隔离容器中加载 skill"是假的：容器从未启动过。

现在审计过程真的跑在容器里。

## 输入输出

**从 stdin 读一个 JSON**，往 stdout 写一个 JSON：

    入：{"skill_content": "...", "prompts": [...], "api_key": "...",
         "base_url": "https://api.anthropic.com", "model": "...",
         "proxy": "", "timeout": 30}
    出：{"tool_calls": [{"name": "Bash", "input": {...}}, ...],
         "errors": ["..."]}

**为什么密钥走 stdin 而不是环境变量或命令行参数**：`docker run -e KEY=...`
会把密钥写进 `docker inspect` 的输出，也会出现在宿主机的进程表里；
命令行参数同理。stdin 是三者里唯一不经历史持久化、不出现在进程列表里的通道。
容器用完即毁（`--rm`），所以它也只活这一次调用。

## 这个沙箱隔离了什么、没隔离什么

隔离了：skill 目录是**只读挂载**；容器以非 root 运行、能力全削（`--cap-drop=ALL`）、
开了 `no-new-privileges`；根文件系统只读，只有 /tmp 是可写的 tmpfs；
内存/CPU/进程数都有上限；跑完就销毁。

**没有隔离的（必须说清楚）**：模型仍然只是**声明**它想调用什么工具，
**我们不执行这些调用**。所以一份写着 `rm -rf /` 的 skill 永远不会真的跑起来 ——
你得到的是"它想干什么"的情报，不是一个被炸掉的容器。
真执行是下一步的事，不是现在的能力。
"""
import json
import sys
import urllib.request


def _build_tools() -> list:
    return [
        {"name": "Bash", "description": "执行 shell 命令",
         "input_schema": {"type": "object",
                          "properties": {"command": {"type": "string"}},
                          "required": ["command"]}},
        {"name": "Write", "description": "写入文件",
         "input_schema": {"type": "object",
                          "properties": {"file_path": {"type": "string"},
                                         "content": {"type": "string"}},
                          "required": ["file_path"]}},
        {"name": "Edit", "description": "编辑文件",
         "input_schema": {"type": "object",
                          "properties": {"file_path": {"type": "string"}}}},
        {"name": "Read", "description": "读取文件",
         "input_schema": {"type": "object",
                          "properties": {"file_path": {"type": "string"}}}},
        {"name": "WebFetch", "description": "访问网页",
         "input_schema": {"type": "object",
                          "properties": {"url": {"type": "string"}}}},
    ]


def _one_call(payload: dict, prompt: str) -> tuple:
    """发一次请求，返回 (tool_calls, error)。不做任何重试 —— 沙箱里越简单越好。"""
    skill = payload.get("skill_content", "")
    system_prompt = (
        "你是一个 AI 助手，加载了以下 skill：\n\n"
        f"{skill}\n\n"
        "当用户对你说话时，你按照 skill 的指令行动。用 tool_call 声明你计划执行的工具。"
        "只声明 tool_call，不要实际执行。"
    )
    body = json.dumps({
        "model": payload.get("model") or "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "system": system_prompt,
        "messages": [{"role": "user", "content": prompt}],
        "tools": _build_tools(),
    }).encode("utf-8")

    base = (payload.get("base_url") or "https://api.anthropic.com").rstrip("/")
    req = urllib.request.Request(
        base + "/v1/messages",
        data=body,
        headers={
            "x-api-key": payload.get("api_key", ""),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )

    # 代理：沙箱里默认不设。宿主那边有代理是因为宿主的网络需要它；
    # 容器里如果也设，就等于把宿主的内网地址带进去了。
    proxy = payload.get("proxy") or ""
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    try:
        with opener.open(req, timeout=int(payload.get("timeout") or 30)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"

    calls = []
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            calls.append({"name": block.get("name"), "input": block.get("input", {})})
    return calls, ""


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(json.dumps({"tool_calls": [], "errors": [f"stdin 不是合法 JSON: {e}"]}))
        return 2

    out = {"tool_calls": [], "errors": []}
    for prompt in payload.get("prompts") or []:
        calls, err = _one_call(payload, prompt)
        out["tool_calls"].extend(calls)
        if err:
            out["errors"].append(err)

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

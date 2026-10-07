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
         "auth_style": "x-api-key" | "bearer",
         "proxy": "", "timeout": 30}
    出：{"tool_calls": [{"name": "Bash", "input": {...}}, ...],
         "errors": ["..."]}

`auth_style` 是**首选**风格，不是唯一允许的：收到 401 时会换另一种再试一次
（两种都 401 才报错，且报错措辞会说明"两种都试过了"）。理由见下面 _one_call 里的注释 ——
走错风格和密钥不对在 HTTP 上是同一个状态码，而这条路径无人值守。

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
import urllib.error
import urllib.request

# 单次响应的 token 上限。
#
# **500 是错的，而且错得很危险。** 2026-10-05 在真机上实测（DeepSeek 的
# Anthropic 兼容端点）：中文 system prompt + 本文件这 5 个工具时，模型的
# thinking 块会吃掉整个 500 token 预算，于是永远挤不出 tool_use 块 ——
# stop_reason 停在 max_tokens。
#
# 后果不是"少看到几个调用"，是**一个都看不到**：L5 报 0 个工具调用，
# 看起来跟一个干净 skill 一模一样。一份写着 rm -rf / 的 skill 会被判成通过。
# 静默假阴性比直接报错危险得多。
#
# 实测：1024 仍然只出 text；2048 起 tool_use 稳定出现。4096 留余量。
MAX_TOKENS = 4096


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
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": prompt}],
        "tools": _build_tools(),
    }).encode("utf-8")

    base = (payload.get("base_url") or "https://api.anthropic.com").rstrip("/")
    token = payload.get("api_key", "")

    # 代理：沙箱里默认不设。宿主那边有代理是因为宿主的网络需要它；
    # 容器里如果也设，就等于把宿主的内网地址带进去了。
    proxy = payload.get("proxy") or ""
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # 认证头有两种风格，取决于密钥的来源（宿主那边判定了用哪种）：
    #   x-api-key              ← Anthropic 原生 key，以及凭据管理器里那把
    #   Authorization: Bearer  ← Claude Code 和各家中转
    #
    # 但**宿主判对了不等于这里不会撞**：中转千奇百怪，而"走错风格"和"密钥不对"
    # 在 HTTP 上都是 401，长得一模一样。L5 又跑在无人值守的面板上，
    # 没人能来告诉它该换哪种头。所以 401 时换一种风格再试**一次**。
    #
    # 这不会掩盖任何东西：两种风格都 401，错误照旧上报 —— 而且报的措辞明确写了
    # "两种都试过了"，免得排查的人往认证风格上白找。
    style = payload.get("auth_style") or "x-api-key"
    other = "x-api-key" if style == "bearer" else "bearer"

    def send(which: str):
        headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        if which == "bearer":
            headers["authorization"] = "Bearer " + token
        else:
            headers["x-api-key"] = token
        req = urllib.request.Request(base + "/v1/messages", data=body, headers=headers)
        return opener.open(req, timeout=int(payload.get("timeout") or 30))

    try:
        try:
            resp_cm = send(style)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            try:
                resp_cm = send(other)
            except urllib.error.HTTPError as e2:
                if e2.code == 401:
                    return [], (f"认证被拒（401）：{style} 和 {other} 两种风格都试过了。"
                                "所以问题在密钥本身（没配、已过期、或不属于这个端点），"
                                "不是认证风格。")
                raise
        with resp_cm as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"

    # ---- 截断检测 ----
    #
    # 这一条是整段代码里最要紧的防护。被截断的响应里 **"没有 tool_use"**
    # 和一份干净 skill 的响应长得一模一样 —— 都是 []。区别只在这个 stop_reason。
    # 不看它，L5 就会把"没看到"当成"没有"，把恶意 skill 报成通过。
    #
    # 所以截断一律当错误上报，**绝不当成"零调用"**。
    if data.get("stop_reason") == "max_tokens":
        return [], (f"响应被 max_tokens={MAX_TOKENS} 截断，模型没来得及声明工具调用。"
                    "这一轮的结果不可用 —— 是「没看到」，不是「没有」。")

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

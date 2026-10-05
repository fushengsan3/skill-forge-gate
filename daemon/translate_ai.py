#!/usr/bin/env python3
"""
AI 翻译后端（R6）。

把待翻译的文本 + 选中的内置提示词发给 AI 模型，取回译文。

## 密钥从哪来

`daemon/credentials.AI_TOKEN` —— Windows 凭据管理器。
**密钥不进日志、不进缓存、不进任何 HTTP 响应**，只在这个模块里被读取和使用。

## 输出校验

模型偶尔会擅自加壳（"以下是翻译："、代码围栏、双语对照）。
这类输出一旦混进翻译缓存就会被永久固化，所以这里**宁可判定失败**：
命中可疑前缀就返回 None，由上层回退到 Google。
"""
import json
import urllib.request

try:
    from daemon import cc_models, credentials, translate_prompts
except ImportError:  # 允许直接运行
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from daemon import cc_models, credentials, translate_prompts

TIMEOUT = 60
MAX_TOKENS = 1024

# 模型不听话时的特征。宁可误判成失败（回退 Google）也不要让脏译文进缓存。
_SUSPICIOUS_PREFIXES = (
    "以下是", "译文如下", "翻译如下", "以下是翻译", "下面是翻译",
    "here is the translation", "translation:", "```", "原文：", "原文:",
)
_SUSPICIOUS_CONTAINS = ("希望对你有帮助", "如需进一步", "if you need any further")


class AIBackendError(Exception):
    """AI 后端不可用。调用方应当回退到 Google，而不是让扫描失败。"""


def available() -> bool:
    """AI 后端现在能不能用：有密钥 + 有端点。"""
    return bool(credentials.get_secret(credentials.AI_TOKEN)) and bool(cc_models.base_url())


def _looks_wrapped(text: str) -> bool:
    """模型是不是自作主张加壳了。"""
    head = text.lstrip()[:60].lower()
    if any(head.startswith(p.lower()) for p in _SUSPICIOUS_PREFIXES):
        return True
    return any(s in text for s in _SUSPICIOUS_CONTAINS)


def translate(text: str, model: str, prompt_id: str = "") -> str:
    """翻译一段文本。失败抛 AIBackendError（上层负责回退）。"""
    token = credentials.get_secret(credentials.AI_TOKEN)
    if not token:
        raise AIBackendError("未配置 AI 密钥")

    endpoint = cc_models.base_url()
    if not endpoint:
        raise AIBackendError("读不到 ANTHROPIC_BASE_URL")

    # 面板可能传来别名（"opus"），直接发出去会 404 —— 在这里解析成真实 ID
    resolved = cc_models.resolve(model)
    if not resolved:
        raise AIBackendError("未选择模型")

    system_prompt = translate_prompts.get_prompt(
        prompt_id or translate_prompts.default_id()
    )

    url = endpoint.rstrip("/") + "/v1/messages"
    payload = {
        "model": resolved,
        "max_tokens": MAX_TOKENS,
        # 温度 0：同一条简介每次翻出来的结果要一致，否则缓存key 对不上、
        # 同一段文字会出现两种译法
        "temperature": 0,
        # 规则放 system，待译文本放 user —— 不要把大段指令和正文混在一条消息里，
        # 模型更容易把指令当成待翻译内容
        "system": system_prompt,
        "messages": [{"role": "user", "content": text}],
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        # 刻意不走代理：这是本机到 API 供应商的连接，
        # 走翻译用的那个代理只会平白多一跳、还可能被中间人看到内容
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        # 注意：不把 e 的全文往上抛 —— 有些库会把请求头（含 x-api-key）印进错误里
        raise AIBackendError(f"请求失败: {type(e).__name__}") from None

    parts = []
    for block in data.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    out = "".join(parts).strip()

    if not out:
        raise AIBackendError("模型返回空内容")
    if _looks_wrapped(out):
        raise AIBackendError("模型输出带解释或包裹，拒绝采纳")

    return out

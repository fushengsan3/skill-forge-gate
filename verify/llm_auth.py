#!/usr/bin/env python3
"""
「要调一次模型」的那几层（L4 深度分析、L5 沙箱）共用的凭据判定、端点与响应取值。

## 为什么必须有这么一个模块

L4 和 L5 原先各写一份，两份都只认 `ANTHROPIC_API_KEY`。而 Claude Code
默认配的是 `ANTHROPIC_AUTH_TOKEN`（走第三方中转）—— 于是配了中转的机器上，
两处功能都在、都一次不触发。（2026-10-05 在真机上撞到。）

两条路各写一套判定，迟早分叉，而分叉的那一边就是没人看着的那一边。
所以判定只留这一份。

## 凭据：凭据管理器**优先**，环境变量兜底

2026-10-05 实测：`ANTHROPIC_AUTH_TOKEN` 这类变量**只存在于 Claude Code 的
shell 进程里** —— `[Environment]::GetEnvironmentVariables('User')` 和 `('Machine')`
两级都查不到任何一个 `ANTHROPIC*`。也就是说它们是**按进程注入**的。

而面板一键安装跑在 bridge 这个**常驻进程**里：它由任务计划 / HKCU\\Run 在登录时
拉起，继承的是登录环境，**拿不到**那些注入的变量。后果是 `daemon/precheck.py`
那条路上 `credentials()` 永远返回空 → **L5 从来没跑过**，而且看起来一切正常
（"前置缺失，跳过"，不是失败）。这是本次审计里最隐蔽的一条。

Windows 凭据管理器（`daemon/credentials.py`，DPAPI 按当前用户加密）是**跨进程**的，
这才是无人值守路径唯一能用的来源。环境变量保留为兜底 —— Claude Code 交互路径
和开发调试都靠它。

**为什么凭据管理器排在前面而不是后面**：面板路径只有它；交互路径上两者都在时，
必须有一个**确定**的赢家，不能靠"碰巧读到哪个"。而且决策记录 §3.3 已经定过
"密钥进凭据管理器"，环境变量是历史通道，不是设计通道。

## 端点与模型：`cc_models` 为主，环境变量覆盖

`daemon/cc_models.py` 读的是 `~/.claude/settings.json` —— **持久文件，跨进程可读**，
跟凭据管理器同一个道理。所以它同样是无人值守路径能用的来源。

这里和环境变量**反过来**（环境变量优先），是有意的：端点和模型不是秘密，
也不存在"哪条路只有哪个来源"的问题，此时"显式设置胜过隐式配置"才是对的直觉。

## 这个模块不做什么

它**不碰密钥存储** —— 读写凭据管理器那套全在 `daemon/credentials.py`，
这里只调用它的 `get_secret()`，并且**只拿走 token 本身**。
`daemon/` 是延迟导入的（见下），所以本模块在没有 `daemon/` 的环境里仍可单独工作，
只是少了凭据管理器这一路。
"""

import os

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_BASE_URL = "https://api.anthropic.com"

# L5 想要的是"够便宜、够快、会调工具"的档位。cc_models 里这个别名对应的
# 是快档（Haiku 那一列）。
CHEAP_MODEL_ALIAS = "haiku"


def _cc_models():
    """拿到 `daemon.cc_models`，拿不到返回 None。

    **延迟导入是有意的**：`verify/` 被设计成能单独跑（`python verify/l5_sandbox.py`），
    而 `daemon/` 不一定在旁边。拿不到就退化成"只有默认端点和默认模型"，
    不该因此整个模块 import 不进来。
    """
    try:
        from daemon import cc_models
        return cc_models
    except Exception:
        return None


def _credential_manager_lookup() -> tuple:
    """查凭据管理器，返回 `(token, 出了什么事)`。

    ⚠️ 第二个值是**故障描述，不是来源名**。正常（读到了 / 确实没配）时是空串。
    把来源名塞进来（第一版就这么写的）会让 `describe()["key_note"]` 在
    一切正常时也显示一串字，于是"有备注 = 有问题"这个约定就废了 ——
    而排查的人正是靠那个约定一眼看出有没有事。

    取不到就返回空串 token。**"没配"和"配了但读不出来"必须分开** ——
    两者在 `credentials()` 里都退化成空串、都让 L5 跳过，但排查方向完全相反：
    前者去设置页填一次，后者要去凭据管理器看那条记录是不是坏了。
    混成一句话，用户会在设置页反复保存，而问题一次都不动。
    """
    try:
        from daemon import credentials
    except Exception:
        return "", ""          # daemon/ 不在旁边（单独跑 verify/ 时）。不是异常。
    try:
        state = credentials.read_state(credentials.AI_TOKEN)
    except Exception as e:
        return "", f"凭据管理器查不动：{type(e).__name__}: {e}"
    if state == "ok":
        token = (credentials.get_secret(credentials.AI_TOKEN) or "").strip()
        return (token, "") if token else ("", "")
    if state == "absent":
        return "", ""
    if state == "unavailable":
        return "", "凭据管理器用不了（没装 pywin32 或不是 Windows）"
    return "", state             # "unreadable: …"


def _credential_manager_token() -> str:
    """从 Windows 凭据管理器取 AI 密钥。取不到返回空串。"""
    return _credential_manager_lookup()[0]


def credentials() -> tuple:
    """挑一个能用的凭据，返回 `(token, auth_style, 来源名)`。

      1. 凭据管理器 `SkillForge/ai-token` → `x-api-key`
      2. `ANTHROPIC_API_KEY`               → `x-api-key`（Anthropic 原生的那个，语义无歧义）
      3. `ANTHROPIC_AUTH_TOKEN`            → `bearer`（Claude Code 和第三方中转走这条）
      4. 都没有                             → `("", "", "")`

    第 4 种情况调用方按「前置缺失」处理 —— **跳过，不是拒绝，也不是通过**。

    第 1 条的认证风格刻意跟 `daemon/translate_ai.py` 一致（同一个口令槽、同一种发法）：
    同一个密钥被两个消费者用两种头发出去，迟早出事，而其中一个消费者还无人值守。
    """
    token = _credential_manager_token()
    if token:
        # 来源名在这里写，不在 _credential_manager_lookup 里 —— 那里回的是
        # "出了什么事"，正常时是空串（见那个函数的说明）。
        return token, "x-api-key", "凭据管理器 SkillForge/ai-token"

    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return key, "x-api-key", "ANTHROPIC_API_KEY"

    token = (os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if token:
        return token, "bearer", "ANTHROPIC_AUTH_TOKEN"

    return "", "", ""


def base_url() -> str:
    """API 端点：环境变量 → Claude Code 配置 → 内置默认。

    写死 `https://api.anthropic.com` 的后果：配了中转的机器会去调一个
    它根本没有密钥的端点，然后收到 401 —— 而 401 看起来像"网络问题"。
    """
    explicit = (os.environ.get("ANTHROPIC_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    cc = _cc_models()
    if cc is not None:
        try:
            configured = (cc.base_url() or "").strip()
        except Exception:
            configured = ""
        if configured:
            return configured.rstrip("/")

    return DEFAULT_BASE_URL


def model() -> str:
    """模型名：环境变量 → Claude Code 配置的**快档** → 内置默认。

    **刻意不继承 Claude Code 当前用的那个模型。** L5 每次安装都要跑，
    而"当前档"可能正好是最贵的那档 —— 无人值守地反复用贵档调模型，
    账单会在你注意到之前涨起来。这里固定取快档。
    """
    explicit = (os.environ.get("ANTHROPIC_MODEL") or "").strip()
    if explicit:
        return explicit

    cc = _cc_models()
    if cc is not None:
        try:
            resolved = cc.resolve(CHEAP_MODEL_ALIAS) or ""
        except Exception:
            resolved = ""
        # `resolve()` 认不出来时会把输入**原样返回**（"haiku"）。那不是模型 ID，
        # 发出去就是 404。所以只有确实解析出了别的东西才用。
        if resolved and resolved != CHEAP_MODEL_ALIAS:
            return resolved

    return DEFAULT_MODEL


def describe() -> dict:
    """给人和给排查用：这次会发给谁、用什么模型、凭据从哪来。**绝不回 token 本身。**

    存在的理由：`credentials()` 返回空时，"为什么是空"有**四种**完全不同的原因
    （凭据管理器读不出来 / 没装 pywin32 / 没配 / 环境变量没有）。只回一个空串，
    排查的人只能猜 —— 而猜错方向就会去改一个本来没坏的东西。
    """
    token, auth_style, source = credentials()
    _, note = _credential_manager_lookup()
    return {
        "key_source": source,
        "auth_style": auth_style,
        "has_token": bool(token),
        # 凭据管理器那条路**出了什么事**。空 = 正常（要么读到了，要么确实没配）。
        # ⚠️ 这里放的是故障描述，不是来源名 —— 来源名在 key_source 里。
        # 两者混淆过一次：一切正常时 key_note 却显示着来源名，
        # 于是"有备注 = 有问题"这个一眼判断就失效了。
        "key_note": note,
        "base_url": base_url(),
        "model": model(),
    }


def auth_headers(token: str, auth_style: str) -> dict:
    """装好认证头。

    两种风格**互斥**，不能都发：原生端点对多余的 `authorization` 会直接报错。

    容器里（`sandbox/runner.py`）还有一次"401 之后换风格重试"的兜底 ——
    因为走错风格和密钥不对的表现一模一样，而面板那条路上没人能来纠正它。
    """
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if auth_style == "bearer":
        headers["authorization"] = "Bearer " + token
    else:
        headers["x-api-key"] = token
    return headers


def first_text(response: dict, default: str = "") -> str:
    """取响应里**第一个 text 块**的正文。

    别写 `response["content"][0]["text"]`。开了思考的模型（DeepSeek 等中转默认开）
    会把 `thinking` 块放在最前面，它没有 `text` 字段 —— 那样取出来永远是
    空字符串或"分析失败"，而且看上去像是模型没答，不像代码有 bug。
    """
    for block in response.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            return block.get("text", default)
    return default


if __name__ == "__main__":
    # 手工排查用：一条命令回答"L5 到底会不会跑"。
    # **只回来源名和布尔，不回 token。**
    import json
    print(json.dumps(describe(), ensure_ascii=False, indent=2))

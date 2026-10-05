#!/usr/bin/env python3
"""
读取 Claude Code 本地配置里的模型清单（R6）。

## ⚠ 这个文件必须用白名单，不能用黑名单

`~/.claude/settings.json` 的 `env` 里**同时装着模型名和 API 密钥**
（`ANTHROPIC_AUTH_TOKEN` 就在旁边）。一旦用"排除掉含 TOKEN 的键"这种黑名单写法，
将来有人往 env 里加一个叫 `ANTHROPIC_PROXY_KEY` 或 `ANTHROPIC_SESSION` 的变量，
它就会被顺手发给浏览器页面。

所以这里**只认精确匹配白名单的键**，其余一律不读、不返回。
读取结果会经由 bridge 发到面板，必须假定它会落到一个渲染不受信数据的地方。
"""
import json
import os
import re
from pathlib import Path

# 精确白名单：只有这些键会被读出。
#   ANTHROPIC_MODEL                       当前使用的模型（"opus" 这类别名）
#   ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL        别名 → 真实模型 ID
#   ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL_NAME   真实模型 ID（可读名）
ALLOWED_KEYS = re.compile(
    r"^ANTHROPIC_(MODEL|DEFAULT_(OPUS|SONNET|HAIKU)_MODEL(_NAME)?)$"
)

SETTINGS = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"

# 面板上给人看的顺序和称呼
ALIASES = [
    ("OPUS", "Opus", "高能力档"),
    ("SONNET", "Sonnet", "均衡档"),
    ("HAIKU", "Haiku", "快速档"),
]


def _read_env() -> dict:
    if not SETTINGS.exists():
        return {}
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    # 白名单过滤 —— 不匹配的键连读都不读出来
    return {k: v for k, v in env.items()
            if isinstance(k, str) and ALLOWED_KEYS.match(k) and isinstance(v, str)}


def base_url() -> str:
    """API 端点。**仅本地使用，绝不外发**。

    它本身不是秘密（就是个 URL），但没必要让浏览器知道你的请求打到哪，
    所以它不在 list_models() 的返回值里 —— 只有 bridge 进程内部调模型时才读它。
    """
    if not SETTINGS.exists():
        return ""
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return ""
    env = data.get("env") or {}
    return env.get("ANTHROPIC_BASE_URL", "") if isinstance(env, dict) else ""


def list_models() -> dict:
    """返回面板可用的模型清单。

    形状: {"ok": bool, "current": "模型ID", "models": [{"id","label","tier"}], "source": "路径"}
    """
    env = _read_env()
    if not env:
        return {"ok": False, "current": "", "models": [],
                "source": str(SETTINGS),
                "error": "读不到 Claude Code 配置（文件不存在或没有模型项）"}

    models = []
    seen = set()

    def add(model_id, label, tier):
        if not model_id or model_id in seen:
            return
        seen.add(model_id)
        models.append({"id": model_id, "label": label, "tier": tier})

    # 先按档位列（Opus/Sonnet/Haiku），这样面板上的顺序稳定
    for alias, label, tier in ALIASES:
        add(env.get(f"ANTHROPIC_DEFAULT_{alias}_MODEL_NAME") or env.get(f"ANTHROPIC_DEFAULT_{alias}_MODEL"),
            f"{label}", tier)

    # 再补上当前实际使用的那个（可能是别名，也可能不在上面三项里）
    current_raw = env.get("ANTHROPIC_MODEL", "")
    current = ""
    if current_raw:
        # "opus" 这种别名要解析成真实 ID，否则调 API 会 404
        resolved = env.get(f"ANTHROPIC_DEFAULT_{current_raw.upper()}_MODEL_NAME") \
            or env.get(f"ANTHROPIC_DEFAULT_{current_raw.upper()}_MODEL") \
            or current_raw
        current = resolved
        add(resolved, "当前使用", "current")

    return {"ok": True, "current": current, "models": models, "source": str(SETTINGS)}


def resolve(model_id: str) -> str:
    """把面板传来的模型标识解析成真正能发给 API 的模型 ID。

    面板可能传来别名（"opus"），直接拿去调 API 会 404，所以在这里统一解析。
    白名单之外的值原样返回 —— 不认识的模型由调用方去撞错误，不要在这里猜。
    """
    env = _read_env()
    upper = (model_id or "").upper()
    if f"ANTHROPIC_DEFAULT_{upper}_MODEL_NAME" in env:
        return env[f"ANTHROPIC_DEFAULT_{upper}_MODEL_NAME"]
    if f"ANTHROPIC_DEFAULT_{upper}_MODEL" in env:
        return env[f"ANTHROPIC_DEFAULT_{upper}_MODEL"]
    return model_id or ""

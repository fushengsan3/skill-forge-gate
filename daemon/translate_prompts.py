#!/usr/bin/env python3
"""
内置翻译提示词的加载器（R6）。

每个提示词就是一份可直接当 `system` 用的正文，存成 markdown 文件放在
`templates/translate_prompts/`，由 `manifest.json` 索引。

## 为什么提示词要能选多个

短简介和整篇文档需要的约束不一样：简介要短、要狠（只出译文，别废话），
文档要保结构（标题层级、表格、frontmatter）。用一份通吃的结果是两头都不讨好。

## 关于第三方提示词

引入外部提示词前**必须确认许可证**。质量再高，没有许可证声明就是默认保留所有权利，
不能随程序分发 —— 见 manifest 里的 `_notes`。
"""
import json
from pathlib import Path

SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"
PROMPT_DIR = SKILL_ROOT / "templates" / "translate_prompts"
MANIFEST = PROMPT_DIR / "manifest.json"

FALLBACK_ID = "skill-forge-tech"


def _manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def list_prompts() -> dict:
    """给面板用的清单。

    **不含正文** —— 正文只在服务端组装请求时用，没必要发给浏览器。
    面板需要的只是"有哪些可选、各是什么许可"。
    """
    m = _manifest()
    entries = []
    for p in m.get("prompts", []):
        if not isinstance(p, dict) or not p.get("id"):
            continue
        # 只暴露面板真正要显示的字段，多余的（如内部路径）不外发
        entries.append({
            "id": p["id"],
            "name": p.get("name", p["id"]),
            "description": p.get("description", ""),
            "license": p.get("license", ""),
            "source": p.get("source", ""),
            "available": (PROMPT_DIR / p.get("file", "")).exists() if p.get("file") else False,
        })
    return {
        "ok": bool(entries),
        "default": m.get("default", FALLBACK_ID),
        "prompts": entries,
    }


def get_prompt(prompt_id: str) -> str:
    """取某个提示词的正文。取不到时回退到内置默认，绝不返回空字符串 ——
    空 system prompt 会让模型自由发挥，产出带解释的译文，污染翻译缓存。"""
    m = _manifest()
    for p in m.get("prompts", []):
        if isinstance(p, dict) and p.get("id") == prompt_id:
            path = PROMPT_DIR / p.get("file", "")
            if path.exists():
                try:
                    return path.read_text(encoding="utf-8").strip()
                except Exception:
                    break
            break

    if prompt_id != FALLBACK_ID:
        return get_prompt(FALLBACK_ID)
    # 连内置的都读不到 —— 给一段最小可用的兜底，保证输出格式仍然可控
    return (
        "把用户给出的英文技术文本翻译成简体中文。"
        "只输出译文本身，不要任何解释、说明、引导语或双语对照。"
        "代码、命令、变量名、文件路径、URL 与版本号保持原样。"
    )


def default_id() -> str:
    return _manifest().get("default", FALLBACK_ID)

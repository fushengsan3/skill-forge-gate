#!/usr/bin/env python3
"""
翻译后端的配置（R6）。

## 为什么不能只存 localStorage

改这个设置的是**面板**，但执行翻译的是 **daemon**（每周扫描时批量翻 description）。
daemon 是独立进程，读不到浏览器的 localStorage —— 如果配置只存浏览器，
用户在面板上选了 AI 后端，实际扫描时还是走 Google，而且不会有任何报错。
这种"设置看起来生效了但其实没有"的失败最难查。

所以配置的真源是磁盘文件：
    面板 --HTTP--> bridge --写--> templates/translate-config.json <--读-- daemon

面板读设置也走 bridge，不自己缓存一份 —— 两个真源迟早会不一致。
"""
import json
from pathlib import Path

SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"
CONFIG_FILE = SKILL_ROOT / "templates" / "translate-config.json"

BACKENDS = ("google", "ai")

DEFAULTS = {
    "backend": "google",     # 默认 Google，免费且不依赖任何密钥
    "model": "",             # AI 后端用的模型 ID（来自 Claude Code 配置）
    "prompt": "",            # 用哪个内置提示词；空 = 用 manifest 里的默认
}


def load() -> dict:
    """读配置。文件不存在或损坏时返回默认值 —— 绝不能因此让扫描挂掉。"""
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception:
            pass

    # 校验：非法值一律退回默认。
    # 这一步很重要 —— 配置文件是可以被手改的，一个拼错的后端名
    # 不该让整个翻译环节静默失效。
    if cfg.get("backend") not in BACKENDS:
        cfg["backend"] = DEFAULTS["backend"]
    for key in ("model", "prompt"):
        if not isinstance(cfg.get(key), str):
            cfg[key] = DEFAULTS[key]
    return cfg


def save(updates: dict) -> dict:
    """合并写入。只接受已知字段，防止面板顺手塞进别的东西。"""
    cfg = load()
    for key in ("backend", "model", "prompt"):
        if key in updates and isinstance(updates[key], str):
            cfg[key] = updates[key]
    if cfg["backend"] not in BACKENDS:
        cfg["backend"] = DEFAULTS["backend"]

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg

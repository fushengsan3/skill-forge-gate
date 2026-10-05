#!/usr/bin/env python3
"""
已安装 skill 的快照。

同一个名单有两个投递渠道：

- bridge 在线时，走 HTTP `GET /installed`（面板实时拉取）
- bridge 不在线时，由 daemon 在生成面板时直接嵌进 HTML（R4-a）

两个渠道必须产出**完全相同的形状** —— 否则面板要维护两套渲染逻辑，
早晚会漂移成"在线时能看、离线时看不了"这种最难查的不一致。
所以转换逻辑只写在这里一份。
"""
import json
from pathlib import Path

SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"


def installed_snapshot(skill_root=None) -> list:
    """读取 sources.json，返回面板直接可用的列表（按安装时间倒序）。

    参数 skill_root 用于测试注入；默认取真实安装目录。
    """
    root = Path(skill_root) if skill_root else SKILL_ROOT
    sources_file = root / "sources.json"

    data = {}
    if sources_file.exists():
        try:
            data = json.loads(sources_file.read_text(encoding="utf-8"))
        except Exception:
            # sources.json 损坏时返回空名单，不要把面板整个拖垮
            data = {}

    skills = []
    for name, info in data.items():
        if not isinstance(info, dict):
            continue
        skills.append({
            "name": name,
            "type": info.get("type", "unknown"),
            "url": info.get("url", ""),
            "installed_sha": str(info.get("installed_sha", ""))[:8],
            "installed_at": info.get("installed_at", ""),
            "is_self": info.get("self", False),
        })

    skills.sort(key=lambda s: s.get("installed_at", ""), reverse=True)
    return skills

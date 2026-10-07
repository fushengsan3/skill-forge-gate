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
from datetime import datetime
from pathlib import Path

SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"
LOG_FILE = SKILL_ROOT / "daemon" / "watchdog.log"


def log(msg: str):
    """记一行到与 installer / watchdog 同一个日志文件。"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [installed] {msg}\n")
    except OSError:
        pass


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
        except Exception as e:
            # 文件**存在**却读不出来 —— 这与"文件不存在"（确实什么都没装）完全不同。
            # 以前两者都安静地变成 `{}`，面板显示"已安装：0"，用户看到的是
            # "我的 skill 全没了"，而不是"名单读不出来"。
            # 空名单不改（面板不该被一个坏文件拖垮），但必须留痕。
            log(f"sources.json 存在但解析失败（{type(e).__name__}）—— "
                "本次返回空名单，但这不是「没有已安装的 skill」")
            data = {}

    skills = []
    for name, info in data.items():
        if not isinstance(info, dict):
            continue
        # `trust_level` 空字符串 = **未记录**（出现这个字段之前装的条目）。
        # 面板必须把它显示成「未记录」而不是默认已验证 —— fail-closed，
        # 与 `installer._trust_level()` 对"没有结论"的处理一致。
        raw_trust = info.get("trust_level", "")
        skills.append({
            "name": name,
            "type": info.get("type", "unknown"),
            "url": info.get("url", ""),
            "installed_sha": str(info.get("installed_sha", ""))[:8],
            "installed_at": info.get("installed_at", ""),
            "is_self": info.get("self", False),
            "trust_level": raw_trust if raw_trust in ("verified", "partial", "unverified") else "",
            # D-1：为什么是这个等级（哪一层、什么结论）。面板拿它做徽章的提示 ——
            # 只有等级没有理由的话，一枚「部分验证」读了等于没读。
            "trust_note": info.get("trust_note", "") or "",
            # D3：monorepo 里装的子 skill 会带上它在仓库里的位置；根目录装的为空。
            # 以前这个字段写死空串且无人读，所以历史条目这里也是空 —— 面板按"没有就不显示"处理。
            "subpath": info.get("subpath", "") or "",
        })

    skills.sort(key=lambda s: s.get("installed_at", ""), reverse=True)
    return skills

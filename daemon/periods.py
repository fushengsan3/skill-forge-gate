#!/usr/bin/env python3
"""
把历史各期扫描结果打包给面板（R2 分组 + R3 冻结展示用）。

## 为什么需要它

面板要能**冻结在旧的一期**（R3：今天不想看新的，明天打开还是同一期），
所以每一期的数据都得随页面一起发出去 —— 面板不能反过来找 daemon 要。

直接把 8 个 `weekly-*.json` 全塞进 HTML 是最省事的做法，但实测：
8 期共 424 条记录，其中只有 **61 个不重复的 skill**，平均每个在 6.4 期里重复出现。
全量嵌入 = 413 KB，而且**每周线性增长**（一年后 ~3 MB）。

所以拆成两层：

- **catalog**：每个 skill 的重字段（描述、URL…）只存**一份**
- **periods**：每期只存**轻量易变**的字段（stars / score / status / updated_at…）

同样的信息量，体积降到约 1/5，且增长曲线平缓得多。
"""
import json
from pathlib import Path

# 只存一份的字段：**重**（描述）或**稳定**（URL、建仓时间）。
# 它们放 catalog；其余（stars / score / status / 时间戳…）留在每期，因为它们会变。
SHARED_FIELDS = ("name", "full_name", "description", "description_zh",
                 "url", "source", "created_at")

# 只嵌最近这么多期。
# 冻结功能实际不会往回翻十几期，而每期都会给 HTML 增重，
# 不设上限的话页面会随周数无限膨胀。被截掉的期数由 total_periods 如实报出，
# 面板据此显示"更早的期未收录"，不让用户以为是数据丢了。
MAX_PERIODS = 12


def load_periods(discover_dir) -> dict:
    """读取 discover/ 下所有 weekly-*.json。

    返回 {"catalog": {...}, "periods": [...], "first_seen": {...}, "total_periods": int}

    periods 按期号升序（最早的在前），first_seen 是 name → 最早出现的那期日期。

    注意：`first_seen` 与 `total_periods` 都基于**全部**存档计算，
    即使 periods 因 MAX_PERIODS 被截断 —— 否则"首次发现于哪期"会被截断点污染，
    变成一个随配置改变而变化的假事实。
    """
    discover_dir = Path(discover_dir)
    catalog = {}
    first_seen = {}
    periods = []

    for path in sorted(discover_dir.glob("weekly-*.json")):
        date = path.stem[len("weekly-"):]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # 某一期损坏不该让整个面板生成不出来 —— 跳过它，其余照常
            continue

        items = {}
        for cat, skills in (data.get("categories") or {}).items():
            for s in skills:
                name = s.get("name")
                if not name:
                    continue

                # 稳定/重的字段进目录（后写的覆盖先写的：保留最新一次的说法）
                shared = {k: s[k] for k in SHARED_FIELDS if k in s}
                if shared:
                    catalog.setdefault(name, {}).update(shared)

                # 易变字段留在本期
                light = {k: v for k, v in s.items() if k not in SHARED_FIELDS}
                light["category"] = cat        # 分类只在 categories 的键上，补回记录里
                items[name] = light

                # first_seen 只在第一次遇到时写入 —— 依赖 periods 升序
                first_seen.setdefault(name, date)

        periods.append({
            "date": date,
            # 注意：老存档的 generated 可能是 None（早期字段还没加），面板要能容忍
            "generated": data.get("generated"),
            "total": data.get("total", len(items)),
            "new_count": data.get("new_count", 0),
            "update_count": data.get("update_count", 0),
            "items": items,
        })

    total_periods = len(periods)
    if total_periods > MAX_PERIODS:
        periods = periods[-MAX_PERIODS:]

    return {
        "catalog": catalog,
        "periods": periods,
        "first_seen": first_seen,
        "total_periods": total_periods,
    }

#!/usr/bin/env python3
"""
面板分期测试 —— R2（本期新增 / 更早发现分组）+ R3（冻结展示 + 手动推进）。

用**真实的生产载荷形状**（`_catalog` + `_periods` + `_first_seen`）注入模板，
再交给 Node + jsdom 跑。这一点很重要：其它面板测试注入的都是旧的
「只有 categories」载荷，走的是一条兼容分支，**覆盖不到 R2/R3 的新路径**。

依赖：Node.js + jsdom。找不到 jsdom 时跳过（退出码 2）。

用法：
    python tests/test_panel_period.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from daemon.safe_embed import json_for_script
from daemon.bridge_auth import KEY_PLACEHOLDER
from test_panel_xss import find_jsdom_base

TEST_BRIDGE_KEY = "test-key-not-real"

# 三期，逐期增长；first_seen 决定分组归属
P1, P2, P3 = "2026-09-01", "2026-09-08", "2026-09-15"

CATALOG = {
    n: {"name": n, "full_name": "a/" + n, "description": n + " 的描述",
        "url": "https://github.com/a/" + n, "source": "github-topic",
        "created_at": "2025-03-04T00:00:00Z"}   # R1：建仓时间 = 发布时间
    for n in ("a-skill", "b-skill", "c-skill", "d-skill")
}

FIRST_SEEN = {
    "a-skill": P1,
    "b-skill": P1,
    "c-skill": P2,   # 第二期才出现
    "d-skill": P3,   # 第三期才出现 → 最新一期的"本期新增"
}


def item(cat="工作流", stars=10, pushed=None, updated="2026-09-15T08:00:00"):
    """pushed=None 用来模拟拿不到 pushed_at 的源（如 claudskills）。"""
    rec = {"category": cat, "stars": stars, "score": float(stars),
           "status": "new", "updated_at": updated}
    if pushed:
        rec["pushed_at"] = pushed
    return rec


PERIODS = [
    {"date": P1, "generated": P1 + "T00:00:00", "total": 2, "new_count": 2, "update_count": 0,
     "items": {"a-skill": item(), "b-skill": item()}},
    {"date": P2, "generated": P2 + "T00:00:00", "total": 3, "new_count": 1, "update_count": 0,
     "items": {"a-skill": item(), "b-skill": item(), "c-skill": item(stars=20)}},
    {"date": P3, "generated": P3 + "T00:00:00", "total": 4, "new_count": 1, "update_count": 0,
     "items": {"a-skill": item(),
               "b-skill": item(),
               # c-skill 只有 updated_at、没有 pushed_at —— 用来验证不会冒充成"最近推送"
               "c-skill": item(stars=20, updated="2026-09-14T00:00:00"),
               "d-skill": item(stars=30, pushed="2026-09-12T00:00:00")}},
]

PAYLOAD = {
    "_catalog": CATALOG,
    "_periods": PERIODS,
    "_first_seen": FIRST_SEEN,
    # 故意报一个比实际多的总数：用来验证"更早的 N 期未收录"提示
    "_periods_total": 9,
    "_installed": [],
    # 顶层保留旧字段，确认面板不会因为它的存在而走错分支
    "categories": {},
    "generated": P3 + "T00:00:00",
    "total": 4, "new_count": 1, "update_count": 0,
}


def main():
    base = find_jsdom_base()
    if base is None:
        print("SKIP: 未找到 jsdom，跳过分期测试。")
        print("      安装：cd skill-forge && npm install jsdom")
        return 2

    template_path = ROOT / "panel" / "fallback.html"
    harness_src = HERE / "panel_period_harness.mjs"
    if not template_path.exists() or not harness_src.exists():
        print(f"FAIL: 缺少 {template_path} 或 {harness_src}")
        return 1

    template = template_path.read_text(encoding="utf-8")
    injected = template.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(PAYLOAD))
    injected = injected.replace(KEY_PLACEHOLDER, json_for_script(TEST_BRIDGE_KEY))

    harness_dst = base / "panel_period_harness.mjs"
    page = HERE / "_panel_period.html"
    try:
        page.write_text(injected, encoding="utf-8")
        shutil.copyfile(harness_src, harness_dst)

        print("=" * 60)
        print("面板分期测试（R2 分组 + R3 冻结）")
        print("=" * 60)
        print(f"模板:   {template_path}")
        print(f"期数:   {len(PERIODS)}（声明总数 {PAYLOAD['_periods_total']}）")
        print("")

        proc = subprocess.run(
            ["node", str(harness_dst), str(page)],
            cwd=str(base), capture_output=True, text=True, errors="replace",
        )
        sys.stdout.write(proc.stdout)
        if proc.stderr.strip():
            print("--- node stderr ---")
            print(proc.stderr.strip())
        print("")
        print("RESULT: " + ("PASS" if proc.returncode == 0 else "FAIL"))
        return proc.returncode
    finally:
        for p in (harness_dst, page):
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

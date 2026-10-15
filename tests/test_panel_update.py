#!/usr/bin/env python3
"""卡片「安装 / 重装 / 更新」四态 + 功能板块高星榜的面板测试。

## 这个文件钉的是什么

用户的原话是："标注已安装 skill 的功能并没有直接在选项上体现安装更新，
只有标注功能"。查下来是 `skillCard` 里的一处**分支顺序**问题：

    isInstalled 写在 isUpdate **前面**，而"有更新"按定义必然是已安装的 ——
    于是"🔄 更新"那句文案基本不可达，卡片渲染出来是「✅ 已安装」，
    而那个灰样式还不 disable 按钮，点了只是把同一个 skill 再装一遍。

所以这里要钉住：

  1. 四种状态的**按钮文案各自正确**，尤其"有更新"不能显示成"已安装"
  2. 重装/更新按钮**可点**（只有"已加入队列"才是禁用态）
  3. 点更新**真的发 POST /update**，且请求体**只有 name、没有 url**
     —— URL 必须由 bridge 从 sources.json 取；前端能传 URL 就等于
     给常驻 bridge 开了一个"装任意仓库"的口子
  4. 更新成功后那条的"有更新"状态被就地清掉（否则徽章会一直骗人）
  5. 三个功能板块渲染出标题/计数/可安装的卡片

依赖：Node.js + jsdom。找不到 jsdom 时跳过（退出码 2）。

用法：
    python tests/test_panel_update.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))   # pytest 收集时 tests/ 不在 sys.path 上

from daemon.safe_embed import json_for_script
from daemon.bridge_auth import KEY_PLACEHOLDER
from test_panel_xss import find_jsdom_base

TEST_BRIDGE_KEY = "test-key-not-real"

PAYLOAD = {
    "generated": "2026-03-01T00:00:00",
    "total": 3, "new_count": 3, "update_count": 1,
    "categories": {"工作流": [
        {"name": "plain-new", "description": "没装过", "stars": 5,
         "url": "https://github.com/a/plain-new", "source": "github-topic",
         "status": "new", "score": 5.0},
        {"name": "plain-installed", "description": "装了，无更新", "stars": 6,
         "url": "https://github.com/a/plain-installed", "source": "github-topic",
         "status": "new", "score": 6.0},
        {"name": "has-update", "description": "装了，有更新", "stars": 7,
         "url": "https://github.com/a/has-update", "source": "github-topic",
         "status": "update_available", "score": 7.0,
         "installed_sha": "bbbb2222", "latest_sha": "cccc3333"},
    ]},
    "_installed": [
        {"name": "plain-installed", "type": "skill",
         "url": "https://github.com/a/plain-installed",
         "installed_sha": "aaaa1111", "installed_at": "2026-01-01T00:00:00", "is_self": False},
        {"name": "has-update", "type": "skill",
         "url": "https://github.com/a/has-update",
         "installed_sha": "bbbb2222", "installed_at": "2026-02-01T00:00:00", "is_self": False},
    ],
    # 功能板块：count=4 而 items 只有 1 条 —— 这正是要验的
    # "全量"与"列前 N 条"是两个数，不能被合成一个
    "_sections": {
        "学术相关": {
            "icon": "🎓", "count": 4,
            "items": [
                {"name": "paper-tool", "description": "arxiv papers search", "stars": 900,
                 "url": "https://github.com/b/paper-tool", "source": "section",
                 "source_label": "学术相关"},
            ],
        },
    },
}


def main():
    base = find_jsdom_base()
    if base is None:
        print("SKIP: 未找到 jsdom，跳过卡片四态 + 板块测试。")
        print("      安装：cd skill-forge && npm install jsdom")
        return 2

    template_path = ROOT / "panel" / "fallback.html"
    harness_src = HERE / "panel_update_harness.mjs"
    if not template_path.exists() or not harness_src.exists():
        print(f"FAIL: 缺少 {template_path} 或 {harness_src}")
        return 1

    template = template_path.read_text(encoding="utf-8")
    injected = template.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(PAYLOAD))
    injected = injected.replace(KEY_PLACEHOLDER, json_for_script(TEST_BRIDGE_KEY))

    harness_dst = base / "panel_update_harness.mjs"
    page = HERE / "_panel_update.html"
    try:
        page.write_text(injected, encoding="utf-8")
        shutil.copyfile(harness_src, harness_dst)

        print("=" * 60)
        print("卡片四态 + 功能板块测试")
        print("=" * 60)
        print(f"模板:   {template_path}")
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

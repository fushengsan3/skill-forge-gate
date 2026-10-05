#!/usr/bin/env python3
"""
面板安装后重渲染测试（R4-c）。

对应决策记录 §6 最后一条验收标准：

    安装一个 skill 后打开面板：**立刻显示为「已安装」**

做法：在 jsdom 里跑面板，用假的 bridge 让 `POST /install` 成功、
并让随后的 `GET /installed` 多返回一个 skill，断言面板自己跟上了。

依赖：Node.js + jsdom。找不到 jsdom 时跳过（退出码 2）。

用法：
    python tests/test_panel_install.py
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
    "total": 2, "new_count": 2, "update_count": 0,
    "categories": {"工作流": [
        {"name": "alpha-skill", "description": "已装", "stars": 5,
         "url": "https://github.com/a/alpha", "source": "github-topic",
         "status": "new", "score": 5.0},
        {"name": "gamma-skill", "description": "待装", "stars": 6,
         "url": "https://github.com/a/gamma", "source": "github-topic",
         "status": "new", "score": 6.0},
    ]},
    # 嵌的是"安装前"的名单：正是要靠 R4-c 把页面刷新到"安装后"
    "_installed": [
        {"name": "alpha-skill", "type": "skill", "url": "https://github.com/a/alpha",
         "installed_sha": "aaaa1111", "installed_at": "2026-01-01T00:00:00", "is_self": False},
    ],
}


def main():
    base = find_jsdom_base()
    if base is None:
        print("SKIP: 未找到 jsdom，跳过面板安装后重渲染测试。")
        print("      安装：cd skill-forge && npm install jsdom")
        return 2

    template_path = ROOT / "panel" / "fallback.html"
    harness_src = HERE / "panel_install_harness.mjs"
    if not template_path.exists() or not harness_src.exists():
        print(f"FAIL: 缺少 {template_path} 或 {harness_src}")
        return 1

    template = template_path.read_text(encoding="utf-8")
    injected = template.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(PAYLOAD))
    injected = injected.replace(KEY_PLACEHOLDER, json_for_script(TEST_BRIDGE_KEY))

    harness_dst = base / "panel_install_harness.mjs"
    page = HERE / "_panel_install.html"
    try:
        page.write_text(injected, encoding="utf-8")
        shutil.copyfile(harness_src, harness_dst)

        print("=" * 60)
        print("面板安装后重渲染测试（R4-c）")
        print("=" * 60)
        print(f"模板:   {template_path}")
        print("")

        proc = subprocess.run(
            ["node", str(harness_dst), str(page)],
            cwd=str(base), capture_output=True, text=True,
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

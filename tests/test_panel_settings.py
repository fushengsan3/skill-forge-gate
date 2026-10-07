#!/usr/bin/env python3
"""
设置弹窗测试（R6 模型选择 + R9 密钥）。

最关键的一条是**安全性质**：面板从 bridge 收到的凭据信息里只有布尔值，
页面上不可能出现密钥本身。这条如果破了，等于把密钥交给一个
渲染不受信数据（GitHub skill 描述）的页面。

依赖：Node.js + jsdom。找不到 jsdom 时跳过（退出码 2）。

用法：
    python tests/test_panel_settings.py
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

PAYLOAD = {
    "_catalog": {"a-skill": {"name": "a-skill", "description": "d",
                             "url": "https://github.com/a/a", "source": "github-topic"}},
    "_periods": [{"date": "2026-10-04", "generated": "2026-10-04T00:00:00", "total": 1,
                  "new_count": 1, "update_count": 0,
                  "items": {"a-skill": {"category": "工作流", "stars": 5, "score": 5.0,
                                        "status": "new", "updated_at": "2026-10-01T00:00:00"}}}],
    "_first_seen": {"a-skill": "2026-10-04"},
    "_periods_total": 1,
    "_installed": [],
    "categories": {},
}


def main():
    base = find_jsdom_base()
    if base is None:
        print("SKIP: 未找到 jsdom，跳过设置弹窗测试。")
        return 2

    template_path = ROOT / "panel" / "fallback.html"
    harness_src = HERE / "panel_settings_harness.mjs"
    if not template_path.exists() or not harness_src.exists():
        print(f"FAIL: 缺少 {template_path} 或 {harness_src}")
        return 1

    template = template_path.read_text(encoding="utf-8")
    injected = template.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(PAYLOAD))
    injected = injected.replace(KEY_PLACEHOLDER, json_for_script(TEST_BRIDGE_KEY))

    harness_dst = base / "panel_settings_harness.mjs"
    page = HERE / "_panel_settings.html"
    try:
        page.write_text(injected, encoding="utf-8")
        shutil.copyfile(harness_src, harness_dst)

        print("=" * 60)
        print("设置弹窗测试（R6 模型选择 / R9 密钥）")
        print("=" * 60)
        print(f"模板:   {template_path}")
        print("")

        proc = subprocess.run(
            ["node", str(harness_dst), str(page), TEST_BRIDGE_KEY],
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

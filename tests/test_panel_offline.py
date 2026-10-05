#!/usr/bin/env python3
"""
面板离线可用性测试（R4-a / R4-d）。

对应决策记录 §6 第一条验收标准：

    关掉 bridge 进程，打开面板：**已安装列表仍能显示**（来自嵌入数据），
    不出现"Bridge 未运行"

做法：用生产注入路径把 `_installed` 嵌进模板（和 daemon 生成 `discover/latest.html`
时完全一致），再交给 Node + jsdom，其中 fetch 一律 reject（模拟 bridge 进程不在）。

依赖：Node.js + jsdom。找不到 jsdom 时跳过（退出码 2）。

用法：
    python tests/test_panel_offline.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
# 直接运行本文件时 sys.path[0] 就是 tests/，但 pytest 收集时不是，
# 所以显式加一次，两种跑法都能 import 到隔壁模块
sys.path.insert(0, str(HERE))

from daemon.safe_embed import json_for_script
from daemon.bridge_auth import KEY_PLACEHOLDER

# 复用 XSS 测试里的 jsdom 定位逻辑，两处保持一致
from test_panel_xss import find_jsdom_base

TEST_BRIDGE_KEY = "test-key-not-real"

# 嵌入的名单：两个已装 skill
EMBEDDED = [
    {"name": "alpha-skill", "type": "skill", "url": "https://github.com/a/alpha",
     "installed_sha": "aaaa1111", "installed_at": "2026-01-01T10:00:00", "is_self": False},
    {"name": "beta-skill", "type": "skill", "url": "https://github.com/a/beta",
     "installed_sha": "bbbb2222", "installed_at": "2026-01-02T10:00:00", "is_self": False},
]

PAYLOAD = {
    "generated": "2026-01-03T00:00:00",
    "total": 1,
    "new_count": 1,
    "update_count": 0,
    "categories": {
        "工作流": [{
            "name": "alpha-skill", "full_name": "a/alpha", "description": "已装的那个",
            "stars": 12, "installs": 3, "url": "https://github.com/a/alpha",
            "source": "github-topic", "status": "new", "score": 9.0,
        }]
    },
    "_installed": EMBEDDED,
}


def render(template: str, payload: dict) -> str:
    """按生产路径注入（与 watchdog.generate_panel_html 同一手法）。"""
    html = template.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(payload))
    return html.replace(KEY_PLACEHOLDER, json_for_script(TEST_BRIDGE_KEY))


def main():
    base = find_jsdom_base()
    if base is None:
        print("SKIP: 未找到 jsdom，跳过面板离线可用性测试。")
        print("      安装：cd skill-forge && npm install jsdom")
        return 2

    template_path = ROOT / "panel" / "fallback.html"
    harness_src = HERE / "panel_offline_harness.mjs"
    if not template_path.exists() or not harness_src.exists():
        print(f"FAIL: 缺少 {template_path} 或 {harness_src}")
        return 1

    template = template_path.read_text(encoding="utf-8")

    # 场景 1/2 用带 _installed 的页面；场景 3 用不带 _installed 的（证明会如实说明）
    bare_payload = dict(PAYLOAD)
    bare_payload.pop("_installed")

    with_embed = HERE / "_panel_offline_with.html"
    bare = HERE / "_panel_offline_bare.html"
    harness_dst = base / "panel_offline_harness.mjs"

    try:
        with_embed.write_text(render(template, PAYLOAD), encoding="utf-8")
        bare.write_text(render(template, bare_payload), encoding="utf-8")
        shutil.copyfile(harness_src, harness_dst)

        print("=" * 60)
        print("面板离线可用性测试（R4-a）")
        print("=" * 60)
        print(f"模板:   {template_path}")
        print(f"嵌入名单: {[s['name'] for s in EMBEDDED]}")
        print("")

        proc = subprocess.run(
            ["node", str(harness_dst), str(with_embed), str(bare)],
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
        for p in (harness_dst, with_embed, bare):
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

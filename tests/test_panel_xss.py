#!/usr/bin/env python3
"""
面板 XSS 回归测试（R8）。

思路：用**生产注入路径**（daemon.safe_embed.json_for_script）把带攻击载荷的数据
注进 panel/fallback.html，再交给 Node + jsdom 在真实 DOM 里跑一遍，断言脚本没被执行。

覆盖的验收标准（决策记录 §6）：
  - description 含 </script><img src=x onerror=...>  → 不执行   (R8-1)
  - name 含 <img src=x onerror=...>                   → 不执行   (R8-2)
  - url 为 javascript:...                             → 不可点击 (R8-3)
  - 已装列表 name 含 ');alert(1);//                   → 不执行   (R8-4)

依赖：Node.js + jsdom。找不到 jsdom 时跳过（退出码 2），不阻塞其它测试。

准备 jsdom（任选其一）：
    npm install jsdom                     # 在 skill-forge/ 或 skill-forge/tests/ 下
    SF_JSDOM_DIR=/path/to/dir npm install jsdom

用法：
    python tests/test_panel_xss.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# 与生产走同一条注入路径 —— 这正是本测试的意义所在
from daemon.safe_embed import json_for_script
from daemon.bridge_auth import KEY_PLACEHOLDER

# 固定假密钥：测试不该创建或读取用户真实的 ~/.claude/.../.bridge-key
TEST_BRIDGE_KEY = "test-key-not-real"

HOSTILE_NAME = "<img src=x onerror=\"window.__XSS__='name'\">"
HOSTILE_DESC = "</script><img src=x onerror=\"window.__XSS__='desc'\">"
HOSTILE_URL = "javascript:window.__XSS__='url'"


def _skill(name, desc, url, stars=10):
    return {
        "name": name,
        "full_name": "attacker/evil-repo",
        "description": desc,
        "stars": stars,
        "installs": 5,
        "url": url,
        "source": "github-topic",
        "status": "new",
        "score": 42.0,
    }


ATTACK_DATA = {
    "generated": "2026-01-01T00:00:00",
    "total": 4,
    "new_count": 4,
    "update_count": 0,
    "categories": {
        "攻击载荷": [
            _skill(HOSTILE_NAME, "正常描述", "https://github.com/a/b", stars=11),
            _skill("desc-attack", HOSTILE_DESC, "https://github.com/a/c", stars=12),
            _skill("url-attack", "正常描述", HOSTILE_URL, stars=13),
            _skill("plain", "正常描述", "https://github.com/a/d", stars=14),
        ]
    },
}


def find_jsdom_base():
    """返回一个"其下有 node_modules/jsdom"的目录，找不到返回 None。"""
    candidates = []
    env = os.environ.get("SF_JSDOM_DIR")
    if env:
        candidates.append(Path(env))
    candidates += [HERE, ROOT, Path(tempfile.gettempdir()) / "sf-r8"]
    for base in candidates:
        if (base / "node_modules" / "jsdom").is_dir():
            return base
    return None


def main():
    base = find_jsdom_base()
    if base is None:
        print("SKIP: 未找到 jsdom，跳过面板 XSS 回归测试。")
        print("      安装：cd skill-forge && npm install jsdom")
        return 2

    template_path = ROOT / "panel" / "fallback.html"
    if not template_path.exists():
        print(f"FAIL: 找不到模板 {template_path}")
        return 1

    template = template_path.read_text(encoding="utf-8")
    injected = template.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(ATTACK_DATA))
    injected = injected.replace(KEY_PLACEHOLDER, json_for_script(TEST_BRIDGE_KEY))

    harness_src = HERE / "panel_xss_harness.mjs"
    if not harness_src.exists():
        print(f"FAIL: 找不到执行端 {harness_src}")
        return 1

    # 把执行端复制到 jsdom 所在目录，好让 ESM 能解析到 jsdom
    harness_dst = base / "panel_xss_harness.mjs"
    attack_html = HERE / "_panel_xss_attack.html"
    try:
        shutil.copyfile(harness_src, harness_dst)
        attack_html.write_text(injected, encoding="utf-8")

        print("=" * 60)
        print("面板 XSS 回归测试（R8）")
        print("=" * 60)
        print(f"模板:   {template_path}")
        print(f"执行端: {harness_dst}")
        print("")

        proc = subprocess.run(
            ["node", str(harness_dst), str(attack_html), TEST_BRIDGE_KEY],
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
        harness_dst.unlink(missing_ok=True)
        attack_html.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""面板多语言的实际生效测试（问题清单 #51）。

## 这条缺陷是什么

`applyLang()` 里有个分支专门处理输入框的 placeholder：

    } else if (el.tagName === 'INPUT' && el.hasAttribute('data-lang-placeholder')) {
      el.placeholder = t(el.getAttribute('data-lang-placeholder'));

但它的外层循环选的是 `[data-lang]`，而搜索框**只带** `data-lang-placeholder`
—— 没有 `data-lang`。所以那个分支**永远不可达**，placeholder 一次都没被设过，
搜索框里一片空白。

它一直没被发现，是因为症状是"功能不存在"而不是"翻译不对"：
一个从来没有过 placeholder 的输入框，看起来就像本来就没打算有。

## 为什么这个文件必须跑真 DOM

静态看一眼 `applyLang`，会觉得它明明处理了 INPUT。**grep 只会给你假通过** ——
这个项目已经因为"一条从来没匹配上的 grep"吃过一次亏
（见 tests/test_panel_sim.py 里那条写了很久的 `s.source === 'jeremylongshore'`）。
所以这里在 jsdom 里真的加载面板、真的调用它、真的读 DOM 属性。

依赖：Node.js + jsdom。找不到 jsdom 时跳过（退出码 2）。
用法：
    python tests/test_panel_lang.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from test_panel_xss import find_jsdom_base, json_for_script, KEY_PLACEHOLDER, TEST_BRIDGE_KEY


def render(template: str, payload: dict) -> str:
    """按生产路径注入（与 watchdog.generate_panel_html 同一手法）。"""
    html = template.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(payload))
    return html.replace(KEY_PLACEHOLDER, json_for_script(TEST_BRIDGE_KEY))


PAYLOAD = {
    "generated": "2026-01-03T00:00:00",
    "total": 0, "new_count": 0, "update_count": 0, "categories": {},
    "_installed": [],
}


def main():
    base = find_jsdom_base()
    if base is None:
        print("SKIP: 未找到 jsdom，跳过面板语言测试。")
        print("      安装：cd skill-forge && npm install jsdom")
        return 2

    template_path = ROOT / "panel" / "fallback.html"
    harness_src = HERE / "panel_lang_harness.mjs"
    if not template_path.exists() or not harness_src.exists():
        print(f"FAIL: 缺少 {template_path} 或 {harness_src}")
        return 1

    template = template_path.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        page = work / "panel.html"
        page.write_text(render(template, PAYLOAD), encoding="utf-8")

        # ⚠️ harness 必须放在 **base 里**，不能放临时目录。
        # `import ... from 'jsdom'` 是按**导入文件自己的位置**往上找 node_modules 的，
        # 放临时目录就找不到（ERR_MODULE_NOT_FOUND）。现有的几个 harness 都是这个写法。
        harness = base / "panel_lang_harness.mjs"
        shutil.copyfile(harness_src, harness)

        print("=" * 60)
        print("面板语言生效测试（#51 搜索框 placeholder）")
        print("=" * 60)
        try:
            proc = subprocess.run(["node", str(harness), str(page)],
                                  cwd=str(base), capture_output=True, text=True,
                                  errors="replace", timeout=120)
        finally:
            harness.unlink(missing_ok=True)

        sys.stdout.write(proc.stdout)
        if proc.stderr.strip():
            print("--- node stderr ---")
            print(proc.stderr.strip()[:800])
        print("")
        print("RESULT: " + ("PASS" if proc.returncode == 0 else "FAIL"))
        return proc.returncode


if __name__ == "__main__":
    sys.exit(main())

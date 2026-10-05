#!/usr/bin/env python3
"""
面板注入契约测试（R4-a 服务端一半）。

daemon 生成 `discover/latest.html` 时，必须把**已安装名单**一并嵌进去，
否则 bridge 一停面板就没名单可显示（用户报的 BUG）。

这里直接调 `daemon.watchdog.generate_panel_html`（生产函数，不是复制品），
断言它产出的 HTML 里：

  1. 嵌了 `_installed`，且内容与 sources.json 一致
  2. 嵌的形态和 bridge 的 `GET /installed` 完全一致（同一函数产出）
  3. `_installed` 不会渗进传给它的 `categorized`（调用方还要用这个 dict）
  4. 攻击性内容走的是 safe_embed，`</script>` 不会逃逸

用法：
    python tests/test_panel_embed.py
"""
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon.watchdog import generate_panel_html
from daemon import watchdog
from daemon.installed import installed_snapshot
from daemon.bridge_auth import KEY_PLACEHOLDER

errors = []
infos = []


def check(ok, label, detail=""):
    (infos if ok else errors).append(f"{label}" + (f" — {detail}" if detail else ""))


SOURCES = {
    "alpha-skill": {
        "type": "skill", "url": "https://github.com/a/alpha",
        "installed_sha": "aaaa111122223333", "installed_at": "2026-01-01T10:00:00",
    },
    "evil-skill": {
        "type": "skill", "url": "https://github.com/a/evil",
        "installed_sha": "deadbeefdeadbeef", "installed_at": "2026-01-02T10:00:00",
        # 目录名是攻击者可控的存储型注入点
        "self": False,
    },
}

CATEGORIZED = {
    "generated": "2026-01-03T00:00:00",
    "total": 1, "new_count": 1, "update_count": 0,
    "categories": {"工作流": [{
        "name": "x", "description": "正常描述", "stars": 1, "url": "https://github.com/a/x",
        "source": "github-topic", "status": "new", "score": 1.0,
    }]},
}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="sf-embed-"))
    try:
        # 搭一个假安装根目录：panel/fallback.html 用真的，sources.json 用假的
        (tmp / "panel").mkdir()
        shutil.copyfile(ROOT / "panel" / "fallback.html", tmp / "panel" / "fallback.html")
        (tmp / "sources.json").write_text(json.dumps(SOURCES), encoding="utf-8")

        real_root = watchdog.SKILL_ROOT
        watchdog.SKILL_ROOT = tmp
        try:
            out = tmp / "latest.html"
            data = dict(CATEGORIZED)
            before_keys = set(data.keys())
            generate_panel_html(data, out)
        finally:
            watchdog.SKILL_ROOT = real_root

        if not out.exists():
            print("FAIL: generate_panel_html 没有产出文件")
            return 1
        html = out.read_text(encoding="utf-8")

        # 1) _installed 确实嵌进去了，内容对得上
        expected = installed_snapshot(tmp)
        m = re.search(r"const DISCOVER_DATA = (\{.*?\});", html, re.S)
        embedded = json.loads(m.group(1)) if m else None
        check(bool(m), "从产出里取回 DISCOVER_DATA")
        check(embedded is not None and "_installed" in embedded, "已嵌 _installed")
        check(embedded and embedded.get("_installed") == expected,
              "嵌入名单内容与 sources.json 一致",
              f"{len(expected)} 项")

        # 2) 与 bridge 的 /installed 同形状（同函数产出，这里验证关键字段都在）
        fields = {"name", "type", "url", "installed_sha", "installed_at", "is_self"}
        got = set(embedded["_installed"][0].keys()) if embedded and embedded["_installed"] else set()
        check(got == fields, "嵌入名单字段与 bridge 一致", f"{sorted(got)}")
        check(all(len(s["installed_sha"]) == 8 for s in embedded["_installed"]),
              "installed_sha 已截成 8 位（与 bridge 相同）")

        # 3) 不污染调用方的 dict
        check(set(data.keys()) == before_keys,
              "categorized 未被写入 _installed（调用方还要用）",
              f"{sorted(set(data.keys()) - before_keys)}")

        # 4) 安全：注入必须走 safe_embed。往数据里塞 </script> 试逃逸
        hostile = dict(CATEGORIZED)
        hostile["categories"] = {"攻击": [{
            "name": "x", "description": "</script><img src=x onerror=alert(1)>",
            "stars": 1, "url": "https://github.com/a/x",
            "source": "github-topic", "status": "new", "score": 1.0,
        }]}
        out2 = tmp / "latest2.html"
        watchdog.SKILL_ROOT = tmp
        try:
            generate_panel_html(hostile, out2)
        finally:
            watchdog.SKILL_ROOT = real_root
        html2 = out2.read_text(encoding="utf-8")

        # 每个 <script> 恰好有一个 </script>。载荷若能逃逸，就会多出一个无主的闭合标签，
        # 两个计数不再相等 —— 这是最直接的"跳出脚本块"探测器。
        check(html2.count("</script>") == html2.count("<script"),
              "脚本块数量闭合（无逃逸的 </script>）",
              f"<script={html2.count('<script')} </script>={html2.count('</script>')}")

        check("\\u003c/script\\u003e" in html2,
              "攻击载荷 </script> 已被转义（R8-1 兜住）")
        check("__DATA_PLACEHOLDER__" not in html, "数据占位符已被替换")
        check(KEY_PLACEHOLDER not in html, "密钥占位符已被替换")

        # 5) 兼容性：只传 categorized 的旧调用方式（签名未变）仍然工作
        empty = tmp / "latest3.html"
        watchdog.SKILL_ROOT = tmp
        try:
            generate_panel_html({"categories": {}}, empty)
        finally:
            watchdog.SKILL_ROOT = real_root
        check(empty.exists() and empty.stat().st_size > 0,
              "旧调用方式（只有 categorized）仍然工作")

        print("=" * 60)
        print("面板注入契约测试（R4-a 服务端）")
        print("=" * 60)
        for i in infos:
            print("  [PASS] " + i)
        for e in errors:
            print("  [FAIL] " + e)
        print("")
        print(f"总计 {len(infos) + len(errors)} 项，失败 {len(errors)} 项")
        print("RESULT: " + ("PASS" if not errors else "FAIL"))
        return 0 if not errors else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

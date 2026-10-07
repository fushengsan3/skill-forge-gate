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
        # D1：安装当时 L1–L5 的结论，落盘在这里
        "trust_level": "verified",
        # D-1：为什么是这个等级。全 PASS 时是空串（见 installer._trust_note）
        "trust_note": "",
    },
    "legacy-skill": {
        "type": "skill", "url": "https://github.com/a/legacy",
        "installed_sha": "cafe000011112222", "installed_at": "2025-12-01T10:00:00",
        # 这个字段出现**之前**装的条目 —— 没有 trust_level。
        # 面板必须显示成「未记录」，绝不能默认成"已验证"。
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
        #
        # ⚠️ 这是**全等**断言，是故意的：嵌入路径和 bridge 路径必须产出完全相同的
        # 形状，否则面板要维护两套渲染逻辑，早晚漂移成"在线时能看、离线时看不了"。
        # 所以新增字段时**必须同步改这里** —— 2026-10-06 加 `trust_level`（D1）
        # 时就撞上了它，那是它在正常工作，不是它碍事。
        fields = {"name", "type", "url", "installed_sha", "installed_at", "is_self",
                  "trust_level", "subpath", "trust_note"}
        got = set(embedded["_installed"][0].keys()) if embedded and embedded["_installed"] else set()
        check(got == fields, "嵌入名单字段与 bridge 一致", f"{sorted(got)}")

        # ★ D1：trust_level 的**fail-closed 默认值**。
        # 这是这个字段最容易出错的地方：漏掉它，面板就会把"没记录"显示成"已验"，
        # 而那正是 D1 要根除的那种"没有依据的结论"。
        by_name = {s["name"]: s for s in (embedded.get("_installed") or [])}
        check(by_name.get("alpha-skill", {}).get("trust_level") == "verified",
              "有结论的条目照常带出来",
              repr(by_name.get("alpha-skill", {}).get("trust_level")))
        check(by_name.get("legacy-skill", {}).get("trust_level") == "",
              "★ 历史条目（字段出现之前装的）→ 空串，**不是**默认 verified",
              repr(by_name.get("legacy-skill", {}).get("trust_level")))
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

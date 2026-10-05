#!/usr/bin/env python3
"""
面板 CSP 测试（R8 第五层：外发信道闸门）。

前四层保证"不受信数据不会变成可执行代码"。这一层不保证那个 ——
面板自己要跑一大段内联脚本，`script-src` 必然要开 `'unsafe-inline'`。

它保的是"前四层万一被绕过之后，攻击者还能把东西送出去吗"：
没有外发信道，密钥/已装列表/扫描结果就回不到攻击者服务器。

这个测试做两类事：

  1. **策略本身**：默认拒绝、不许有外部来源、不许有 unsafe-eval、不许写会被忽略的指令
  2. **策略与页面同步**：面板当前没有外部资源，所以 `default-src 'none'` 不会打死它。
     将来谁加了 `<script src=...>`，这里会红 —— 提醒他要么改策略，要么别加。

纯 Python，不依赖 jsdom，任何环境都能跑。

用法：
    python tests/test_panel_csp.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon.safe_embed import json_for_script
from daemon.bridge_auth import KEY_PLACEHOLDER

TEMPLATE = ROOT / "panel" / "fallback.html"

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def parse_csp(html: str):
    """取出 meta 里的 CSP 原文，返回 (策略字符串 or None, 指令字典)。

    分两步：先切出 meta 标签，再单独取 content 的值。
    不能用一个正则同时匹配属性引号和值 —— 策略里有 `'none'` 这类单引号，
    `[^"']+` 会在第一个单引号处就截断，解析出来是空策略（看起来像"没配 CSP"）。
    """
    tag = re.search(r"<meta[^>]*Content-Security-Policy[^>]*>", html, re.I)
    if not tag:
        return None, {}
    tag = tag.group(0)
    m = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I) or \
        re.search(r"content\s*=\s*'([^']*)'", tag, re.I)
    if not m:
        return None, {}
    policy = m.group(1)
    directives = {}
    for part in policy.split(";"):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        directives[bits[0].lower()] = bits[1:]
    return policy, directives


def main():
    html = TEMPLATE.read_text(encoding="utf-8")
    policy, d = parse_csp(html)

    print("=" * 60)
    print("面板 CSP 测试（R8 第五层）")
    print("=" * 60)

    # ---- 1. 存在性与位置 ----
    check(policy is not None, "模板里有 CSP")
    if policy is None:
        return finish()

    head_start = html.lower().find("<head")
    csp_at = html.find("http-equiv")
    first_res = min([p for p in [
        html.find("<style"), html.find("<script"),
        html.find("<link"), html.find("<img"),
    ] if p != -1] or [len(html)])
    check(head_start < csp_at < first_res,
          "CSP 出现在 <head> 里、且在任何样式/脚本之前",
          f"head={head_start} csp={csp_at} 首个资源={first_res}")

    # ---- 2. 默认拒绝 ----
    check(d.get("default-src") == ["'none'"], "default-src 是 'none'（默认拒绝）",
          str(d.get("default-src")))

    for name in ("object-src", "frame-src", "child-src", "worker-src",
                 "media-src", "font-src", "manifest-src"):
        check(name not in d, f"没有单独放宽 {name}（交给 default-src 'none'）",
              str(d.get(name)))

    # ---- 3. 不许有外部来源 ----
    #
    # 注意不能拿整串去 `in` —— CSP 里 `http://127.0.0.1:18970` 也含子串 `http:`，
    # 那样查会误报。CSP 的 scheme-source 是**独立的一项**（`http:` 单独出现
    # 才表示"任意 http 源"），所以要逐项按等值比。
    values = [v for vals in d.values() for v in vals]
    for bad, why in [("*", "通配符（任意来源）"),
                     ("http:", "任意 http 源"),
                     ("https:", "任意 https 源"),
                     ("ws:", "任意 WebSocket 源"),
                     ("wss:", "任意 wss 源"),
                     ("data:", "data: URL"),
                     ("blob:", "blob: URL"),
                     ("'unsafe-eval'", "动态求值"),
                     ("'unsafe-hashes'", "按哈希放行内联处理器"),
                     ("'strict-dynamic'", "动态放行脚本")]:
        check(bad not in values, f"CSP 里没有 {bad}（{why}）")

    # 任何带协议的 host-source 都只能是本机 bridge
    host_sources = [v for v in values if "://" in v]
    check(all(v.startswith("http://127.0.0.1:") for v in host_sources),
          "带协议的来源只有本机 bridge", str(host_sources))

    # ---- 4. 外发信道只留本地 bridge ----
    connect = d.get("connect-src", [])
    check(len(connect) == 1, "connect-src 只有一项（不给第二个去处）", str(connect))
    if connect:
        check(connect[0].startswith("http://127.0.0.1:"),
              "connect-src 指向本机 bridge", connect[0])

    # 策略必须覆盖面板**实际**要访问的地址 —— 否则就是自己把自己打死
    origins = set(re.findall(r"https?://[0-9a-zA-Z_.:-]+", html))
    bridge_origins = {o.rstrip("/") for o in origins if "127.0.0.1" in o}
    check(bridge_origins, "从面板里找到了 bridge 地址", str(bridge_origins))
    for o in bridge_origins:
        check(o in [c.rstrip("/") for c in connect],
              f"connect-src 覆盖面板实际访问的 {o}")

    # ---- 5. script-src：只能是内联，不许引外部 ----
    script = d.get("script-src", [])
    check(script == ["'unsafe-inline'"],
          "script-src 只开 'unsafe-inline'（不引外部脚本）", str(script))

    # ---- 6. 会被浏览器忽略的指令不要写 ----
    for ignored in ("frame-ancestors", "report-uri", "sandbox"):
        check(ignored not in d,
              f"没有把 {ignored} 写进 meta（只在响应头生效，写了会被忽略并报警告）")

    # ---- 7. 策略与页面同步：不能把现有功能打死 ----
    check(not re.search(r"<script[^>]*\bsrc\s*=", html, re.I),
          "面板没有外部 <script src=>（有的话会被 default-src 'none' 拦掉）")
    check(not re.search(r"<link\b", html, re.I),
          "面板没有 <link>（外部样式/图标会被拦掉）")
    check("@import" not in html, "面板没有 @import")
    check(not re.search(r"<img\b", html, re.I), "面板没有 <img>")
    check(not re.search(r"url\(\s*['\"]?https?:", html, re.I),
          "CSS 里没有指向外部的 url()")

    # ---- 8. 走生产注入路径后 CSP 还在 ----
    payload = {"generated": "2026-01-01T00:00:00", "total": 0, "categories": {}}
    rendered = html.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(payload))
    rendered = rendered.replace(KEY_PLACEHOLDER, json_for_script("k"))
    r_policy, r_d = parse_csp(rendered)
    check(r_policy == policy, "注入数据之后 CSP 原样保留（不被替换逻辑吃掉）")
    check(r_d.get("connect-src") == d.get("connect-src"),
          "注入后 connect-src 未被改动")

    return finish()


def finish():
    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

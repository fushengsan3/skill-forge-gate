#!/usr/bin/env python3
"""`verify/l4_conflict_detect.claude_deep_analysis` —— L4 的深度分析（问题清单 #34）。

## 这段代码此前一次都没被跑过

2026-10-05 的审计插桩实测：**调用 0 次**。原因是它在 `main()` 里，
而面板一键安装那条路走的是 `precheck` → `l4.detect_conflicts()`，
**不经过 `claude_deep_analysis`**。所以这段代码"在，但从没运行过"。

它自己要修的正是这个毛病的一半（注释里写着：原先只认 `ANTHROPIC_API_KEY`，
于是配第三方中转的机器上永远不触发）。剩下这一半 —— **没有任何测试碰过它** ——
由这个文件补上。

## 最要紧的一条：思考块

响应里 `thinking` 块排在最前面，它**没有** `text` 字段。用
`response["content"][0]["text"]` 会永远取到"分析失败"，而且看起来像
"模型没答"，不像代码有 bug。所以这里专门喂一个 thinking 在前的响应。

## 不发真请求

`urllib.request.build_opener` 被换掉，凭据判定被打桩。
测试绝不去调真 API —— 那会花用户的钱，而且结果不确定。

用法：
    python tests/test_l4_deep_analysis.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from verify import l4_conflict_detect as l4
from verify import llm_auth

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


class FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []
        self.opened = 0

    def open(self, req, timeout=None):
        self.opened += 1
        self.requests.append(req)
        if self.error:
            raise self.error
        return FakeResp(self.response or {})


class patched:
    def __init__(self, opener, key="k", style="x-api-key"):
        self.opener = opener
        self.key = key
        self.style = style

    def __enter__(self):
        import urllib.request
        self.real_build = urllib.request.build_opener
        self.real_creds = llm_auth.credentials
        self.real_model = llm_auth.model
        self.real_base = llm_auth.base_url
        urllib.request.build_opener = lambda *a, **k: self.opener
        llm_auth.credentials = lambda: (self.key, self.style, "凭据管理器 SkillForge/ai-token")
        llm_auth.model = lambda: "test-model"
        llm_auth.base_url = lambda: "https://api.example.invalid"
        return self.opener

    def __exit__(self, *a):
        import urllib.request
        urllib.request.build_opener = self.real_build
        llm_auth.credentials = self.real_creds
        llm_auth.model = self.real_model
        llm_auth.base_url = self.real_base
        return False


def report_with_system_write():
    return {"conflicts": {"red": [
        {"paths": ["C:/Windows/System32/drivers/etc/hosts"],
         "requires_claude_analysis": True},
    ]}}


def main():
    print("=" * 60)
    print("L4 深度分析（claude_deep_analysis）")
    print("=" * 60)

    print("--- 1. ★ 没有系统盘写入 → 一次请求都不发 ---")
    opener = FakeOpener()
    with patched(opener):
        out = l4.claude_deep_analysis({"conflicts": {"red": [
            {"paths": ["~/notes.md"], "requires_claude_analysis": False}]}})
    check(opener.opened == 0,
          "★ 不需要深度分析时**不发请求**（别为了「顺手问一句」花用户的钱）",
          f"opened={opener.opened}")
    check("claude_analysis" not in out, "也不往报告里塞一个空的分析结果")

    print("--- 2. ★ 需要分析但没凭据 → 记下来，不发请求 ---")
    opener2 = FakeOpener()
    with patched(opener2, key=""):
        out2 = l4.claude_deep_analysis(report_with_system_write())
    check(opener2.opened == 0, "★ 没凭据时不发请求", f"opened={opener2.opened}")
    check("跳过" in str((out2.get("claude_analysis") or {}).get("error", "")),
          "★ 报告里说明了是**跳过**（不是「分析完成、结论为空」）",
          str(out2.get("claude_analysis"))[:80])

    print("--- 3. ★ 思考块排在最前面时，仍能取到正文 ---")
    # 这就是那段注释警告的坑：content[0] 是 thinking，没有 text 字段。
    resp = {"content": [
        {"type": "thinking", "thinking": "让我想想……这个 skill 要写 hosts 文件……"},
        {"type": "text", "text": '{"risk": "dangerous", "reason": "改 hosts 可劫持域名"}'},
    ]}
    opener3 = FakeOpener(response=resp)
    with patched(opener3):
        out3 = l4.claude_deep_analysis(report_with_system_write())
    analysis = out3.get("claude_analysis") or {}
    check(opener3.opened == 1, "需要分析时发了**一次**请求", f"opened={opener3.opened}")
    check("dangerous" in str(analysis.get("assessment", "")),
          "★ 取到的是 text 块的内容（不是 thinking，也不是「分析失败」）",
          str(analysis.get("assessment"))[:70])
    check(analysis.get("assessment") != "分析失败",
          "★★ 没有落到默认值 —— 用 content[0] 取就会永远是这个值",
          str(analysis.get("assessment"))[:50])
    check(analysis.get("model") == "test-model", "记下了用的模型", str(analysis.get("model")))
    check("凭据管理器" in str(analysis.get("key_source", "")),
          "记下了密钥来源（排查「为什么没用上 AI」要看它）",
          str(analysis.get("key_source")))

    print("--- 4. 请求形状 ---")
    req = opener3.requests[0]
    check(req.full_url == "https://api.example.invalid/v1/messages",
          "★ 端点来自 llm_auth.base_url()，不是写死的 api.anthropic.com",
          req.full_url)
    body = json.loads(req.data.decode())
    check(body.get("max_tokens") == 1024,
          "★ max_tokens 是 1024（300 那种量级会被 thinking 吃光，L5 被同一个坑坑过）",
          str(body.get("max_tokens")))
    check("x-api-key" in {k.lower() for k in req.headers},
          "认证头按 auth_style 装（这里是 x-api-key）",
          str([k for k in req.headers]))

    print("--- 5. 认证风格互斥 ---")
    opener5 = FakeOpener(response=resp)
    with patched(opener5, style="bearer"):
        l4.claude_deep_analysis(report_with_system_write())
    hdrs = {k.lower() for k in opener5.requests[0].headers}
    check("authorization" in hdrs and "x-api-key" not in hdrs,
          "★ bearer 风格下**不带** x-api-key（两种都发，原生端点会直接报错）",
          str(sorted(hdrs)))

    print("--- 6. 请求失败 → 记进报告，不抛 ---")
    opener6 = FakeOpener(error=OSError("网络不通"))
    with patched(opener6):
        out6 = l4.claude_deep_analysis(report_with_system_write())
    check("OSError" in str((out6.get("claude_analysis") or {}).get("error", "")),
          "★ 失败记进 claude_analysis.error（不让整个 L4 崩掉）",
          str(out6.get("claude_analysis"))[:70])

    print("--- 7. 原报告不被破坏 ---")
    rep = report_with_system_write()
    with patched(FakeOpener(response=resp)):
        l4.claude_deep_analysis(rep)
    check("conflicts" in rep and rep["conflicts"]["red"],
          "原有内容还在（是往报告里加字段，不是替换）")

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

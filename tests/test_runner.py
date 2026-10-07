#!/usr/bin/env python3
"""沙箱内 runner 的截断检测（问题清单 #29）。

## 为什么这条最要紧

`_one_call()` 拿到的响应里如果**没有 tool_use**，它和一份干净 skill 的响应
长得**一模一样** —— 都是 `[]`。区别只在一个字段：`stop_reason`。

不看它，L5 就会把"没看到"当成"没有"，于是：
**一个把恶意指令写到把模型答满的 skill，会被报成通过。**

所以截断一律当错误上报，绝不当成"零调用"。这个文件钉的就是这条分界。

## 为什么能在这里测

`runner.py` 跑在容器里，但它只依赖 `json` / `sys` / `urllib`，宿主上能 import。
把它内部 `build_opener` 打桩成一个假 opener，就能走**未改动的真实代码路径** ——
比把判定逻辑抄一份到测试里强得多（抄一份，改的时候两边就会分叉）。

用法：
    python tests/test_runner.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from sandbox import runner

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


class FakeResp:
    def __init__(self, data):
        self._body = json.dumps(data).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """按顺序吐出预设响应；元素是 Exception 就抛出来（模拟 HTTP 错误）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        r = self.responses.pop(0) if self.responses else {}
        if isinstance(r, Exception):
            raise r
        return FakeResp(r)


class patched_opener:
    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        self._real = runner.urllib.request.build_opener
        runner.urllib.request.build_opener = lambda *a, **k: self.fake
        return self.fake

    def __exit__(self, *a):
        runner.urllib.request.build_opener = self._real
        return False


PAYLOAD = {"base_url": "https://api.example.com", "api_key": "k",
           "auth_style": "x-api-key"}


def main():
    print("=" * 60)
    print("沙箱 runner 截断检测（#29）")
    print("=" * 60)

    print("--- 1. ★ 被截断 ≠ 零调用 ---")
    truncated = {"stop_reason": "max_tokens", "content": []}
    with patched_opener(FakeOpener([truncated])):
        calls, err = runner._one_call(PAYLOAD, "prompt")
    check(calls == [] and err != "",
          "★ max_tokens 截断 → 返回错误，而不是安静的零调用", repr(err[:70]))
    check("截断" in err and "不是「没有」" in err,
          "★ 错误措辞说清了「没看到」≠「没有」（这是 L5 判 PASS 还是 ERROR 的分界）",
          err[:90])

    print("--- 2. 正常结束 + 有 tool_use → 照常返回 ---")
    ok_resp = {"stop_reason": "end_turn",
               "content": [{"type": "tool_use", "name": "Bash",
                            "input": {"command": "ls"}}]}
    with patched_opener(FakeOpener([ok_resp])):
        calls2, err2 = runner._one_call(PAYLOAD, "prompt")
    check(err2 == "" and len(calls2) == 1 and calls2[0]["name"] == "Bash",
          "正常响应照常解析出工具调用", f"{calls2} err={err2!r}")

    print("--- 3. 正常结束但**确实没有**工具调用 → 空，且不是错误 ---")
    clean = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "好"}]}
    with patched_opener(FakeOpener([clean])):
        calls3, err3 = runner._one_call(PAYLOAD, "prompt")
    check(calls3 == [] and err3 == "",
          "★ 干净的响应是「真的没有」，不是错误 —— 别把两者混起来",
          f"calls={calls3} err={err3!r}")

    print("--- 4. MAX_TOKENS 的绑定 ---")
    check(runner.MAX_TOKENS == 4096,
          "★ MAX_TOKENS 仍是 4096", str(runner.MAX_TOKENS))
    check(str(runner.MAX_TOKENS) in err,
          "★ 报错里带的那个数字**就是** MAX_TOKENS（不是另写死的 4096）", err[:60])

    print("--- 5. 401 换风格再试一次，两种都拒时才报错 ---")
    import urllib.error
    e401 = urllib.error.HTTPError("u", 401, "Unauthorized", None, None)
    opener = FakeOpener([e401, e401])
    with patched_opener(opener):
        calls5, err5 = runner._one_call(PAYLOAD, "prompt")
    check(len(opener.requests) == 2,
          "★ 401 时真的换了第二种认证风格重试（只重试一次）",
          f"发了 {len(opener.requests)} 次")
    check(calls5 == [] and "两种风格都试过了" in err5,
          "★ 两种都 401 时的措辞点明「问题在密钥本身，不是风格」", err5[:80])

    print("--- 6. 认证风格真的换了（不是发两遍同一个头）---")
    shapes = []
    for r in opener.requests:
        h = {k.lower(): v for k, v in r.headers.items()}
        shapes.append("authorization" if "authorization" in h else "x-api-key" if "x-api-key" in h else "?")
    check(shapes == ["x-api-key", "authorization"],
          "★ 第一次 x-api-key、第二次 bearer（顺序也对）", str(shapes))

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

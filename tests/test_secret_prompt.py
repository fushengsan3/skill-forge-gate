#!/usr/bin/env python3
"""`daemon/secret_prompt.py` 的两条不变式（问题清单 #30）。

## 一、永远不把密钥返回给调用方

调用方是 HTTP handler —— 它拿到的任何东西都会**进 HTTP 响应**，
而响应会进浏览器、可能进日志。所以密钥在 `ask_and_store` 内部就被消费掉，
返回值只有 `(bool, 状态字符串)`。

这条以前只是一句 docstring。这里把它钉成断言：**返回值里不许出现那个值**。

## 二、同一时刻只开一个输入框

用户点两下「设置密钥」，或者两个来源同时请求 —— 会弹出两个原生对话框，
两个都在等输入，而先填的那个会**被后填的覆盖**（或者反过来）。
`_dialog_lock` 用非阻塞获取，第二个直接 `DialogBusy`。

## 这里不弹真对话框

`_run`（真正 spawn PowerShell 的那一层）被换掉。测试**不能**在跑的时候
弹一个窗口出来等人输入 —— 那是无人值守环境里最糟的失败方式。

用法：
    python tests/test_secret_prompt.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon import credentials, secret_prompt

results = []

SENTINEL = "SENTINEL-PROMPTED-KEY-3b9e"


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


class patched:
    """把 `_run` 和 `credentials.set_secret` 换成假货。"""

    def __init__(self, run_result=("ok", SENTINEL), store_error=None, available=True):
        self.run_result = run_result
        self.store_error = store_error
        self.available = available
        self.runs = []
        self.stored = []

    def __enter__(self):
        self.real_run = secret_prompt._run
        self.real_set = credentials.set_secret
        self.real_avail = credentials.AVAILABLE

        def fake_run(title, prompt, hint):
            self.runs.append({"title": title, "prompt": prompt, "hint": hint})
            status, value = self.run_result
            return (value, status)

        def fake_set(name, value):
            if self.store_error:
                raise self.store_error
            self.stored.append((name, value))

        secret_prompt._run = fake_run
        credentials.set_secret = fake_set
        credentials.AVAILABLE = self.available
        return self

    def __exit__(self, *a):
        secret_prompt._run = self.real_run
        credentials.set_secret = self.real_set
        credentials.AVAILABLE = self.real_avail
        return False


def main():
    print("=" * 60)
    print("密钥输入框（daemon/secret_prompt.py）")
    print("=" * 60)

    print("--- 1. ★ 永远不把密钥返回给调用方 ---")
    with patched() as p:
        result = secret_prompt.ask_and_store("ai-token")
    check(result == (True, "ok"), "成功时返回 (True, 'ok')", str(result))
    check(SENTINEL not in repr(result),
          "★★ 返回值里**没有**密钥值（调用方是 HTTP handler，返回值会进响应）",
          repr(result))
    check(p.stored == [("ai-token", SENTINEL)],
          "★ 值**原样**交给了 set_secret（没有中途编码/改写）",
          f"{len(p.stored)} 条，值长度 {len(p.stored[0][1]) if p.stored else 0}")

    print("--- 2. ★ 同一时刻只开一个输入框 ---")
    secret_prompt._dialog_lock.acquire()
    try:
        busy = None
        try:
            secret_prompt.ask_and_store("ai-token")
        except secret_prompt.DialogBusy as e:
            busy = e
        check(busy is not None,
              "★ 已有对话框开着时，第二次调用抛 DialogBusy（不是排队等第二个框）",
              str(busy) or "（没抛）")
    finally:
        secret_prompt._dialog_lock.release()
    # 释放之后必须能再用 —— 别把锁做成一次性的
    with patched():
        check(secret_prompt.ask_and_store("ai-token") == (True, "ok"),
              "★ 上一个结束之后，锁被正确释放（还能再开）")

    print("--- 3. 用户取消 / 各种失败路径 ---")
    with patched(run_result=("cancelled", "")):
        check(secret_prompt.ask_and_store("ai-token") == (False, "cancelled"),
              "取消 → (False, cancelled)，且不写凭据")
    with patched(run_result=("timeout", "")):
        check(secret_prompt.ask_and_store("ai-token") == (False, "timeout"),
              "超时 → (False, timeout)")
    with patched(run_result=("ok", "")):
        check(secret_prompt.ask_and_store("ai-token") == (False, "ok"),
              "★ 状态 ok 但**值为空** → 不写（空值不能覆盖掉已有密钥）")
    with patched(store_error=credentials.CredentialError("写不进去")):
        check(secret_prompt.ask_and_store("ai-token") == (False, "store_failed"),
              "★ 落盘失败 → (False, store_failed)，不报成功")
    with patched(available=False) as p3:
        got = secret_prompt.ask_and_store("ai-token")
        check(got == (False, "credentials_unavailable") and p3.runs == [],
              "★ 凭据管理器不可用 → 直接返回，**连对话框都不弹**",
              f"{got} runs={len(p3.runs)}")

    print("--- 4. 失败路径同样不泄漏 ---")
    for rr, label in [(("cancelled", ""), "取消"), (("ok", ""), "空值")]:
        with patched(run_result=rr) as p4:
            got4 = secret_prompt.ask_and_store("ai-token")
        check(p4.stored == [] and SENTINEL not in repr(got4),
              f"{label}时不写凭据、返回值不带值", str(got4))

    print("--- 5. 锁在异常路径上也会释放 ---")
    with patched(store_error=credentials.CredentialError("炸")):
        secret_prompt.ask_and_store("ai-token")
    check(secret_prompt._dialog_lock.acquire(blocking=False),
          "★ 抛异常之后锁仍被释放（finally 生效）—— 否则输入框功能会永久卡死")
    secret_prompt._dialog_lock.release()

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

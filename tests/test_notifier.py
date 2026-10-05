#!/usr/bin/env python3
"""
通知模块测试。

这个模块出过两类问题，都值得钉住：

  1. **死代码冒充功能。** 曾经构造过一个给 Toast 绑点击动作的 `$activation`
     脚本，但**从未被引用** —— 真正执行的 `ps_script` 从零新建 `$template`。
     于是文档里"点击通知打开面板"是假的，实际是无条件 `os.startfile`。
     这种"看起来做了其实没做"比没有更糟：它让人不再去看真相。

  2. **PowerShell 注入。** 标题/正文原先被原样拼进 `-Command` 执行的脚本，
     而 PowerShell 双引号字符串里 `$(...)` 会被求值 —— 一个带 `$(calc)` 的
     标题就是任意命令执行。本模块自己有命令行入口，那条路径直接可达。

全程不发真通知：把 subprocess.run 换成替身，只看它**被喂了什么**。

用法：
    python tests/test_notifier.py
"""
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon import notifier

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


class FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


def main():
    print("=" * 60)
    print("通知模块测试")
    print("=" * 60)

    calls = []

    def fake_run(args, **kw):
        calls.append({"args": args, "env": dict(kw.get("env") or {}),
                      "flags": kw.get("creationflags", 0)})
        return FakeProc()

    real_run = notifier.subprocess.run
    notifier.subprocess.run = fake_run
    try:
        run_checks(calls)
    finally:
        notifier.subprocess.run = real_run

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


def run_checks(calls):
    src = (ROOT / "daemon" / "notifier.py").read_text(encoding="utf-8")

    # 注释里**故意**留着"$activation / 点击通知"这些词 —— 不解释旧行为，
    # 下一个人就会再写一遍。所以静态检查只针对**可执行代码**。
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    # 模块顶部的 docstring 也是注释性质，从可执行代码里剔掉
    code = re.sub(r'^""".*?"""', "", code, count=1, flags=re.S)

    print("--- 1. 死代码已经清掉 ---")
    check("action_script" not in code, "★ 可执行代码里没有 `action_script` 了")
    check("$activation" not in code, "★ 可执行代码里没有 `$activation` 了")
    check("那是假的" in src and ("从来没被用上" in src or "死代码" in src),
          "文档里明确写了旧行为是假的（而不是默默删掉）")
    check("不是点击通知打开的" in src or "而不是点击" in src,
          "文档里写清了实际行为（通知后自动打开）")

    print("--- 2. 标题/正文不许出现在脚本里（注入防护）---")
    calls.clear()
    EVIL = 'x$(calc)"; [System.Diagnostics.Process]::Start("cmd","/c whoami"); "'
    notifier.send_notification(EVIL, "正常正文")
    check(len(calls) == 1, "调了一次 PowerShell", f"{len(calls)} 次")
    if calls:
        # args 里只有 powershell / -NoProfile / -Command / <脚本>；
        # 敌意串**只该出现在 env 里** —— 这正是这次修的东西。
        check(all(EVIL not in str(a) for a in calls[0]["args"]),
              "（前置）敌意串一次都没出现在命令行参数里")
        script = calls[0]["args"][-1]
        check("calc" not in script, "★ 标题没有出现在脚本正文里（不插值）")
        check("whoami" not in script, "★ 标题里的命令片段没进脚本")
        check("$env:SKILL_FORGE_TOAST_TITLE" in script,
              "脚本从环境变量取标题")
        check("$env:SKILL_FORGE_TOAST_MESSAGE" in script,
              "脚本从环境变量取正文")
        check(calls[0]["env"].get("SKILL_FORGE_TOAST_TITLE") == EVIL,
              "★ 标题原样走环境变量（数据，不是代码）")

    print("--- 3. 引号 / 换行 / 反引号等也走环境变量 ---")
    for payload, why in [('he said "hi"', "双引号"),
                         ("line1\nline2", "换行"),
                         ("`whoami`", "反引号（PowerShell 里是子表达式）"),
                         ("$($env:PATH)", "子表达式"),
                         ("'单引号'", "单引号"),
                         ("emoji 😀 中文", "非 ASCII")]:
        calls.clear()
        notifier.send_notification("T", payload)
        if not calls:
            check(False, f"3 {why}：没调用")
            continue
        script = calls[0]["args"][-1]
        leaked = payload.strip() and (payload in script)
        check(not leaked, f"3 {why} 的值没进脚本")
        check(calls[0]["env"].get("SKILL_FORGE_TOAST_MESSAGE") == payload,
              f"3 {why} 的值原样在环境变量里")

    print("--- 4. 脚本本身是固定的（不含任何变量插值）---")
    calls.clear()
    notifier.send_notification("A", "B")
    calls.clear()
    notifier.send_notification("完全不同的标题", "完全不同的正文")
    s1 = calls[0]["args"][-1]
    calls.clear()
    notifier.send_notification("x", "y")
    s2 = calls[0]["args"][-1]
    check(s1 == s2, "★ 两次调用生成的脚本一字不差（说明没有任何插值）")

    print("--- 5. 面板打开行为 ---")
    calls.clear()
    missing = ROOT / "不存在的面板.html"
    with patch.object(notifier.os, "startfile", create=True) as sf:
        notifier.send_notification("t", "m", panel_path=str(missing))
        check(not sf.called, "面板文件不存在时不调 startfile")
    check(len(calls) == 1, "但通知照发")

    real_panel = ROOT / "SKILL.md"          # 借一个确实存在的文件
    with patch.object(notifier.os, "startfile", create=True) as sf:
        notifier.send_notification("t", "m", panel_path=str(real_panel))
        check(sf.called, "★ 面板存在时调 startfile（这是真正打开面板的路径）")
        if sf.called:
            # 直接比参数，不要拿路径去过 repr 的子串比对 ——
            # repr 里反斜杠是转义的，`\U` 之类会把人绕晕
            got = sf.call_args[0][0] if sf.call_args[0] else None
            check(got == str(real_panel), "打开的是那个面板文件", str(got))

    print("--- 6. PowerShell 起不来也不能崩 ---")
    def boom(*a, **k):
        raise FileNotFoundError("powershell 不在")
    notifier.subprocess.run = boom
    try:
        notifier.send_notification("t", "m")
        check(True, "★ PowerShell 缺失时静默返回，不抛异常（通知失败不该拖垮扫描）")
    except Exception as e:
        check(False, "PowerShell 缺失时不抛异常", f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""`daemon/credentials.py` 的**写路径**（问题清单 #25）。

## 为什么只测写路径

读路径已经由 `tests/test_llm_auth.py` 覆盖了（44 项）。剩下的是写：
`set_secret` / `delete_secret` —— 也就是"密钥进不进得去、出不出得来"的那一半。

这个模块在 2026-10-05 之前**一条测试都没有**，而它决定的正是
"L5 到底跑不跑"。判定型模块的错误模式是**静默**的（存不进去 → 读出来是空 →
跳过 → 看起来一切正常），所以它最该被钉住。

## 这里不碰真实的凭据管理器

`win32cred` 整个被换成一个记录调用参数的假货。
测试**绝不**往用户真实的 Windows 凭据管理器里写东西 ——
那正是 README 里承诺过的事（"密钥全程不落盘、不进浏览器"），
在测试里违反它同样不可接受。

用法：
    python tests/test_credentials.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon import credentials

results = []

# 一个一眼能认出来的哨兵值：它**只应该**出现在 CredWrite 的参数里，
# 不该出现在任何异常文本或日志里。
SENTINEL = "SENTINEL-KEY-DO-NOT-LEAK-7c41"


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


class fake_win32cred:
    """记录每次调用，并按需抛错。"""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def __init__(self, write_error=None, delete_error=None):
        self.writes = []
        self.deletes = []
        self.write_error = write_error
        self.delete_error = delete_error

    def CredWrite(self, cred, flags):
        if self.write_error:
            raise self.write_error
        self.writes.append(cred)

    def CredDelete(self, target, ctype):
        if self.delete_error:
            raise self.delete_error
        self.deletes.append((target, ctype))


class patched:
    def __init__(self, fake, available=True):
        self.fake = fake
        self.available = available

    def __enter__(self):
        self.real_cred = getattr(credentials, "win32cred", None)
        self.real_avail = credentials.AVAILABLE
        credentials.win32cred = self.fake
        credentials.AVAILABLE = self.available
        return self.fake

    def __exit__(self, *a):
        credentials.win32cred = self.real_cred
        credentials.AVAILABLE = self.real_avail
        return False


def main():
    print("=" * 60)
    print("凭据写路径（daemon/credentials.py）")
    print("=" * 60)

    print("--- 1. ★ 空密钥绝不写进去 ---")
    # 空值写进去的后果不是"没用"，是**覆盖掉一把好的**：
    # 用户下次读出来的是一把空密钥 → L5 报"没配" → 他去设置页反复保存。
    for bad, why in [("", "空串"), ("   ", "全空白"), ("\n\t ", "只有空白符")]:
        fake = fake_win32cred()
        with patched(fake):
            raised = None
            try:
                credentials.set_secret("ai-token", bad)
            except credentials.CredentialError as e:
                raised = e
        check(raised is not None and fake.writes == [],
              f"★ 拒绝 {why}，且**没有**调用 CredWrite",
              f"raised={type(raised).__name__ if raised else None} writes={len(fake.writes)}")

    print("--- 2. ★ 正常写入：参数形状正确 ---")
    fake = fake_win32cred()
    with patched(fake):
        credentials.set_secret("ai-token", SENTINEL)
    check(len(fake.writes) == 1, "调用了一次 CredWrite", str(len(fake.writes)))
    cred = fake.writes[0] if fake.writes else {}
    check(cred.get("CredentialBlob") == SENTINEL,
          "★ CredentialBlob 就是原值（pywin32 自己转 UTF-16 —— 这里若手工编码就成双重编码）",
          repr(cred.get("CredentialBlob"))[:40])
    check(cred.get("TargetName") == credentials._target("ai-token"),
          "TargetName 带上了 skill-forge 前缀（枚举时只碰自己的）",
          str(cred.get("TargetName")))
    check(str(cred.get("TargetName", "")).startswith(credentials.TARGET_PREFIX),
          "★ 前缀是 TARGET_PREFIX，不是硬编码的字符串",
          str(cred.get("TargetName")))
    check(cred.get("Type") == fake_win32cred.CRED_TYPE_GENERIC,
          "类型是 GENERIC")
    check(cred.get("Persist") == fake_win32cred.CRED_PERSIST_LOCAL_MACHINE,
          "持久化到本机（任务计划拉起的进程读得到）")

    print("--- 3. ★ 写入失败时，密钥不许出现在异常文本里 ---")
    # 异常文本会进日志、进而可能进 HTTP 响应。`CredentialError(f"写入失败: {e}")`
    # 把底层异常原样带上 —— 而底层异常**可能**包含参数。
    # 这条用例钉的是"我们自己不主动把值放进去"。
    fake2 = fake_win32cred(write_error=OSError("底层炸了"))
    with patched(fake2):
        raised2 = None
        try:
            credentials.set_secret("ai-token", SENTINEL)
        except credentials.CredentialError as e:
            raised2 = str(e)
    check(raised2 is not None, "写入失败 → 抛 CredentialError", str(raised2))
    check(SENTINEL not in (raised2 or ""),
          "★ 异常文本里没有密钥值", repr(raised2)[:70])

    print("--- 4. win32cred 不可用时拒绝，不静默成功 ---")
    fake3 = fake_win32cred()
    with patched(fake3, available=False):
        raised3 = None
        try:
            credentials.set_secret("ai-token", SENTINEL)
        except credentials.CredentialError as e:
            raised3 = e
        check(raised3 is not None and fake3.writes == [],
              "★ 不可用 → 抛错，**不是**假装写成功（否则用户以为存上了）",
              str(raised3))
        check(credentials.delete_secret("ai-token") is False,
              "不可用时 delete 返回 False（不是 True）")

    print("--- 5. 删除 ---")
    fake4 = fake_win32cred()
    with patched(fake4):
        got = credentials.delete_secret("ai-token")
    check(got is True and len(fake4.deletes) == 1,
          "删除成功返回 True 且调了 CredDelete", str(fake4.deletes))
    check(fake4.deletes and fake4.deletes[0][0] == credentials._target("ai-token"),
          "删的是带前缀的那个 target（不会误删别人写的凭据）",
          str(fake4.deletes))

    fake5 = fake_win32cred(delete_error=OSError("没有这条"))
    with patched(fake5):
        got5 = credentials.delete_secret("ai-token")
    check(got5 is False, "★ 删不掉（没配过）→ False，不把失败报成成功")

    print("--- 5b. ★ 粘错了内容要被挡住（2026-10-07 实测事故）---")
    # 真实事故：用户在原生弹框里粘错了东西（粘成了别处的一句中文），
    # 而那个框是**密码框**，他自己看不见粘了什么。于是凭据管理器里存进了一句
    # 32 个汉字的话，而 describe() 一切正常（has_token: true、key_note: ""）——
    # 直到真发请求才炸，报的是 `latin-1 codec can't encode` 这种跟密钥无关的错。
    #
    # 下面这条就是**事故现场那个值**，逐字符一模一样。
    ACCIDENT = "轮换 DeepSeek ANTHROPIC_AUTH_TOKEN"
    fake = fake_win32cred()
    with patched(fake):
        raised = None
        try:
            credentials.set_secret("ai-token", ACCIDENT)
        except credentials.CredentialError as e:
            raised = str(e)
    check(raised is not None and fake.writes == [],
          "★ 事故现场那个值 → 拒绝，且**没有**写进凭据管理器",
          str(raised)[:60])
    check("粘错" in (raised or ""),
          "★ 理由说得清是「粘错了」而不是笼统的格式错误", str(raised)[:70])

    for bad, why in [("sk-abc def", "有空格"), ("short", "太短"),
                     ("密钥abcdefghijklmnop", "非 ASCII")]:
        check(credentials._looks_like_key(bad) != "",
              f"★ 拒绝形态不对的：{why}", repr(bad)[:24])
    for good in ["sk-abcdefghijklmnop", "sk-ant-" + "a" * 40, SENTINEL]:
        check(credentials._looks_like_key(good) == "",
              f"★ 正常形态的放行（判据要宽松，不能替用户判断格式）", good[:24])

    print("--- 6. 前缀本身 ---")
    check(credentials.TARGET_PREFIX.endswith("/"),
          "TARGET_PREFIX 以 / 结尾，拼出来是 `前缀/名字` 的形状",
          credentials.TARGET_PREFIX)
    check(credentials._target("x") == credentials.TARGET_PREFIX + "x",
          "_target 就是前缀拼接")

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

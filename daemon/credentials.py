#!/usr/bin/env python3
"""
密钥存储 —— Windows 凭据管理器（R9）。

## 为什么不用文件、不用 localStorage

决策记录 §3.3 已否决"密钥存 localStorage"：面板会渲染来自 GitHub 的不受信数据，
把密钥放在那个页面上等于交给 XSS。存普通文件也不行 —— 明文落盘，
任何读到它的程序都能拿走。

Windows 凭据管理器由系统用 DPAPI 按**当前用户**加密，其他用户/进程读不到，
并且在系统的"凭据管理器"界面里可见可删，用户有完全的掌控权。

## 实现说明

用 `win32cred`（pywin32 自带）而不是 `keyring` —— 本机没装 keyring，
而 win32cred 已经可用，少一个依赖。

**一个容易踩的坑**：`CredentialBlob` 不是 UTF-8 而是 **UTF-16-LE**。
直接按 UTF-8 解码会得到一堆带 \\x00 的乱码，而且不会报错。
"""
import re

try:
    import win32cred
    AVAILABLE = True
except ImportError:  # 非 Windows 或缺 pywin32
    AVAILABLE = False

# ---- 写入前的形态校验 ----
#
# **为什么必须有这一道**（2026-10-07 实测撞到）：
# 用户在原生弹框里粘错了东西（粘成了别处的文字），而那个框是**密码框**
# （`UseSystemPasswordChar`）—— **他自己看不见粘了什么**。于是凭据管理器里
# 存进了一句 32 个汉字的话，而 `llm_auth.describe()` 一切正常
# （`has_token: true`、`key_note: ""`），直到真的发请求才炸，
# 报的还是 `latin-1 codec can't encode` —— 跟密钥八竿子打不着的一个错。
#
# 判据**刻意宽松**：只挡"明显不是密钥"的，不替用户判断格式（各家密钥长得都不一样）。
# 它挡的是"粘错了"，不是"格式不合规"。
_WHITESPACE = re.compile(r"\s")


def _looks_like_key(value: str) -> str:
    """→ 空串 = 通过；否则返回一句能直接给人看的拒绝理由。"""
    if _WHITESPACE.search(value):
        return "里面有空格或换行 —— 密钥不会有空白字符（多半是粘错了内容）"
    if not value.isascii():
        return "里面有非 ASCII 字符 —— 密钥只能是 ASCII，非 ASCII 存进去必然发不出去"
    if not value.isprintable():
        return "里面有不可打印字符"
    if len(value) < 16:
        return f"只有 {len(value)} 位，太短了（API 密钥一般至少 16 位）"
    return ""

# 凭据的 TargetName 前缀。加前缀是为了：
#   1. 在系统凭据界面里能一眼看出是 skill-forge 的
#   2. 枚举时可以安全地只列出自己写的，不碰用户其它凭据
TARGET_PREFIX = "SkillForge/"

# 已知的密钥名。集中在这里，避免各处硬编码字符串拼错。
GITHUB_TOKEN = "github-token"   # GitHub PAT，解开 API 限流（60/小时 → 5000/小时）
AI_TOKEN = "ai-token"           # AI 供应商密钥。**两个消费者**：AI 翻译后端（translate_ai.py）
                                # 和 L4/L5 的模型调用（verify/llm_auth.py）。两处都按 x-api-key 发 ——
                                # 同一个口令槽被两种头发出去，迟早出事，而其中一处还无人值守。


class CredentialError(Exception):
    """凭据读写失败。调用方应当把它当成"没配好"来处理，而不是崩溃。"""


def _target(name: str) -> str:
    return TARGET_PREFIX + name


def set_secret(name: str, value: str) -> None:
    """写入（或覆盖）一个密钥。"""
    if not AVAILABLE:
        raise CredentialError("win32cred 不可用，无法访问 Windows 凭据管理器")
    value = (value or "").strip()
    if not value:
        raise CredentialError("密钥为空，拒绝写入")
    # 形态校验 —— 挡住"粘错了内容"。理由见 `_looks_like_key` 上面的注释。
    why = _looks_like_key(value)
    if why:
        raise CredentialError(f"拒绝写入：{why}")

    try:
        win32cred.CredWrite({
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": _target(name),
            "UserName": "skill-forge",
            "CredentialBlob": value,          # pywin32 会自己转 UTF-16
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
            "Comment": "skill-forge 密钥，可在 Windows 凭据管理器中删除",
        }, 0)
    except Exception as e:
        raise CredentialError(f"写入失败: {e}") from e


# CredRead 失败有**两种**，含义完全相反：
#   1168 ERROR_NOT_FOUND → 用户没配过。正常状态，静默处理。
#   其它（凭据库损坏、权限、win32cred 半死不活）→ **配了，但读不出来**。
#
# 2026-10-05 之前这两条都走 `except Exception: return None`，于是调用方一律
# 当"没配"。用户明明在面板里存过密钥，L5 却报"没配 AI 凭据"，而且没有任何
# 地方说得出区别 —— 他会在设置页里反复保存，问题一次都不动。
NOT_FOUND = 1168


def _read(name: str) -> tuple:
    """返回 `(值 或 None, 状态)`。状态 ∈ `ok` / `absent` / `unavailable` / `unreadable: …`。

    只有这里知道凭据管理器到底出了什么事。`get_secret()` 是它的丢信息版本
    （"没配"和"读不出来"都变成 None），给不需要区分的老调用方用。
    """
    if not AVAILABLE:
        return None, "unavailable"
    try:
        cred = win32cred.CredRead(_target(name), win32cred.CRED_TYPE_GENERIC)
    except Exception as e:
        code = getattr(e, "winerror", None)
        if code is None and isinstance(e.args, tuple) and e.args:
            code = e.args[0]
        if code == NOT_FOUND:
            return None, "absent"
        return None, f"unreadable: {e}"

    blob = cred.get("CredentialBlob")
    if blob is None:
        return None, "absent"
    if isinstance(blob, bytes):
        # 坑：CredentialBlob 是 UTF-16-LE，不是 UTF-8
        for enc in ("utf-16-le", "utf-16", "utf-8"):
            try:
                text = blob.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return None, "unreadable: CredentialBlob 不是任何一种已知编码"
    else:
        text = str(blob)
    text = text.rstrip("\x00")
    if not text:
        return None, "absent"
    return text, "ok"


def get_secret(name: str):
    """读取密钥。没配过返回 None（不是抛错 —— "没配"是正常状态）。

    ⚠️ **读不出来也返回 None**（见 `_read` 的说明）。要区分这两种情形，
    用 `read_state()` 或 `diagnose()`。
    """
    return _read(name)[0]


def read_state(name: str) -> str:
    """这条凭据是 `ok` / `absent` / `unavailable` / `unreadable: …`。**不含值。**"""
    return _read(name)[1]


def diagnose() -> dict:
    """`configured()` 的详细版：每条已知密钥的**状态串**，以及是否可读。

    **绝不回值本身。** 存在的理由跟 `list_stored()` 一样 —— `configured()`
    只回两个布尔，答不了"我明明配过，为什么说没配"。
    """
    out = {}
    for name in (GITHUB_TOKEN, AI_TOKEN):
        state = read_state(name)
        entry = {"state": state, "configured": state == "ok"}
        if state.startswith("unreadable"):
            entry["hint"] = ("凭据存在但读不出来 —— 不是「没配」，是读不到。"
                             "去 Windows 凭据管理器（控制面板 → 用户账户 → "
                             "凭据管理器 → Windows 凭据）看看这条在不在，"
                             "在的话删掉重存一次。")
        out[name] = entry
    return out


def delete_secret(name: str) -> bool:
    """删除密钥。返回是否真的删掉了（没配过返回 False）。"""
    if not AVAILABLE:
        return False
    try:
        win32cred.CredDelete(_target(name), win32cred.CRED_TYPE_GENERIC)
        return True
    except Exception:
        return False


def has_secret(name: str) -> bool:
    return get_secret(name) is not None


def configured() -> dict:
    """哪些密钥已配置。

    **只返回布尔值，绝不返回值本身** —— 这个结果会经由 bridge 发给浏览器页面，
    任何"顺手把值也带上"的写法都等于把密钥交给 XSS。
    """
    return {
        "available": AVAILABLE,
        "github_token": has_secret(GITHUB_TOKEN),
        "ai_token": has_secret(AI_TOKEN),
    }


def list_stored() -> list:
    """列出 skill-forge 自己写过的凭据名（用于诊断，同样不含值）。

    存在的理由：`configured()` 只回两个布尔，答不了"为什么会说没配"。
    凭据管理器里可能躺着一条名字稍有不同的陈旧条目（改名、手滑建错），
    那要靠枚举才能看见。**只回名字，不回值。**
    """
    if not AVAILABLE:
        return []
    try:
        out = []
        for c in win32cred.CredEnumerate(None, 0):
            t = str(c.get("TargetName", ""))
            if t.startswith(TARGET_PREFIX):
                out.append(t[len(TARGET_PREFIX):])
        return sorted(out)
    except Exception:
        return []


if __name__ == "__main__":
    # 手工排查用：这个模块原先没有命令行入口，于是 list_stored() 写了却
    # 谁也够不着 —— "功能在、入口不在"。接上它就一行的事。
    import json
    print(json.dumps({
        "available": AVAILABLE,
        "configured": configured(),
        "stored_names": list_stored(),
        # diagnose() 回答"为什么说我没配" —— 是没配，还是配了读不出来。
        "diagnose": diagnose(),
    }, ensure_ascii=False, indent=2))

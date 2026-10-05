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
from pathlib import Path

try:
    import win32cred
    import pywintypes
    AVAILABLE = True
except ImportError:  # 非 Windows 或缺 pywin32
    AVAILABLE = False

# 凭据的 TargetName 前缀。加前缀是为了：
#   1. 在系统凭据界面里能一眼看出是 skill-forge 的
#   2. 枚举时可以安全地只列出自己写的，不碰用户其它凭据
TARGET_PREFIX = "SkillForge/"

# 已知的密钥名。集中在这里，避免各处硬编码字符串拼错。
GITHUB_TOKEN = "github-token"   # GitHub PAT，解开 API 限流（60/小时 → 5000/小时）
AI_TOKEN = "ai-token"           # AI 供应商密钥，供 AI 翻译后端用


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


def get_secret(name: str):
    """读取密钥。没配过返回 None（不是抛错 —— "没配"是正常状态）。"""
    if not AVAILABLE:
        return None
    try:
        cred = win32cred.CredRead(_target(name), win32cred.CRED_TYPE_GENERIC)
    except Exception:
        return None

    blob = cred.get("CredentialBlob")
    if blob is None:
        return None
    if isinstance(blob, bytes):
        # 坑：CredentialBlob 是 UTF-16-LE，不是 UTF-8
        for enc in ("utf-16-le", "utf-16", "utf-8"):
            try:
                text = blob.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return None
    else:
        text = str(blob)
    return text.rstrip("\x00") or None


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
    }, ensure_ascii=False, indent=2))

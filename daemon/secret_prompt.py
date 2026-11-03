#!/usr/bin/env python3
"""
唤起原生密钥输入框，并把结果直接写进 Windows 凭据管理器。

## 安全性质（这是整个 R9 的关键）

密钥的路径是：**输入框 → stdout 管道 → bridge 进程 → 凭据管理器**。

它**不经过**：磁盘临时文件、日志、HTTP 响应、浏览器页面。
面板只能通过 bridge 得知"配没配"，永远拿不到值本身。

## 并发

输入框是阻塞的（要等人操作，最多 180 秒）。bridge 用 ThreadingHTTPServer，
所以它不会卡住面板的其它请求；但必须保证**同时只有一个框**，
否则用户会看到一堆叠在一起的窗口 —— 用一把模块级锁挡住。
"""
import base64
import subprocess
import sys
import threading
from pathlib import Path

try:
    from daemon import credentials
except ImportError:  # 允许直接以脚本身份运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from daemon import credentials

SCRIPT = Path(__file__).resolve().parent / "prompt_secret.ps1"
TIMEOUT = 180

# 同一时刻只允许一个输入框
_dialog_lock = threading.Lock()

# 人类可读的提示语，集中在这里
PROMPTS = {
    credentials.GITHUB_TOKEN: (
        "GitHub 个人访问令牌 (PAT)",
        "粘贴你的 GitHub Token。\n"
        "令牌保存在 Windows 凭据管理器，用于读取 GitHub 数据、减少匿名访问限流。\n"
        "不配置也可使用公开来源；遇到限流时，L2 来源检查可能跳过。",
        "创建入口：GitHub → Settings → Developer settings → Personal access tokens。\n"
        "Skill Forge 的读取用途无需仓库写权限，不会用它推送代码。\n"
        "留空或取消会保留已有配置。",
    ),
    credentials.AI_TOKEN: (
        "DeepSeek API 密钥",
        "粘贴 DeepSeek API Key。\n"
        "它由模块简介翻译与 AI 模糊搜索共用，仅向 DeepSeek 官方接口发送用于认证。",
        "密钥保存在 Windows 凭据管理器，不经过浏览器页面。\n"
        "留空或取消会保留已有配置。",
    ),
}


# A coreless distribution has no L2 check to describe in its credential prompt.
from daemon import edition
if edition.ID == "coreless":
    title, prompt, hint = PROMPTS[credentials.GITHUB_TOKEN]
    PROMPTS[credentials.GITHUB_TOKEN] = (title, prompt.replace(
        "L2 来源检查可能跳过", "来源数据可能暂时不可用"), hint)


class DialogBusy(Exception):
    """已经有一个输入框开着。"""


def _run(title: str, prompt: str, hint: str):
    """跑一次输入框。返回 (值或 None, 状态)。"""
    if not SCRIPT.exists():
        return None, "missing_script"

    cmd = [
        "powershell", "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(SCRIPT),
        "-Title", title, "-Prompt", prompt, "-Hint", hint,
    ]
    try:
        proc = subprocess.run(
            # errors="replace"：解码失败时 subprocess 不抛异常，它把 stdout/stderr
            # 变成 None（见 daemon/installer.py 的 run() 那条注释）。这个函数的返回值
            # 决定"密钥到底存没存进去"，绝不能被一个静默的 None 带偏。
            cmd, capture_output=True, text=True, errors="replace", timeout=TIMEOUT,
            # CREATE_NO_WINDOW：别闪一个黑框出来。
            # 注意这不影响输入框本身 —— 它是 GUI 窗口，不是控制台。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except FileNotFoundError:
        return None, "no_powershell"

    out = (proc.stdout or "").strip()

    if proc.returncode == 0:
        if not out:
            return None, "empty_output"
        try:
            return base64.b64decode(out).decode("utf-8"), "ok"
        except Exception:
            # 解不出就当失败，不要把那串 base64 当密钥存进去
            return None, "bad_output"
    if proc.returncode == 2:
        return None, "cancelled"
    if proc.returncode == 3:
        return None, "empty"
    return None, "error"


def ask_and_store(name: str):
    """弹框问密钥，成功后直接写入凭据管理器。

    返回 (是否成功, 状态字符串)。**永远不把密钥返回给调用方** ——
    调用方（HTTP handler）拿到的只有结果，值在函数内部就被消费掉了。
    """
    if not credentials.AVAILABLE:
        return False, "credentials_unavailable"

    title, prompt, hint = PROMPTS.get(
        name, ("Skill Forge Gate", f"请输入 {name}：", "")
    )

    if not _dialog_lock.acquire(blocking=False):
        raise DialogBusy("已经有一个密钥输入框开着")

    try:
        value, status = _run(title, prompt, hint)
        if status != "ok" or not value:
            return False, status
        try:
            credentials.set_secret(name, value)
        except credentials.CredentialError as e:
            # 形态不对（多半是粘错了内容）**单独报**，别和"写盘失败"混成一个状态 ——
            # 前者用户自己就能改，后者要查凭据库。见 credentials._looks_like_key。
            if "拒绝写入" in str(e):
                return False, "bad_format"
            return False, "store_failed"
        return True, "ok"
    finally:
        _dialog_lock.release()

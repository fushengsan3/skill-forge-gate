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
        "粘贴你的 GitHub Token。\n它将存进 Windows 凭据管理器，用来把 API 限流从 60 次/小时提到 5000 次/小时。",
        "创建入口：GitHub → Settings → Developer settings → Personal access tokens。\n留空或取消则维持现状（不配置）。",
    ),
    credentials.AI_TOKEN: (
        "AI 供应商密钥",
        "粘贴 AI 服务的 API Key。\n它用于 AI 翻译后端，同样存进 Windows 凭据管理器。",
        "密钥不会经过浏览器页面，也不会以明文写进任何文件。",
    ),
}


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
            cmd, capture_output=True, text=True, timeout=TIMEOUT,
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
        name, ("Skill Forge", f"请输入 {name}：", "")
    )

    if not _dialog_lock.acquire(blocking=False):
        raise DialogBusy("已经有一个密钥输入框开着")

    try:
        value, status = _run(title, prompt, hint)
        if status != "ok" or not value:
            return False, status
        try:
            credentials.set_secret(name, value)
        except credentials.CredentialError:
            return False, "store_failed"
        return True, "ok"
    finally:
        _dialog_lock.release()

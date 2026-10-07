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
        # ⚠️ 2026-10-07：原话说"用来把 API 限流从 60/小时提到 5000/小时" —— 那只是
        # **读**的用途。实测撞到过：一个只为提限流建的 fine-grained token 是**只读**的，
        # 拿它 `git push` 会 403（`Permission to <owner>/<repo> denied`）。
        # 要推代码就得有 **Contents: Read and write**。
        "粘贴你的 GitHub Token。\n"
        "它将存进 Windows 凭据管理器，供 skill-forge **读取** GitHub API 使用：\n"
        "  · 把限流从 60 次/小时提到 5000 次/小时\n"
        "  · 不配也能跑，但容易被限流，L2 来源校验会被跳过\n"
        "skill-forge 自己**不会**用它 push、也不会用它 clone 私有仓库。\n"
        "（你自己 git push 到自己的仓库时可以复用同一个 token ——\n"
        " 那需要在它上面勾 Contents: Read and write）",
        "创建入口：GitHub → Settings → Developer settings → Personal access tokens。\n"
        "只给读用途的话，不要勾写权限。\n"
        "留空或取消则维持现状（不配置）。",
    ),
    credentials.AI_TOKEN: (
        "AI 供应商密钥",
        # ⚠️ 这里原先写的是"它用于 AI 翻译后端" —— **说轻了**。这个密钥真正的用途是
        # **L4 深度分析与 L5 沙箱**（装每个 skill 时都要调模型）。只提翻译会让人
        # 以为它是可选的附属功能，而没配它 L4/L5 会直接跳过 —— 那是安全流水线的一半。
        "粘贴 AI 服务的 API Key。\n"
        "它将存进 Windows 凭据管理器，供 L4 深度分析 / L5 沙箱 / AI 翻译后端"
        "调用模型时使用。",
        "密钥不会经过浏览器页面，也不会以明文写进任何文件。\n"
        "留空或取消则维持现状（不配置）。",
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

#!/usr/bin/env python3
"""
Windows 通知发送模块 — 通过 PowerShell Toast 通知用户，随后打开发现面板

## 关于"点击通知打开面板"（2026-10-05 更正）

这里的注释原先写着"Toast 点击后自动在浏览器中打开发现面板"，**那是假的**。

原因：本模块曾经构造过一个 `$activation` 脚本想给 Toast 绑点击动作，
但**它从来没被用上** —— 下面真正执行的 `ps_script` 是从零新建 `$template`
的，一个字都没引用 `$activation`。也就是说那段是死代码，Toast 上没有任何
激活动作，"点击"不会发生任何事。

真正打开面板的是后面的 `os.startfile(panel_path)`：**无条件、不等点击**，
通知一发出就把浏览器拉起来。

要给 Toast 绑点击动作，得注册 AppUserModelID 或走协议处理器 ——
是另一件事，不是在这里加两行的事。所以现在的做法是：
**把死代码删掉，把行为写清楚**，不假装有那个功能。
"""
import subprocess
import sys
import json
import os


# 标题/正文通过**环境变量**交给 PowerShell，不拼进脚本。
#
# 为什么：脚本是用 `-Command` 执行的，而 PowerShell 的双引号字符串里
# `$(...)` **会被求值**。原先写成 `CreateTextNode("{title}")` ——
# 一个带 `$(calc)` 或带双引号的标题就是任意命令执行。
# 而本模块自己有命令行入口（sys.argv[1] / [2] 就是这两个值），
# 也就是说那条路径是直接可达的。
#
# 环境变量是**数据**，永远不会被当成代码解析，所以不需要转义 ——
# 也就不会出现"转义写漏一个字符就破防"这种事。
ENV_TITLE = "SKILL_FORGE_TOAST_TITLE"
ENV_MESSAGE = "SKILL_FORGE_TOAST_MESSAGE"

PS_TEMPLATE = '''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$textNodes = $template.GetElementsByTagName("text")
$textNodes.Item(0).AppendChild($template.CreateTextNode($env:__ENV_TITLE__)) > $null
$textNodes.Item(1).AppendChild($template.CreateTextNode($env:__ENV_MESSAGE__)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Skill Forge").Show($toast)
'''


def send_notification(title: str, message: str, panel_path: str = None):
    """发送 Windows 10/11 Toast 通知；给了 panel_path 就在通知后打开面板。

    注意：面板是**通知发出后自动打开**的，不是点击通知打开的。
    """
    ps_script = (PS_TEMPLATE
                 .replace("__ENV_TITLE__", ENV_TITLE)
                 .replace("__ENV_MESSAGE__", ENV_MESSAGE))

    env = os.environ.copy()
    env[ENV_TITLE] = str(title)
    env[ENV_MESSAGE] = str(message)

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, timeout=10, env=env,
            # 后台守护进程不该闪一个控制台窗口出来
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass

    # Toast 发送后，用默认浏览器打开面板
    if panel_path and os.path.exists(panel_path):
        try:
            os.startfile(panel_path)
        except Exception:
            pass


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        title = sys.argv[1]
        message = sys.argv[2]
        panel_path = sys.argv[3] if len(sys.argv) > 3 else None
    else:
        data = json.loads(sys.stdin.read())
        title = data.get("title", "Skill Forge Gate")
        message = data.get("message", "")
        panel_path = data.get("panel_path")

    send_notification(title, message, panel_path)
    print(json.dumps({"ok": True}))

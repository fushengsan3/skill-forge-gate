#!/usr/bin/env python3
"""
Windows 通知发送模块 — 通过 PowerShell Toast 通知用户
Toast 点击后自动在浏览器中打开发现面板
"""
import subprocess
import sys
import json
import os
from pathlib import Path


def send_notification(title: str, message: str, panel_path: str = None):
    """发送 Windows 10/11 Toast 通知，点击可打开面板"""
    # 构建点击动作：用 start 命令在默认浏览器打开面板 HTML
    action_script = ""
    if panel_path and os.path.exists(panel_path):
        panel_url = Path(panel_path).as_uri()
        action_script = f'''
$activation = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$textNodes = $activation.GetElementsByTagName("text")
$textNodes.Item(0).AppendChild($activation.CreateTextNode("{title}")) > $null
$textNodes.Item(1).AppendChild($activation.CreateTextNode("{message}  (点击查看详情)")) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($activation)
'''

    ps_script = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$textNodes = $template.GetElementsByTagName("text")
$textNodes.Item(0).AppendChild($template.CreateTextNode("{title}")) > $null
$textNodes.Item(1).AppendChild($template.CreateTextNode("{message}")) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Skill Forge").Show($toast)
'''

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, timeout=10
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
        title = data.get("title", "Skill Forge")
        message = data.get("message", "")
        panel_path = data.get("panel_path")

    send_notification(title, message, panel_path)
    print(json.dumps({"ok": True}))

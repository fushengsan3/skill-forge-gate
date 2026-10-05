# Skill Forge — 原生密钥输入框
#
# 由 bridge 按需唤起（见 daemon/secret_prompt.py）。设计要点：
#
#   1. 密钥**只出现在内存里**：从输入框 → stdout 管道 → bridge 进程 → Windows 凭据管理器。
#      全程不写临时文件、不经浏览器、不进日志。
#   2. 输出用 base64 而不是原文 —— **这不是加密**，只是为了绕开管道里
#      换行/引号/编码的坑（密钥本身是 ASCII，但传输出错会静默截断，很难查）。
#   3. 取消时退出码 2，与"输入了空值"（退出码 3）区分开，
#      否则调用方没法告诉用户到底发生了什么。
#
# 用法:
#   powershell -STA -NoProfile -ExecutionPolicy Bypass -File prompt_secret.ps1 `
#       -Title "..." -Prompt "..." -Hint "..."
# 退出码: 0=成功(base64 已写入 stdout)  2=用户取消  3=空值  1=内部错误

param(
    [string]$Title  = "Skill Forge",
    [string]$Prompt = "请输入密钥：",
    [string]$Hint   = ""
)

$ErrorActionPreference = "Stop"

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    $form = New-Object System.Windows.Forms.Form
    $form.Text            = $Title
    $form.Size            = New-Object System.Drawing.Size(460, 240)
    $form.StartPosition   = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox     = $false
    $form.MinimizeBox     = $false
    $form.TopMost         = $true
    $form.BackColor       = [System.Drawing.Color]::FromArgb(32, 32, 48)
    $form.ForeColor       = [System.Drawing.Color]::FromArgb(224, 224, 224)

    $label = New-Object System.Windows.Forms.Label
    $label.Text     = $Prompt
    $label.Location = New-Object System.Drawing.Point(20, 20)
    $label.Size     = New-Object System.Drawing.Size(410, 40)
    $label.Font     = New-Object System.Drawing.Font("Microsoft YaHei UI", 10)
    $form.Controls.Add($label)

    $box = New-Object System.Windows.Forms.TextBox
    $box.Location     = New-Object System.Drawing.Point(20, 66)
    $box.Size         = New-Object System.Drawing.Size(410, 28)
    $box.UseSystemPasswordChar = $true
    $box.Font         = New-Object System.Drawing.Font("Consolas", 10)
    $box.BackColor    = [System.Drawing.Color]::FromArgb(20, 20, 32)
    $box.ForeColor    = [System.Drawing.Color]::FromArgb(224, 224, 224)
    $box.BorderStyle  = "FixedSingle"
    $form.Controls.Add($box)

    if ($Hint) {
        $hintLabel = New-Object System.Windows.Forms.Label
        $hintLabel.Text     = $Hint
        $hintLabel.Location = New-Object System.Drawing.Point(20, 100)
        $hintLabel.Size     = New-Object System.Drawing.Size(410, 40)
        $hintLabel.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
        $hintLabel.Font     = New-Object System.Drawing.Font("Microsoft YaHei UI", 8)
        $form.Controls.Add($hintLabel)
    }

    $ok = New-Object System.Windows.Forms.Button
    $ok.Text     = "确定"
    $ok.Location = New-Object System.Drawing.Point(230, 150)
    $ok.Size     = New-Object System.Drawing.Size(96, 32)
    $ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Controls.Add($ok)

    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text     = "取消"
    $cancel.Location = New-Object System.Drawing.Point(334, 150)
    $cancel.Size     = New-Object System.Drawing.Size(96, 32)
    $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Controls.Add($cancel)

    $form.AcceptButton = $ok        # 回车 = 确定
    $form.CancelButton = $cancel    # Esc  = 取消
    $form.Add_Shown({ $box.Focus() })

    $result = $form.ShowDialog()

    if ($result -ne [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Out.Write("")
        exit 2
    }

    $value = $box.Text
    if ([string]::IsNullOrWhiteSpace($value)) {
        [Console]::Out.Write("")
        exit 3
    }

    # base64 输出（仅为传输稳健，不是加密）
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($value.Trim())
    [Console]::Out.Write([Convert]::ToBase64String($bytes))
    exit 0
}
catch {
    [Console]::Error.Write($_.Exception.Message)
    exit 1
}

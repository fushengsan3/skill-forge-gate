# Skill Forge Gate — 原生密钥输入框
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
    [string]$Title  = "Skill Forge Gate",
    [string]$Prompt = "请输入密钥：",
    [string]$Hint   = ""
)

$ErrorActionPreference = "Stop"

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    $form = New-Object System.Windows.Forms.Form
    $form.Text            = $Title
    $form.Font            = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
    $form.AutoScaleMode   = [System.Windows.Forms.AutoScaleMode]::Dpi
    $form.AutoScaleDimensions = New-Object System.Drawing.SizeF(96, 96)
    $form.AutoSize        = $true
    $form.AutoSizeMode    = [System.Windows.Forms.AutoSizeMode]::GrowAndShrink
    $form.StartPosition   = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox     = $false
    $form.MinimizeBox     = $false
    $form.TopMost         = $true
    $form.BackColor       = [System.Drawing.Color]::FromArgb(32, 32, 48)
    $form.ForeColor       = [System.Drawing.Color]::FromArgb(224, 224, 224)

    # Let every row grow with its text; fixed label heights clip Chinese at higher DPI.
    $layout = New-Object System.Windows.Forms.TableLayoutPanel
    $layout.AutoSize = $true
    $layout.AutoSizeMode = [System.Windows.Forms.AutoSizeMode]::GrowAndShrink
    $layout.Dock = [System.Windows.Forms.DockStyle]::Fill
    $layout.Padding = New-Object System.Windows.Forms.Padding(20)
    $layout.ColumnCount = 1
    $layout.RowCount = 4
    [void]$layout.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::AutoSize)))
    for ($row = 0; $row -lt 4; $row++) {
        [void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize)))
    }
    $form.Controls.Add($layout)

    $label = New-Object System.Windows.Forms.Label
    # Older bridge processes may still send Markdown emphasis; native labels are plain text.
    $label.Text     = $Prompt.Replace("**", "")
    $label.AutoSize = $true
    $label.MinimumSize = New-Object System.Drawing.Size(520, 0)
    $label.MaximumSize = New-Object System.Drawing.Size(520, 0)
    $label.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 14)
    $layout.Controls.Add($label, 0, 0)

    $box = New-Object System.Windows.Forms.TextBox
    $box.Width        = 520
    $box.Dock         = [System.Windows.Forms.DockStyle]::Fill
    $box.Margin       = New-Object System.Windows.Forms.Padding(0)
    $box.UseSystemPasswordChar = $true
    $box.Font         = New-Object System.Drawing.Font("Consolas", 10)
    $box.BackColor    = [System.Drawing.Color]::FromArgb(20, 20, 32)
    $box.ForeColor    = [System.Drawing.Color]::FromArgb(224, 224, 224)
    $box.BorderStyle  = "FixedSingle"
    $layout.Controls.Add($box, 0, 1)

    if ($Hint) {
        $hintLabel = New-Object System.Windows.Forms.Label
        $hintLabel.Text     = $Hint.Replace("**", "")
        $hintLabel.AutoSize = $true
        $hintLabel.MinimumSize = New-Object System.Drawing.Size(520, 0)
        $hintLabel.MaximumSize = New-Object System.Drawing.Size(520, 0)
        $hintLabel.Margin = New-Object System.Windows.Forms.Padding(0, 12, 0, 0)
        $hintLabel.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
        $layout.Controls.Add($hintLabel, 0, 2)
    }

    $buttons = New-Object System.Windows.Forms.FlowLayoutPanel
    $buttons.AutoSize = $true
    $buttons.AutoSizeMode = [System.Windows.Forms.AutoSizeMode]::GrowAndShrink
    $buttons.MinimumSize = New-Object System.Drawing.Size(520, 0)
    $buttons.FlowDirection = [System.Windows.Forms.FlowDirection]::RightToLeft
    $buttons.WrapContents = $false
    $buttons.Margin = New-Object System.Windows.Forms.Padding(0, 18, 0, 0)
    $layout.Controls.Add($buttons, 0, 3)

    $ok = New-Object System.Windows.Forms.Button
    $ok.Text     = "确定"
    $ok.AutoSize = $true
    $ok.MinimumSize = New-Object System.Drawing.Size(100, 34)
    $ok.Margin = New-Object System.Windows.Forms.Padding(0, 0, 8, 0)
    $ok.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $ok.DialogResult = [System.Windows.Forms.DialogResult]::OK

    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text     = "取消"
    $cancel.AutoSize = $true
    $cancel.MinimumSize = New-Object System.Drawing.Size(100, 34)
    $cancel.Margin = New-Object System.Windows.Forms.Padding(0)
    $cancel.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $buttons.Controls.Add($cancel)
    $buttons.Controls.Add($ok)

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

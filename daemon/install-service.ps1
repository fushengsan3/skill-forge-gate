# Skill Forge — 注册 Windows 开机自启任务
# 以管理员权限运行此脚本以安装守护进程
# 用法: powershell -ExecutionPolicy Bypass -File install-service.ps1

$ErrorActionPreference = "Stop"
$taskName = "SkillForgeWatcher"

# ============================================================
# 1. 查找真实的 Python 可执行文件（排除 WindowsApps 0字节假体）
# ============================================================
function Find-RealPython {
    param([string]$ExeName)
    $candidates = Get-Command $ExeName -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }
    foreach ($candidate in $candidates) {
        if ($candidate -match 'Microsoft\\WindowsApps') { continue }  # 跳过 0 字节假体
        if (-not (Test-Path $candidate)) { continue }
        $file = Get-Item $candidate -ErrorAction SilentlyContinue
        if ($file.Length -gt 0) {
            return $candidate
        }
    }
    return $null
}

$pythonPath = Find-RealPython "pythonw"
if (-not $pythonPath) {
    $pythonPath = Find-RealPython "python"
}
if (-not $pythonPath) {
    Write-Host "错误：找不到有效的 Python 安装（pythonw.exe 或 python.exe）"
    Write-Host "请安装 Python 3.9+（https://python.org）并确保添加到 PATH"
    exit 1
}

Write-Host "✅ Python: $pythonPath"

# ============================================================
# 2. 验证 skill-forge 已安装
# ============================================================
$skillForgePath = "$env:USERPROFILE\.claude\skills\skill-forge"
$scriptPath = "$skillForgePath\daemon\watchdog.py"

if (-not (Test-Path $skillForgePath)) {
    Write-Host "错误：skill-forge 目录不存在：$skillForgePath"
    Write-Host "运行: git clone https://github.com/fushengsan3/skill-forge-gate.git $skillForgePath"
    exit 1
}

if (-not (Test-Path $scriptPath)) {
    Write-Host "错误：找不到 watchdog.py：$scriptPath"
    Write-Host "请确保 skill-forge 版本包含 daemon/watchdog.py"
    exit 1
}

Write-Host "✅ Skill Forge: $skillForgePath"

# ============================================================
# 3. 删除已存在的任务
# ============================================================
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "已存在旧任务，正在更新..."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

# ============================================================
# 4. 注册新任务
# ============================================================
$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument "`"$scriptPath`"" `
    -WorkingDirectory $skillForgePath

# 触发器：登录后延迟 2 分钟 + 随机延迟避免所有任务同时启动
$trigger = New-ScheduledTaskTrigger -AtLogon -RandomDelay (New-TimeSpan -Minutes 2)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -Compatibility Win8

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Skill Forge 守护进程 — 每周检查新 skill 并通知用户" `
    -Force

# ============================================================
# 5. 验证注册结果
# ============================================================
$registered = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($registered) {
    Write-Host ""
    Write-Host "✅ Skill Forge 守护进程已注册为开机自启任务"
    Write-Host "   任务名称: $taskName"
    Write-Host "   下次登录后自动延迟启动（随机 0-2 分钟）"
    Write-Host ""
    Write-Host "管理命令："
    Write-Host "   查看状态: Get-ScheduledTask -TaskName '$taskName'"
    Write-Host "   手动运行: Start-ScheduledTask -TaskName '$taskName'"
    Write-Host "   查看历史: Get-ScheduledTaskInfo -TaskName '$taskName'"
    Write-Host "   停止运行: Stop-ScheduledTask -TaskName '$taskName'"
    Write-Host "   禁用:     Disable-ScheduledTask -TaskName '$taskName'"
    Write-Host "   启用:     Enable-ScheduledTask -TaskName '$taskName'"
    Write-Host "   删除:     Unregister-ScheduledTask -TaskName '$taskName'"
} else {
    Write-Host "❌ 注册失败，请以管理员权限运行此脚本"
    exit 1
}

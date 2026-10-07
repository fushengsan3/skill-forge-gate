# Skill Forge Gate — 注册队列桥接（Bridge）开机自启任务
#
# R4-b：把 bridge 从 watchdog 里拆出来，做成独立常驻服务。
#
# 为什么需要它：bridge 原本只在每周扫描时随 watchdog 活 5 分钟，
# 面板的「一键安装」「卸载」几乎永远调不通（安装队列本身离线可用，
# 但真要执行时必须有人在 127.0.0.1:18970 上应答）。
#
# 用法（普通权限即可，无需管理员）:
#   powershell -ExecutionPolicy Bypass -File install-bridge-service.ps1

$ErrorActionPreference = "Stop"
$taskName = "SkillForgeBridge"

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

# 优先 pythonw：常驻服务不该弹黑窗口
$pythonPath = Find-RealPython "pythonw"
if (-not $pythonPath) {
    $pythonPath = Find-RealPython "python"
}
if (-not $pythonPath) {
    Write-Host "错误：找不到有效的 Python 安装（pythonw.exe 或 python.exe）"
    exit 1
}
Write-Host "✅ Python: $pythonPath"

# ============================================================
# 2. 验证 skill-forge 已安装，且 version 包含 queue_bridge.py
# ============================================================
$skillForgePath = "$env:USERPROFILE\.claude\skills\skill-forge"
$scriptPath = "$skillForgePath\daemon\queue_bridge.py"

if (-not (Test-Path $skillForgePath)) {
    Write-Host "错误：skill-forge 目录不存在：$skillForgePath"
    exit 1
}
if (-not (Test-Path $scriptPath)) {
    Write-Host "错误：找不到 queue_bridge.py：$scriptPath"
    Write-Host "请先把 skill-forge 更新到含 R4-b 的版本"
    exit 1
}
Write-Host "✅ 桥接脚本: $scriptPath"

# ============================================================
# 3. 删除已存在的任务
# ============================================================
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "已存在旧任务，正在更新..."
    # 先停掉，否则正在运行的实例会继续占着端口
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

# ============================================================
# 4. 注册新任务（提权失败则退到免提权的 HKCU Run）
# ============================================================
$useRunKey = $false

$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument "`"$scriptPath`"" `
    -WorkingDirectory $skillForgePath

$trigger = New-ScheduledTaskTrigger -AtLogon -RandomDelay (New-TimeSpan -Minutes 1)

# -ExecutionTimeLimit 0 是关键：任务计划的默认上限是 72 小时，
# 到点会**无声地**杀掉进程 —— 常驻服务必须显式关掉，否则每三天掉一次线。
# 失败重启也调密一些（1 分钟），bridge 挂了面板的安装按钮就废了。
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -Compatibility Win8

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Skill Forge Gate 队列桥接 — 常驻 127.0.0.1:18970，供面板读已装列表、执行安装/卸载" `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "✅ 已注册计划任务（带崩溃自动重启）"
} catch {
    # 这台机器上计划任务要求提权（连 onlogon 也不例外）。
    # 退到 HKCU\...\Run —— 用户级自启，不需要管理员，效果一样是登录后自动运行。
    # 代价：没有计划任务那种"崩溃自动重启"，
    #       由 queue_bridge.start_bridge() 自己的重试循环兜底。
    Write-Host "⚠ 计划任务注册被拒（需要管理员权限），改用免提权的 HKCU Run 自启"
    $useRunKey = $true

    $runValue = "`"$pythonPath`" `"$scriptPath`""
    $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    New-ItemProperty -Path $runKey -Name $taskName -Value $runValue `
        -PropertyType String -Force | Out-Null

    $check = (Get-ItemProperty -Path $runKey -Name $taskName -ErrorAction SilentlyContinue).$taskName
    if (-not $check) {
        Write-Host "❌ HKCU Run 也写入失败"
        exit 1
    }
    Write-Host "✅ 已写入 $runKey\$taskName"
}

# ============================================================
# 5. 立刻启动一次（不必等下次登录）
# ============================================================
$listening = (Test-NetConnection -ComputerName 127.0.0.1 -Port 18970 -WarningAction SilentlyContinue).TcpTestSucceeded
if ($listening) {
    Write-Host "ℹ 127.0.0.1:18970 已有服务在跑，跳过启动"
} else {
    # pythonw 无控制台、无窗口
    Start-Process -FilePath $pythonPath -ArgumentList "`"$scriptPath`"" -WorkingDirectory $skillForgePath -WindowStyle Hidden
    Start-Sleep -Seconds 2
    $listening = (Test-NetConnection -ComputerName 127.0.0.1 -Port 18970 -WarningAction SilentlyContinue).TcpTestSucceeded
}
if ($listening) {
    Write-Host "✅ 桥接已在 127.0.0.1:18970 监听"
} else {
    Write-Host "⚠ 端口尚未监听 —— 等 2 秒再看，或用下面的命令查"
}

# ============================================================
# 6. 收尾说明
# ============================================================
Write-Host ""
Write-Host "   监听地址: 127.0.0.1:18970"
if ($useRunKey) {
    Write-Host ""
    Write-Host "管理命令（HKCU Run 方式）："
    Write-Host "   查看: Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '$taskName' -WhatIf"
    Write-Host "   删除: Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '$taskName'"
    Write-Host "   停止: Get-Process pythonw | Where-Object { `$_.Path -like '*pythonw*' } | Stop-Process"
    Write-Host ""
    Write-Host "（想换成带崩溃自动重启的计划任务，用管理员权限重跑本脚本即可）"
} else {
    Write-Host ""
    Write-Host "管理命令（计划任务方式）："
    Write-Host "   查看状态: Get-ScheduledTask -TaskName '$taskName'"
    Write-Host "   手动运行: Start-ScheduledTask -TaskName '$taskName'"
    Write-Host "   停止运行: Stop-ScheduledTask -TaskName '$taskName'"
    Write-Host "   删除:     Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
}

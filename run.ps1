# BI 归因分析 Agent — 交互式管理脚本（PowerShell）
# 用法: .\run.ps1
# 启动的是 Streamlit Web 前端(app/app.py);优先用 .venv 里的解释器
# (本机 Anaconda 全局 streamlit 1.45.1 缺 altair_chart 的 width 参数,渲染会报错)。

$ErrorActionPreference = "SilentlyContinue"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectRoot ".agent.pid"
$logDir = Join-Path $projectRoot "logs"
$accessLog = Join-Path $logDir "agent.out.log"
$errorLog = Join-Path $logDir "agent.err.log"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

function Get-Port {
    $config = Join-Path $projectRoot ".streamlit\config.toml"
    if (Test-Path $config) {
        $match = Select-String -Path $config -Pattern 'server\s*\.\s*port\s*=\s*(\d+)'
        if ($match) { return [int]$match.Matches[0].Groups[1].Value }
    }
    return 8501
}

function Get-AgentProcess {
    if (-not (Test-Path $pidFile)) { return $null }
    try {
        # 变量名避开 $pid:$PID 是只读自动变量,赋值会抛错(被 catch 吞掉就永远返回 null)
        $procId = [int](Get-Content $pidFile -Raw).Trim()
        return Get-Process -Id $procId -ErrorAction SilentlyContinue
    } catch {
        return $null
    }
}

function Start-Agent {
    if (Get-AgentProcess) {
        Write-Host "[!] 已有实例在运行" -ForegroundColor Yellow
        return
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    $port = Get-Port
    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $launcher = $venvPython
        $args = @("-m", "streamlit", "run", "app/app.py", "--server.port", "$port")
    } else {
        Write-Host "[!] 未找到 .venv,回退到全局 python(其 streamlit 若 <1.46,瀑布图渲染会报错)" -ForegroundColor Yellow
        $launcher = "python"
        $args = @("-m", "streamlit", "run", "app/app.py", "--server.port", "$port")
    }
    Write-Host -NoNewline "[->] 后台启动中 (端口 $port)... "
    $proc = Start-Process -FilePath $launcher -ArgumentList $args -WorkingDirectory $projectRoot `
        -NoNewWindow -PassThru -RedirectStandardOutput $accessLog -RedirectStandardError $errorLog
    $proc.Id | Out-File -FilePath $pidFile -NoNewline
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        $check = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
        if (-not $check -or $check.HasExited) { break }
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
            $ready = $true; break
        }
        Start-Sleep -Seconds 1
    }
    if ($ready) {
        Write-Host "成功! PID=$($proc.Id), 访问 http://localhost:$port" -ForegroundColor Green
    } else {
        Write-Host "失败! 查看日志: Get-Content $errorLog -Tail 30" -ForegroundColor Red
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        $check = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
        if ($check -and -not $check.HasExited) {
            $check.Kill(); $null = $check.WaitForExit(5000)
        }
    }
}

function Stop-Agent {
    $proc = Get-AgentProcess
    if (-not $proc) {
        Write-Host "[!] 未找到运行中的进程" -ForegroundColor Yellow
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        return
    }
    Write-Host -NoNewline "[->] 正在停止 PID=$($proc.Id)... "
    $proc.Kill()
    $null = $proc.WaitForExit(5000)
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    Write-Host "已停止" -ForegroundColor Green
}

function Restart-Agent {
    Write-Host "[->] 重启中..." -ForegroundColor Yellow
    $proc = Get-AgentProcess
    if ($proc) {
        $proc.Kill()
        $null = $proc.WaitForExit(5000)
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    Start-Agent
}

function Show-Status {
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  BI 归因分析 Agent (Streamlit)" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    $proc = Get-AgentProcess
    if ($proc) {
        Write-Host "  状态: [OK] 运行中" -ForegroundColor Green
        Write-Host "  PID : $($proc.Id)"
        Write-Host "  端口: $(Get-Port)"
        Write-Host "  访问: http://localhost:$(Get-Port)"
        Write-Host "  日志: $logDir"
    } else {
        Write-Host "  状态: [XX] 未运行" -ForegroundColor Red
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
    Write-Host "========================================" -ForegroundColor Cyan
}

function Show-Logs {
    Write-Host "=== 错误日志 ($errorLog) ===" -ForegroundColor Cyan
    if (Test-Path $errorLog) {
        Get-Content $errorLog -Tail 30
    } else {
        Write-Host "(暂无)" -ForegroundColor DarkGray
    }
    Write-Host ""
    Write-Host "=== 运行日志 ($accessLog) ===" -ForegroundColor Cyan
    if (Test-Path $accessLog) {
        Get-Content $accessLog -Tail 30
    } else {
        Write-Host "(暂无)" -ForegroundColor DarkGray
    }
}

# 主菜单
while ($true) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  BI 归因分析 Agent — 管理菜单" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    $proc = Get-AgentProcess
    if ($proc) {
        Write-Host "  当前状态: [OK] 运行中 (PID=$($proc.Id), 端口=$(Get-Port))" -ForegroundColor Green
    } else {
        Write-Host "  当前状态: [XX] 未运行" -ForegroundColor Red
    }
    Write-Host "----------------------------------------" -ForegroundColor Cyan
    Write-Host "  1. 启动服务"
    Write-Host "  2. 重启服务"
    Write-Host "  3. 停止服务"
    Write-Host "  4. 查看状态"
    Write-Host "  5. 查看日志"
    Write-Host "  0. 退出"
    Write-Host "========================================" -ForegroundColor Cyan
    $choice = Read-Host "请输入选项 [0-5]"

    switch ($choice) {
        "1" { Start-Agent }
        "2" { Restart-Agent }
        "3" { Stop-Agent }
        "4" { Show-Status }
        "5" { Show-Logs }
        "0" { Write-Host "再见!"; exit 0 }
        default { Write-Host "[!] 无效选项，请输入 0-5" -ForegroundColor Yellow }
    }
}
# 文件服务：用 Python 自带的 http.server 把 runs\ 目录共享到局域网，测试人员用浏览器看生成结果。
# install：注册成开机自启的计划任务（SYSTEM 身份，不用登录），并放行防火墙端口；uninstall：全部撤掉。
param([ValidateSet("install", "uninstall")][string]$Action = "install", [int]$Port = 8000)
$ErrorActionPreference = "Stop"
$Name = "评测文件服务"
$Root = if ($env:EVAL_FILE_ROOT) { $env:EVAL_FILE_ROOT } else { Join-Path $PSScriptRoot "runs" }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "需要管理员权限：右键这个 bat，选「以管理员身份运行」"; exit 1 }

if ($Action -eq "uninstall") {
    Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue
    Write-Host "已停止并删除文件服务（计划任务和防火墙规则）。runs\ 里的文件没有动。"
    exit 0
}

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py -or $py -like "*WindowsApps*") {
    Write-Host "找不到 Python（或者只找到了应用商店的占位程序）。先装 Python 并加到 PATH。"; exit 1
}
New-Item -ItemType Directory -Force -Path $Root | Out-Null

$act = New-ScheduledTaskAction -Execute $py -Argument "-m http.server $Port --bind 0.0.0.0 --directory `"$Root`"" -WorkingDirectory $Root
$trg = New-ScheduledTaskTrigger -AtStartup
$pri = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $Name -Action $act -Trigger $trg -Principal $pri -Settings $set -Force | Out-Null

Remove-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $Name -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null

Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $Name
Write-Host "文件服务已安装并启动，开机会自动运行。共享目录：$Root"
Write-Host "局域网内用浏览器打开（任选一个本机地址）："
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    ForEach-Object { Write-Host "  http://$($_.IPAddress):$Port/" }
Write-Host "运行评测前设置一次 EVAL_FILE_URL（例如 setx EVAL_FILE_URL http://上面的地址:$Port），生成情况.md 里就会是可点击的链接。"

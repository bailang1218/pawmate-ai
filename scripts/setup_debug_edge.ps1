<#
  setup_debug_edge.ps1
  一次性把 Edge 配成「带调试端口、用你的真实 profile」启动，并消除后台常驻抢占。

  注意：这个脚本会关闭所有 Edge 窗口并写入 HKCU Edge 策略。它只作为人工装机步骤放置，
  PawMate 代码不会自动调用它。
#>

$ErrorActionPreference = "Stop"
$port = 9222

Write-Host "== PawMate 调试 Edge 一次性设置 ==" -ForegroundColor Cyan

$edge = Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe" }
if (-not (Test-Path $edge)) { throw "找不到 msedge.exe，请确认已安装 Microsoft Edge。" }
Write-Host "Edge: $edge"

$pol = "HKCU:\Software\Policies\Microsoft\Edge"
New-Item -Path $pol -Force | Out-Null
Set-ItemProperty -Path $pol -Name "StartupBoostEnabled"   -Value 0 -Type DWord
Set-ItemProperty -Path $pol -Name "BackgroundModeEnabled" -Value 0 -Type DWord
Write-Host "已关闭 启动增强 / 后台常驻（HKCU 策略）" -ForegroundColor Green

$running = Get-Process msedge -ErrorAction SilentlyContinue
if ($running) {
    Write-Warning "即将关闭所有 Edge 窗口以清除 profile 占用。3 秒后继续，Ctrl+C 可取消..."
    Start-Sleep -Seconds 3
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
}
Write-Host "无残留 msedge 进程" -ForegroundColor Green

Start-Process $edge -ArgumentList "--remote-debugging-port=$port"
Write-Host "已以调试模式启动 Edge（真实 profile，端口 $port）"

$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$port/json/version" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
if ($ok) {
    Write-Host "调试端口已就绪：http://127.0.0.1:$port/json/version" -ForegroundColor Green
} else {
    Write-Warning "端口未就绪。可能仍有 msedge 残留进程占用 profile，或被安全软件拦截。请重跑本脚本。"
}

$lnkPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Edge (调试模式).lnk"
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $edge
$lnk.Arguments  = "--remote-debugging-port=$port"
$lnk.IconLocation = "$edge,0"
$lnk.Save()
Write-Host "已创建桌面快捷方式：Edge (调试模式)" -ForegroundColor Green

Write-Host ""
Write-Host "完成。以后想让 PawMate 接管真实浏览器，就用这个「Edge (调试模式)」快捷方式开 Edge。" -ForegroundColor Cyan
Write-Host "config.json 里设：attach_mode=attach, cdp_url=http://127.0.0.1:$port, profile_directory=native"


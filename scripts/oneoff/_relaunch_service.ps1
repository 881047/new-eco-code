# NEW ECO service launcher (agent use; 2026-09-05 hardened):
# pre-checks port 8787 so an old process can't fake "ready"; wraps uvicorn in
# cmd /c so >> redirect binds to the uvicorn process (2026-09-03 lesson).
# NOTE: keep this file ASCII-only - PowerShell 5.1 reads no-BOM UTF-8 as GBK
# and Chinese comments can swallow line breaks.
# NOTE (2026-09-06 N9-4): interpreter path D:\Python313\python.exe is hardcoded
# FOR THIS MACHINE ONLY; do not copy this launcher to another host.
# 2026-09-15: switched project interpreter 3.11.9 -> 3.13.15 (.venv now 3.13).
$port = 8787
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
  Write-Output "already-running"
  exit 0
}
$env:PYTHONPATH = 'D:\new eco\.venv\Lib\site-packages'
Add-Content -Path 'D:\new eco\logs\launch.log' -Value "$(Get-Date -Format 'yyyy/MM/dd HH:mm:ss') manual start (NEW ECO)"
Start-Process -FilePath 'cmd' -ArgumentList '/c', '""D:\Python313\python.exe" -u -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8787 >> "D:\new eco\logs\server.log" 2>&1"' -WorkingDirectory 'D:\new eco' -WindowStyle Minimized
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Seconds 3
  try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/overview' -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch {}
}
if ($ok) {
  Add-Content -Path 'D:\new eco\logs\launch.log' -Value "$(Get-Date -Format 'yyyy/MM/dd HH:mm:ss') service ready"
} else {
  Add-Content -Path 'D:\new eco\logs\launch.log' -Value "$(Get-Date -Format 'yyyy/MM/dd HH:mm:ss') startup timeout"
}
Write-Output "ready=$ok"

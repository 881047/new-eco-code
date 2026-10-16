@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Port single source (2026-09-22): NEWECO_PORT wins; the 8787 default MUST match
rem app/config.py DEFAULT_SERVER_PORT -- tests/test_port_single_source.py guards that.
if not defined NEWECO_PORT set "NEWECO_PORT=8787"
rem 2026-09-13 (07 #29): the logon autostart / hidden launcher (scripts\start_hidden.vbs)
rem passes --hidden. A hidden window must never pause -- nobody can see it to close it.
if /i "%~1"=="--hidden" set "NE_HIDDEN=1"
set LOG=%~dp0logs\launch.log
rem NOTE (2026-09-13): keep batch *control flow* and *comments* ASCII-only. A Chinese
rem rem-line right above this block made cmd silently swallow the next line (the status
rem echo never ran). Same reason scripts/watchdog_task.cmd is English-only.
if defined NE_HIDDEN echo %date% %time% 登录自启-隐藏窗口 >> "%LOG%"
if not defined NE_HIDDEN echo %date% %time% 快捷方式启动 >> "%LOG%"
rem Service is already up -> just open the dashboard (probe via venv python, no curl).
.venv\Scripts\python.exe -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:%NEWECO_PORT%/api/overview',timeout=2)" 2>nul && goto open
echo 正在启动 NEW ECO 服务(冷启动约10-20秒)...
echo %date% %time% 未检测到服务, 开始启动 >> "%LOG%"
rem Start the server minimized; output lands in logs\server.log. Do not close that window
rem (closing it stops the service).
rem Fix (2026-09-03): `start` does not forward the >> redirection to the child process,
rem and the venv python.exe is a forwarding stub (its pipe forwarding once blocked the
rem console write and hung the log). So: resolve the real interpreter, use the venv
rem site-packages, and wrap in `cmd /c` so the redirection binds to the uvicorn process.
rem 2026-09-04 review P3-2: the interpreter path is no longer hard-coded -- it is parsed
rem from the `home` key of .venv\pyvenv.cfg (survives a move to another drive/machine),
rem falling back to the venv's own launcher (.venv\Scripts\python.exe) only when that fails.
rem 2026-09-15: the fallback used to be a hard-coded D:\python\python.exe; after the 3.13
rem migration that path points at a DIFFERENT (stale) install, so a pyvenv.cfg parse failure
rem would silently start the service on the wrong interpreter. Fall back to the venv's own
rem launcher instead -- it always resolves to whichever base interpreter the venv was built from.
set "PYTHONPATH=%~dp0.venv\Lib\site-packages"
set "REALPY="
for /f "usebackq tokens=1,* delims== " %%a in ("%~dp0.venv\pyvenv.cfg") do if /i "%%a"=="home" set "REALPY=%%b\python.exe"
if not defined REALPY set "REALPY=%~dp0.venv\Scripts\python.exe"
if not exist "%REALPY%" (
  echo interpreter not found: %REALPY% -- check the home key of .venv\pyvenv.cfg
  echo %date% %time% 找不到真实解释器 %REALPY% >> "%LOG%"
  if not defined NE_HIDDEN pause
  exit /b 1
)
rem 2026-09-14 (07 #44): a hidden start must NOT use `start /min` -- minimized != invisible
rem (IsWindowVisible() is still true, so the user gets a persistent minimized console:
rem  measured 08:35:43 "cmd.exe class=ConsoleWindowClass visible=Y title='NEW ECO SERVER...'").
rem In hidden mode hand off to the watchdog entry: it spawns the server with DETACHED_PROCESS
rem (no console at all) and now also re-probes liveness within a grace window (07 #38).
if defined NE_HIDDEN goto start_hidden
rem 2026-10-15 (07 #68 todo): cold start now ALSO routes through scripts\service_supervisor.py
rem so shortcut-launched instances get exit-code forensics (same as watchdog launches).
rem Tested end-to-end 2026-10-15 21:23 (probe=200; kill -> exit code logged 0xFFFFFFFF).
start "NEW ECO SERVER - 请勿关闭" /min cmd /c ""%REALPY%" -u -X utf8 "%~dp0scripts\service_supervisor.py" "%REALPY%" -u -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port %NEWECO_PORT% >> "%~dp0logs\server.log" 2>&1"
goto after_start
:start_hidden
wscript //B "%~dp0scripts\watchdog_task_hidden.vbs"
:after_start
rem Poll until the service answers, at most 90 seconds.
set /a tries=0
:wait
timeout /t 3 /nobreak >nul
.venv\Scripts\python.exe -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:%NEWECO_PORT%/api/overview',timeout=2)" 2>nul && goto open
set /a tries+=1
if %tries% lss 30 goto wait
echo %date% %time% 启动超时 >> "%LOG%"
echo 服务启动超时, 请查看 logs\server.log 的报错信息
if not defined NE_HIDDEN pause
exit /b 1
:open
echo %date% %time% 打开看板 >> "%LOG%"
start http://127.0.0.1:%NEWECO_PORT%

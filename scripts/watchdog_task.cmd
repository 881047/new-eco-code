@echo off
rem NEW ECO watchdog task entry (2026-09-13)
rem
rem NOTE: this file must stay ASCII-only AND must keep CRLF line endings.
rem 2026-09-13: the first version was written with LF endings (apply_patch writes LF)
rem and cmd.exe mis-parsed it mid-line ("EALPY is not recognized", "ODING is not
rem recognized"). start.bat has always had CRLF, which is why that one works.
rem
rem Why a wrapper instead of putting python in the scheduled task: scheduled tasks
rem have no console, and .venv\Scripts\python.exe is a forwarding stub (past incident:
rem console write blocked -> log deadlock). Same approach as start.bat: resolve the
rem real interpreter from .venv\pyvenv.cfg 'home' and redirect output to a real file.
rem
rem Exit codes: 0 alive/restarted, 1 unreachable but cooling down, 2 restart failed,
rem 3 interpreter not found.
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\.venv\Lib\site-packages"
set "PYTHONIOENCODING=utf-8"
set "REALPY="
for /f "usebackq tokens=1,* delims== " %%a in (".venv\pyvenv.cfg") do if /i "%%a"=="home" set "REALPY=%%b\python.exe"
rem 2026-09-22: align the fallback with start.bat:36 -- it falls back to the venv's own
rem launcher. This file used to hardcode D:\Python313, which breaks on another machine;
rem both launch paths should share one fallback rule.
if not defined REALPY set "REALPY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%REALPY%" (
  echo %date% %time% interpreter not found "%REALPY%" >> "logs\watchdog_task.log"
  exit /b 3
)
"%REALPY%" -X utf8 "scripts\service_watchdog.py" >> "logs\watchdog_task.log" 2>&1
exit /b %ERRORLEVEL%

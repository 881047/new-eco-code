@echo off
rem NEW ECO baostock chain task entry (2026-10-13 rebuild).
rem
rem NOTE: this file must stay ASCII-only AND must keep CRLF line endings.
rem Same rule and same reason as watchdog_task.cmd: cmd.exe mis-parses LF-only
rem files mid-line under the scheduled-task code page.
rem
rem Why a wrapper instead of putting python straight into the scheduled task:
rem scheduled tasks have no console, and .venv\Scripts\python.exe is a
rem forwarding stub (past incident: console write blocked -> log deadlock).
rem Same approach as start.bat / watchdog_task.cmd: resolve the real
rem interpreter from .venv\pyvenv.cfg 'home' and redirect output to a real file.
rem
rem cwd is set to the repo root (same as start.bat). The supervisor launches the
rem stage scripts WITHOUT cwd, so they inherit this directory; both stage scripts
rem use absolute paths internally on purpose (see backfill_baostock_witness.py).
rem
rem Exit codes: 0 normal (including "nothing to do"), 2 failed to launch guard,
rem 3 interpreter not found.
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\.venv\Lib\site-packages"
set "PYTHONIOENCODING=utf-8"
set "REALPY="
for /f "usebackq tokens=1,* delims== " %%a in (".venv\pyvenv.cfg") do if /i "%%a"=="home" set "REALPY=%%b\python.exe"
if not defined REALPY set "REALPY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%REALPY%" (
  echo %date% %time% interpreter not found "%REALPY%" >> "logs\baostock_chain_task.log"
  exit /b 3
)
"%REALPY%" -X utf8 "scripts\baostock_chain_supervise.py" >> "logs\baostock_chain_task.log" 2>&1
exit /b %ERRORLEVEL%

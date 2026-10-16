@echo off
rem OLD-project monthly chain check (read-only, one-shot 2026-10-01 19:00, W2-F64).
rem ASCII-only comments: cmd parses bat under OEM codepage (registered pitfall).
cd /d D:\NEW ECO
if not exist data\logs mkdir data\logs
echo === %date% %time% monthly chain check start === >> data\logs\monthly_chain_check.log
.venv\Scripts\python.exe -X utf8 scripts\check_monthly_chain.py >> data\logs\monthly_chain_check.log 2>&1
rem 2026-10-13: echo resets ERRORLEVEL to 0 -- capture it first, then propagate.
set "RC=%ERRORLEVEL%"
echo [check exit %RC%] === %date% %time% === >> data\logs\monthly_chain_check.log
exit /b %RC%

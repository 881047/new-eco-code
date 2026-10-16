@echo off
rem NEW ECO satellite rebal cohort tracker daily run (v2 prereg plans/index-rebal-satellite-v2-prereg-20260928.md).
rem ASCII-only comments: cmd parses bat under OEM codepage (registered pitfall).
cd /d D:\NEW ECO
if not exist data\logs mkdir data\logs
if not exist data\satellite_rebal mkdir data\satellite_rebal
echo === %date% %time% satellite rebal start === >> data\logs\satellite_rebal.log
set "RC=0"
.venv\Scripts\python.exe -X utf8 scripts\satellite_rebal_paper.py --run >> data\logs\satellite_rebal.log 2>&1
if not "%ERRORLEVEL%"=="0" if "%RC%"=="0" set "RC=%ERRORLEVEL%"
.venv\Scripts\python.exe -X utf8 scripts\satellite_rebal_paper.py --run >> data\logs\satellite_rebal.log 2>&1
if not "%ERRORLEVEL%"=="0" if "%RC%"=="0" set "RC=%ERRORLEVEL%"
.venv\Scripts\python.exe -X utf8 scripts\satellite_rebal_paper.py --mark >> data\logs\satellite_rebal.log 2>&1
if not "%ERRORLEVEL%"=="0" if "%RC%"=="0" set "RC=%ERRORLEVEL%"
.venv\Scripts\python.exe -X utf8 scripts\satellite_rebal_paper.py --mark >> data\logs\satellite_rebal.log 2>&1
if not "%ERRORLEVEL%"=="0" if "%RC%"=="0" set "RC=%ERRORLEVEL%"
rem 2026-10-13: echo resets ERRORLEVEL to 0 -- capture it first, then propagate.
echo [satellite exit %RC%] === %date% %time% === >> data\logs\satellite_rebal.log
exit /b %RC%

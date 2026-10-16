' Silent wrapper for task NEWECO_satellite_rebal_daily (added 2026-09-30).
' Runs the frozen bat with no visible console window, waits, propagates exit code. ASCII only.
WScript.Quit CreateObject("WScript.Shell").Run("cmd.exe /c """"D:\NEW ECO\scripts\satellite_rebal_daily.bat""""", 0, True)

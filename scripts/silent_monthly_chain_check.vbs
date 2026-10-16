' Silent wrapper for task NEWECO_monthly_chain_check (added 2026-09-30).
' Runs the frozen bat with no visible console window, waits, propagates exit code. ASCII only.
WScript.Quit CreateObject("WScript.Shell").Run("cmd.exe /c """"D:\NEW ECO\scripts\check_monthly_chain.bat""""", 0, True)

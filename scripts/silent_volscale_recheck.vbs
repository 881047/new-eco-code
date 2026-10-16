' Silent wrapper for task NEWECO_volscale_recheck (created 2026-10-03, one-shot 2026-12-27 19:00).
' Runs the volscale quarterly recheck (frozen criteria, dynamic current-pin NAV source) with no visible console. ASCII only.
WScript.Quit CreateObject("WScript.Shell").Run("cmd.exe /c """"D:\NEW ECO\.venv\Scripts\python.exe"" -X utf8 -u ""D:\NEW ECO\scripts\volscale_recheck_runner.py"" >> ""D:\NEW ECO\logs\volscale_recheck.log"" 2>&1""", 0, True)

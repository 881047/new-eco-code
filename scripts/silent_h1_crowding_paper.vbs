' Silent wrapper for task NEWECO_h1_crowding_paper_daily (created 2026-10-03).
' H1 crowding-reversal 6-month forward paper runner (prereg plans/h1-crowding-6m-paper-prereg-20261003.md). ASCII only.
WScript.Quit CreateObject("WScript.Shell").Run("cmd.exe /c """"D:\NEW ECO\.venv\Scripts\python.exe"" -X utf8 -u ""D:\NEW ECO\scripts\h1_crowding_paper.py"" >> ""D:\NEW ECO\logs\h1_crowding_paper.log"" 2>&1""", 0, True)

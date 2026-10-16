' Silent wrapper for task NEWECO_trough_tilt_observer_monthly (created 2026-10-01).
' T1 forward observer: monthly block-IC trigger status + realized mirror spreads.
' Frozen trigger def = plans/trough-tilt-prereg-20260930.md. Ledger 05 no.52. ASCII only.
WScript.Quit CreateObject("WScript.Shell").Run("cmd.exe /c """"D:\NEW ECO\.venv\Scripts\python.exe"" -X utf8 -u ""D:\NEW ECO\scripts\trough_tilt_observer.py"" --run >> ""D:\NEW ECO\logs\trough_tilt_observer.log"" 2>&1""", 0, True)

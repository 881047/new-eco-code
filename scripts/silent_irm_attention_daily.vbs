' Silent wrapper for task NEWECO_irm_attention_daily (created 2026-10-02).
' IRM attention forward tracker: daily rolling-window Q&A harvest for the top-300
' scored pool (prereg plans/irm-attention-family-prereg-20261002.md). ASCII only.
WScript.Quit CreateObject("WScript.Shell").Run("cmd.exe /c """"D:\NEW ECO\.venv\Scripts\python.exe"" -X utf8 -u ""D:\NEW ECO\scripts\irm_attention_tracker.py"" >> ""D:\NEW ECO\logs\irm_attention.log"" 2>&1""", 0, True)

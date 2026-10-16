' Silent wrapper for task NEWECO_fulltext_s3 (recreated 2026-09-30, 8-shard speedup).
' Same frozen command as the original task action, run with no visible console window. ASCII only.
' -u added 2026-09-30: stderr block-buffering made the log lag minutes behind reality.
WScript.Quit CreateObject("WScript.Shell").Run("cmd.exe /c """"D:\NEW ECO\.venv\Scripts\python.exe"" -X utf8 -u ""D:\NEW ECO\scripts\collect_text_fulltext.py"" --all --shard 3 --nshards 8 --time-box 1500 >> ""D:\NEW ECO\logs\fulltext_s3.log"" 2>&1""", 0, True)

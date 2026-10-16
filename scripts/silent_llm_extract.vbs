' Silent wrapper for task NEWECO_llm_extract (added 2026-09-30, full-scale LLM field extraction).
' Runs the --all mode with a 25-min time-box; checkpoint/resume via manifest_llm_all.json. ASCII only.
WScript.Quit CreateObject("WScript.Shell").Run("cmd.exe /c """"D:\NEW ECO\.venv\Scripts\python.exe"" -X utf8 -u ""D:\NEW ECO\scripts\extract_fulltext_fields_llm.py"" --all --workers 4 --time-box 1500 --fewshot -1 >> ""D:\NEW ECO\logs\llm_extract.log"" 2>&1""", 0, True)

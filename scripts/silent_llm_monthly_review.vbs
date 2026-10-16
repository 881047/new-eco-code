' Silent wrapper for task NEWECO_llm_monthly_review (created 2026-10-02).
' P3 monthly review: deterministic fact sheet + mechanical checks first,
' LLM narrative layer (fail-open to mechanical-only on any LLM/network failure).
' LLM output is human-readable only, never enters any decision chain. ASCII only.
WScript.Quit CreateObject("WScript.Shell").Run("cmd.exe /c """"D:\NEW ECO\.venv\Scripts\python.exe"" -X utf8 -u ""D:\NEW ECO\scripts\llm_monthly_review.py"" >> ""D:\NEW ECO\logs\llm_monthly_review.log"" 2>&1""", 0, True)

' NEW ECO baostock chain hidden launcher (2026-10-13 rebuild).
'
' Why this exists: the scheduled task is registered "Interactive only", so
' running a console program directly flashes a window on the desktop. wscript
' runs this file in batch mode (//B) and Run(..., 0, ...) hides the window,
' while the .cmd keeps resolving the real interpreter and redirecting output
' to a log. Same pattern as watchdog_task_hidden.vbs.
'
' Register / update the task (no admin needed):
'   schtasks /change /tn "NEW ECO Baostock Chain" /tr "wscript.exe //B \"D:\NEW ECO\scripts\baostock_chain_hidden.vbs\""
'
' This file must stay ASCII-only AND keep CRLF line endings (non-ASCII breaks
' parsing in the scheduled-task code page -- same rule as watchdog_task_hidden.vbs).
Option Explicit
Dim fso, sh, here, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)      ' ...\scripts
cmd = "cmd.exe /c " & Chr(34) & here & "\baostock_chain_task.cmd" & Chr(34)
Set sh = CreateObject("WScript.Shell")
' 0 = hidden window; False = do not wait (the supervisor writes its own log)
sh.Run cmd, 0, False

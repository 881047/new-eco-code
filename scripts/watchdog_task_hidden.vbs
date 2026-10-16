' NEW ECO watchdog hidden launcher (2026-09-13)
'
' Why this exists: the scheduled task used to run `watchdog_task.cmd` directly, and
' the task is registered "Interactive only" -> a console window flashed on the
' desktop every 10 minutes (user report: "a window pops up and disappears").
' wscript runs this file in batch mode (//B) and Run(..., 0, ...) hides the window,
' while the .cmd keeps resolving the real interpreter and redirecting output to a log.
'
' Register / update the task (no admin needed):
'   schtasks /change /tn "NEW ECO Watchdog" /tr "wscript.exe //B \"D:\NEW ECO\scripts\watchdog_task_hidden.vbs\""
'
' This file must stay ASCII-only (same rule as watchdog_task.cmd: non-ASCII breaks
' parsing in the scheduled-task code page).
Option Explicit
Dim fso, sh, here, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)      ' ...\scripts
cmd = "cmd.exe /c " & Chr(34) & here & "\watchdog_task.cmd" & Chr(34)
Set sh = CreateObject("WScript.Shell")
' 0 = hidden window; False = do not wait (the watchdog writes its own log)
sh.Run cmd, 0, False

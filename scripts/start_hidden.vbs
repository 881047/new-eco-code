' NEW ECO hidden starter (2026-09-13)
'
' Why this exists: the Startup-folder entry and the desktop shortcut ran `start.bat`
' (a batch file) directly, so every logon flashed a console window that closed as
' soon as the dashboard was opened -- the user report "a window pops up and
' disappears", same symptom as incident 07 #27 but a *different* source (that one
' was the 10-minute watchdog scheduled task).
'
' Usage: wscript.exe //B "D:\NEW ECO\scripts\start_hidden.vbs"
' `--hidden` only tells start.bat to skip the `pause` prompts: an invisible window
' waiting on a keypress would hang forever with nobody able to see it.
'
' This file must stay ASCII-only (same rule as watchdog_task_hidden.vbs).
Option Explicit
Dim fso, sh, root, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
' ...\scripts -> ...\ (repo root)
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
cmd = "cmd.exe /c " & Chr(34) & root & "\start.bat" & Chr(34) & " --hidden"
Set sh = CreateObject("WScript.Shell")
' 0 = hidden window; False = do not wait (start.bat opens the dashboard when ready)
sh.Run cmd, 0, False

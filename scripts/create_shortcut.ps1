# 在桌面创建快捷方式(指向本仓库 start.bat; 2026-09-04 改名 NEW ECO, 按脚本位置解析不硬编码盘符)
# NOTE (2026-09-13, 07 #29/#31): comments below stay ASCII. Windows PowerShell 5.1 reads a
# BOM-less .ps1 as system ANSI(936) -- UTF-8 Chinese then leaves a dangling lead byte that
# EATS the following newline, merging the next command into the comment (reproduced here:
# `$startup = ...` was swallowed and stayed $null). This file therefore carries a UTF-8 BOM
# *and* ASCII comments; tests/test_script_encoding.py keeps the rule for all scripts\*.ps1.
$root = Split-Path $PSScriptRoot -Parent
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'NEW ECO.lnk'
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = Join-Path $root 'start.bat'
$lnk.WorkingDirectory = $root
$lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$lnk.Description = 'NEW ECO 资产配置看板'
$lnk.Save()
Write-Output "快捷方式已创建: $lnkPath -> $($lnk.TargetPath)"

# Logon autostart (2026-09-13 07 #29, 2026-09-19 07 #60): NEVER put a .bat in the Startup
# folder -- Explorer launches it through cmd.exe, so a console window is created and SHOWN
# even when the batch itself only calls wscript (measured 2026-09-19 with diag_window_flash.py:
# a shell-launched probe .bat produced cmd.exe class=PseudoConsoleWindow visible=Y).
# The entry is therefore a .lnk straight to wscript.exe (GUI subsystem, no console at all)
# running scripts\start_hidden.vbs.
$startup = [Environment]::GetFolderPath('Startup')
$vbs = Join-Path $root 'scripts\start_hidden.vbs'
$startupLnk = Join-Path $startup 'NEW ECO.lnk'
$sl = $ws.CreateShortcut($startupLnk)
$sl.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
$sl.Arguments = '//B "' + $vbs + '"'
$sl.WorkingDirectory = $root
$sl.WindowStyle = 7
$sl.Description = 'NEW ECO 登录自启(无控制台)'
$sl.Save()
Write-Output "登录自启已安装(无控制台): $startupLnk -> wscript.exe $($sl.Arguments)"
# Upgrade path: drop the legacy batch entry, otherwise a machine that ran the old installer
# keeps flashing a console on every logon (two entries, one of them a .bat).
$legacyBat = Join-Path $startup 'NEW ECO.bat'
if (Test-Path -LiteralPath $legacyBat) {
    Remove-Item -LiteralPath $legacyBat -Force
    Write-Output "已删除旧批处理自启(会闪窗): $legacyBat"
}

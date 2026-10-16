# 检查桌面快捷方式与开机自启指向(2026-09-04: 项目迁至 D:\NEW ECO, 快捷方式已改名 NEW ECO.lnk)
# 2026-09-14 按 07 #39 修：原版取 bat 里匹配 'start\.bat' 的那行再 .ToString() —— 自启链改成
# "wscript //B start_hidden.vbs" 后该行不再匹配 ⇒ $null.ToString() 抛异常，而脚本仍以
# **退出码 0** 结束（"看着跑完了，其实什么都没验证"）。现在只认当前链路，任何不符 ⇒ 退出码 1。
$fail = 0
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop 'NEW ECO.lnk'
$lnk = $ws.CreateShortcut($lnkPath)
Write-Output ("桌面快捷方式: " + $lnkPath)
Write-Output ("  Target: " + $lnk.TargetPath)
Write-Output ("  WorkDir: " + $lnk.WorkingDirectory)
if (-not (Test-Path $lnk.TargetPath)) { Write-Output "  x Target 不存在"; $fail = 1 }

# 2026-09-19 (07 #60): 登录入口从 Startup\NEW ECO.bat 改成 Startup\NEW ECO.lnk -> wscript.exe。
# Explorer 拉起 .bat 会开一个**可见**控制台（哪怕里面只调 wscript），所以现在只认"快捷方式 +
# wscript 宿主"，并把回潮的 .bat 显式判红。
$startup = [Environment]::GetFolderPath('Startup')
$legacyBat = Join-Path $startup 'NEW ECO.bat'
if (Test-Path -LiteralPath $legacyBat) {
    Write-Output ("  x 启动文件夹里又有批处理入口（登录必闪控制台）: " + $legacyBat)
    $fail = 1
}
$startupLnk = Join-Path $startup 'NEW ECO.lnk'
if (-not (Test-Path -LiteralPath $startupLnk)) { Write-Output ("x 开机自启快捷方式不存在: " + $startupLnk); exit 1 }
$sl = $ws.CreateShortcut($startupLnk)
Write-Output ("开机自启: " + $startupLnk)
Write-Output ("  Target: " + $sl.TargetPath + " " + $sl.Arguments)
if ([IO.Path]::GetFileName($sl.TargetPath) -ne 'wscript.exe') {
    Write-Output "  x 自启目标不是 wscript.exe（控制台程序会被 Windows 新建一个窗口）—— 见 07 #60"
    $fail = 1
} else {
    Write-Output "  ok 宿主 wscript.exe（GUI 子系统，无控制台）"
}
$m = [regex]::Match($sl.Arguments, '"([A-Za-z]:\\[^"]*start_hidden\.vbs)"')
if ($m.Success) {
    $vbs = $m.Groups[1].Value
    if (Test-Path -LiteralPath $vbs) { Write-Output ("  ok 启动器存在: " + $vbs) }
    else { Write-Output ("  x 启动器不存在: " + $vbs); $fail = 1 }
} else {
    Write-Output "  x 参数里解析不出 start_hidden.vbs 路径"
    $fail = 1
}
if ($fail -ne 0) { Write-Output "结论: 自启链有问题（见上面带 x 的项）"; exit 1 }
Write-Output "结论: 桌面快捷方式与开机自启链正常"
exit 0

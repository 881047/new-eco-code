#Requires -Version 5.1
<#
    NEW ECO Watchdog - idempotent scheduled-task installer (2026-09-22)

    WHY THIS EXISTS
    ---------------
    Enabling the self-healing watchdog was documented as a HAND-TYPED schtasks
    command, and that documented command did not match the task that actually
    works on this machine:

      * documented  : /tr "python.exe -X utf8 scripts\service_watchdog.py"
                      -> scheduled tasks have NO console; a python.exe entry
                         flashes a console window every 10 minutes (incident 07 #27)
      * actually run: wscript.exe //B "scripts\watchdog_task_hidden.vbs"
                      -> hidden launcher, no console at all

    Re-installing from the docs would silently reintroduce the flashing window.
    This script encodes the KNOWN-GOOD definition instead.

    WHY XML INSTEAD OF `schtasks /create ... /tr "..."`
    --------------------------------------------------
    The command line for this task contains a path WITH SPACES and needs nested
    quoting:  wscript.exe //B "D:\NEW ECO\scripts\watchdog_task_hidden.vbs"
    Passing that through cmd.exe requires backslash-escaped inner quotes
    (\"...\"), and getting it subtly wrong produces a task that LOOKS created
    but never runs. An earlier draft of this very script had exactly that bug.
    Using /xml removes shell quoting from the equation entirely, and has the
    side benefit of letting us set the battery settings in the same step
    (schtasks /create has no switch for them).

    DESIGN NOTES (each one is a past incident)
    ------------------------------------------
    * Entry point MUST be the hidden launcher (wscript.exe //B <vbs>), never
      python.exe / cmd.exe directly          -> 07 #27 (console flash)
    * Trigger is a TimeTrigger with PT10M repetition. Do NOT add a logon
      trigger: login autostart is a separate Startup-folder shortcut, and a
      second trigger would start the service twice          -> 07 #39
    * DisallowStartIfOnBatteries / StopIfGoingOnBatteries = false, otherwise a
      laptop on battery silently stops self-healing          -> README:222
    * Runs as the current user (InteractiveToken) => NO admin required.
    * After install, VERIFY THE SIDE EFFECT, not just the exit code
      -> 07 #61: a task can register fine and still never run.
    * Pure ASCII on purpose: PS 5.1 reads non-BOM files as ANSI and multi-byte
      text can swallow the next line (07 #30/#31); tests/test_script_encoding.py
      enforces it.

    USAGE
    -----
      powershell -ExecutionPolicy Bypass -File scripts\install_watchdog.ps1 -DryRun
      powershell -ExecutionPolicy Bypass -File scripts\install_watchdog.ps1
      powershell -ExecutionPolicy Bypass -File scripts\install_watchdog.ps1 -Verify
      powershell -ExecutionPolicy Bypass -File scripts\install_watchdog.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$DryRun,      # print what would happen; change nothing
    [switch]$Verify,      # only report current state
    [switch]$Uninstall    # remove the task (turns self-healing off)
)

$ErrorActionPreference = 'Stop'
$TaskName = 'NEW ECO Watchdog'
$Root     = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Vbs      = Join-Path $Root 'scripts\watchdog_task_hidden.vbs'
$Wlog     = Join-Path $Root 'logs\watchdog_task.log'
$User     = "$env:USERDOMAIN\$env:USERNAME"

function Write-Step($m) { Write-Host "  $m" }
function Write-Head($m) { Write-Host "== $m ==" }

# NOTE: do NOT parse `schtasks /query /fo LIST /v` text. On a non-English Windows
# the field labels come back localized, so an English regex silently matches
# nothing -- an earlier draft of this script did exactly that and reported
# "entry point is not wscript.exe" for a perfectly good task.
# The ScheduledTask cmdlets return objects, so they are language-independent.
function Get-TaskState {
    $t = $null
    try { $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch { return $null }
    if (-not $t) { return $null }
    $i = $null
    try { $i = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop } catch { }
    $act = @($t.Actions) | Select-Object -First 1
    $exe = ''
    $arg = ''
    if ($act) { $exe = [string]$act.Execute; $arg = [string]$act.Arguments }
    $next = ''
    if ($i -and $i.NextRunTime) { $next = $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') }
    $last = ''
    if ($i) { $last = [string]$i.LastTaskResult }
    return @{
        Status      = [string]$t.State
        LastResult  = $last
        NextRunTime = $next
        Execute     = $exe
        Arguments   = $arg
        ToRun       = ("$exe $arg").Trim()
    }
}

# The known-good definition. Reproduced from the task that was verified working
# on this machine (2026-09-22): TimeTrigger/PT10M, wscript.exe //B <vbs>,
# battery settings off, no logon trigger.
function New-TaskXml {
    $start = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')
    $vbsXml = [System.Security.SecurityElement]::Escape($Vbs)
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>$start</Date>
    <Description>NEW ECO service watchdog: probe 127.0.0.1:8787, restart the service per start.bat if unreachable (5-min cooldown). Delete this task to stop self-healing.</Description>
    <URI>\$TaskName</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>$User</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
  </Settings>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>$start</StartBoundary>
      <Repetition>
        <Interval>PT10M</Interval>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>//B "$vbsXml"</Arguments>
    </Exec>
  </Actions>
</Task>
"@
}

# ---------------------------------------------------------------- Verify only
if ($Verify) {
    Write-Head "Verify: $TaskName"
    $st = Get-TaskState
    if (-not $st) { Write-Step "NOT INSTALLED"; exit 1 }
    Write-Step "Status        : $($st.Status)"
    Write-Step "Last Result   : $($st.LastResult)"
    Write-Step "Next Run Time : $($st.NextRunTime)"
    Write-Step "Task To Run   : $($st.ToRun)"
    if ($st.ToRun -notmatch 'wscript\.exe') {
        Write-Step "WARNING: entry point is not wscript.exe -> console window will flash (07 #27)"
    }
    if ($st.LastResult -and $st.LastResult -ne '0') {
        Write-Step "WARNING: Last Result != 0 -> watchdog is failing (07 #61)"
    }
    exit 0
}

# ---------------------------------------------------------------- Uninstall
if ($Uninstall) {
    Write-Head "Uninstall: $TaskName"
    if ($DryRun) { Write-Step "would run: schtasks /delete /tn `"$TaskName`" /f"; exit 0 }
    & schtasks /delete /tn $TaskName /f 2>&1 | ForEach-Object { Write-Step $_ }
    if (Get-TaskState) { Write-Step "FAILED: task still present"; exit 1 }
    Write-Step "removed. NOTE: self-healing is now OFF."
    exit 0
}

# ---------------------------------------------------------------- Install
Write-Head "Install: $TaskName"
Write-Step "repo root : $Root"
Write-Step "launcher  : $Vbs"
Write-Step "principal : $User (InteractiveToken, no admin needed)"

if (-not (Test-Path $Vbs)) {
    Write-Step "FAILED: hidden launcher not found -> refusing to create a console-flashing task"
    exit 2
}

$xml = New-TaskXml

if ($DryRun) {
    Write-Step "would run: schtasks /delete /tn `"$TaskName`" /f   (ignore failure)"
    Write-Step "would run: schtasks /create /tn `"$TaskName`" /xml <generated> /f"
    Write-Step ""
    Write-Step "----- generated task XML -----"
    $xml -split "`n" | ForEach-Object { Write-Step $_.TrimEnd() }
    exit 0
}

$existing = Get-TaskState
if ($existing) {
    Write-Step "existing task found -> deleting first (idempotent reinstall)"
    & schtasks /delete /tn $TaskName /f 2>&1 | ForEach-Object { Write-Step $_ }
} else {
    Write-Step "no existing task"
}

# schtasks expects the XML file; write it as UTF-16 to match the declaration.
$tmp = Join-Path $env:TEMP ("neweco_watchdog_{0}.xml" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
$xml | Out-File -FilePath $tmp -Encoding Unicode
Write-Step "wrote task XML: $tmp"

& schtasks /create /tn $TaskName /xml $tmp /f 2>&1 | ForEach-Object { Write-Step $_ }
$rc = $LASTEXITCODE
Remove-Item $tmp -ErrorAction SilentlyContinue
if ($rc -ne 0) { Write-Step "FAILED: schtasks /create returned $rc"; exit 3 }

# VERIFY THE SIDE EFFECT, not just the exit code (07 #61)
Write-Head "Verify after install"
$st = Get-TaskState
if (-not $st) { Write-Step "FAILED: task not found after create"; exit 4 }
Write-Step "Status        : $($st.Status)"
Write-Step "Next Run Time : $($st.NextRunTime)"
Write-Step "Task To Run   : $($st.ToRun)"
if ($st.ToRun -notmatch 'wscript\.exe') {
    Write-Step "FAILED: entry point is not wscript.exe (07 #27)"; exit 5
}

$before = if (Test-Path $Wlog) { (Get-Item $Wlog).Length } else { -1 }
Write-Step "running once now (probe) ..."
& schtasks /run /tn $TaskName 2>&1 | ForEach-Object { Write-Step $_ }
Start-Sleep -Seconds 12
$after = if (Test-Path $Wlog) { (Get-Item $Wlog).Length } else { -1 }
if ($before -ge 0 -and $after -gt $before) {
    Write-Step "OK: $Wlog grew ($before -> $after bytes) => the task really ran"
} else {
    Write-Step "WARNING: log did not grow ($before -> $after) -> inspect:"
    Write-Step "         schtasks /query /tn `"$TaskName`" /fo LIST /v"
}

Write-Head "Done"
Write-Step "re-check any time: powershell -File scripts\install_watchdog.ps1 -Verify"
exit 0

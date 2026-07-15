# install_queue_monitor_task.ps1 — Register queue_monitor.py as a persistent
# Windows Task Scheduler service (live Pushover alerts + best-effort self-heal).
#
# Why: queue_monitor.py watches the Perplexity research FIFO queue snapshot +
# activity log and fires Pushover alerts (stalled holder, deep queue, long run,
# error/timeout, stalled-near-empty) plus a rate-limited keeper self-heal. To be
# useful it must run continuously in the background. Sibling of the
# PerplexitySessionKeeper / BrowserBridgeKeeper tasks.
#
# ARCHITECTURE DIFFERENCE vs the keepers: those are ONE-SHOT (check → act → exit)
# and rely on task repetition to re-run. queue_monitor.py is a PERSISTENT poll
# loop (its tail offsets + per-key alert dedup live in-process; --once seeds at
# EOF and detects nothing). So this task runs a long-lived pythonw daemon and
# uses MultipleInstances=IgnoreNew: the AtLogOn trigger starts it, and a 5-min
# repetition acts as a watchdog — if the daemon is alive the new instance is
# ignored, if it died the repetition relaunches it. No duplicate daemons.
#
# The daemon is launched with --daemon so pythonw's discarded stdout/stderr are
# redirected to ~/.claude/logs/queue_monitor.log (see _harden_for_windows_daemon).
#
# Run ONCE from PowerShell the first time (elevation only if your policy requires
# it for Register-ScheduledTask; the current-user Interactive principal usually
# does not). Re-running is idempotent (updates the task in place).
#
# Usage:
#   PowerShell -ExecutionPolicy Bypass -File install_queue_monitor_task.ps1
#   PowerShell -ExecutionPolicy Bypass -File install_queue_monitor_task.ps1 -Uninstall
#   PowerShell -ExecutionPolicy Bypass -File install_queue_monitor_task.ps1 -Interval 15 -NoPushover

param(
    [switch]$Uninstall,
    [switch]$NoPushover,
    [int]$Interval    = 15,
    [string]$TaskName = "PerplexityQueueMonitor",
    [string]$PythonW  = ""   # auto-detect if empty
)

$ErrorActionPreference = "Stop"

$monitorPath = "$HOME\.claude\council-automation\queue_monitor.py"
$logDir      = "$HOME\.claude\logs"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Removing scheduled task '$TaskName'..."
        # Stop a running instance first so the daemon actually exits.
        try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Done. The queue monitor daemon will no longer auto-start at logon."
        Write-Host "Any still-running pythonw daemon: find via"
        Write-Host "  Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { `$_.CommandLine -like '*queue_monitor*' }"
    } else {
        Write-Host "Task '$TaskName' not registered. Nothing to remove."
    }
    return
}

# Validate monitor script exists
if (-not (Test-Path $monitorPath)) {
    Write-Error "queue_monitor.py not found at: $monitorPath"
    exit 1
}

# Find pythonw.exe (no-console Python interpreter)
if (-not $PythonW) {
    $pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $pythonExe) {
        Write-Error "python not found on PATH. Pass -PythonW <path> explicitly."
        exit 1
    }
    $PythonW = Join-Path (Split-Path $pythonExe -Parent) "pythonw.exe"
    if (-not (Test-Path $PythonW)) {
        Write-Error "pythonw.exe not found next to python ($pythonExe). Pass -PythonW <path>."
        exit 1
    }
}
Write-Host "pythonw.exe: $PythonW"
Write-Host "monitor:     $monitorPath"

# Create log directory
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

# Build the action: pythonw.exe -u queue_monitor.py --daemon --interval N [--no-pushover]
$argList = "-u `"$monitorPath`" --daemon --interval $Interval"
if ($NoPushover) { $argList += " --no-pushover" }
$action = New-ScheduledTaskAction `
    -Execute $PythonW `
    -Argument $argList `
    -WorkingDirectory (Split-Path $monitorPath -Parent)

# Triggers: at logon (start when the user signs in) + every 5 minutes (watchdog
# relaunch if the daemon ever died). With MultipleInstances=IgnoreNew below, the
# repetition is a no-op while the daemon is alive.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"
$periodicTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 365)

# Run as the current user, "only when logged on" (Interactive) — same principal as
# the keepers. The monitor reads local queue files + imports research_monitor for
# Pushover/keeper-trigger, so it needs the interactive user's environment.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Settings: never spawn a second daemon (IgnoreNew), restart on failure, no time
# limit (persistent loop), allow on battery.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)   # 0 = no time limit

# Remove existing task if present (idempotent install)
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName' before re-registering..."
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Persistent background monitor for the Perplexity research FIFO queue: live Pushover alerts (stalls, deep queue, long runs, errors, stalled-near-empty) + rate-limited keeper self-heal. Long-lived pythonw daemon; 5-min repetition is a watchdog (IgnoreNew)." `
    -Action $action `
    -Trigger @($logonTrigger, $periodicTrigger) `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host ""
Write-Host "[OK] Task '$TaskName' registered."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Start it now (one-time):"
Write-Host "       Start-ScheduledTask -TaskName $TaskName"
Write-Host "  2. Verify it's running:"
Write-Host "       Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "  3. Watch the daemon log:"
Write-Host "       Get-Content `"$logDir\queue_monitor.log`" -Wait -Tail 20"
Write-Host ""
Write-Host "Uninstall later with:"
Write-Host "  PowerShell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"

# install_bridge_keeper_task.ps1 — Register browser_bridge_keeper.py as a Windows Task Scheduler task.
#
# Why: the browser-bridge MCP (remote Claude's browser automation) needs a Chrome
# with the "Claude Browser Bridge" MV3 extension to be running at all times so its
# WebSocket client can connect to the MCP server on ws://127.0.0.1:8765. This task
# keeps a DEDICATED, minimized bridge Chrome alive — launched at logon and
# re-launched by a periodic watchdog if it ever dies. Sibling of the
# PerplexitySessionKeeper task (install_keeper_task.ps1).
#
# Task Scheduler (not a long-lived daemon) is used for the same reasons as the
# Perplexity keeper: it invokes pythonw.exe with sane stdio, auto-restarts on
# failure, runs at logon, and runs as the logged-in interactive user so Chrome
# has a GUI session to render in. The keeper itself is ONE-SHOT (check → launch
# if dead → exit); the task repetition drives the watchdog.
#
# Run ONCE from an elevated PowerShell (Run as Administrator) the first time.
# Re-running is idempotent (updates the task in place).
#
# Usage:
#   PowerShell -ExecutionPolicy Bypass -File install_bridge_keeper_task.ps1
#   PowerShell -ExecutionPolicy Bypass -File install_bridge_keeper_task.ps1 -Uninstall

param(
    [switch]$Uninstall,
    [string]$TaskName = "BrowserBridgeKeeper",
    [string]$PythonW   = ""   # auto-detect if empty
)

$ErrorActionPreference = "Stop"

$keeperPath = "$HOME\.claude\council-automation\browser_bridge_keeper.py"
$logDir     = "$HOME\.claude\logs"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Removing scheduled task '$TaskName'..."
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Done. Bridge Chrome will no longer auto-start at logon."
        Write-Host "Run 'python `"$keeperPath`" --stop' to close the current bridge Chrome."
    } else {
        Write-Host "Task '$TaskName' not registered. Nothing to remove."
    }
    return
}

# Validate keeper script exists
if (-not (Test-Path $keeperPath)) {
    Write-Error "browser_bridge_keeper.py not found at: $keeperPath"
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
Write-Host "keeper:      $keeperPath"

# Create log directory
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

# Build the action: pythonw.exe -u browser_bridge_keeper.py
$action = New-ScheduledTaskAction `
    -Execute $PythonW `
    -Argument "-u `"$keeperPath`"" `
    -WorkingDirectory (Split-Path $keeperPath -Parent)

# Triggers: at logon (start when the user signs in) + every 5 minutes (watchdog).
# The bridge Chrome rarely dies, so 5 min is ample — and the reuse path is just a
# ~1s process scan with NO relaunch. (Contrast: PerplexitySessionKeeper uses 8 min
# because it is driven by the ~10-min pplx.session-id cookie TTL; this keeper has
# no cookies to refresh, only process liveness to guard.)
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"
$periodicTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 365)

# Run as the current user, "only when logged on" (Interactive). This is MANDATORY
# for a GUI Chrome launch: "run whether user is logged on or not" would start
# Chrome in a non-interactive window station where it won't render. A disconnected-
# but-logged-in RDP session keeps Chrome + its service worker alive and the periodic
# task still fires; only a full logoff kills Chrome, recovered by the AtLogOn trigger.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Settings: restart on failure, no time limit, allow on battery
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)   # 0 = no time limit

# Remove existing task if present (idempotent install)
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName' before re-registering..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Keeps the user's default-profile Chrome (which hosts the Claude Browser Bridge extension) running so the browser-bridge MCP is always connectable." `
    -Action $action `
    -Trigger @($logonTrigger, $periodicTrigger) `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host ""
Write-Host "[OK] Task '$TaskName' registered."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Start the task now (one-time):"
Write-Host "       Start-ScheduledTask -TaskName $TaskName"
Write-Host "  2. Verify it's running:"
Write-Host "       Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "       python `"$keeperPath`" --status"
Write-Host "  3. Watch the logs:"
Write-Host "       Get-Content `"$logDir\browser_bridge_keeper.log`" -Wait -Tail 20"
Write-Host ""
Write-Host "Uninstall later with:"
Write-Host "  PowerShell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"

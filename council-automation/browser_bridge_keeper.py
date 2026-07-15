"""browser_bridge_keeper.py — keep the Claude Browser Bridge Chrome always running.

The browser-bridge MCP (remote Claude's browser automation) relies on a Chrome
MV3 extension whose service worker is a WebSocket CLIENT to the MCP server at
ws://127.0.0.1:8765. When that Chrome is closed the bridge disconnects. The user
is almost always remote, so the bridge is dead most of the time.

This keeper guarantees the Chrome that HAS the bridge extension is always up.

Host = the user's DEFAULT Chrome profile (`AppData\\Local\\Google\\Chrome\\User Data`,
profile "Default"). WHY the default profile and not a dedicated one:
  - The bridge extension is installed UNPACKED (developer-mode Load-unpacked,
    manifest location=4) in the DEFAULT profile and loads reliably there.
  - On this machine (AzureAD-managed, Chrome 150) the `--load-extension`
    command-line flag is IGNORED — verified 2026-07-12 via a CDP probe (0
    service-worker targets) — so a dedicated profile CANNOT force-load the
    extension. Google is deprecating command-line extension loading.
  - The default profile is also logged into the sites the automation needs
    (Facebook, etc.), which a fresh dedicated profile would not be.

Architecture (mirrors session_keeper.py's one-shot pivot): each run checks
whether a default-profile Chrome is alive; if not, it launches one detached and
exits. Windows Task Scheduler drives the watchdog cadence (AtLogOn + every 5 min).

Usage:
    python browser_bridge_keeper.py            # ensure the bridge Chrome is up (one-shot)
    python browser_bridge_keeper.py --status   # is a default-profile Chrome running?
    python browser_bridge_keeper.py --stop      # clear keeper state (does NOT close your Chrome)

To stop auto-relaunch: install_bridge_keeper_task.ps1 -Uninstall.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The bridge extension lives in the DEFAULT profile. We target it explicitly so
# Chrome opens the profile that actually has the extension even if the user has
# multiple profiles.
PROFILE_DIRECTORY = "Default"

PID_FILE = Path.home() / ".claude" / "config" / "bridge_keeper.pid"
LOG_PATH = Path.home() / ".claude" / "logs" / "browser_bridge_keeper.log"

LAUNCH_CONFIRM_TIMEOUT_S = 15  # how long to wait for the launched Chrome to appear

# Windows constants
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_STARTF_USESHOWWINDOW = 0x00000001
_SW_SHOWMINNOACTIVE = 7  # start minimized, do NOT activate (no focus steal)


# ---------------------------------------------------------------------------
# Logging / PID state
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    """Emit a timestamped keeper log line (also lands in LOG_PATH under Task Scheduler)."""
    ts = time.strftime("%H:%M:%S")
    print(f"[bridge-keeper {ts}] {msg}", flush=True)


def _write_pid() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _clear_pid() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def _harden_for_windows_daemon() -> None:
    """Redirect fds 1/2 to LOG_PATH so Task Scheduler runs are diagnosable.

    Called from main() ONLY for the keep action, AFTER the --status/--stop
    branches, so operator output for those isn't swallowed into the log.
    """
    if sys.platform != "win32":
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(str(LOG_PATH), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
        sys.stdout = os.fdopen(1, "w", buffering=1)
        sys.stderr = os.fdopen(2, "w", buffering=1)
    except (OSError, ValueError):
        pass
    for sig_name in ("SIGBREAK", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass


# ---------------------------------------------------------------------------
# Chrome discovery / process scanning
# ---------------------------------------------------------------------------

def _find_chrome_executable() -> str | None:
    """Find a Chrome executable on Windows (system install locations)."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _default_chrome_pids() -> list[int]:
    """Read-only: PIDs of DEFAULT-profile Chrome BROWSER processes.

    A default-profile Chrome's main (browser) process has no `--type=` and no
    explicit `--user-data-dir=` (our keeper profiles always set one, so this
    cleanly distinguishes the user's real Chrome from session_keeper/other
    custom-profile Chromes). Non-empty ⇒ the bridge Chrome is alive. Windows-only.
    """
    if sys.platform != "win32":
        return []
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine -notlike '*--type=*' "
            "-and $_.CommandLine -notlike '*--user-data-dir=*' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
        )
        pids: list[int] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids
    except Exception:
        return []


def _chrome_version(chrome_exe: str) -> str:
    """Return Chrome's product version, or 'unknown' (logged each launch)."""
    if sys.platform != "win32":
        return "unknown"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{chrome_exe}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=10,
        )
        v = (result.stdout or "").strip()
        return v or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def _launch_default_chrome() -> bool:
    """Launch the user's DEFAULT-profile Chrome (which has the bridge extension),
    detached and started minimized (a hint — Chrome restores its own window
    state, which is correct for the user's real browser). Returns True if a live
    default-profile Chrome is observed afterward.
    """
    chrome_exe = _find_chrome_executable()
    if not chrome_exe:
        _log("ERROR: Chrome not found in standard install locations. Install Google Chrome.")
        return False

    _log(f"Chrome {_chrome_version(chrome_exe)} | launching default profile '{PROFILE_DIRECTORY}'")

    # Minimal flags — this is the user's REAL browser, so we do NOT force it
    # offscreen, disable occlusion, or force-minimize it. No explicit
    # --user-data-dir (uses the default User Data dir). No URL — let Chrome do
    # its normal startup / session restore per the user's settings.
    chrome_args = [
        chrome_exe,
        f"--profile-directory={PROFILE_DIRECTORY}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    creationflags = 0
    startupinfo = None
    if sys.platform == "win32":
        creationflags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= _STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = _SW_SHOWMINNOACTIVE

    _log("Launching default-profile Chrome (detached, minimized hint)...")
    try:
        subprocess.Popen(
            chrome_args,
            creationflags=creationflags,
            startupinfo=startupinfo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        _log(f"ERROR: failed to launch Chrome: {e}")
        return False

    deadline = time.time() + LAUNCH_CONFIRM_TIMEOUT_S
    pids: list[int] = []
    while time.time() < deadline:
        pids = _default_chrome_pids()
        if pids:
            break
        time.sleep(1)

    if not pids:
        _log(f"ERROR: default-profile Chrome did not appear within {LAUNCH_CONFIRM_TIMEOUT_S}s.")
        return False

    _log(f"Bridge Chrome up (default profile, pids={pids}). Extension should reconnect the WS bridge.")
    return True


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def status() -> None:
    pids = _default_chrome_pids()
    if pids:
        _log(f"Default-profile Chrome RUNNING (pids={pids}) — bridge host is up.")
    else:
        _log("Default-profile Chrome NOT running — bridge host is down.")


def stop() -> None:
    # Deliberately does NOT kill Chrome: this mode manages the user's REAL
    # browser, and force-closing it would discard their tabs. Just clear keeper
    # state; use install_bridge_keeper_task.ps1 -Uninstall to stop auto-relaunch.
    _clear_pid()
    _log("Keeper state cleared. This mode manages your real Chrome and will NOT "
         "force-close it. Uninstall the scheduled task to stop auto-relaunch; "
         "close Chrome yourself if you want it down.")


def ensure_running() -> int:
    """One-shot: launch the default-profile Chrome if it isn't already up."""
    pids = _default_chrome_pids()
    if pids:
        _log(f"Bridge Chrome already up (default profile, pids={pids}); nothing to do.")
        return 0
    ok = _launch_default_chrome()
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Keep the Claude Browser Bridge Chrome alive.")
    parser.add_argument("--status", action="store_true", help="Report whether the bridge Chrome is running")
    parser.add_argument("--stop", action="store_true", help="Clear keeper state (does NOT close your Chrome)")
    args = parser.parse_args()

    if args.status:
        status()
        return
    if args.stop:
        stop()
        return

    # Keep action only: now safe to redirect stdio to the log (see docstring).
    _harden_for_windows_daemon()
    _write_pid()
    code = ensure_running()
    sys.exit(code)


if __name__ == "__main__":
    main()

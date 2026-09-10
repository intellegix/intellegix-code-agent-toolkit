"""session_keeper.py — Persistent Perplexity session keeper.

Launches a single headful Chrome via Playwright, navigates to perplexity.ai,
and stays alive — periodically re-navigating to let Cloudflare re-issue
__cf_bm + pplx.edge-sid in-place. Cookies are written back to
playwright-session.json after each refresh so council_browser.py /
research_query / extended_research_runner pick them up automatically.

User experience (Option 3 from 2026-05-20 audit):
  1. Run `python session_keeper.py` ONCE.
  2. Headful Chrome window pops up (Cloudflare requires headful for cookie issuance).
  3. User MINIMIZES the window — stays minimized forever.
  4. Keeper re-navigates every 20 min in the SAME minimized window — no new
     pop-ups, no flicker. Cookies stay fresh.
  5. Research calls run headless against the freshly-maintained cookies.
  6. To stop: `python session_keeper.py --stop` OR close the Chrome window.

Usage:
    python session_keeper.py                    # foreground, 20-min refresh
    python session_keeper.py --interval 600     # 10-min refresh (more aggressive)
    python session_keeper.py --status           # is keeper running?
    python session_keeper.py --stop             # terminate running keeper
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

# WINDOWS DAEMON HARDENING (2026-05-22 Q5 followup fix per Perplexity audit):
# When launched detached via subprocess.Popen with DETACHED_PROCESS + stdin=DEVNULL,
# the keeper process was exiting silently 30-60s into `await asyncio.sleep(1200)`.
# Root cause ranking from Perplexity diagnosis:
#   (1) Playwright's Node.js subprocess detects stdin EOF and exits → tears down
#       Playwright IPC → asyncio task raises CancelledError → asyncio.run unwinds.
#   (2) Windows console-subsystem signals leaking through CREATE_NEW_PROCESS_GROUP.
# Mitigations applied below:
#   - Re-open sys.stdin to a real file handle (not the DEVNULL we inherited) so
#     any subprocess that probes stdin sees a readable pipe instead of EOF.
#   - Install signal handlers that ignore CTRL_CLOSE_EVENT / CTRL_BREAK_EVENT /
#     SIGBREAK on Windows so we don't die when the parent console terminates.
#   - Keep an idle stdin reader task pinned in the event loop so the loop never
#     thinks it has no work to do.
_LOG_MAX_BYTES = 5 * 1024 * 1024   # rotate session_keeper.log past ~5 MB
_LOG_BACKUPS = 3                   # keep .log.1 .. .log.3


def _harden_for_windows_daemon() -> None:
    """Daemonize stdio at the OS file-descriptor level so children inherit sanely.

    Q5-followup fix (2026-05-22) reassigned Python's `sys.stdin` object only —
    child processes (Playwright's Node.js driver) inherit the OS-level fd 0,
    not the Python object. Under Task Scheduler, fd 0 is INVALID_HANDLE_VALUE,
    so the Node driver sees EOF on stdin ~5s into the run, tears down
    Playwright IPC, our asyncio task is cancelled, the keeper exits.

    Real fix (2026-05-22 second pass): replace fds 0/1/2 via os.dup2. Python's
    os.dup2 on Windows also calls SetStdHandle for fd 0/1/2, so child processes
    inheriting via STARTUPINFO get the right handles too. fds 1/2 are pointed
    at ~/.claude/logs/session_keeper.log (append) so future daemon failures
    are visible without rebuilding Task Scheduler XML.
    """
    if sys.platform != "win32":
        return

    # 1. fd 0 -> readable handle on NUL. Children probing stdin see EOF on
    #    first read but a VALID handle, which Playwright's Node driver tolerates.
    try:
        devnull_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull_fd, 0)
        os.close(devnull_fd)
    except (OSError, ValueError):
        pass

    # 2. fds 1 & 2 -> append-mode log file. Captures both Python print() and
    #    any subprocess that inherits these handles.
    try:
        log_path = Path.home() / ".claude" / "logs" / "session_keeper.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Size-based rotation AT STARTUP. The log is written by pointing fds 1/2
        # at the file via os.dup2, so a logging.RotatingFileHandler cannot manage
        # it -- rotation has to happen before the descriptors are opened. A
        # one-shot process that runs every ~8 minutes gives us a natural, safe
        # rotation point. Before this, the file had grown to 8.6 MB / 116k lines
        # over 76 days without ever rotating.
        # Best-effort: two keeper invocations overlapping could race on the
        # rename, and a failed rotation must never stop the keeper from starting.
        try:
            if log_path.exists() and log_path.stat().st_size > _LOG_MAX_BYTES:
                for n in range(_LOG_BACKUPS - 1, 0, -1):
                    older, newer = log_path.with_suffix(f".log.{n + 1}"), log_path.with_suffix(f".log.{n}")
                    if newer.exists():
                        older.unlink(missing_ok=True)
                        newer.replace(older)
                log_path.replace(log_path.with_suffix(".log.1"))
        except OSError:
            pass
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
    except (OSError, ValueError):
        pass

    # 3. Refresh Python's sys.stdin / sys.stdout / sys.stderr objects so any
    #    Python code that probes them via the high-level API gets the new
    #    handles too (the os.dup2 calls above only touched the OS fds).
    try:
        sys.stdin = os.fdopen(0, "r")
    except (OSError, ValueError):
        pass
    try:
        sys.stdout = os.fdopen(1, "w", buffering=1)
        sys.stderr = os.fdopen(2, "w", buffering=1)
    except (OSError, ValueError):
        pass

    # 4. Ignore Windows-specific termination signals from a closed parent
    #    console (unchanged from Q5 fix).
    for sig_name in ("SIGBREAK", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass


# NOTE: _harden_for_windows_daemon() is NOT called at module import — only
# from main() right before we enter the daemon loop. Calling it unconditionally
# would also redirect stdout/stderr for `--status` / `--stop` invocations,
# making those commands silently dump their output into the log file instead
# of the terminal where the operator can read them.

# Reuse paths from council_config
try:
    from council_config import BROWSER_LOCALSTORAGE_PATH, BROWSER_SESSION_PATH
    _HAS_COUNCIL = True
except ImportError:
    BROWSER_SESSION_PATH = Path.home() / ".claude" / "config" / "playwright-session.json"
    BROWSER_LOCALSTORAGE_PATH = Path.home() / ".claude" / "config" / "playwright-localstorage.json"
    _HAS_COUNCIL = False

KEEPER_PID_FILE = Path.home() / ".claude" / "config" / "session_keeper.pid"
KEEPER_HEARTBEAT_FILE = Path.home() / ".claude" / "config" / "session_keeper.heartbeat"
KEEPER_CDP_FILE = Path.home() / ".claude" / "config" / "session_keeper.cdp"
DEFAULT_INTERVAL_S = 1200  # 20 min (CF __cf_bm has ~30min TTL; refresh well before expiry)
# Chrome DevTools Protocol port the keeper owns, for council_browser's CDP
# attach. Moved off 9222 on 2026-08-08: ~/.claude/browser-relay/relay.mjs (the
# /takeover phone relay) binds 9222 too, whichever process boots first wins,
# and that collision caused BOTH the 08-07 and 08-08 machine-wide research
# outages. The keeper and the relay now have disjoint ports and can coexist,
# which is also what unblocks re-enabling the PerplexitySessionKeeper task
# (Disabled since 2026-07-30 because re-enabling it would have fought the relay
# for 9222). Must match council_browser.KEEPER_CDP_PORT -- both read
# COUNCIL_KEEPER_CDP_PORT so a single env var moves them together.
DEFAULT_CDP_PORT = int(os.environ.get("COUNCIL_KEEPER_CDP_PORT", "9223"))


def _log(msg: str) -> None:
    # Date REQUIRED, not cosmetic. Until 2026-08-09 this emitted %H:%M:%S only,
    # and the log is append-only across months. Grouping lines by clock-minute
    # therefore collapses every day in the file into the same bucket, so a
    # once-per-8-minutes schedule reads as a burst whose height equals the
    # NUMBER OF DAYS present. That artifact cost a full evening: it produced a
    # false all-clear, then a "69 navigations in one minute" burst hypothesis,
    # then a retraction of the retraction, then a whole separate investigation
    # session -- all from a timestamp that identifies its subject by clock time
    # alone and cannot distinguish today from nine weeks ago.
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[keeper {ts}] {msg}", flush=True)


def _write_pid() -> None:
    KEEPER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEEPER_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _read_pid() -> int | None:
    if not KEEPER_PID_FILE.exists():
        return None
    try:
        return int(KEEPER_PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _clear_pid() -> None:
    try:
        KEEPER_PID_FILE.unlink()
    except FileNotFoundError:
        pass


def _heartbeat(stage: str) -> None:
    """Write heartbeat so external watchers can detect keeper liveness."""
    KEEPER_HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "stage": stage,
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    KEEPER_HEARTBEAT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _process_alive(pid: int) -> bool:
    """Check whether a PID is alive (Windows + Unix safe).

    On Windows, signal 0 isn't valid for TerminateProcess — os.kill raises
    OSError(WinError 87) which CPython surfaces as SystemError. Fall back to
    ctypes.OpenProcess in that case for a reliable PID existence probe.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # process exists, inaccessible (cross-user) — treat as alive
    except (ProcessLookupError, OSError, SystemError):
        if sys.platform == "win32":
            try:
                import ctypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = ctypes.windll.kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
                )
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    return True
            except Exception:
                pass
        return False


def is_running() -> bool:
    pid = _read_pid()
    if pid is None:
        return False
    if _process_alive(pid):
        return True
    # Stale pid file
    _clear_pid()
    return False


def _kill_chrome_using_profile(profile_dir_basename: str) -> int:
    """Kill all chrome.exe processes whose command-line references this profile dir.

    Used by both stop_running() (so --stop cleans up Chrome too — taskkill /F
    on pythonw doesn't kill its DETACHED Chrome child) and by main_loop's
    Chrome launch (so a fresh keeper start doesn't end up attached to a
    leftover Chrome from a prior crash). Windows-only — no-op elsewhere.
    Returns count of processes terminated.
    """
    if sys.platform != "win32":
        return 0
    try:
        import subprocess
        ps_cmd = (
            "$pids = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -like '*" + profile_dir_basename + "*' } | "
            "Select-Object -ExpandProperty ProcessId; "
            "$pids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; "
            "($pids | Measure-Object).Count"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
        )
        try:
            return int((result.stdout or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0
    except Exception:
        return 0


def stop_running() -> bool:
    pid = _read_pid()
    if pid is None:
        _log("No PID file — keeper not running")
        # Even with no PID, sweep stale Chromes (operator may have killed
        # pythonw manually leaving Chrome orphaned).
        killed = _kill_chrome_using_profile("session_keeper_profile")
        if killed:
            _log(f"Swept {killed} orphan Chrome process(es) using keeper profile")
        # Also unlink the CDP endpoint file so council_browser stops trying
        # to attach to a now-dead Chrome.
        try:
            KEEPER_CDP_FILE.unlink()
        except FileNotFoundError:
            pass
        return False
    try:
        if sys.platform == "win32":
            # taskkill /F on pythonw skips its finally — Chrome is DETACHED and
            # would be orphaned. We sweep Chrome explicitly right after.
            import subprocess
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, text=True, timeout=10)
        else:
            os.kill(pid, signal.SIGTERM)
        _log(f"Sent terminate to PID {pid}")
    except Exception as e:
        _log(f"Failed to terminate PID {pid}: {e}")
        return False
    finally:
        _clear_pid()
        # Reap the orphan Chrome that pythonw didn't get to clean up.
        killed = _kill_chrome_using_profile("session_keeper_profile")
        if killed:
            _log(f"Reaped {killed} orphan Chrome process(es)")
        try:
            KEEPER_CDP_FILE.unlink()
        except FileNotFoundError:
            pass
    return True


async def _save_cookies_and_storage(context, page) -> int:
    """Capture cookies + localStorage from the live context, write to disk.

    Returns the count of cookies persisted.
    """
    cookies = await context.cookies()
    now = time.time()
    # Drop only IMMINENTLY-expiring (< 60s) cookies — preserve freshly-issued
    # short-TTL cookies (the whole point of this script is to keep them fresh).
    filtered = []
    for c in cookies:
        exp = c.get("expires", -1)
        if isinstance(exp, (int, float)) and exp > 0 and (exp - now) < 60:
            continue
        filtered.append(c)

    localstorage = await page.evaluate("""() => {
        const items = {};
        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            items[key] = localStorage.getItem(key);
        }
        return items;
    }""")

    BROWSER_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    BROWSER_SESSION_PATH.write_text(
        json.dumps(filtered, indent=2, default=str), encoding="utf-8"
    )
    BROWSER_LOCALSTORAGE_PATH.write_text(
        json.dumps(localstorage, indent=2, default=str), encoding="utf-8"
    )
    return len(filtered)


async def _navigate_and_warm(page) -> bool:
    """Navigate to perplexity.ai, wait for auth hydration, return logged_in."""
    try:
        await page.goto("https://www.perplexity.ai/", wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        _log(f"Navigation failed: {e}")
        return False
    await page.wait_for_timeout(3000)
    for selector in ("#ask-input", "textarea[placeholder]", "[data-testid='ask-input']"):
        try:
            await page.wait_for_selector(selector, timeout=5000)
            return True
        except Exception:
            continue
    return False


def _cdp_serving_pid(port: int) -> int | None:
    """PID of the process LISTENING on `port`, or None if it cannot be read.

    This is the pid published in session_keeper.cdp -- readers need the process
    that actually answers CDP, which outlives this one-shot keeper invocation.
    """
    if sys.platform != "win32":
        return None
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
             f"-ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess"],
            capture_output=True, text=True, timeout=15,
        )
        return int((out.stdout or "").strip())
    except Exception as exc:
        _log(f"CDP serving-pid probe failed ({type(exc).__name__}: {exc})")
        return None


def _cdp_port_owner_cmdline(port: int) -> str | None:
    """Command line of the process LISTENING on `port`, or None if unknown.

    Windows-only. None means "could not determine" and must be treated as
    unknown, never as "foreign" -- a failed probe should not stop the keeper.
    Mirrors council_browser.PerplexityCouncil._cdp_port_owner_cmdline.
    """
    if sys.platform != "win32":
        return None
    ps = (
        f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen "
        f"-ErrorAction SilentlyContinue | Select-Object -First 1; "
        f"if ($c) {{ (Get-CimInstance Win32_Process -Filter "
        f"\"ProcessId=$($c.OwningProcess)\").CommandLine }}"
    )
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        _log(f"CDP owner probe failed ({type(exc).__name__}: {exc}) - owner unknown")
        return None
    return (out.stdout or "").strip() or None


async def _wait_for_cdp(port: int, timeout: float = 30.0) -> dict | None:
    """Poll Chrome's CDP /json/version endpoint until it responds, or timeout."""
    import urllib.request as _ur
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with _ur.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                return json.loads(r.read())
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.5)
    _log(f"CDP port {port} never responded: {last_err}")
    return None


def _find_chrome_executable() -> str | None:
    """Find a Chrome executable on Windows (system install preferred over Playwright bundle)."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


async def main_loop(interval_s: int, cdp_port: int = DEFAULT_CDP_PORT) -> None:
    """Launch Chrome via subprocess (so --remote-debugging-port actually opens),
    attach via Playwright connect_over_cdp, then loop the refresh cycles.

    Playwright's chromium.launch() uses pipe-based DevTools internally and
    overrides/ignores --remote-debugging-port. Launching Chrome directly via
    subprocess.Popen is the only way to get a real listening CDP port that
    other Playwright clients (council_browser.py) can attach to.
    """
    from playwright.async_api import async_playwright

    if not BROWSER_SESSION_PATH.exists():
        _log(f"ERROR: no existing session at {BROWSER_SESSION_PATH}")
        _log("Run `python council_browser.py --save-session` once to log in interactively.")
        sys.exit(1)

    try:
        old_cookies = json.loads(BROWSER_SESSION_PATH.read_text(encoding="utf-8"))
        if not isinstance(old_cookies, list):
            _log("ERROR: session file is not Playwright-native (expected JSON array)")
            sys.exit(1)
    except Exception as e:
        _log(f"ERROR reading session file: {e}")
        sys.exit(1)

    old_localstorage: dict[str, str] = {}
    if BROWSER_LOCALSTORAGE_PATH.exists():
        try:
            old_localstorage = json.loads(BROWSER_LOCALSTORAGE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    chrome_exe = _find_chrome_executable()
    if not chrome_exe:
        _log("ERROR: Chrome not found in standard install locations. Install Google Chrome.")
        sys.exit(1)
    _log(f"Chrome binary: {chrome_exe}")

    # Dedicated profile dir for the keeper. Stable across restarts so Chrome
    # reuses prior CF clearance signals (lower bot-score risk on each launch).
    keeper_profile_dir = Path.home() / ".claude" / "config" / "session_keeper_profile"
    keeper_profile_dir.mkdir(parents=True, exist_ok=True)

    # NOTE on Chrome args: --disable-blink-features=AutomationControlled was
    # previously included for WebDriver anti-detection, but the keeper attaches
    # via CDP (not WebDriver) so navigator.webdriver isn't set in the first
    # place — the flag is cosmetic AND Chrome displays a yellow "unsupported
    # command-line flag" banner across the top of every page when present.
    # Removing it eliminates the banner without compromising Cloudflare clearance
    # (Cloudflare accepted prior sessions launched with this flag, and the cookie
    # state we re-use is the dominant signal anyway).
    chrome_args = [
        chrome_exe,
        f"--remote-debugging-port={cdp_port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={keeper_profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,900",
        "about:blank",
    ]

    _heartbeat("starting")
    # One-shot architecture: if Chrome from a prior keeper run is still alive
    # and serving CDP on the expected port, REUSE it. Avoid the orphan-sweep
    # that would kill our own long-lived Chrome.
    chrome_already_up = False
    try:
        import urllib.request as _ur
        with _ur.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2) as _r:
            _ = _r.read()
        chrome_already_up = True
    except Exception:
        chrome_already_up = False
    if chrome_already_up:
        # "Something answers CDP here" is NOT "our Chrome is here". Reusing a
        # foreign browser hands the keeper a cookie-less profile it then
        # publishes as the live Perplexity session -- the 2026-08-07 failure,
        # from the other side. Prove the listener is running OUR profile dir
        # before adopting it; otherwise refuse the port loudly rather than
        # silently keeping a session nobody is logged in to.
        owner = _cdp_port_owner_cmdline(cdp_port)
        if owner is not None and keeper_profile_dir.name not in owner:
            _log(f"REFUSING port {cdp_port}: it is served by a FOREIGN process "
                 f"(no '{keeper_profile_dir.name}' in its command line). Set "
                 f"COUNCIL_KEEPER_CDP_PORT to a free port. Owner: {owner[:160]}")
            return
        _log(f"Chrome already serving CDP on port {cdp_port}; reusing it (no relaunch)")
    if not chrome_already_up:
        # Only sweep orphans if NOTHING is serving CDP — that means whatever
        # Chrome was tied to our profile dir is in a half-dead state and
        # needs cleaning before we relaunch.
        swept = _kill_chrome_using_profile(keeper_profile_dir.name)
        if swept:
            _log(f"Swept {swept} half-dead Chrome process(es) from prior run")
            await asyncio.sleep(2)  # let Chrome release file locks
        _log(f"Launching Chrome with CDP port {cdp_port}...")
    # DETACHED_PROCESS so Chrome survives parent process exit (one-shot
    # architecture relies on Chrome outliving each pythonw invocation).
    chrome_proc = None
    if not chrome_already_up:
        DETACHED_PROCESS = 0x00000008 if sys.platform == "win32" else 0
        CREATE_NEW_PROCESS_GROUP = 0x00000200 if sys.platform == "win32" else 0
        # CREATE_BREAKAWAY_FROM_JOB is what actually makes the one-shot design
        # work, and its absence is why the keeper has effectively never stayed
        # up. DETACHED_PROCESS only detaches the console; it does NOT escape a
        # Windows job object, and every realistic launcher puts us in one --
        # Task Scheduler assigns each task instance a job, and so does the
        # agent harness that runs ad-hoc commands. When the launching task
        # instance ended, the job was torn down and took Chrome with it within
        # a minute or two, cleanly (Chrome even removed its DevToolsActivePort
        # file, which is what made this look like a normal shutdown rather than
        # a kill). Every subsequent run then found no CDP, fell back to a local
        # launch, and ran the local-launch freshness guard -- the code path that
        # produced the 2026-08-08 outage.
        #
        # MEASURED CAVEAT — the flag helps, but NOT under Task Scheduler.
        # Breakaway only succeeds if the CONTAINING job grants
        # JOB_OBJECT_LIMIT_BREAKAWAY_OK, and Task Scheduler's job does not:
        # launched from the task, this raises [WinError 5] Access is denied and
        # falls back below (observed 2026-08-08 07:27). Launched from an
        # interactive shell it works and Chrome persists. This is the same
        # constraint that killed the Santee demo app — already proved with
        # IsProcessInJob, see memory port-registry, do not re-litigate it.
        #
        # The proper fix there was to make the long-lived process the task's OWN
        # process rather than a child of it. That is NOT applied here: it would
        # need either a new scheduled task (schtasks /Create is denied without
        # elevation on this box) or reverting the keeper to a long-lived loop,
        # which was abandoned in May because pythonw under Task Scheduler died
        # silently inside asyncio.sleep (see memory session-keeper-history).
        #
        # Consequence, stated plainly: under Task Scheduler the keeper's Chrome
        # lives on the order of minutes and is re-established by the task's own
        # repetition. Nothing DEPENDS on it — the local-launch path has a
        # correct freshness guard and a working refresher — so this is a
        # latency optimization, not a availability requirement. Left as a known
        # open item rather than papered over.
        import subprocess
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000 if sys.platform == "win32" else 0
        base_flags = (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
        popen_kwargs = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            chrome_proc = subprocess.Popen(
                chrome_args,
                creationflags=base_flags | CREATE_BREAKAWAY_FROM_JOB,
                **popen_kwargs,
            )
            _log("Chrome launched with CREATE_BREAKAWAY_FROM_JOB (survives launcher teardown)")
        except OSError as exc:
            _log(f"Job breakaway not permitted ({exc}); launching without it — "
                 f"Chrome may not outlive this launcher")
            chrome_proc = subprocess.Popen(
                chrome_args, creationflags=base_flags, **popen_kwargs,
            )
        _log(f"Chrome subprocess pid={chrome_proc.pid}; waiting for CDP port {cdp_port}...")
    # Wait for CDP to be responsive — whether from a fresh launch or a
    # pre-existing Chrome.
    ver = await _wait_for_cdp(cdp_port, timeout=20)
    if not ver:
        _log("ERROR: Chrome failed to open CDP port. Killing subprocess.")
        if chrome_proc is not None:
            try:
                chrome_proc.terminate()
            except Exception:
                pass
        sys.exit(1)
    _log(f"CDP up: Browser={ver.get('Browser')} Protocol={ver.get('Protocol-Version')}")

    pw = await async_playwright().start()
    browser = None
    context = None
    try:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        contexts = browser.contexts
        if not contexts:
            _log("ERROR: connect_over_cdp returned 0 contexts")
            sys.exit(1)
        context = contexts[0]
        _log(f"Connected via CDP. {len(contexts)} context(s), {len(context.pages)} page(s)")

        # Inject cookies from playwright-session.json into the default context.
        await context.add_cookies(old_cookies)
        _log(f"Injected {len(old_cookies)} cookies into CDP context")

        # Use or open a page in the keeper context.
        if context.pages:
            page = context.pages[0]
        else:
            page = await context.new_page()

        # Initial warm + cookie write.
        _log("Navigating to perplexity.ai (initial warm)...")
        logged_in = await _navigate_and_warm(page)
        if not logged_in:
            _log("ERROR: not logged in — re-run `python council_browser.py --save-session`")
            sys.exit(1)
        n = await _save_cookies_and_storage(context, page)
        # Publish the CDP endpoint so council_browser.connect_over_cdp can attach.
        # Playwright launches Chrome with --remote-debugging-port=N which serves
        # the WebSocket Browser endpoint at http://localhost:N — the standard CDP URL.
        KEEPER_CDP_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Use 127.0.0.1 explicitly — `localhost` may resolve to IPv6 [::1] on
        # Windows, but Chrome's --remote-debugging-port listens on IPv4 only,
        # causing Playwright's connect_over_cdp to ECONNREFUSED on IPv6.
        cdp_endpoint = f"http://127.0.0.1:{cdp_port}"
        # `pid` MUST be the pid of the process actually SERVING CDP, i.e. Chrome
        # -- not this keeper python. The keeper is one-shot per invocation and
        # exits seconds from now while its DETACHED_PROCESS Chrome lives on, so
        # recording os.getpid() here published a pid that was always dead by the
        # time any reader looked. council_browser's liveness gate then read the
        # healthy keeper as a stale .cdp file, deleted it, and fell back to a
        # local launch on EVERY run -- which is how the 08-08 outage reached the
        # local-launch freshness guard at all. Keep the launcher pid separately
        # for diagnostics; it is not an identity signal.
        chrome_pid = _cdp_serving_pid(cdp_port)
        KEEPER_CDP_FILE.write_text(
            json.dumps({
                "port": cdp_port,
                "endpoint": cdp_endpoint,
                "pid": chrome_pid,
                "keeper_python_pid": os.getpid(),
            }),
            encoding="utf-8",
        )
        _log(f"Refresh complete: {n} cookies written. CDP endpoint: {cdp_endpoint}")
        _heartbeat("done")
        # NOTE on architecture (2026-05-25 rewrite): this used to be a
        # long-lived asyncio.sleep(interval_s) loop, but pythonw under Task
        # Scheduler kept exiting silently after the first sleep — neither
        # `except CancelledError` nor `except Exception` in the outer loop
        # caught anything, the process just disappeared with no traceback.
        # Switched to a ONE-SHOT design: each keeper invocation does exactly
        # one warm cycle then exits cleanly. Chrome stays alive across
        # invocations via DETACHED_PROCESS — only one Chrome ever exists for
        # the keeper profile (subsequent subprocess.Popen requests are
        # singleton-suppressed by Chrome's user-data-dir lock). Task Scheduler
        # repetition interval (now 20min — see install_keeper_task.ps1) drives
        # the cadence instead of asyncio.sleep.
    finally:
        # One-shot cleanup: detach Playwright cleanly but LEAVE Chrome alive.
        # Chrome is the long-lived process — pythonw exits, but Chrome stays
        # on port 9222 serving CDP, and the CDP file stays valid for
        # council_browser to attach. Next keeper invocation (every 20 min via
        # Task Scheduler) sees Chrome already up and skips the launch.
        #
        # DO NOT call browser.close() — sends Browser.close CDP which kills
        # Chrome.
        # DO NOT call chrome_proc.terminate() — same effect.
        # DO NOT unlink KEEPER_CDP_FILE — Chrome is still serving the port.
        try:
            await pw.stop()
        except Exception:
            pass
        # PID file cleanup happens in main()'s outer finally; don't duplicate.


def main() -> int:
    parser = argparse.ArgumentParser(description="Persistent Perplexity session keeper")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S,
                        help=f"Refresh interval in seconds (default {DEFAULT_INTERVAL_S})")
    parser.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT,
                        help=f"Chrome DevTools Protocol port (default {DEFAULT_CDP_PORT})")
    parser.add_argument("--status", action="store_true", help="Check if keeper is running")
    parser.add_argument("--stop", action="store_true", help="Terminate running keeper")
    args = parser.parse_args()

    if args.status:
        if is_running():
            pid = _read_pid()
            print(f"RUNNING pid={pid}")
            if KEEPER_HEARTBEAT_FILE.exists():
                try:
                    hb = json.loads(KEEPER_HEARTBEAT_FILE.read_text(encoding="utf-8"))
                    age = time.time() - hb.get("ts", 0)
                    print(f"  stage={hb.get('stage')} heartbeat_age={age:.0f}s")
                except Exception:
                    pass
            return 0
        print("STOPPED")
        return 1

    if args.stop:
        stopped = stop_running()
        return 0 if stopped else 1

    if is_running():
        pid = _read_pid()
        print(f"Keeper already running (pid={pid}). Use --stop to terminate or --status to inspect.")
        return 1

    # Now that we know we're entering daemon mode (not --status / --stop),
    # harden stdio at OS fd level so Playwright Node child inherits valid
    # handles and stdout/stderr land in the log file.
    _harden_for_windows_daemon()

    _write_pid()
    _log(f"Starting keeper (pid={os.getpid()}, interval={args.interval}s, cdp_port={args.cdp_port})")
    # Outer restart-on-failure loop. Defense-in-depth: if main_loop raises
    # despite the OS-fd hardening (e.g. Playwright IPC drops for any other
    # reason), we sleep + retry instead of letting Task Scheduler see a clean
    # exit (which it won't restart). SystemExit and KeyboardInterrupt explicitly
    # break out — unrecoverable / user-initiated stops.
    backoff_s = 10
    try:
        for attempt in range(1, 1001):
            try:
                asyncio.run(main_loop(args.interval, args.cdp_port))
                _log(f"main_loop returned cleanly (attempt {attempt}); not restarting")
                break
            except KeyboardInterrupt:
                _log("Interrupted by user")
                break
            except SystemExit as e:
                _log(f"main_loop sys.exit({e.code}) -- not restarting (auth/config issue)")
                break
            except Exception as e:
                _log(f"main_loop raised {type(e).__name__}: {e}; restart in {backoff_s}s (attempt {attempt})")
                try:
                    time.sleep(backoff_s)
                except KeyboardInterrupt:
                    break
                backoff_s = min(backoff_s * 2, 300)
                # main_loop's finally cleared our PID; re-write so is_running() works
                _write_pid()
    finally:
        # NOTE: do NOT unlink KEEPER_CDP_FILE here. The new one-shot architecture
        # keeps Chrome alive across keeper invocations — the CDP file is the
        # handoff for council_browser to find the keeper's Chrome. Only
        # stop_running() (--stop) removes the CDP file (because it also kills
        # Chrome).
        _clear_pid()
    return 0


if __name__ == "__main__":
    sys.exit(main())

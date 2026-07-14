"""Cross-process advisory lock for the Perplexity submit critical section.

Two Claude Code sessions running /research-perplexity simultaneously can race
on the Windows OS-level keyboard focus during slash-command typing. The
second Chrome launch can call SetForegroundWindow / BringWindowToTop mid-
keystroke on the first session, sending keys to the wrong target and
silently submitting the query in Search mode instead of Research mode.
Empty .prose -> server.js retry-once -> new subprocess -> new Chrome
window. User sees "browsers canceling each other and trying."

The fix is a cross-process file lock around the focus-sensitive submit
window (input focus click + slash-command type + Space commit + verify
+ query submission + first streaming token observed). Hold time is
typically 10-25s; browser launch and synthesis collection remain fully
parallel.

Usage (sync):
    with get_submit_lock():
        ...focus-sensitive work...

Usage (async, blocking event loop is unacceptable):
    lock = get_submit_lock()
    await asyncio.to_thread(lock.acquire)
    try:
        ...focus-sensitive work...
    finally:
        lock.release()

Stale-lock reclaim: if a process is SIGKILL'd before its __exit__ runs,
the lock file persists with the old PID. The 180s mtime check is the
only escape hatch -- any caller that finds a lock older than that
unlinks it before attempting acquire.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from filelock import FileLock

# Co-located with the SessionSemaphore so cleanup paths see them together.
# Dotfile prefix prevents accidental matching by SessionSemaphore's
# slot-*.lock glob (verified -- different prefix family).
LOCK_PATH = (
    Path.home()
    / ".claude"
    / "config"
    / "browser-sessions"
    / ".perplexity_submit.lock"
)
STALE_AGE_S = 180  # crashed-holder lock left this long -> reclaim


def get_submit_lock(timeout: float | None = None) -> FileLock:
    """Return a FileLock for the Perplexity submit critical section.

    Timeout scales with MAX_CONCURRENT_SESSIONS so the Nth caller has
    time to drain the queue ahead of it. Floor 120s for single-session
    use; scales up to ~240s at full saturation (MAX_CONCURRENT_SESSIONS=8
    sessions x ~30s each = ~240s worst-case wait at the tail).
    """
    if timeout is None:
        # Lazy import to avoid a circular dep with council_config and
        # to keep this module importable from anywhere.
        try:
            from council_config import MAX_CONCURRENT_SESSIONS
            timeout = max(120.0, float(MAX_CONCURRENT_SESSIONS) * 30.0)
        except ImportError:
            timeout = 240.0  # safe floor without config

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Reclaim a stale lock from a crashed holder. This runs BEFORE
    # FileLock.acquire() so even if filelock's internal acquire would
    # block forever on a wedged file, we get an escape hatch.
    try:
        if LOCK_PATH.exists():
            age = time.time() - LOCK_PATH.stat().st_mtime
            if age > STALE_AGE_S:
                try:
                    LOCK_PATH.unlink()
                except OSError:
                    pass
    except OSError:
        pass

    # thread_local=False is CRITICAL when acquire() runs in a different thread
    # than release() — e.g., when caller wraps acquire in asyncio.to_thread() and
    # releases from the main asyncio coroutine. filelock's default thread_local=True
    # stores _lock_counter / is_locked state per-thread, so:
    #   worker_thread.acquire()  → counter[worker]=1, OS lock held, fd open
    #   main_thread.release()    → counter[main]=0 (always was), release short-
    #                              circuits because is_locked=False, fd never
    #                              closed, OS lock stays held until process exit
    # With thread_local=False, the counter is shared across threads in the
    # process and release correctly closes the fd / releases the OS lock.
    return FileLock(str(LOCK_PATH), timeout=timeout, thread_local=False)


HEARTBEAT_INTERVAL_S = 60.0  # default mtime-refresh cadence for long holds


class LockHeartbeat:
    """Background thread that periodically refreshes a held lock file's mtime.

    The stale-reclaim check in `get_submit_lock()` treats any lock file
    older than `STALE_AGE_S` (180s) as abandoned by a crashed holder and
    unlinks it so a new caller can proceed. That assumption is correct
    for the lock's original design (10-25s submit-window holds), but
    breaks if a caller legitimately holds the lock for minutes (e.g. the
    RESEARCH_QUEUE_STOPGAP whole-run widening in council_browser.py) --
    a second session would reclaim and acquire the "stale" lock out from
    under a still-running holder, causing the exact overlap the widening
    was meant to prevent.

    LockHeartbeat only opts a caller INTO this behavior when explicitly
    started (see `start_lock_heartbeat`); nothing here runs unless a
    caller creates one. A live holder's heartbeat thread keeps touching
    the lock file's mtime every `interval_s`, so `age > STALE_AGE_S`
    never trips while the holder is alive. If the holder process
    crashes, its heartbeat thread dies with it -- the mtime stops
    advancing, ages past `STALE_AGE_S`, and the existing reclaim logic
    in `get_submit_lock()` fires exactly as it does today.
    """

    def __init__(self, lock_path: Path, interval_s: float = HEARTBEAT_INTERVAL_S) -> None:
        self._lock_path = lock_path
        self._interval_s = interval_s
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="submit-lock-heartbeat", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        # wait() returns True only when stop_event is set before the
        # timeout elapses, so this loop refreshes mtime once per
        # interval and exits promptly (no extra sleep) once stopped.
        while not self._stop_event.wait(self._interval_s):
            try:
                os.utime(self._lock_path, None)
            except OSError:
                # Lock file briefly missing/replaced -- best-effort only;
                # the next tick will retry.
                pass

    def stop(self) -> None:
        """Signal the heartbeat thread to stop and wait for it to exit."""
        self._stop_event.set()
        self._thread.join(timeout=5.0)


def start_lock_heartbeat(
    lock: FileLock, interval_s: float = HEARTBEAT_INTERVAL_S
) -> LockHeartbeat:
    """Start a background heartbeat that keeps `lock`'s file mtime fresh.

    Only call this for a lock intended to be held far longer than
    `STALE_AGE_S` (180s) -- e.g. under RESEARCH_QUEUE_STOPGAP, where
    submit_lock spans a whole Perplexity run instead of just the submit
    window. Callers MUST call `.stop()` on the returned LockHeartbeat
    once the lock is released (or before releasing it) so the thread
    doesn't keep touching a file the caller no longer owns.

    Not called anywhere by default -- opt-in only, so default (no
    heartbeat) behavior is unaffected.
    """
    return LockHeartbeat(Path(lock.lock_file), interval_s=interval_s)

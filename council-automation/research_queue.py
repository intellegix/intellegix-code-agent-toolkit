"""Crash-safe, cross-process FIFO queue for serializing Perplexity research
runs across concurrent local Claude Code sessions.

Design: `~/.claude/docs/plans/2026-07-11-perplexity-research-queue-design.md`
Plan:   `~/.claude/docs/plans/2026-07-11-perplexity-research-queue.md`

Two layers, so the queue is both simple and bulletproof:

  ORDERING  -- atomic ticket files under `QUEUE_DIR/tickets/`, named
              `<seq>-<pid>.ticket`, seq allocated by `atomic_next_seq()`.
              Tickets decide *who gets to try* and provide FIFO visibility.

  CORRECTNESS -- one shared `active.lock` (py-filelock). A process attempts
              it, non-blocking, ONLY when it is the head (lowest-seq live
              ticket). Whoever holds `active.lock` is the single active run.
              The FileLock is authoritative for mutual exclusion; two
              waiters can never both hold it even if the head-selection
              logic has a bug. On Windows the OS frees the lock handle when
              the holding process dies, so a crash mid-run self-heals the
              queue without any explicit cleanup.

This module is standalone -- nothing imports it yet (wiring into
`CouncilBrowser.run()` is a separate, later task). All paths are exposed as
plain module-level globals and re-read from the module namespace on every
call (never cached into a local at import time), so tests can safely do
`monkeypatch.setattr(research_queue, "QUEUE_DIR", tmp_path)` etc.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from filelock import FileLock, Timeout

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Paths (monkeypatchable module globals -- functions below always resolve
# these by name at call time, never by capturing a local alias at import
# time, so reassigning them in a test takes effect immediately).
# --------------------------------------------------------------------------
QUEUE_DIR: Path = Path.home() / ".claude" / "config" / "research-queue"
ACTIVITY_LOG: Path = Path.home() / ".claude" / "council-logs" / "perplexity-activity.jsonl"
SNAPSHOT: Path = Path.home() / ".claude" / "council-logs" / "perplexity-queue.json"

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
TTL_S = 120  # heartbeat-staleness backstop (holder touches every 10s -> 12x margin).
             # PID-liveness (see pid_alive()) is the PRIMARY reclaim signal;
             # this TTL only guards against a live process whose heartbeat
             # thread has wedged/died without the process itself dying.
HEARTBEAT_INTERVAL_S = 10
POLL_S = 1.5
MAX_WAIT_S = int(os.environ.get("RESEARCH_QUEUE_MAX_WAIT", "1200"))
ACTIVITY_LOG_LOCK_TIMEOUT_S = 10  # exposed for tests (monkeypatchable)
SNAPSHOT_LOCK_TIMEOUT_S = 5  # exposed for tests (monkeypatchable)
# os.replace() onto a destination with a concurrent open handle raises
# PermissionError (WinError 5 / ERROR_ACCESS_DENIED) on Windows. The snapshot
# FileLock serializes writers but cannot exclude readers (monitor, queue
# daemon, MCP status tool, other runners checking their position), so the
# swap is retried a few times with a short backoff to ride out that transient
# sharing violation. 12 * 25ms = 300ms worst case; readers hold the file for
# microseconds so the swap almost always succeeds on the first or second try.
_ATOMIC_REPLACE_RETRIES = 12  # exposed for tests (monkeypatchable)
_ATOMIC_REPLACE_BACKOFF_S = 0.025  # exposed for tests (monkeypatchable)
STATS_LOOKBACK_LINES = 5000  # tail bound for today's-stats scan (see _compute_today_stats)

# Windows OpenProcess() access right sufficient only to query existence --
# never modify/terminate the target process.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_KERNEL32 = None  # lazy-initialized ctypes.WinDLL handle, Windows only


class QueueTimeout(RuntimeError):
    """Raised when a caller waits longer than MAX_WAIT_S for a queue slot."""


# --------------------------------------------------------------------------
# Path helpers -- always recompute from the current QUEUE_DIR/ACTIVITY_LOG
# globals so monkeypatching those in tests is honored everywhere.
# --------------------------------------------------------------------------
def _tickets_dir() -> Path:
    return QUEUE_DIR / "tickets"


def _counter_path() -> Path:
    return QUEUE_DIR / "counter"


def _counter_lock_path() -> Path:
    return QUEUE_DIR / "counter.lock"


def _active_lock_path() -> Path:
    return QUEUE_DIR / "active.lock"


def _ticket_path(seq: int, pid: int) -> Path:
    return _tickets_dir() / f"{seq:08d}-{pid}.ticket"


def _activity_lock_path() -> Path:
    return ACTIVITY_LOG.parent / (ACTIVITY_LOG.name + ".lock")


def _snapshot_lock_path() -> Path:
    return SNAPSHOT.parent / (SNAPSHOT.name + ".lock")


def ensure_dirs() -> None:
    """Create QUEUE_DIR/tickets and the parent dirs of ACTIVITY_LOG/SNAPSHOT."""
    _tickets_dir().mkdir(parents=True, exist_ok=True)
    ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Low-level atomic I/O
# --------------------------------------------------------------------------
def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` via tmp-file-in-same-dir + os.replace.

    os.replace is atomic on both POSIX and Windows (unlike a bare
    open(path, "w"), which can leave a torn/partial file visible to a
    concurrent reader on Windows if the writer is interrupted).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        # os.replace is atomic, but on Windows it raises PermissionError
        # (WinError 5) when the destination has a concurrent open handle -- a
        # reader (monitor / queue daemon / MCP status tool / another runner
        # checking its position) holding the file open at the instant of the
        # swap. That sharing violation is transient (readers hold it for
        # microseconds), so retry the swap a few times with a short backoff
        # before surfacing the error. Without this, a lost race silently drops
        # a run's completion/instrumentation write, or aborts the caller's run
        # outright (see publish_snapshot: WinError 5 is not the Timeout it
        # catches, so it would otherwise propagate).
        for _attempt in range(_ATOMIC_REPLACE_RETRIES):
            try:
                os.replace(tmp_name, path)
                break
            except PermissionError:
                if _attempt == _ATOMIC_REPLACE_RETRIES - 1:
                    raise
                time.sleep(_ATOMIC_REPLACE_BACKOFF_S)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError as exc:
            logger.debug("_atomic_write: could not clean up tmp file %s (%s)", tmp_name, exc)
        raise


def _get_kernel32() -> Any:
    """Lazily construct and cache a properly-typed ctypes handle to
    kernel32.dll (Windows only). Caching avoids re-resolving the DLL/
    function pointers on every pid_alive() call (called every POLL_S,
    ~1.5s, across up to 9 concurrent sessions).

    `restype`/`argtypes` are set explicitly to `wintypes.HANDLE` --
    without them, ctypes assumes a 32-bit `c_int` return value for
    OpenProcess, but a Win64 HANDLE is a 64-bit pointer. On 64-bit
    Windows that mismatch can truncate the handle value, causing
    CloseHandle() to silently fail on a bad handle -- a slow handle
    leak (one per pid_alive() call that never gets freed).
    `use_last_error=True` makes ctypes.get_last_error() reflect the
    real Win32 GetLastError() value after each call through this DLL
    instance.
    """
    global _KERNEL32
    if _KERNEL32 is None:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _KERNEL32 = k32
    return _KERNEL32


def pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently running.

    WINDOWS SAFETY NOTE: `os.kill(pid, 0)` is NOT a safe existence probe on
    Windows the way it is on POSIX. CPython's Windows implementation of
    os.kill() does not special-case signal 0 -- for any non-console-event
    signal value it opens the process and calls
    `TerminateProcess(handle, sig)`, i.e. `os.kill(pid, 0)` actually KILLS
    the target process (with exit code 0) instead of merely checking it
    exists. Ticket PIDs here can belong to sibling Claude Code sessions
    (including ones actively mid-research), so using os.kill(pid, 0) for
    liveness checking would be destructive. We instead call
    OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) on Windows, which only
    queries -- it never touches the target process -- and close the handle
    immediately. On POSIX, `os.kill(pid, 0)` is the standard, safe,
    side-effect-free existence probe and is used unchanged.

    If OpenProcess fails with ERROR_ACCESS_DENIED (5), the process DOES
    exist but this caller lacks rights to query it (e.g. a differently-
    privileged sibling session) -- that must be reported as alive, never
    as dead, or a live holder's ticket could be wrongly reclaimed out
    from under it.
    """
    if os.name == "nt":
        import ctypes

        kernel32 = _get_kernel32()
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            if not kernel32.CloseHandle(handle):
                logger.warning(
                    "pid_alive: CloseHandle failed for pid=%s (possible handle leak)", pid
                )
            return True
        err = ctypes.get_last_error()
        if err == _ERROR_ACCESS_DENIED:
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except OverflowError:
        return False
    return True


# --------------------------------------------------------------------------
# Ticket lifecycle
# --------------------------------------------------------------------------
def write_ticket(
    seq: int,
    pid: int,
    run_id: str,
    session: str,
    state: str,
    query_preview: str,
    mode: str = "research",
) -> Path:
    """Write a new ticket file and return its path.

    Ticket schema: {seq, pid, run_id, session, state ("waiting"/"active"),
    query_preview (truncated to 80 chars), mode, enqueued_at, heartbeat_at}.
    """
    ensure_dirs()
    now = time.time()
    data: Dict[str, Any] = {
        "seq": seq,
        "pid": pid,
        "run_id": run_id,
        "session": session,
        "state": state,
        "query_preview": (query_preview or "")[:80],
        "mode": mode,
        "enqueued_at": now,
        "heartbeat_at": now,
    }
    path = _ticket_path(seq, pid)
    _atomic_write(path, json.dumps(data))
    return path


def touch_heartbeat(path: Path) -> None:
    """Refresh a ticket's heartbeat_at timestamp. Best-effort; a ticket that
    has already been removed (e.g. by a concurrent GC) is silently skipped.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("touch_heartbeat: ticket %s unreadable (%s), skipping", path, exc)
        return
    data["heartbeat_at"] = time.time()
    try:
        _atomic_write(path, json.dumps(data))
    except OSError as exc:
        logger.debug("touch_heartbeat: failed to write ticket %s (%s)", path, exc)


def read_tickets() -> List[Tuple[Path, Dict[str, Any]]]:
    """Return all currently-on-disk tickets, sorted by seq ascending.
    Does NOT garbage-collect -- see live_tickets() for that.

    A ticket file that is valid JSON but missing (or has a non-int)
    `seq` is skipped rather than propagating a KeyError out of the sort
    -- an uncaught KeyError here would crash every waiter's poll loop
    (`live_tickets()` / `_wait_for_promotion()`), taking down the whole
    queue over a single malformed/torn ticket.
    """
    tickets_dir = _tickets_dir()
    if not tickets_dir.exists():
        return []
    out: List[Tuple[Path, Dict[str, Any]]] = []
    for f in tickets_dir.glob("*.ticket"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("read_tickets: skipping unreadable ticket %s (%s)", f, exc)
            continue
        if not isinstance(data.get("seq"), int):
            logger.warning("read_tickets: skipping ticket %s with missing/invalid seq", f)
            continue
        out.append((f, data))
    out.sort(key=lambda t: t[1].get("seq", 0))
    return out


def gc_dead_tickets() -> None:
    """Reclaim (delete) tickets whose owning process is dead, or whose
    heartbeat has gone stale past TTL_S. PID-liveness is the PRIMARY
    signal (fast, unambiguous); heartbeat staleness is a backstop only,
    so a long GC/scheduler pause never wrongly reclaims a live holder's
    ticket while it may still hold active.lock.
    """
    now = time.time()
    for f, d in read_tickets():
        try:
            alive = pid_alive(d["pid"])
            stale = (now - d.get("heartbeat_at", 0)) > TTL_S
        except Exception as exc:
            # Malformed ticket data -- treat as reclaimable.
            logger.warning("gc_dead_tickets: malformed ticket %s (%s), reclaiming", f, exc)
            alive, stale = False, True
        if not alive or stale:
            try:
                f.unlink()
            except OSError as exc:
                logger.debug("gc_dead_tickets: could not unlink %s (%s)", f, exc)


def live_tickets() -> List[Tuple[Path, Dict[str, Any]]]:
    """GC dead tickets, then return the remaining ones sorted by seq."""
    gc_dead_tickets()
    return read_tickets()


# --------------------------------------------------------------------------
# Sequence allocator
# --------------------------------------------------------------------------
def atomic_next_seq() -> int:
    """Allocate the next monotonic sequence number, race-free across
    processes via a brief FileLock around a read-increment-write of the
    `counter` file.
    """
    ensure_dirs()
    with FileLock(str(_counter_lock_path()), timeout=10):
        counter_path = _counter_path()
        n = 0
        if counter_path.exists():
            try:
                n = int(counter_path.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                n = 0
        n += 1
        _atomic_write(counter_path, str(n))
        return n


# --------------------------------------------------------------------------
# Central activity log + live snapshot
# --------------------------------------------------------------------------
def log_event(
    event: str,
    run_id: str,
    session: str,
    seq: int,
    query_preview: str,
    mode: str,
    **extra: Any,
) -> None:
    """Append one JSON line describing a queue transition to ACTIVITY_LOG.

    Events: enqueued | started | completed | error | timeout. Extra
    keyword args (wait_s, duration_s, status, error, ...) are merged
    directly into the record.
    """
    ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    record: Dict[str, Any] = {
        "ts": time.time(),
        "run_id": run_id,
        "event": event,
        "session": session,
        "seq": seq,
        "query_preview": (query_preview or "")[:80],
        "mode": mode,
        **extra,
    }
    line = json.dumps(record)
    # Best-effort: a busy activity-log lock must never break the caller's
    # actual queue flow (enqueue/promote/release). Losing one log line to
    # contention is acceptable; raising Timeout out of an unguarded
    # enqueued/started/completed call site is not.
    try:
        with FileLock(str(_activity_lock_path()), timeout=ACTIVITY_LOG_LOCK_TIMEOUT_S):
            with open(ACTIVITY_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Timeout:
        logger.warning(
            "log_event: activity log lock busy past %ss, dropping event=%s run_id=%s",
            ACTIVITY_LOG_LOCK_TIMEOUT_S, event, run_id,
        )


def _read_activity_events(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Read parsed JSONL records from ACTIVITY_LOG, optionally limited to the
    last `limit` lines. Malformed lines are skipped, not fatal.
    """
    if not ACTIVITY_LOG.exists():
        return []
    try:
        lines = ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.debug("_read_activity_events: could not read %s (%s)", ACTIVITY_LOG, exc)
        return []
    if limit is not None:
        lines = lines[-limit:]
    out: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.debug("_read_activity_events: skipping malformed line (%s)", exc)
            continue
    return out


def _read_recent_activity(limit: int = 20) -> List[Dict[str, Any]]:
    return _read_activity_events(limit=limit)


def _is_same_local_day(ts: float, now: float) -> bool:
    """True if `ts` falls on the same local calendar day as `now`."""
    a = time.localtime(ts)
    b = time.localtime(now)
    return (a.tm_year, a.tm_yday) == (b.tm_year, b.tm_yday)


def _compute_today_stats(now: float) -> Tuple[int, int]:
    """Compute (total_today, errors_today) from the activity log.

    `total_today` counts DISTINCT RUNS (not raw events) that started today,
    identified by a `started` event's run_id. `errors_today` counts `error`
    and `timeout` events logged today. Bounded to the last
    STATS_LOOKBACK_LINES lines of the activity log for performance (this is
    read on every publish_snapshot() call, which can fire every ~1.5s per
    waiting session) -- more than sufficient to cover a single day's events
    under normal usage.
    """
    events = _read_activity_events(limit=STATS_LOOKBACK_LINES)
    started_run_ids_today: set[str] = set()
    errors_today = 0
    for e in events:
        ts = e.get("ts")
        if not isinstance(ts, (int, float)) or not _is_same_local_day(ts, now):
            continue
        event = e.get("event")
        if event == "started":
            run_id = e.get("run_id")
            if run_id:
                started_run_ids_today.add(run_id)
        elif event in ("error", "timeout"):
            errors_today += 1
    return len(started_run_ids_today), errors_today


def publish_snapshot(my_seq: Optional[int] = None) -> None:
    """Regenerate SNAPSHOT from live_tickets() plus a bounded tail of the
    activity log. Atomic tmp+os.replace write. Carries a monotonic
    `version` (wall-clock nanoseconds -- comparable across processes,
    unlike time.monotonic_ns() whose reference point is undefined across
    interpreters) plus `updated_at`, so a reader can discard an
    out-of-order last-writer-wins snapshot write.

    `my_seq` is accepted for API symmetry with the polling loop in
    acquire_slot (every caller's own ticket is already reflected via
    live_tickets(), so it is not otherwise consulted) -- kept as a
    parameter so a future caller-relative optimization (e.g. only
    recomputing this caller's position) doesn't need a signature change.
    """
    ensure_dirs()
    tickets = live_tickets()
    now = time.time()

    active: Optional[Dict[str, Any]] = None
    queued: List[Dict[str, Any]] = []
    for _, d in tickets:
        if d.get("state") == "active":
            # active_since is stamped on the ticket at promotion time (see
            # acquire_slot). Fall back to enqueued_at only for tickets
            # written before that field existed, so elapsed_s reflects
            # time-since-promotion, not time-since-enqueue (which double-
            # counts the wait already reported via the "started" wait_s
            # log event).
            started_at = d.get("active_since", d["enqueued_at"])
            active = {
                "run_id": d["run_id"],
                "session": d["session"],
                "query_preview": d["query_preview"],
                "started_at": started_at,
                "elapsed_s": round(now - started_at, 1),
                "heartbeat_age_s": round(now - d["heartbeat_at"], 1),
            }
        else:
            queued.append(
                {
                    "run_id": d["run_id"],
                    "session": d["session"],
                    "position": 0,  # filled below, 1-indexed
                    "query_preview": d["query_preview"],
                    "wait_s": round(now - d["enqueued_at"], 1),
                }
            )
    for i, item in enumerate(queued, start=1):
        item["position"] = i

    recent = _read_recent_activity(limit=20)
    total_today, errors_today = _compute_today_stats(now)

    snapshot = {
        "version": time.time_ns(),
        "updated_at": now,
        "active": active,
        "queued": queued,
        "recent": recent,
        "stats": {
            "depth": len(queued) + (1 if active else 0),
            "total_today": total_today,
            "errors_today": errors_today,
        },
    }
    text = json.dumps(snapshot)
    # Many processes can regenerate this snapshot concurrently while
    # polling. A short FileLock serializes the replace so concurrent
    # os.replace() calls onto the same destination never race into a
    # transient Windows ERROR_ACCESS_DENIED. Best-effort only: the
    # ticket dir remains the true source of truth, so a rare lock
    # timeout here must never block the caller's actual queue progress.
    try:
        with FileLock(str(_snapshot_lock_path()), timeout=SNAPSHOT_LOCK_TIMEOUT_S):
            _atomic_write(SNAPSHOT, text)
    except Timeout:
        logger.warning(
            "publish_snapshot: snapshot lock busy past %ss, skipping this write "
            "(ticket dir remains source of truth)", SNAPSHOT_LOCK_TIMEOUT_S,
        )


# --------------------------------------------------------------------------
# Heartbeat thread
# --------------------------------------------------------------------------
class _HeartbeatThread:
    """Daemon thread that periodically touches a ticket's heartbeat_at.

    Started for BOTH waiting and active tickets so a crashed *waiter* is
    also reclaimable via TTL_S, not just a crashed active holder.
    """

    def __init__(self, ticket_path: Path, interval_s: float = HEARTBEAT_INTERVAL_S) -> None:
        self._ticket_path = ticket_path
        self._interval_s = interval_s
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="research-queue-heartbeat", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            touch_heartbeat(self._ticket_path)

    def stop(self) -> None:
        """Signal the heartbeat thread to stop and wait for it to exit."""
        self._stop_event.set()
        self._thread.join(timeout=5.0)


# --------------------------------------------------------------------------
# The core primitive
# --------------------------------------------------------------------------
@contextmanager
def acquire_slot(session: str, query_preview: str, mode: str = "research") -> Iterator[Dict[str, Any]]:
    """Block (cross-process, FIFO) until this caller is the sole active
    Perplexity run, then yield a slot descriptor `{run_id, queued,
    waited_s, seq}` for the duration of the `with` block.

    If RESEARCH_QUEUE_ENABLED=="0", this is a no-op context manager that
    yields `{"run_id": <uuid>, "queued": False}` immediately (today's
    unserialized behavior).

    Raises QueueTimeout if MAX_WAIT_S elapses before promotion to active.
    """
    if os.environ.get("RESEARCH_QUEUE_ENABLED", "1") == "0":
        yield {"run_id": str(uuid.uuid4()), "queued": False}
        return

    ensure_dirs()
    run_id = str(uuid.uuid4())
    pid = os.getpid()
    seq = atomic_next_seq()
    ticket_path = write_ticket(seq, pid, run_id, session, "waiting", query_preview, mode)
    log_event("enqueued", run_id, session, seq, query_preview, mode)

    heartbeat = _HeartbeatThread(ticket_path)
    lock = FileLock(str(_active_lock_path()), thread_local=False)
    lock_acquired = False
    t0 = time.time()

    def _wait_for_promotion() -> float:
        """Block until this ticket is promoted to active; return wait_s."""
        nonlocal lock_acquired
        while True:
            tickets = live_tickets()
            head = tickets[0][1] if tickets else None

            if head is not None and head["seq"] == seq:
                try:
                    lock.acquire(timeout=0)
                    lock_acquired = True
                except Timeout:
                    # A failed non-blocking acquire here is NORMAL -- a
                    # crashed holder's OS lock handle can lag a moment
                    # before it's actually released. Never fatal: back
                    # off and retry. Only a genuine filelock/OS error
                    # (permissions/FS -- NOT Timeout) should propagate.
                    lock_acquired = False

                if lock_acquired:
                    # Strict-FIFO belt-and-suspenders (mutex is already
                    # safe without this -- it only tightens fairness):
                    # re-confirm we're still the lowest-seq live ticket
                    # now that we hold active.lock.
                    recheck = live_tickets()
                    recheck_head = recheck[0][1] if recheck else None
                    if recheck_head is None or recheck_head["seq"] != seq:
                        lock.release()
                        lock_acquired = False
                        time.sleep(POLL_S)
                        continue
                    return round(time.time() - t0, 1)

            if time.time() - t0 > MAX_WAIT_S:
                waited_s = round(time.time() - t0, 1)
                log_event(
                    "timeout", run_id, session, seq, query_preview, mode,
                    wait_s=waited_s,
                )
                raise QueueTimeout(
                    f"research_queue: wait exceeded MAX_WAIT_S={MAX_WAIT_S}s "
                    f"(run_id={run_id}, seq={seq})"
                )

            touch_heartbeat(ticket_path)
            publish_snapshot(my_seq=seq)
            time.sleep(POLL_S)

    try:
        waited_s = _wait_for_promotion()
    except QueueTimeout:
        # Already logged its own "timeout" event above -- avoid a
        # duplicate "error" log for the same failure.
        heartbeat.stop()
        try:
            ticket_path.unlink()
        except OSError as exc:
            logger.debug("acquire_slot: could not unlink ticket %s after timeout (%s)", ticket_path, exc)
        publish_snapshot()
        raise
    except BaseException as exc:
        log_event(
            "error", run_id, session, seq, query_preview, mode,
            error=str(exc), wait_s=round(time.time() - t0, 1),
        )
        heartbeat.stop()
        try:
            ticket_path.unlink()
        except OSError as unlink_exc:
            logger.debug("acquire_slot: could not unlink ticket %s after error (%s)", ticket_path, unlink_exc)
        publish_snapshot()
        raise

    # Promoted to active.
    active_since = time.time()
    try:
        data = json.loads(ticket_path.read_text(encoding="utf-8"))
        data["state"] = "active"
        data["active_since"] = active_since
        _atomic_write(ticket_path, json.dumps(data))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "acquire_slot: failed to stamp ticket %s as active (run_id=%s): %s",
            ticket_path, run_id, exc,
        )
    log_event("started", run_id, session, seq, query_preview, mode, wait_s=waited_s)
    publish_snapshot()

    try:
        yield {"run_id": run_id, "queued": True, "waited_s": waited_s, "seq": seq}
    except BaseException as exc:
        duration_s = round(time.time() - active_since, 1)
        log_event(
            "error", run_id, session, seq, query_preview, mode,
            error=str(exc), duration_s=duration_s,
        )
        raise
    else:
        duration_s = round(time.time() - active_since, 1)
        log_event(
            "completed", run_id, session, seq, query_preview, mode,
            duration_s=duration_s,
        )
    finally:
        if lock_acquired:
            try:
                lock.release()
            except Exception as exc:
                # Notable (not routine-best-effort): a failed release of
                # the active-run mutex is worth surfacing even though we
                # still proceed with cleanup -- the OS frees the handle
                # on process exit regardless, so this is not fatal.
                logger.warning("acquire_slot: active.lock release failed (run_id=%s): %s", run_id, exc)
        heartbeat.stop()
        try:
            ticket_path.unlink()
        except OSError as exc:
            logger.debug("acquire_slot: could not unlink ticket %s on cleanup (%s)", ticket_path, exc)
        publish_snapshot()

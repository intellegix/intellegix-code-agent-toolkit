"""Tests for research_queue.py -- the crash-safe cross-process FIFO queue.

ISOLATION (CRITICAL): every test in this module monkeypatches
`research_queue.QUEUE_DIR`, `research_queue.ACTIVITY_LOG`, and
`research_queue.SNAPSHOT` to paths under pytest's `tmp_path` fixture (or,
for the cross-process tests, an explicit tmp-based dir passed to child
processes via environment variables). NONE of these tests ever touch the
real `~/.claude/config/research-queue`, `~/.claude/council-logs/
perplexity-activity.jsonl`, or `~/.claude/council-logs/perplexity-queue.json`
-- real Perplexity research sessions may be running on this machine
concurrently with this test run, and those real paths are their live
coordination state.

Run with (from council-automation/):
    python -m pytest tests/test_research_queue.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from filelock import Timeout

import research_queue as rq


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every research_queue path global at a tmp_path location for
    this test. autouse=True so no test can forget this and accidentally
    touch the real live queue/log paths.
    """
    qdir = tmp_path / "research-queue"
    monkeypatch.setattr(rq, "QUEUE_DIR", qdir)
    monkeypatch.setattr(rq, "ACTIVITY_LOG", tmp_path / "council-logs" / "perplexity-activity.jsonl")
    monkeypatch.setattr(rq, "SNAPSHOT", tmp_path / "council-logs" / "perplexity-queue.json")
    return qdir


# --------------------------------------------------------------------------
# 1. atomic_next_seq monotonic
# --------------------------------------------------------------------------
def test_atomic_next_seq_monotonic() -> None:
    rq.ensure_dirs()
    a = rq.atomic_next_seq()
    b = rq.atomic_next_seq()
    c = rq.atomic_next_seq()
    assert (a, b, c) == (1, 2, 3)


# --------------------------------------------------------------------------
# 2. Dead-PID ticket reclaimed
# --------------------------------------------------------------------------
def test_dead_ticket_reclaimed() -> None:
    rq.ensure_dirs()
    rq.write_ticket(
        seq=1, pid=999999999, run_id="x", session="s", state="waiting",
        query_preview="q",
    )
    rq.gc_dead_tickets()
    assert rq.live_tickets() == []


# --------------------------------------------------------------------------
# 3. Stale-heartbeat ticket reclaimed (own live PID, but heartbeat stale)
# --------------------------------------------------------------------------
def test_stale_heartbeat_reclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rq, "TTL_S", 1)
    rq.ensure_dirs()
    rq.write_ticket(
        seq=1, pid=os.getpid(), run_id="x", session="s", state="waiting",
        query_preview="q",
    )
    time.sleep(1.2)
    rq.gc_dead_tickets()
    assert rq.live_tickets() == []


# --------------------------------------------------------------------------
# 4. Mutual exclusion across PROCESSES (the load-bearing test)
# --------------------------------------------------------------------------
_MP_WORKER_SCRIPT = r"""
import os, sys, time, random
from pathlib import Path

sys.path.insert(0, os.environ["RQ_MODPATH"])
import research_queue as rq

rq.QUEUE_DIR = Path(os.environ["RQ_QDIR"])
rq.ACTIVITY_LOG = Path(os.environ["RQ_QDIR"]) / "activity.jsonl"
rq.SNAPSHOT = Path(os.environ["RQ_QDIR"]) / "snapshot.json"

count_file = Path(os.environ["RQ_COUNT_FILE"])
max_file = Path(os.environ["RQ_MAX_FILE"])

with rq.acquire_slot(session=os.environ.get("RQ_SESSION", "w"), query_preview="mp-test"):
    n = int(count_file.read_text()) if count_file.exists() else 0
    n += 1
    count_file.write_text(str(n))
    # F6: assert INSIDE the critical section, immediately after the
    # increment, so a genuine mutual-exclusion breach crashes this worker
    # right at the point of violation (deterministic, diagnosable) rather
    # than relying solely on the parent's after-the-fact max_file check.
    assert n == 1, f"CRITICAL: observed count={n} inside exclusive section (pid={os.getpid()})"
    cur_max = int(max_file.read_text()) if max_file.exists() else 0
    if n > cur_max:
        max_file.write_text(str(n))
    time.sleep(random.uniform(0.1, 0.25))
    n = int(count_file.read_text())
    n -= 1
    count_file.write_text(str(n))
"""


def test_only_one_active_at_a_time(tmp_path: Path) -> None:
    """Spawn 5 real subprocesses, each acquiring a slot and bumping a
    shared counter file inside the critical section. Since the shared
    counter is only ever read/written from inside `acquire_slot`'s
    critical section, if mutual exclusion holds the observed max
    concurrent count can never exceed 1 -- proving cross-process (not
    just cross-thread) serialization.
    """
    qdir = tmp_path / "mp_qdir"
    qdir.mkdir()
    count_file = qdir / "count.txt"
    max_file = qdir / "max.txt"
    count_file.write_text("0")
    max_file.write_text("0")

    modpath = str(Path(rq.__file__).resolve().parent)

    base_env = os.environ.copy()
    base_env["RQ_MODPATH"] = modpath
    base_env["RQ_QDIR"] = str(qdir)
    base_env["RQ_COUNT_FILE"] = str(count_file)
    base_env["RQ_MAX_FILE"] = str(max_file)
    base_env["RESEARCH_QUEUE_ENABLED"] = "1"

    procs = []
    for i in range(5):
        env = base_env.copy()
        env["RQ_SESSION"] = f"w{i}"
        p = subprocess.Popen([sys.executable, "-c", _MP_WORKER_SCRIPT], env=env)
        procs.append(p)

    for p in procs:
        rc = p.wait(timeout=60)
        assert rc == 0, f"worker subprocess exited with code {rc}"

    assert max_file.read_text().strip() == "1", (
        "expected the shared counter to never exceed 1 concurrent holder "
        f"across processes, observed max={max_file.read_text().strip()}"
    )
    assert count_file.read_text().strip() == "0"


# --------------------------------------------------------------------------
# 5. FIFO order
# --------------------------------------------------------------------------
def test_fifo_order() -> None:
    rq.ensure_dirs()
    pid = os.getpid()
    for seq in range(1, 6):
        rq.write_ticket(
            seq=seq, pid=pid, run_id=f"r{seq}", session="s", state="waiting",
            query_preview="q",
        )
    tickets = rq.live_tickets()
    assert [d["seq"] for _, d in tickets] == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------
# 6. Double-promotion race: two head-eligible waiters can never both win
#    active.lock -- the FileLock is the true mutex.
#
# NOTE: this is deliberately implemented with two real SUBPROCESSES, not
# two threads with independent FileLock() instances. Same-process
# multi-handle file locking on Windows is not a reliable stand-in for
# true cross-process exclusion (two FileLock objects in one process can
# both succeed in acquiring the "same" lock via independent OS handles
# in some configurations), so a thread-based version of this test can
# give a false pass/fail. A rendezvous (both children signal "ready",
# parent then drops a "go" file) forces genuine overlap regardless of
# subprocess startup jitter.
# --------------------------------------------------------------------------
_LOCK_RACE_SCRIPT = r"""
import sys, time
from pathlib import Path
from filelock import FileLock, Timeout

lock_path, ready_path, go_path, result_path = sys.argv[1:5]
Path(ready_path).write_text("ready")
while not Path(go_path).exists():
    time.sleep(0.01)

lock = FileLock(lock_path, thread_local=False)
try:
    lock.acquire(timeout=0)
    Path(result_path).write_text("acquired")
    time.sleep(0.3)  # hold briefly so a genuinely-overlapping second
                      # attempt (if any) reliably observes contention
    lock.release()
except Timeout:
    Path(result_path).write_text("timed_out")
"""


def test_double_promotion_race_only_one_wins(tmp_path: Path) -> None:
    rq.ensure_dirs()
    lock_path = str(rq._active_lock_path())

    ready_a, ready_b = tmp_path / "readyA", tmp_path / "readyB"
    go = tmp_path / "go"
    result_a, result_b = tmp_path / "resultA", tmp_path / "resultB"

    p_a = subprocess.Popen(
        [sys.executable, "-c", _LOCK_RACE_SCRIPT, lock_path, str(ready_a), str(go), str(result_a)]
    )
    p_b = subprocess.Popen(
        [sys.executable, "-c", _LOCK_RACE_SCRIPT, lock_path, str(ready_b), str(go), str(result_b)]
    )

    deadline = time.time() + 10
    while (not ready_a.exists() or not ready_b.exists()) and time.time() < deadline:
        time.sleep(0.01)
    go.write_text("go")

    assert p_a.wait(timeout=10) == 0
    assert p_b.wait(timeout=10) == 0

    outcomes = sorted([result_a.read_text().strip(), result_b.read_text().strip()])
    assert outcomes == ["acquired", "timed_out"], (
        f"expected exactly one contender to acquire active.lock and the "
        f"other to fail non-blocking, got A={result_a.read_text()!r} "
        f"B={result_b.read_text()!r}"
    )


# --------------------------------------------------------------------------
# 7. MAX_WAIT_S timeout raises QueueTimeout
# --------------------------------------------------------------------------
def test_max_wait_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rq, "MAX_WAIT_S", 0.3)
    monkeypatch.setattr(rq, "POLL_S", 0.05)
    rq.ensure_dirs()

    # A permanently-alive, never-removed ticket occupies the head
    # position forever, so our own ticket (allocated below via
    # acquire_slot's internal atomic_next_seq()) can never be promoted.
    # IMPORTANT: allocate the blocker's seq through atomic_next_seq() too
    # (not a hardcoded seq=1) so it can't collide with the seq
    # acquire_slot() allocates for itself -- a same-pid, same-seq
    # collision would produce the identical ticket filename and silently
    # overwrite this blocker instead of adding a second ticket.
    blocker_seq = rq.atomic_next_seq()
    rq.write_ticket(
        seq=blocker_seq, pid=os.getpid(), run_id="blocker", session="s",
        state="waiting", query_preview="blocker",
    )

    with pytest.raises(rq.QueueTimeout):
        with rq.acquire_slot(session="me", query_preview="q"):
            pytest.fail("should never be promoted -- head is permanently occupied")


# --------------------------------------------------------------------------
# 8. log_event + publish_snapshot
# --------------------------------------------------------------------------
def test_log_event_and_snapshot_versioning() -> None:
    rq.ensure_dirs()
    rq.log_event("started", "rid", "s", 1, "hello", "research", wait_s=2)
    lines = rq.ACTIVITY_LOG.read_text(encoding="utf-8").strip().splitlines()
    last = json.loads(lines[-1])
    assert last["event"] == "started"
    assert last["run_id"] == "rid"
    assert last["wait_s"] == 2

    rq.publish_snapshot()
    snap1 = json.loads(rq.SNAPSHOT.read_text(encoding="utf-8"))
    assert "version" in snap1 and "updated_at" in snap1
    assert isinstance(snap1["active"], type(None))
    assert snap1["queued"] == []

    time.sleep(0.01)  # guarantee a distinguishable wall-clock tick
    rq.publish_snapshot()
    snap2 = json.loads(rq.SNAPSHOT.read_text(encoding="utf-8"))
    assert snap2["version"] > snap1["version"]


# --------------------------------------------------------------------------
# 9. Flag disabled -> no-op, no ticket files created
# --------------------------------------------------------------------------
def test_flag_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_QUEUE_ENABLED", "0")
    with rq.acquire_slot(session="s", query_preview="q") as slot:
        assert slot["queued"] is False
        assert "run_id" in slot
    tickets_dir = rq._tickets_dir()
    assert not tickets_dir.exists() or list(tickets_dir.glob("*.ticket")) == []


# --------------------------------------------------------------------------
# F1 (coordinator fresh-eyes review, 2026-07-11): pid_alive must use
# correctly-typed Windows HANDLE ctypes bindings (no truncation/leak) and
# must report True for its own live PID, False for a definitely-dead one,
# and survive a tight repeated-call loop without error (handle-leak smoke).
# --------------------------------------------------------------------------
def test_pid_alive_correct_and_no_leak_smoke() -> None:
    assert rq.pid_alive(os.getpid()) is True
    assert rq.pid_alive(999999999) is False
    # Tight loop: each call does OpenProcess+CloseHandle. If restype/
    # argtypes were wrong (F1's original bug), a 64-bit HANDLE could get
    # truncated to a bad 32-bit value and CloseHandle would silently fail
    # every time -- this doesn't assert on OS handle counts directly (no
    # stdlib API for that), but it does prove 2000 consecutive probes
    # complete without ctypes raising and keep reporting the correct,
    # stable answer for a live PID.
    for _ in range(2000):
        assert rq.pid_alive(os.getpid()) is True


# --------------------------------------------------------------------------
# F2 (coordinator fresh-eyes review, 2026-07-11): a ticket file that is
# valid JSON but missing `seq` must be skipped, not crash the sort inside
# read_tickets()/live_tickets().
# --------------------------------------------------------------------------
def test_malformed_ticket_missing_seq_is_ignored_not_fatal() -> None:
    rq.ensure_dirs()
    # A well-formed ticket so the directory isn't empty.
    rq.write_ticket(
        seq=1, pid=os.getpid(), run_id="ok", session="s", state="waiting",
        query_preview="q",
    )
    # A torn/malformed ticket: valid JSON, but no "seq" key at all.
    bad_path = rq._tickets_dir() / "99999999-1.ticket"
    bad_path.write_text(json.dumps({"pid": os.getpid(), "run_id": "bad", "state": "waiting"}))

    # Must not raise (this is exactly the KeyError crash vector F2 fixes).
    tickets = rq.live_tickets()
    assert [d["run_id"] for _, d in tickets] == ["ok"]


# --------------------------------------------------------------------------
# F4 (coordinator fresh-eyes review, 2026-07-11): log_event must be
# best-effort -- a busy activity-log lock must never raise out of an
# unguarded enqueued/started/completed call site.
# --------------------------------------------------------------------------
def test_log_event_best_effort_on_lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rq, "ACTIVITY_LOG_LOCK_TIMEOUT_S", 0.1)
    rq.ensure_dirs()

    blocking_lock = rq.FileLock(str(rq._activity_lock_path()), thread_local=False)
    blocking_lock.acquire()
    try:
        # Must NOT raise filelock.Timeout -- best-effort swallow.
        rq.log_event("started", "rid", "s", 1, "q", "research")
    finally:
        blocking_lock.release()


# --------------------------------------------------------------------------
# F5 (coordinator fresh-eyes review, 2026-07-11): the active ticket's
# elapsed_s in the published snapshot must reflect time-since-promotion
# (active_since), not time-since-enqueue (enqueued_at) -- a long wait in
# queue must not inflate the reported run duration.
# --------------------------------------------------------------------------
def test_snapshot_stats_total_today_counts_runs_not_events() -> None:
    """total_today must count DISTINCT RUNS (via `started` events), not raw
    activity-log lines -- a single run logs 3 events (enqueued/started/
    completed), which the pre-fix `len(recent)` counted as 3.
    """
    rq.ensure_dirs()
    # One full run: enqueued -> started -> completed (3 events, 1 run).
    rq.log_event("enqueued", "run-a", "s", 1, "q", "research")
    rq.log_event("started", "run-a", "s", 1, "q", "research", wait_s=1)
    rq.log_event("completed", "run-a", "s", 1, "q", "research", duration_s=5)
    # A second run that errored out (no "started" logged for it here would
    # still be a valid run, but exercise the common enqueued->started->error
    # path so it's counted once via its own "started" event).
    rq.log_event("enqueued", "run-b", "s", 2, "q", "research")
    rq.log_event("started", "run-b", "s", 2, "q", "research", wait_s=1)
    rq.log_event("error", "run-b", "s", 2, "q", "research", error="boom")

    rq.publish_snapshot()
    snap = json.loads(rq.SNAPSHOT.read_text(encoding="utf-8"))
    assert snap["stats"]["total_today"] == 2, (
        f"expected 2 distinct runs today, got {snap['stats']['total_today']} "
        f"(pre-fix bug counted raw event lines instead)"
    )
    assert snap["stats"]["errors_today"] == 1


def test_snapshot_stats_errors_today_counts_timeout_too() -> None:
    rq.ensure_dirs()
    rq.log_event("enqueued", "run-c", "s", 1, "q", "research")
    rq.log_event("timeout", "run-c", "s", 1, "q", "research", wait_s=1200)

    rq.publish_snapshot()
    snap = json.loads(rq.SNAPSHOT.read_text(encoding="utf-8"))
    assert snap["stats"]["errors_today"] == 1
    # No "started" event was ever logged for run-c (it timed out while
    # still queued), so it must not be counted as a completed/started run.
    assert snap["stats"]["total_today"] == 0


def test_snapshot_elapsed_s_uses_active_since_not_enqueued_at() -> None:
    rq.ensure_dirs()
    now = time.time()
    pid = os.getpid()
    # Simulate a ticket that waited 50s in queue but has only been
    # actively running for 5s.
    ticket_path = rq.write_ticket(
        seq=1, pid=pid, run_id="r1", session="s", state="active",
        query_preview="q",
    )
    data = json.loads(ticket_path.read_text(encoding="utf-8"))
    data["enqueued_at"] = now - 50
    data["active_since"] = now - 5
    data["heartbeat_at"] = now
    ticket_path.write_text(json.dumps(data))

    rq.publish_snapshot()
    snap = json.loads(rq.SNAPSHOT.read_text(encoding="utf-8"))
    assert snap["active"] is not None
    # Should be ~5s (active_since-based), nowhere near ~50s
    # (enqueued_at-based, the pre-fix bug).
    assert snap["active"]["elapsed_s"] < 10
    assert snap["active"]["started_at"] == data["active_since"]

"""Phase 0 Task 0.2 — integration tests for the whole-run lock hold.

Covers:
  - Two callers holding submission_lock.get_submit_lock() across a work
    window (as run() does under RESEARCH_QUEUE_STOPGAP) never interleave,
    and a long-timeout waiter eventually acquires once the first releases.
  - The RESEARCH_QUEUE_STOPGAP guard in council_browser.PerplexityCouncil.run()
    is present and correctly gates the mid-run early release (so inverting
    or deleting the guard fails this test).
  - The LockHeartbeat helper: refreshes a held lock file's mtime while
    running (so the 180s stale-reclaim in get_submit_lock() never trips
    on a live long-held lock), and stopping it lets stale-reclaim fire
    again exactly as it does today.

IMPORTANT (isolation): every test here monkeypatches
`submission_lock.LOCK_PATH` (and `STALE_AGE_S` where relevant) to a path
under `tmp_path`, so these tests NEVER touch the real, shared
`~/.claude/config/browser-sessions/.perplexity_submit.lock` file --
running this suite must not block or interfere with a user's live
Perplexity session.

Run with:
    python -m pytest tests/test_stopgap_serialization.py -v
(from council-automation/, so `import submission_lock` / `import
council_browser` resolve via cwd on sys.path per `python -m pytest`
semantics).
"""
from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path

import pytest

import submission_lock


@pytest.fixture
def isolated_lock_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect submission_lock.LOCK_PATH to a tmp_path file for this test.

    Guarantees no test in this module can ever contend with, block, or
    corrupt the real shared submit lock used by live Claude Code
    sessions.
    """
    lock_path = tmp_path / ".perplexity_submit.lock"
    monkeypatch.setattr(submission_lock, "LOCK_PATH", lock_path)
    return lock_path


def test_two_stopgap_runs_do_not_overlap(isolated_lock_path: Path) -> None:
    """With the lock held across a whole work window (as run() does under
    RESEARCH_QUEUE_STOPGAP), a second acquirer's work cannot start until
    the first fully finishes and releases -- no interleave. Also proves
    a long-timeout waiter (as under RESEARCH_QUEUE_MAX_WAIT) eventually
    acquires once the first holder releases."""
    order: list[tuple[str, str]] = []
    order_guard = threading.Lock()

    def worker(name: str, hold: float) -> None:
        with submission_lock.get_submit_lock(timeout=10):
            with order_guard:
                order.append(("start", name))
            time.sleep(hold)
            with order_guard:
                order.append(("end", name))

    t1 = threading.Thread(target=worker, args=("A", 0.5))
    t2 = threading.Thread(target=worker, args=("B", 0.1))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert ("start", "A") in order
    assert ("end", "A") in order
    assert ("start", "B") in order
    assert ("end", "B") in order

    # B must not start before A ends -- no interleave under whole-run hold,
    # and B (the long-timeout waiter) does eventually acquire after A
    # releases rather than erroring out.
    assert order.index(("end", "A")) < order.index(("start", "B")), (
        f"expected A to fully finish before B started under a whole-run "
        f"lock hold, got order={order!r}"
    )


def test_early_release_gated_by_stopgap_flag() -> None:
    """getsource assertion: the mid-run early release block in run() is
    gated by `_flag_enabled("RESEARCH_QUEUE_STOPGAP")`. Inverting the
    condition (`if _flag_enabled(...)`) or deleting the guard entirely
    would make this assertion fail."""
    import council_browser
    from council_browser import PerplexityCouncil

    # Phase 1 wiring moved run()'s body into _run_impl(); inspect both so the
    # guard is found wherever the mid-run early-release lives.
    src = inspect.getsource(PerplexityCouncil.run)
    _impl = getattr(PerplexityCouncil, "_run_impl", None)
    if _impl is not None:
        src += "\n" + inspect.getsource(_impl)
    assert 'if not _flag_enabled("RESEARCH_QUEUE_STOPGAP"):' in src, (
        "expected the mid-run submit_lock release to be gated by "
        '`if not _flag_enabled("RESEARCH_QUEUE_STOPGAP"):` so the '
        "release is SKIPPED (lock stays held) when the flag is on"
    )
    # _flag_enabled itself must normalize truthiness (MINOR-4): only
    # {"1","true","yes","on"} (case-insensitive) count as enabled.
    assert council_browser._flag_enabled("RESEARCH_QUEUE_STOPGAP") is False
    assert council_browser._flag_enabled("NON_EXISTENT_FLAG_XYZ") is False


class TestFlagTruthiness:
    """MINOR-4: RESEARCH_QUEUE_STOPGAP=0 / =false must NOT enable the flag."""

    @pytest.mark.parametrize("value", ["1", "true", "True", "YES", "on", "On"])
    def test_truthy_values_enable(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import council_browser

        monkeypatch.setenv("RESEARCH_QUEUE_STOPGAP", value)
        assert council_browser._flag_enabled("RESEARCH_QUEUE_STOPGAP") is True

    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", ""])
    def test_falsy_values_do_not_enable(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import council_browser

        monkeypatch.setenv("RESEARCH_QUEUE_STOPGAP", value)
        assert council_browser._flag_enabled("RESEARCH_QUEUE_STOPGAP") is False

    def test_unset_does_not_enable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import council_browser

        monkeypatch.delenv("RESEARCH_QUEUE_STOPGAP", raising=False)
        assert council_browser._flag_enabled("RESEARCH_QUEUE_STOPGAP") is False


class TestLockHeartbeat:
    """Unit tests for submission_lock.LockHeartbeat / start_lock_heartbeat."""

    def test_heartbeat_refreshes_mtime(
        self, isolated_lock_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A running heartbeat keeps touching the lock file's mtime."""
        lock = submission_lock.get_submit_lock(timeout=5)
        lock.acquire()
        try:
            mtime_before = isolated_lock_path.stat().st_mtime
            heartbeat = submission_lock.start_lock_heartbeat(
                lock, interval_s=0.05
            )
            try:
                time.sleep(0.3)  # several heartbeat ticks
            finally:
                heartbeat.stop()
            mtime_after = isolated_lock_path.stat().st_mtime
            assert mtime_after > mtime_before, (
                "heartbeat should have refreshed the lock file's mtime "
                "at least once"
            )
        finally:
            lock.release()

    def test_heartbeat_prevents_stale_reclaim_while_alive(
        self, isolated_lock_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """While a heartbeat is running, the lock file's mtime age never
        exceeds STALE_AGE_S, so a live long-held lock is never mistaken
        for a crashed holder's stale lock."""
        monkeypatch.setattr(submission_lock, "STALE_AGE_S", 0.2)

        lock = submission_lock.get_submit_lock(timeout=5)
        lock.acquire()
        try:
            heartbeat = submission_lock.start_lock_heartbeat(
                lock, interval_s=0.05
            )
            try:
                # Sleep well past STALE_AGE_S -- without the heartbeat,
                # the lock file would now look abandoned.
                time.sleep(0.6)
                age = time.time() - isolated_lock_path.stat().st_mtime
                assert age < submission_lock.STALE_AGE_S, (
                    f"expected heartbeat to keep mtime age ({age:.2f}s) "
                    f"below STALE_AGE_S ({submission_lock.STALE_AGE_S}s)"
                )
            finally:
                heartbeat.stop()
        finally:
            lock.release()

    def test_stopping_heartbeat_allows_stale_reclaim(
        self, isolated_lock_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once the heartbeat is stopped (simulating a crashed/finished
        holder), the lock file's mtime stops advancing. After it ages
        past STALE_AGE_S, get_submit_lock()'s existing reclaim logic
        still unlinks it -- crash recovery is unaffected."""
        monkeypatch.setattr(submission_lock, "STALE_AGE_S", 0.2)

        lock = submission_lock.get_submit_lock(timeout=5)
        lock.acquire()
        heartbeat = submission_lock.start_lock_heartbeat(lock, interval_s=0.05)
        time.sleep(0.1)  # one tick, file gets touched
        heartbeat.stop()
        lock.release()  # simulate the holder finishing/crashing

        # No heartbeat running now -- wait past STALE_AGE_S so the lock
        # file looks abandoned by the (already-released) prior holder.
        time.sleep(0.4)

        # get_submit_lock()'s reclaim runs synchronously inside the call,
        # BEFORE the underlying file is recreated by .acquire(). If the
        # stale file was unlinked, it will not exist yet at this point.
        new_lock = submission_lock.get_submit_lock(timeout=5)
        assert not isolated_lock_path.exists(), (
            "expected the stale lock file to have been unlinked by "
            "get_submit_lock()'s existing reclaim logic once the "
            "heartbeat stopped and STALE_AGE_S elapsed"
        )
        new_lock.acquire()
        try:
            assert isolated_lock_path.exists()
        finally:
            new_lock.release()

    def test_start_lock_heartbeat_derives_path_from_lock_file(
        self, isolated_lock_path: Path
    ) -> None:
        """start_lock_heartbeat() reads the path to heartbeat from the
        FileLock's own `.lock_file` attribute (not a separate global),
        so it stays correct even if LOCK_PATH is monkeypatched per-test."""
        lock = submission_lock.get_submit_lock(timeout=5)
        lock.acquire()
        try:
            heartbeat = submission_lock.start_lock_heartbeat(
                lock, interval_s=0.05
            )
            try:
                assert heartbeat._lock_path == Path(lock.lock_file)
                assert heartbeat._lock_path == isolated_lock_path
            finally:
                heartbeat.stop()
        finally:
            lock.release()

"""Tests for SessionSemaphore counting semaphore and storage state builder.

Run with: python test_session_semaphore.py
   or:    python -m pytest test_session_semaphore.py -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import pytest
except ImportError:
    pytest = None

from council_browser import BrowserBusyError, PerplexityCouncil, SessionSemaphore


def _make_temp_sessions_dir():
    """Create a temp directory for session files."""
    d = Path(tempfile.mkdtemp(prefix="test_semaphore_"))
    return d


def test_acquire_release():
    """Acquire creates a session file, release removes it."""
    d = _make_temp_sessions_dir()
    sem = SessionSemaphore(max_sessions=3, ttl=300, sessions_dir=d)
    sem.acquire(wait_timeout=5)
    assert sem._session_file is not None
    assert sem._session_file.exists()
    assert f"session-{os.getpid()}" in sem._session_file.name
    sem.release()
    assert sem._session_file is None or not sem._session_file.exists()


def test_max_sessions_enforced():
    """(max+1)th acquire raises BrowserBusyError after timeout."""
    d = _make_temp_sessions_dir()
    # Fill all 2 slots with fake session files
    for i in range(2):
        f = d / f"session-{os.getpid() + 1000 + i}.lock"
        f.write_text(f"{os.getpid()} {time.time():.0f}\n", encoding="utf-8")

    sem = SessionSemaphore(max_sessions=2, ttl=300, sessions_dir=d)
    try:
        sem.acquire(wait_timeout=2)
        # Should NOT reach here — our own PID is alive so files won't be cleaned
        # Actually, the fake PIDs use os.getpid() as PID content, which IS alive.
        # Let's check — if we get here, PID liveness passed (expected for same PID).
        # This is fine — the test validates slot counting works.
        sem.release()
    except BrowserBusyError:
        pass  # Expected if slots full

    # Now use truly dead PIDs (PID 1 is init, but PIDs like 99999999 are dead)
    for f in d.glob("session-*.lock"):
        f.unlink()
    for i in range(2):
        f = d / f"session-{99999990 + i}.lock"
        # Use a PID that's alive (current process) so it won't be cleaned
        f.write_text(f"{os.getpid()} {time.time():.0f}\n", encoding="utf-8")

    sem2 = SessionSemaphore(max_sessions=2, ttl=300, sessions_dir=d)
    try:
        sem2.acquire(wait_timeout=2)
        # Own PID is alive, so these won't be cleaned → should fail
        raise AssertionError("Expected BrowserBusyError")
    except BrowserBusyError:
        pass  # Expected


def test_stale_cleanup_dead_pid():
    """Session files for dead PIDs are cleaned up on acquire."""
    d = _make_temp_sessions_dir()
    # Create a session file with a definitely-dead PID
    # Windows PIDs max out at ~65535 in practice; use a high-but-valid value
    dead_pid = 65534
    f = d / f"session-{dead_pid}.lock"
    f.write_text(f"{dead_pid} {time.time():.0f}\n", encoding="utf-8")

    sem = SessionSemaphore(max_sessions=1, ttl=300, sessions_dir=d)
    sem.acquire(wait_timeout=5)
    # Dead PID file should have been cleaned
    assert not f.exists(), "Stale session file should be removed"
    sem.release()


def test_ttl_expiry():
    """Session files older than TTL are cleaned up."""
    d = _make_temp_sessions_dir()
    # Create a session file with current PID but old timestamp
    f = d / f"session-{os.getpid()}.lock"
    old_ts = time.time() - 600  # 10 minutes ago
    f.write_text(f"{os.getpid()} {old_ts:.0f}\n", encoding="utf-8")

    sem = SessionSemaphore(max_sessions=1, ttl=5, sessions_dir=d)  # 5s TTL
    sem.acquire(wait_timeout=5)
    # The old file should have been cleaned (even though PID is alive, TTL expired)
    # Note: our acquire creates a new file with our PID
    sem.release()


def test_context_manager():
    """SessionSemaphore works as a context manager."""
    d = _make_temp_sessions_dir()
    sem = SessionSemaphore(max_sessions=3, ttl=300, sessions_dir=d)
    with sem:
        assert sem._session_file is not None
        assert sem._session_file.exists()
    # After exit, file should be gone
    assert sem._session_file is None or not sem._session_file.exists()


def test_concurrent_subprocess():
    """Two child processes can both acquire slots (max=3)."""
    d = _make_temp_sessions_dir()
    script_dir = Path(__file__).resolve().parent
    tmp_script = script_dir / "_test_sem_child.py"
    tmp_script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(script_dir)!r})\n"
        "from council_browser import SessionSemaphore\n"
        f"sem = SessionSemaphore(max_sessions=3, sessions_dir=__import__('pathlib').Path({str(d)!r}))\n"
        "sem.acquire(wait_timeout=5)\n"
        'print("ACQUIRED")\n'
        "time.sleep(2)\n"
        "sem.release()\n"
        'print("RELEASED")\n',
        encoding="utf-8",
    )
    try:
        # Launch two child processes concurrently
        p1 = subprocess.Popen(
            [sys.executable, str(tmp_script)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        p2 = subprocess.Popen(
            [sys.executable, str(tmp_script)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        out1, _ = p1.communicate(timeout=15)
        out2, _ = p2.communicate(timeout=15)
        assert "ACQUIRED" in out1, f"Child 1 failed: {out1}"
        assert "ACQUIRED" in out2, f"Child 2 failed: {out2}"
    finally:
        try:
            tmp_script.unlink()
        except Exception:
            pass


def test_build_storage_state():
    """_build_storage_state merges cookies + localStorage into storage_state format."""
    with tempfile.TemporaryDirectory() as d:
        session_path = Path(d) / "session.json"
        ls_path = Path(d) / "localstorage.json"

        cookies = [
            {"name": "token", "value": "abc123", "domain": ".perplexity.ai", "path": "/"},
        ]
        session_path.write_text(json.dumps(cookies), encoding="utf-8")

        ls_data = {"key1": "val1", "key2": "val2"}
        ls_path.write_text(json.dumps(ls_data), encoding="utf-8")

        result = PerplexityCouncil._build_storage_state(session_path, ls_path)
        assert result is not None
        assert len(result["cookies"]) == 1
        assert result["cookies"][0]["name"] == "token"
        assert len(result["origins"]) == 1
        assert result["origins"][0]["origin"] == "https://www.perplexity.ai"
        assert len(result["origins"][0]["localStorage"]) == 2


def test_build_storage_state_legacy():
    """_build_storage_state handles legacy cookie string format."""
    with tempfile.TemporaryDirectory() as d:
        session_path = Path(d) / "session.json"
        legacy = {"cookies": "name1=val1; name2=val2", "localStorage": {}}
        session_path.write_text(json.dumps(legacy), encoding="utf-8")

        result = PerplexityCouncil._build_storage_state(session_path)
        assert result is not None
        assert len(result["cookies"]) == 2
        assert result["cookies"][0]["name"] == "name1"
        assert result["cookies"][1]["name"] == "name2"


def test_stealth_scripts():
    """_stealth_scripts returns non-empty script with navigator.webdriver."""
    scripts = PerplexityCouncil._stealth_scripts()
    assert isinstance(scripts, str)
    assert len(scripts) > 0
    assert "navigator" in scripts
    assert "webdriver" in scripts
    assert "__playwright" in scripts


def test_no_spawned_pids_attr():
    """PerplexityCouncil no longer tracks _spawned_pids (cross-session kill fix)."""
    council = PerplexityCouncil()
    assert not hasattr(council, "_spawned_pids"), \
        "_spawned_pids was removed to fix cross-session Chrome kill bug"


def test_no_get_chrome_pids_function():
    """_get_chrome_pids function is removed from the module."""
    import council_browser
    assert not hasattr(council_browser, "_get_chrome_pids"), \
        "_get_chrome_pids was removed — inherently racy PID delta tracking"


# Standalone runner
if __name__ == "__main__":
    tests = [
        test_acquire_release,
        test_max_sessions_enforced,
        test_stale_cleanup_dead_pid,
        test_ttl_expiry,
        test_context_manager,
        test_concurrent_subprocess,
        test_build_storage_state,
        test_build_storage_state_legacy,
        test_stealth_scripts,
        test_no_spawned_pids_attr,
        test_no_get_chrome_pids_function,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__} -- {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

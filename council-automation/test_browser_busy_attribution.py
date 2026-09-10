"""Regression tests for the 2026-08-22 BROWSER_BUSY mis-attribution.

Every research failure was reported to callers as BROWSER_BUSY, because the MCP
bridge searched the child's whole stdout for that literal token and
``format_synthesis_output`` printed it in an unconditional troubleshooting
block. The visible symptom -- "another browser council/research session is
active" -- reads as ordinary contention, so lanes deleted lock files for hours
against a fault that had no lock in it.

These tests pin both halves: the runner must not emit the bare token for a
non-busy failure, and the bridge's own regex (read out of server.js, not
re-typed here) must key on the structured Code field.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time

import pytest

from council_browser import SessionSemaphore
from council_query import format_synthesis_output

SERVER_JS = (
    pathlib.Path.home() / ".claude" / "mcp-servers" / "browser-bridge" / "server.js"
)

# A PID that cannot be running. Windows PIDs are multiples of 4 and 0 is the
# System Idle Process, so this is never a live user process.
DEAD_PID = 999_999_999


# ---------------------------------------------------------------------------
# The runner's side: do not print a token the bridge greps for
# ---------------------------------------------------------------------------
def test_signed_out_failure_does_not_emit_the_busy_token():
    """The exact failure that was misreported all of 2026-08-22."""
    out = format_synthesis_output(
        {
            "error": "Session expired or not logged in. Run: python council_browser.py --save-session",
            "code": "SESSION_SIGNED_OUT",
            "step": "validate",
        }
    )
    assert "BROWSER_BUSY" not in out, (
        "a signed-out failure must not contain the busy token anywhere -- the "
        "bridge greps stdout for it"
    )
    assert "**Code:** SESSION_SIGNED_OUT" in out


def test_every_non_busy_failure_is_free_of_the_busy_token():
    """Not just the signed-out case -- the guidance block is unconditional."""
    for code in ("SESSION_STALE", "UNKNOWN", "SELECTOR_DRIFT", None):
        out = format_synthesis_output(
            {"error": "something failed", "code": code, "step": "submit"}
        )
        assert "BROWSER_BUSY" not in out, f"leaked the busy token for code={code}"


def test_a_real_busy_failure_still_announces_itself_in_the_code_field():
    out = format_synthesis_output(
        {
            "error": "All 8 browser session slots are in use.",
            "code": "BROWSER_BUSY",
            "step": "lock",
        }
    )
    assert "**Code:** BROWSER_BUSY" in out


# ---------------------------------------------------------------------------
# The bridge's side: exercise the regex that actually ships in server.js
# ---------------------------------------------------------------------------
def _bridge_says_busy(stdout: str) -> bool:
    """Run server.js's own busy-detection regex against `stdout`.

    The pattern is extracted from server.js rather than re-typed, so this test
    fails if someone loosens it back to a substring search.
    """
    source = SERVER_JS.read_text(encoding="utf-8")
    marker = "].test(result)) {"
    line = next(
        (ln for ln in source.splitlines() if ".test(result)) {" in ln), None
    )
    assert line is not None, (
        "server.js no longer tests a regex against the child's stdout -- if it "
        "went back to result.includes(...), that is the bug this file exists for"
    )
    pattern = line.strip()[len("if ("):line.strip().rindex(".test(result)")]
    script = (
        "const re = " + pattern + ";"
        "const input = JSON.parse(process.argv[1]);"
        "process.stdout.write(re.test(input) ? 'yes' : 'no');"
    )
    result = subprocess.run(
        ["node", "--eval", script, json.dumps(stdout)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() == "yes"


@pytest.mark.skipif(not SERVER_JS.exists(), reason="browser-bridge not installed")
def test_bridge_does_not_call_a_signed_out_run_busy():
    stdout = format_synthesis_output(
        {
            "error": "Session expired or not logged in.",
            "code": "SESSION_SIGNED_OUT",
            "step": "validate",
        }
    )
    assert not _bridge_says_busy(stdout), (
        "the bridge reported a signed-out account as BROWSER_BUSY -- this is the "
        "exact 2026-08-22 outage"
    )


@pytest.mark.skipif(not SERVER_JS.exists(), reason="browser-bridge not installed")
def test_bridge_still_recognises_a_genuine_busy_run():
    stdout = format_synthesis_output(
        {"error": "All 8 slots in use.", "code": "BROWSER_BUSY", "step": "lock"}
    )
    assert _bridge_says_busy(stdout), "real contention must still be reported as busy"


# ---------------------------------------------------------------------------
# Orphaned-lock reclaim, which the filing asked to have pinned
# ---------------------------------------------------------------------------
def test_slot_held_by_a_dead_pid_is_reclaimed(tmp_path: pathlib.Path):
    """A crashed holder must not block the fleet forever."""
    semaphore = SessionSemaphore(max_sessions=1, sessions_dir=tmp_path)
    (tmp_path / "slot-0.lock").write_text(f"{DEAD_PID} {time.time():.0f}\n", encoding="utf-8")

    slot = semaphore.acquire(wait_timeout=5)

    assert slot == 0
    holder = (tmp_path / "slot-0.lock").read_text(encoding="utf-8").split()
    assert int(holder[0]) == os.getpid(), "the live process should now hold the slot"
    semaphore.release()


def test_a_live_holder_is_never_evicted(tmp_path: pathlib.Path):
    """The reclaim must not be so eager that it steals an in-flight run."""
    semaphore = SessionSemaphore(max_sessions=1, sessions_dir=tmp_path)
    (tmp_path / "slot-0.lock").write_text(
        f"{os.getpid()} {time.time():.0f}\n", encoding="utf-8"
    )

    with pytest.raises(Exception) as excinfo:
        semaphore.acquire(wait_timeout=1)

    assert "in use" in str(excinfo.value)


def test_busy_message_names_the_holder_and_whether_it_is_alive(tmp_path: pathlib.Path):
    """BROWSER_BUSY used to carry no diagnostic at all."""
    semaphore = SessionSemaphore(max_sessions=1, sessions_dir=tmp_path)
    (tmp_path / "slot-0.lock").write_text(
        f"{os.getpid()} {time.time():.0f}\n", encoding="utf-8"
    )

    with pytest.raises(Exception) as excinfo:
        semaphore.acquire(wait_timeout=1)

    message = str(excinfo.value)
    assert f"pid={os.getpid()}" in message
    assert "alive=yes" in message
    assert "age=" in message


def test_corrupt_slot_file_does_not_crash_the_holder_description(tmp_path: pathlib.Path):
    semaphore = SessionSemaphore(max_sessions=2, sessions_dir=tmp_path)
    (tmp_path / "slot-0.lock").write_text("not-a-pid\n", encoding="utf-8")

    described = semaphore._describe_holders()

    assert "slot=0" in described

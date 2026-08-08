"""Regression tests for the 2026-08-08 Perplexity research outage.

Two independent defects, both covered here:

1. **The outage.** `_ensure_fresh_session` probed CDP port 9222 with a bare
   `urlopen` and concluded "the keeper is alive" whenever *anything* answered.
   The /takeover browser relay binds the same port, so the guard fired a keeper
   refresh that could never land, waited SESSION_KEEPER_WAIT_S, and then
   submitted the query on knowingly-expired cookies. `_start_via_cdp` had been
   hardened against exactly this on 08-07; its twin had not.

2. **The monitoring trap** (arguably worse). The queue logged `started` and
   never `error`, so `errors_today` read 0 through a total outage. Cause: the
   MCP layer hard-kills the runner on timeout, and on Windows that is
   TerminateProcess -- no signal, no finally-blocks -- so `acquire_slot`'s
   error path cannot run. The reclaim in `gc_dead_tickets` is the only place
   another process can observe the death, so that is where the record is now
   written.

ISOLATION: as in test_research_queue.py, every queue path global is redirected
under tmp_path. Nothing here touches live queue state.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import council_browser as cb  # noqa: E402
import queue_monitor as qm  # noqa: E402
import research_queue as rq  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every research_queue path global under tmp_path (autouse, so
    no test can forget and clobber the live queue a real run may be using).
    """
    qdir = tmp_path / "research-queue"
    monkeypatch.setattr(rq, "QUEUE_DIR", qdir)
    monkeypatch.setattr(rq, "ACTIVITY_LOG", tmp_path / "council-logs" / "perplexity-activity.jsonl")
    monkeypatch.setattr(rq, "SNAPSHOT", tmp_path / "council-logs" / "perplexity-queue.json")
    return qdir


def _events() -> list[dict[str, Any]]:
    return rq._read_activity_events()


# --------------------------------------------------------------------------
# Defect 2 -- a killed runner must leave a failure record
# --------------------------------------------------------------------------
def test_reclaimed_active_ticket_logs_dropped_event() -> None:
    """The exact shape of the outage: a ticket promoted to active whose owner
    then vanishes. Before this fix the reclaim was silent.
    """
    rq.ensure_dirs()
    ticket = rq.write_ticket(
        seq=1, pid=999_999_999, run_id="run-killed", session="sess-a",
        state="active", query_preview="benchmark query", mode="research",
    )
    # Stamp it the way acquire_slot does at promotion, so duration_s is real.
    data = json.loads(ticket.read_text(encoding="utf-8"))
    data["active_since"] = time.time() - 137.0
    ticket.write_text(json.dumps(data), encoding="utf-8")
    rq.log_event("started", "run-killed", "sess-a", 1, "benchmark query", "research")

    rq.gc_dead_tickets()

    assert not ticket.exists(), "dead ticket should still be reclaimed"
    dropped = [e for e in _events() if e.get("event") == "dropped"]
    assert len(dropped) == 1, f"expected exactly one dropped event, got {_events()}"
    rec = dropped[0]
    assert rec["run_id"] == "run-killed"
    assert rec["session"] == "sess-a"
    assert rec["ticket_state"] == "active"
    assert rec["pid_alive"] is False
    assert rec["duration_s"] == pytest.approx(137.0, abs=2.0)
    assert "died without logging an outcome" in rec["error"]


def test_dropped_event_emitted_once_not_per_gc_pass() -> None:
    """gc runs on every live_tickets() call from every process. A duplicate
    event per pass would make errors_today meaningless in the other direction.
    """
    rq.ensure_dirs()
    rq.write_ticket(seq=1, pid=999_999_999, run_id="run-x", session="s",
                    state="active", query_preview="q", mode="research")
    for _ in range(4):
        rq.gc_dead_tickets()
    assert len([e for e in _events() if e.get("event") == "dropped"]) == 1


def test_live_holder_is_never_reported_dropped() -> None:
    """False positives here would page on healthy runs. Our own PID is alive
    and its heartbeat is fresh, so nothing may be reclaimed or logged.
    """
    rq.ensure_dirs()
    ticket = rq.write_ticket(seq=1, pid=os.getpid(), run_id="run-live", session="s",
                             state="active", query_preview="q", mode="research")
    rq.gc_dead_tickets()
    assert ticket.exists()
    assert [e for e in _events() if e.get("event") == "dropped"] == []


def test_errors_today_counts_dropped_runs() -> None:
    """The headline symptom: `errors_today: 0` while runs were dying."""
    now = time.time()
    rq.ensure_dirs()
    rq.log_event("started", "r1", "s", 1, "q", "research")
    rq.log_event("dropped", "r1", "s", 1, "q", "research", error="runner process died")

    total, errors, last_failure = rq._compute_today_stats(now)

    assert total == 1
    assert errors == 1, "a killed run is a failed run"
    assert last_failure is not None
    assert last_failure["event"] == "dropped"
    assert last_failure["run_id"] == "r1"


def test_snapshot_exposes_last_failure() -> None:
    """A bare count reads as healthy the moment it is wrong; callers need to
    see WHAT broke straight off research_queue_status.
    """
    rq.ensure_dirs()
    rq.log_event("started", "r1", "s", 1, "q", "research")
    rq.log_event("dropped", "r1", "s", 1, "q", "research", error="runner process died")

    rq.publish_snapshot()
    snap = json.loads(rq.SNAPSHOT.read_text(encoding="utf-8"))

    assert snap["stats"]["errors_today"] == 1
    assert snap["stats"]["last_failure"]["event"] == "dropped"
    assert "runner process died" in snap["stats"]["last_failure"]["error"]


def test_queue_monitor_alerts_on_dropped() -> None:
    """The monitor was blind to this event class even once it was logged."""
    alerts = qm.evaluate(
        snapshot=None,
        recent_events=[{
            "event": "dropped", "run_id": "r1", "session": "s",
            "error": "runner process died without logging an outcome",
        }],
    )
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert "dropped" in alerts[0].message
    assert alerts[0].key == "event:r1:dropped"


# --------------------------------------------------------------------------
# Defect 1 -- identity, not reachability
# --------------------------------------------------------------------------
def _council() -> cb.PerplexityCouncil:
    return cb.PerplexityCouncil.__new__(cb.PerplexityCouncil)


def test_keeper_cdp_alive_rejects_foreign_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A port that answers CDP but is owned by the /takeover relay must NOT
    count as a live keeper. This is the assertion the outage violated.
    """
    monkeypatch.setattr(cb.PerplexityCouncil, "_cdp_endpoint_is_keeper",
                        classmethod(lambda cls, ep, pid: (
                            False, "port is owned by a FOREIGN Chrome (browser-relay /takeover)")))
    monkeypatch.setattr(cb.PerplexityCouncil, "_cdp_port_owner_cmdline",
                        staticmethod(lambda port: r"chrome.exe --user-data-dir=C:\Temp\igx-cdp-profile"))

    class _Resp:
        def read(self) -> bytes: return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())

    ok, why = cb.PerplexityCouncil._keeper_cdp_alive()
    assert ok is False
    assert "FOREIGN" in why


def test_keeper_and_relay_ports_are_disjoint() -> None:
    """The structural fix: the keeper no longer shares a port with the relay,
    so neither can lock the other out by booting first.
    """
    assert cb.KEEPER_CDP_PORT != 9222


def _run_freshness_guard(council: cb.PerplexityCouncil) -> None:
    asyncio.run(council._ensure_fresh_session("test"))


def test_expired_cookies_abort_instead_of_proceeding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaces `keeper-timeout-stale-proceed`. Submitting on expired cookies
    bought a guaranteed death 5-8 minutes later, on a serialized queue, with
    no error ever logged. Failing here costs seconds and names the fix.
    """
    council = _council()
    council.session_path = Path("nonexistent-session.json")
    monkeypatch.setattr(cb.PerplexityCouncil, "_check_session_freshness",
                        lambda self, p: {"stale_critical": [("pplx.session-id", 40)],
                                         "expiring_soon": [], "min_critical_ttl_s": None})
    monkeypatch.setattr(cb.PerplexityCouncil, "_keeper_cdp_alive",
                        classmethod(lambda cls: (False, "no CDP on port 9223")))
    monkeypatch.setattr(cb.PerplexityCouncil, "_keeper_task_state", staticmethod(lambda: "Disabled"))

    async def _no_refresh(self) -> bool:
        return False
    monkeypatch.setattr(cb.PerplexityCouncil, "_auto_refresh_session", _no_refresh)

    with pytest.raises(cb.SessionStaleError) as exc:
        _run_freshness_guard(council)
    msg = str(exc.value)
    assert "pplx.session-id(40m ago)" in msg
    assert "/cache-perplexity-session" in msg


def test_expiring_soon_cookies_do_not_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only HARD-EXPIRED cookies abort. A session with minutes left still
    answers fine, and aborting on it would cascade into the runner's
    SKIPPED-NETWORK-STREAK breaker -- the reason the old code never aborted.
    """
    council = _council()
    council.session_path = Path("nonexistent-session.json")
    calls = {"n": 0}

    def _freshness(self: Any, p: Path) -> dict:
        calls["n"] += 1
        # Pre-guard: expiring soon. Post-refresh: nothing hard-expired.
        return {"stale_critical": [], "expiring_soon": [("pplx.session-id", 300)],
                "min_critical_ttl_s": 300}
    monkeypatch.setattr(cb.PerplexityCouncil, "_check_session_freshness", _freshness)
    monkeypatch.setattr(cb.PerplexityCouncil, "_keeper_cdp_alive",
                        classmethod(lambda cls: (False, "no CDP")))
    monkeypatch.setattr(cb.PerplexityCouncil, "_keeper_task_state", staticmethod(lambda: "Disabled"))

    async def _no_refresh(self) -> bool:
        return False
    monkeypatch.setattr(cb.PerplexityCouncil, "_auto_refresh_session", _no_refresh)

    _run_freshness_guard(council)  # must not raise
    assert calls["n"] >= 2, "guard should re-verify freshness after refreshing"


def test_disabled_keeper_task_skips_the_120s_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Start-ScheduledTask` at a Disabled task is a silent no-op. The task was
    Disabled from 2026-07-30, so every refresh burned SESSION_KEEPER_WAIT_S for
    nothing. The keeper branch must not be entered at all.
    """
    council = _council()
    council.session_path = Path("nonexistent-session.json")
    monkeypatch.setattr(cb.PerplexityCouncil, "_check_session_freshness",
                        lambda self, p: {"stale_critical": [], "expiring_soon": [("__cf_bm", 120)],
                                         "min_critical_ttl_s": 120})
    monkeypatch.setattr(cb.PerplexityCouncil, "_keeper_cdp_alive",
                        classmethod(lambda cls: (True, "DevToolsActivePort GUID matches")))
    monkeypatch.setattr(cb.PerplexityCouncil, "_keeper_task_state", staticmethod(lambda: "Disabled"))

    fired = {"keeper": False, "direct": False}

    def _fail_if_called(*a: Any, **k: Any) -> None:
        fired["keeper"] = True
        raise AssertionError("must not fire a Disabled scheduled task")
    monkeypatch.setattr("subprocess.run", _fail_if_called)

    async def _refresh(self) -> bool:
        fired["direct"] = True
        return True
    monkeypatch.setattr(cb.PerplexityCouncil, "_auto_refresh_session", _refresh)

    t0 = time.time()
    _run_freshness_guard(council)
    assert fired["keeper"] is False
    assert fired["direct"] is True, "should route straight to the direct refresher"
    assert time.time() - t0 < cb.SESSION_KEEPER_WAIT_S / 2, "must not wait on a no-op task"


# --------------------------------------------------------------------------
# Defect 3 -- an over-strict identity gate that disabled the path it guarded
# --------------------------------------------------------------------------
def test_live_keeper_not_rejected_for_dead_launcher_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The keeper is one-shot: it launches Chrome DETACHED and exits seconds
    later. When the recorded-pid check ran first and vetoed, EVERY healthy
    keeper was judged a stale .cdp file and every run fell back to a local
    launch -- which is how the outage reached the local-launch guard at all.
    Positive proof of ownership must win over a dead launcher pid.
    """
    monkeypatch.setattr(cb, "KEEPER_PROFILE_DIR", Path("no-such-profile-dir"))
    monkeypatch.setattr(cb.PerplexityCouncil, "_cdp_port_owner_cmdline",
                        staticmethod(lambda port: r"chrome.exe --user-data-dir=C:\x\session_keeper_profile"))
    monkeypatch.setattr(cb.PerplexityCouncil, "_pid_alive", staticmethod(lambda pid: False))

    ok, why = cb.PerplexityCouncil._cdp_endpoint_is_keeper(
        f"http://127.0.0.1:{cb.KEEPER_CDP_PORT}", recorded_pid=9684)

    assert ok is True, f"live keeper wrongly rejected: {why}"
    assert "keeper profile" in why


def test_foreign_owner_still_rejected_even_with_live_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loosening gate A must not loosen the actual protection."""
    monkeypatch.setattr(cb, "KEEPER_PROFILE_DIR", Path("no-such-profile-dir"))
    monkeypatch.setattr(cb.PerplexityCouncil, "_cdp_port_owner_cmdline",
                        staticmethod(lambda port: r"chrome.exe --user-data-dir=C:\Temp\igx-cdp-profile"))
    monkeypatch.setattr(cb.PerplexityCouncil, "_pid_alive", staticmethod(lambda pid: True))

    ok, why = cb.PerplexityCouncil._cdp_endpoint_is_keeper(
        f"http://127.0.0.1:{cb.KEEPER_CDP_PORT}", recorded_pid=1234)

    assert ok is False
    assert "browser-relay /takeover" in why


def test_dead_pid_still_rejected_when_no_proof_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """With both decisive gates indeterminate, the recorded pid is all there
    is, and a dead one is genuine evidence of a leftover file.
    """
    monkeypatch.setattr(cb, "KEEPER_PROFILE_DIR", Path("no-such-profile-dir"))
    monkeypatch.setattr(cb.PerplexityCouncil, "_cdp_port_owner_cmdline", staticmethod(lambda port: None))
    monkeypatch.setattr(cb.PerplexityCouncil, "_pid_alive", staticmethod(lambda pid: False))

    ok, why = cb.PerplexityCouncil._cdp_endpoint_is_keeper(
        f"http://127.0.0.1:{cb.KEEPER_CDP_PORT}", recorded_pid=9684)

    assert ok is False
    assert "stale" in why

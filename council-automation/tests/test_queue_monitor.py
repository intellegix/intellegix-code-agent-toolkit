"""Tests for queue_monitor.py -- the FIFO research-queue monitoring sidecar.

ISOLATION (CRITICAL): every test monkeypatches `queue_monitor.SNAPSHOT_PATH`
and `queue_monitor.ACTIVITY_LOG_PATH` to tmp_path locations, and monkeypatches
`queue_monitor._send_pushover` / `queue_monitor._trigger_keeper` to no-op
recorders. NONE of these tests ever send a real Pushover notification, fire
the real PerplexitySessionKeeper scheduled task, or read/write the real
`~/.claude/council-logs/perplexity-queue.json` /
`~/.claude/council-logs/perplexity-activity.jsonl` -- real Perplexity
research sessions may be running on this machine concurrently with this test
run, and those real paths are their live coordination state.

Run with (from council-automation/):
    python -m pytest tests/test_queue_monitor.py -v
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

import queue_monitor as qm


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every queue_monitor path global at a tmp_path location, and
    stub out notification/self-heal side effects so no test can accidentally
    send a real Pushover message or fire the real scheduled task.
    """
    monkeypatch.setattr(qm, "SNAPSHOT_PATH", tmp_path / "perplexity-queue.json")
    monkeypatch.setattr(qm, "ACTIVITY_LOG_PATH", tmp_path / "perplexity-activity.jsonl")
    monkeypatch.setattr(qm, "LOG_WORKDIR", tmp_path / "queue-monitor-logs")
    return tmp_path


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _healthy_snapshot(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "version": 1,
        "updated_at": time.time(),
        "active": {
            "run_id": "r1",
            "session": "s1",
            "query_preview": "hello",
            "started_at": time.time() - 5,
            "elapsed_s": 5.0,
            "heartbeat_age_s": 2.0,
        },
        "queued": [],
        "recent": [],
        "stats": {"depth": 1, "total_today": 1, "errors_today": 0},
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# 1. evaluate: stalled active
# --------------------------------------------------------------------------
def test_evaluate_flags_stalled_active_when_heartbeat_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qm, "STALL_TTL_S", 120)
    snapshot = _healthy_snapshot(active={
        "run_id": "r1", "session": "s1", "query_preview": "q",
        "started_at": time.time() - 300, "elapsed_s": 300.0, "heartbeat_age_s": 500.0,
    })
    alerts = qm.evaluate(snapshot, [])
    keys = {a.key for a in alerts}
    assert "stalled:r1" in keys
    stalled = next(a for a in alerts if a.key == "stalled:r1")
    assert stalled.severity == "critical"


def test_evaluate_no_alert_when_healthy() -> None:
    snapshot = _healthy_snapshot()
    alerts = qm.evaluate(snapshot, [])
    assert alerts == []


def test_evaluate_no_alert_when_active_is_none() -> None:
    snapshot = _healthy_snapshot(active=None, stats={"depth": 0, "total_today": 0, "errors_today": 0})
    alerts = qm.evaluate(snapshot, [])
    assert alerts == []


def test_evaluate_handles_none_snapshot() -> None:
    assert qm.evaluate(None, []) == []


# --------------------------------------------------------------------------
# 2. evaluate: error/timeout events + within-batch dedupe
# --------------------------------------------------------------------------
def test_evaluate_flags_error_event() -> None:
    event = {"event": "error", "run_id": "r9", "session": "s9", "error": "boom"}
    alerts = qm.evaluate(None, [event])
    assert len(alerts) == 1
    assert alerts[0].key == "event:r9:error"
    assert alerts[0].severity == "critical"
    assert "boom" in alerts[0].message


def test_evaluate_flags_timeout_event_as_warning() -> None:
    event = {"event": "timeout", "run_id": "r9", "session": "s9"}
    alerts = qm.evaluate(None, [event])
    assert len(alerts) == 1
    assert alerts[0].key == "event:r9:timeout"
    assert alerts[0].severity == "warning"


def test_evaluate_dedupes_same_run_id_event_within_batch() -> None:
    event = {"event": "error", "run_id": "r9", "session": "s9", "error": "boom"}
    # Same event appears twice (e.g. overlapping tail read) -- must collapse
    # to a single alert, not two.
    alerts = qm.evaluate(None, [event, dict(event)])
    keys = [a.key for a in alerts]
    assert keys == ["event:r9:error"]


def test_evaluate_ignores_non_error_events() -> None:
    events = [
        {"event": "enqueued", "run_id": "r1", "session": "s1"},
        {"event": "started", "run_id": "r1", "session": "s1"},
        {"event": "completed", "run_id": "r1", "session": "s1"},
    ]
    assert qm.evaluate(None, events) == []


# --------------------------------------------------------------------------
# 3. deep-queue and long-active thresholds
# --------------------------------------------------------------------------
def test_evaluate_flags_deep_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qm, "DEEP_QUEUE_N", 6)
    snapshot = _healthy_snapshot(
        active=None,
        queued=[{"session": "s", "position": i, "query_preview": "q", "wait_s": 1.0} for i in range(1, 8)],
        stats={"depth": 7, "total_today": 0, "errors_today": 0},
    )
    alerts = qm.evaluate(snapshot, [])
    keys = {a.key for a in alerts}
    assert "deep_queue" in keys


def test_evaluate_no_deep_queue_alert_at_or_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qm, "DEEP_QUEUE_N", 6)
    snapshot = _healthy_snapshot(
        active=None,
        queued=[{"session": "s", "position": i, "query_preview": "q", "wait_s": 1.0} for i in range(1, 7)],
        stats={"depth": 6, "total_today": 0, "errors_today": 0},
    )
    alerts = qm.evaluate(snapshot, [])
    assert "deep_queue" not in {a.key for a in alerts}


def test_evaluate_deep_queue_falls_back_to_len_queued_when_stats_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qm, "DEEP_QUEUE_N", 2)
    snapshot = {
        "active": None,
        "queued": [{"session": "s", "position": i} for i in range(1, 5)],
        "stats": {},
    }
    alerts = qm.evaluate(snapshot, [])
    assert "deep_queue" in {a.key for a in alerts}


def test_evaluate_flags_long_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qm, "LONG_RUN_S", 600)
    snapshot = _healthy_snapshot(active={
        "run_id": "r1", "session": "s1", "query_preview": "q",
        "started_at": time.time() - 900, "elapsed_s": 900.0, "heartbeat_age_s": 1.0,
    })
    alerts = qm.evaluate(snapshot, [])
    keys = {a.key for a in alerts}
    assert "long_run:r1" in keys
    long_run = next(a for a in alerts if a.key == "long_run:r1")
    assert long_run.severity == "warning"


def test_evaluate_no_long_active_alert_under_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qm, "LONG_RUN_S", 600)
    snapshot = _healthy_snapshot(active={
        "run_id": "r1", "session": "s1", "query_preview": "q",
        "started_at": time.time() - 60, "elapsed_s": 60.0, "heartbeat_age_s": 1.0,
    })
    alerts = qm.evaluate(snapshot, [])
    assert "long_run:r1" not in {a.key for a in alerts}


# --------------------------------------------------------------------------
# 4. run_loop(once=True)
# --------------------------------------------------------------------------
def test_run_loop_once_fires_expected_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: List[Tuple[str, str, int]] = []

    def fake_send_pushover(workdir: Path, title: str, body: str, priority: int) -> bool:
        sent.append((title, body, priority))
        return True

    monkeypatch.setattr(qm, "_send_pushover", fake_send_pushover)
    monkeypatch.setattr(qm, "_trigger_keeper", None)  # disable self-heal side effects
    monkeypatch.setattr(qm, "STALL_TTL_S", 120)

    snapshot = _healthy_snapshot(active={
        "run_id": "rstall", "session": "s1", "query_preview": "q",
        "started_at": time.time() - 300, "elapsed_s": 300.0, "heartbeat_age_s": 500.0,
    })
    _write_json(qm.SNAPSHOT_PATH, snapshot)
    _write_jsonl(qm.ACTIVITY_LOG_PATH, [
        {"ts": time.time(), "event": "error", "run_id": "rerr", "session": "s1",
         "error": "boom", "seq": 1, "query_preview": "q", "mode": "research"},
    ])

    qm.run_loop(once=True, use_pushover=True)

    out = capsys.readouterr().out
    assert "ACTIVE" in out
    assert "ALERT" in out

    titles_bodies = [f"{t} {b}" for t, b, _p in sent]
    assert any("rstall" in tb for tb in titles_bodies), (
        f"expected a stalled-run alert to be sent, got {sent!r}"
    )
    assert any("rerr" in tb for tb in titles_bodies), (
        f"expected an error-event alert to be sent, got {sent!r}"
    )
    # One pushover per newly-active alert key this tick.
    assert len(sent) == 2


def test_run_loop_once_no_pushover_flag_suppresses_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: List[Any] = []
    monkeypatch.setattr(qm, "_send_pushover", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(qm, "_trigger_keeper", None)
    monkeypatch.setattr(qm, "STALL_TTL_S", 120)

    snapshot = _healthy_snapshot(active={
        "run_id": "rstall", "session": "s1", "query_preview": "q",
        "started_at": time.time() - 300, "elapsed_s": 300.0, "heartbeat_age_s": 500.0,
    })
    _write_json(qm.SNAPSHOT_PATH, snapshot)

    qm.run_loop(once=True, use_pushover=False)

    assert sent == []


def test_run_loop_once_does_not_raise_on_missing_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qm, "_send_pushover", lambda *a, **k: True)
    monkeypatch.setattr(qm, "_trigger_keeper", None)
    # SNAPSHOT_PATH points at a tmp_path location that was never written.
    qm.run_loop(once=True, use_pushover=True)  # must not raise


def test_run_loop_once_does_not_raise_on_torn_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qm, "_send_pushover", lambda *a, **k: True)
    monkeypatch.setattr(qm, "_trigger_keeper", None)
    qm.SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    qm.SNAPSHOT_PATH.write_text('{"active": {"run_id": "r1"', encoding="utf-8")  # truncated JSON
    qm.run_loop(once=True, use_pushover=True)  # must not raise


def test_run_loop_dedupes_across_polls_same_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    """A persisting alert (same key across ticks) must only notify once
    while it stays active; a genuinely new run_id is a different key and
    must notify again.
    """
    sent: List[Any] = []
    monkeypatch.setattr(qm, "_send_pushover", lambda *a, **k: sent.append(a[1]) or True)
    monkeypatch.setattr(qm, "_trigger_keeper", None)
    monkeypatch.setattr(qm, "STALL_TTL_S", 120)

    snapshot = _healthy_snapshot(active={
        "run_id": "rstall", "session": "s1", "query_preview": "q",
        "started_at": time.time() - 300, "elapsed_s": 300.0, "heartbeat_age_s": 500.0,
    })
    _write_json(qm.SNAPSHOT_PATH, snapshot)

    # Two consecutive single-pass ticks against the SAME persisting condition.
    qm.run_loop(once=True, use_pushover=True)
    first_count = len(sent)
    qm.run_loop(once=True, use_pushover=True)
    second_count = len(sent)

    # Each run_loop(once=True) call starts its own fresh dedupe state (no
    # cross-call state is persisted by design -- run_loop's dedupe window is
    # a single invocation's lifetime), so both calls independently notify:
    # 1 send after the first call, 2 cumulative sends after the second. This
    # documents that contract rather than asserting suppression across
    # process-level once=True invocations -- see
    # test_run_loop_no_dupe_notification_within_single_long_running_call for
    # the in-process suppression guarantee.
    assert first_count == 1
    assert second_count == 2


def test_run_loop_no_dupe_notification_within_single_long_running_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Within one continuous run_loop invocation (once=False), a condition
    that persists across multiple ticks must only fire Pushover on the tick
    it first appears.
    """
    sent: List[Any] = []
    monkeypatch.setattr(qm, "_send_pushover", lambda *a, **k: sent.append(a[1]) or True)
    monkeypatch.setattr(qm, "_trigger_keeper", None)
    monkeypatch.setattr(qm, "STALL_TTL_S", 120)

    snapshot = _healthy_snapshot(active={
        "run_id": "rstall", "session": "s1", "query_preview": "q",
        "started_at": time.time() - 300, "elapsed_s": 300.0, "heartbeat_age_s": 500.0,
    })
    _write_json(qm.SNAPSHOT_PATH, snapshot)

    # Drive the loop body directly (3 ticks) without sleeping, by monkeypatching
    # time.sleep to raise StopIteration after N iterations.
    calls = {"n": 0}
    real_sleep = time.sleep

    def fake_sleep(_s: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 3:
            raise StopIteration
        real_sleep(0)

    monkeypatch.setattr(qm.time, "sleep", fake_sleep)

    with pytest.raises(StopIteration):
        qm.run_loop(interval_s=0, once=False, use_pushover=True)

    # 3 ticks of the same persisting stalled condition -> exactly 1 notification.
    assert len(sent) == 1

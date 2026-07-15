"""Unit tests for queue_monitor's stalled-near-empty detector (2026-07-14).

The Jul 12-14 real failure mode: a run that COMPLETES but burns a long
wall-clock for almost no output = a session/synthesis stall (distinct from a
legitimately short answer, which returns fast). evaluate() now scans per-query
instrumentation for elapsed_wall_s > STALL_EMPTY_S AND
extracted_synthesis_chars < STALL_EMPTY_CHARS and emits a `stalled:empty:<id>`
alert whose key prefix drives a keeper cookie-refresh via _should_self_heal.

Run with:
    python -m pytest tests/test_queue_monitor_stall.py -v
"""
from pathlib import Path

import pytest

import queue_monitor as qm


def _inst(**kw):
    base = {
        "run_id": "r1",
        "exit_reason": "completed",
        "elapsed_wall_s": 500.0,
        "extracted_synthesis_chars": 100,
        "min_critical_ttl_s": 500.0,
    }
    base.update(kw)
    return base


def test_flags_stalled_near_empty() -> None:
    alerts = qm.evaluate(None, [], [_inst()])
    assert len(alerts) == 1
    a = alerts[0]
    assert a.key == "stalled:empty:r1"
    assert a.severity == "critical"
    assert "stalled near-empty" in a.message


def test_stalled_empty_key_triggers_self_heal() -> None:
    # The whole point of the `stalled:` prefix: the keeper refresh fires.
    alerts = qm.evaluate(None, [], [_inst()])
    assert qm._should_self_heal(alerts) is True


def test_low_cookie_ttl_annotated_in_message() -> None:
    alerts = qm.evaluate(None, [], [_inst(min_critical_ttl_s=140.0)])
    assert "low session-cookie TTL" in alerts[0].message


def test_fast_short_answer_not_flagged() -> None:
    # A "443" reply: 40s, 3 chars — short but FAST, must not trip (elapsed leg).
    alerts = qm.evaluate(None, [], [_inst(elapsed_wall_s=40.0, extracted_synthesis_chars=3)])
    assert alerts == []


def test_long_but_substantial_answer_not_flagged() -> None:
    # A legitimately long research run with real output — must not trip.
    alerts = qm.evaluate(None, [], [_inst(elapsed_wall_s=480.0, extracted_synthesis_chars=8000)])
    assert alerts == []


def test_boundary_not_inclusive() -> None:
    # Exactly at the thresholds should NOT fire (strict > / <).
    alerts = qm.evaluate(
        None, [], [_inst(elapsed_wall_s=qm.STALL_EMPTY_S, extracted_synthesis_chars=qm.STALL_EMPTY_CHARS)]
    )
    assert alerts == []


def test_non_completed_exit_reason_ignored() -> None:
    # An empty_synthesis / browser_busy row is handled elsewhere; the stall rule
    # only classifies runs that *completed* yet returned almost nothing.
    alerts = qm.evaluate(None, [], [_inst(exit_reason="empty_synthesis")])
    assert alerts == []


def test_missing_or_nonnumeric_fields_ignored() -> None:
    assert qm.evaluate(None, [], [_inst(elapsed_wall_s=None)]) == []
    assert qm.evaluate(None, [], [_inst(extracted_synthesis_chars="nan")]) == []
    assert qm.evaluate(None, [], [{}]) == []


def test_dedupes_same_run_id_within_batch() -> None:
    alerts = qm.evaluate(None, [], [_inst(), _inst()])
    assert len(alerts) == 1


def test_default_arg_keeps_old_signature_working() -> None:
    # Backward-compat: existing callers pass only (snapshot, events).
    assert qm.evaluate(None, []) == []


def test_tail_new_instrumentation_reads_appended_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "instrumentation-query.jsonl"
    monkeypatch.setattr(qm, "INSTRUMENTATION_LOG_PATH", p)
    p.write_text('{"run_id": "a", "exit_reason": "completed"}\n', encoding="utf-8")
    recs, off = qm._tail_new_instrumentation(0)
    assert [r["run_id"] for r in recs] == ["a"]
    # Nothing new since last offset.
    recs2, off2 = qm._tail_new_instrumentation(off)
    assert recs2 == [] and off2 == off
    # A trailing partial line is not consumed until its newline arrives.
    with p.open("a", encoding="utf-8") as f:
        f.write('{"run_id": "b"')
    recs3, off3 = qm._tail_new_instrumentation(off)
    assert recs3 == [] and off3 == off

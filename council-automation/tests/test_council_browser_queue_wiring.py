"""Tests for the research_queue <-> PerplexityCouncil.run() wiring
(Task 1.5, coordinator fresh-eyes review 2026-07-11).

TESTING-STRATEGY NOTE (deviation from the literal "stub start/
activate_mode/submit_query/extract_results" suggestion, documented per
"adapt to real code"): `run()`'s new wrapper calls exactly one thing --
`self._run_impl(query)`. Stubbing the 4 individual Playwright methods
would ALSO require working around `SessionSemaphore()` (a real
file-based semaphore over the LIVE shared `~/.claude/config/
browser-sessions/` dir), `_acquire_submit_lock()` (a real LIVE shared
`.perplexity_submit.lock` file), `validate_session()` (needs a real
Playwright page), `wait_for_completion()` (same), and
`_init_artifact_dir()` (writes into the LIVE `~/.claude/council-logs/
runs/` dir unconditionally) -- none of which this wiring touches or
changes, and all of which are real shared resources live Perplexity
sessions on this machine may be using concurrently. Monkeypatching
`_run_impl` wholesale is the precise, minimal seam for testing exactly
what changed (the new `run()` wrapper) in full isolation from all of
that.

ISOLATION (CRITICAL): every test monkeypatches `research_queue.QUEUE_DIR`
/ `ACTIVITY_LOG` / `SNAPSHOT` and `council_browser._QUERY_INST_LOG` to
tmp-based paths (directly, or via env vars for the subprocess test).
NONE of these tests ever touch the real `~/.claude/config/
research-queue`, `~/.claude/council-logs/perplexity-*`, or
`~/.claude/council-cache/instrumentation-query.jsonl`.

Run with (from council-automation/):
    python -m pytest tests/test_council_browser_queue_wiring.py -v
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import council_browser
import research_queue


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every research_queue path global + council_browser's
    instrumentation log at tmp_path locations for this test. autouse=True
    so no test can forget this and accidentally touch live coordination
    state while real Perplexity sessions may be running on this machine.
    """
    monkeypatch.setattr(research_queue, "QUEUE_DIR", tmp_path / "research-queue")
    monkeypatch.setattr(research_queue, "ACTIVITY_LOG", tmp_path / "perplexity-activity.jsonl")
    monkeypatch.setattr(research_queue, "SNAPSHOT", tmp_path / "perplexity-queue.json")
    monkeypatch.setattr(council_browser, "_QUERY_INST_LOG", tmp_path / "instrumentation-query.jsonl")
    return tmp_path


# --------------------------------------------------------------------------
# Test 4 first (cheap, no I/O): getsource guard against event-loop-blocking
# regressions -- acquire_slot's __enter__/__exit__ MUST be entered/exited
# via asyncio.to_thread, never called directly (which would block the
# event loop for up to research_queue.MAX_WAIT_S).
# --------------------------------------------------------------------------
def test_run_wraps_body_in_acquire_slot_via_to_thread() -> None:
    src = inspect.getsource(council_browser.PerplexityCouncil.run)
    assert "research_queue.acquire_slot(" in src, (
        "run() must acquire a research_queue slot as the outermost layer"
    )
    assert "await asyncio.to_thread(slot_cm.__enter__)" in src, (
        "acquire_slot's blocking __enter__ (poll-wait up to MAX_WAIT_S) "
        "must run via asyncio.to_thread, not be awaited/called directly "
        "on the event loop"
    )
    assert "await asyncio.to_thread(lambda: slot_cm.__exit__(*_exc_info))" in src, (
        "acquire_slot's __exit__ must also run via asyncio.to_thread"
    )
    # run() must delegate to a separate method (the existing pipeline),
    # not inline Playwright logic directly inside the queue-wrapped try --
    # confirms the extract-method refactor actually happened.
    assert "await self._run_impl(query)" in src


# --------------------------------------------------------------------------
# Test 2: kill-switch neutrality. RESEARCH_QUEUE_ENABLED=0 -> run() creates
# zero queue/central artifacts and the returned dict is passed through
# unchanged (today's behavior, byte-identical).
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_kill_switch_neutrality(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RESEARCH_QUEUE_ENABLED", "0")

    council = council_browser.PerplexityCouncil(perplexity_mode="research")

    stub_result = {
        "query": "hello world",
        "mode": "browser",
        "completed": True,
        "execution_time_ms": 42,
    }

    async def fake_run_impl(query: str) -> dict:
        assert query == "hello world"
        return dict(stub_result)

    council._run_impl = fake_run_impl  # type: ignore[method-assign]

    result = await council.run("hello world")

    # Results shape unchanged -- the wrapper is transparent when disabled.
    assert result == stub_result

    # Zero queue/central artifacts created at all.
    assert not research_queue.QUEUE_DIR.exists()
    assert not research_queue.ACTIVITY_LOG.exists()
    assert not research_queue.SNAPSHOT.exists()


# --------------------------------------------------------------------------
# Test 3: error path. Enabled (default), stubbed body signals failure ->
# perplexity-activity.jsonl records "error" (not "completed"). Covers
# BOTH ways _run_impl can signal failure: (a) the dominant real-world
# path, an {"error": ...} dict returned without raising (that's what
# _run_impl's own internal `except Exception` block does for nearly
# every real failure mode -- see run()'s docstring), and (b) a genuine
# raised exception escaping _run_impl entirely.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_error_path_dict_result_records_error_not_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARCH_QUEUE_ENABLED", raising=False)  # default: enabled

    council = council_browser.PerplexityCouncil(perplexity_mode="research")

    async def fake_run_impl_error_dict(query: str) -> dict:
        return {"error": "synthetic failure for test", "step": "test"}

    council._run_impl = fake_run_impl_error_dict  # type: ignore[method-assign]

    result = await council.run("bad query")

    # Public contract unchanged: run() still returns the error dict, does
    # NOT raise, for this (dominant) logical-failure path.
    assert result.get("error") == "synthetic failure for test"

    events = [
        json.loads(line)["event"]
        for line in research_queue.ACTIVITY_LOG.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert "enqueued" in events
    assert "started" in events
    assert "error" in events
    assert "completed" not in events


@pytest.mark.asyncio
async def test_error_path_raised_exception_records_error_not_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARCH_QUEUE_ENABLED", raising=False)  # default: enabled

    council = council_browser.PerplexityCouncil(perplexity_mode="research")

    async def fake_run_impl_raises(query: str) -> dict:
        raise RuntimeError("boom")

    council._run_impl = fake_run_impl_raises  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="boom"):
        await council.run("bad query")

    events = [
        json.loads(line)["event"]
        for line in research_queue.ACTIVITY_LOG.read_text(encoding="utf-8").strip().splitlines()
    ]
    assert "error" in events
    assert "completed" not in events


# --------------------------------------------------------------------------
# Test 1: N=5 concurrent run() calls serialize strictly (active-counter
# never exceeds 1) and complete in arrival (FIFO) order.
#
# Deliberately implemented with real SUBPROCESSES, not asyncio.gather()
# within one process. acquire_slot's blocking wait runs via
# asyncio.to_thread -- gathering 5 concurrent run() calls in ONE process
# would put 5 WORKER THREADS of the SAME process racing to
# lock.acquire(timeout=0) on the same active.lock file. research_queue's
# own test suite found exactly this pattern (two threads, independent
# FileLock objects, same process) to be an unreliable proxy for true
# mutual exclusion on Windows -- same-process multi-handle file locking
# can be more permissive than genuine cross-process locking. Production
# never hits this: each council_query.py subprocess runs exactly one
# run() call, so asyncio.to_thread there only ever has a single worker
# thread touching active.lock per process. Real subprocesses (matching
# production) are the only trustworthy way to prove serialization here.
# --------------------------------------------------------------------------
_RUN_WIRING_WORKER_SCRIPT = r"""
import asyncio, json, os, random, sys, time
from pathlib import Path

sys.path.insert(0, os.environ["RQ_MODPATH"])
import research_queue
import council_browser

research_queue.QUEUE_DIR = Path(os.environ["RQ_QDIR"])
research_queue.ACTIVITY_LOG = Path(os.environ["RQ_ACTIVITY_LOG"])
research_queue.SNAPSHOT = Path(os.environ["RQ_SNAPSHOT"])
council_browser._QUERY_INST_LOG = Path(os.environ["RQ_INST_LOG"])

count_file = Path(os.environ["RQ_COUNT_FILE"])
max_file = Path(os.environ["RQ_MAX_FILE"])
worker_index = os.environ["RQ_WORKER_INDEX"]
pid_map_dir = Path(os.environ["RQ_PID_MAP_DIR"])
pid_map_dir.mkdir(parents=True, exist_ok=True)
(pid_map_dir / f"{os.getpid()}.pid").write_text(worker_index)

council = council_browser.PerplexityCouncil(perplexity_mode="research")

async def _fake_run_impl(query):
    n = int(count_file.read_text()) if count_file.exists() else 0
    n += 1
    count_file.write_text(str(n))
    # Deterministic breach detection (F6 pattern): assert INSIDE the
    # critical section, immediately after the increment.
    assert n == 1, f"CRITICAL: observed count={n} inside exclusive section (pid={os.getpid()})"
    cur_max = int(max_file.read_text()) if max_file.exists() else 0
    if n > cur_max:
        max_file.write_text(str(n))
    await asyncio.sleep(random.uniform(0.1, 0.25))
    n = int(count_file.read_text())
    n -= 1
    count_file.write_text(str(n))
    return {"query": query, "mode": "browser", "completed": True, "execution_time_ms": 1}

council._run_impl = _fake_run_impl

asyncio.run(council.run(f"wiring test query {worker_index}"))
"""


def test_run_serializes_across_processes_fifo(tmp_path: Path) -> None:
    qdir = tmp_path / "rq"
    activity_log = tmp_path / "activity.jsonl"
    snapshot = tmp_path / "snapshot.json"
    inst_log = tmp_path / "inst.jsonl"
    count_file = tmp_path / "count.txt"
    max_file = tmp_path / "max.txt"
    pid_map_dir = tmp_path / "pidmap"
    count_file.write_text("0")
    max_file.write_text("0")

    modpath = str(Path(research_queue.__file__).resolve().parent)

    base_env = os.environ.copy()
    base_env["RQ_MODPATH"] = modpath
    base_env["RQ_QDIR"] = str(qdir)
    base_env["RQ_ACTIVITY_LOG"] = str(activity_log)
    base_env["RQ_SNAPSHOT"] = str(snapshot)
    base_env["RQ_INST_LOG"] = str(inst_log)
    base_env["RQ_COUNT_FILE"] = str(count_file)
    base_env["RQ_MAX_FILE"] = str(max_file)
    base_env["RQ_PID_MAP_DIR"] = str(pid_map_dir)
    base_env["RESEARCH_QUEUE_ENABLED"] = "1"

    n_workers = 5
    procs = []
    for i in range(n_workers):
        env = base_env.copy()
        env["RQ_WORKER_INDEX"] = str(i)
        p = subprocess.Popen([sys.executable, "-c", _RUN_WIRING_WORKER_SCRIPT], env=env)
        procs.append(p)
        time.sleep(0.05)  # stagger launches so arrival order is unambiguous

    for i, p in enumerate(procs):
        rc = p.wait(timeout=60)
        assert rc == 0, f"worker {i} exited with code {rc}"

    assert max_file.read_text().strip() == "1", (
        "expected max concurrent active runs across processes == 1, got "
        f"{max_file.read_text().strip()}"
    )
    assert count_file.read_text().strip() == "0"

    # Derive ground-truth arrival order and completion order from the
    # actual production artifact (perplexity-activity.jsonl) rather than
    # from Python-side launch-timing assumptions -- map each event's
    # `session` field (ends with ":<pid>", stamped by run()'s
    # `f"{Path.cwd().name}:{os.getpid()}"`) back to a worker index via
    # the pid->index files each child wrote at startup.
    pid_to_index = {f.stem: f.read_text().strip() for f in pid_map_dir.glob("*.pid")}

    lines = [
        json.loads(line)
        for line in activity_log.read_text(encoding="utf-8").strip().splitlines()
    ]

    def _worker_index_for(event: dict) -> str:
        pid = event["session"].rsplit(":", 1)[-1]
        return pid_to_index[pid]

    arrival_order = [_worker_index_for(e) for e in lines if e["event"] == "enqueued"]
    completion_order = [_worker_index_for(e) for e in lines if e["event"] == "completed"]

    assert len(arrival_order) == n_workers
    assert len(completion_order) == n_workers
    assert arrival_order == completion_order, (
        "expected strict FIFO (completion order == arrival order), got "
        f"arrival={arrival_order} completion={completion_order}"
    )

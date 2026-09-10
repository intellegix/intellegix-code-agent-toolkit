"""research_monitor.py — active monitoring sidecar for Perplexity-browser jobs.

Tails the instrumentation / ledger / log sinks of a DETACHED extended-research
run (or an autonomous ``loop_driver`` run) in real time, distills health into a
single ``monitor-status.json``, fires Pushover on state transitions, and
auto-triggers the session keeper when a critical cookie's TTL is too short to
outlast the run.

Design contract:
- **Standalone + fail-open.** Any internal error is logged and swallowed; the
  monitor NEVER raises into the parent and NEVER kills the runner.
- **Read-only on runner state.** It writes only its own ``monitor-status.json``
  (+ ``monitor.log``) and an ADVISORY ``abort_recommended`` field. It never
  signals the runner to stop — the runner's own circuit breakers own that.
- **Thresholds sit one below the runner's** so the monitor WARNS before the
  runner's breaker fires (no race, no masking).

Usage:
    python research_monitor.py --workdir <run_dir> --kind extended-research \
        [--pid <runner_pid>] [--expected-run-min N] [--interval S] \
        [--once] [--no-pushover] [--no-remediate]

``--once`` performs a single evaluation pass over the current files and exits
(used by the unit tests); the default mode loops until the run is terminal.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- paths

HOME = Path.home()
CACHE_DIR = HOME / ".claude" / "council-cache"
GLOBAL_QUERY_INST = CACHE_DIR / "instrumentation-query.jsonl"
SESSION_PATH = HOME / ".claude" / "config" / "playwright-session.json"
PUSHOVER_CONFIG = HOME / ".claude" / "config" / "pushover.json"

# --------------------------------------------------------------------------- thresholds
# Mirror extended_research_runner.py constants, but the streak WARN sits one
# below the runner's ABORT so we warn before the breaker fires.

HEARTBEAT_STALE_S = 300            # runner HEARTBEAT_STALE_THRESHOLD_S
PARSE_FAIL_STREAK_ABORT = 3        # runner PARSE_FAIL_STREAK_THRESHOLD
SKIPPED_NETWORK_STREAK_ABORT = 3   # runner SKIPPED_NETWORK_STREAK_THRESHOLD
STREAK_WARN = 2                    # one below the abort threshold
EMPTY_RETURN_LOUD = 2              # consecutive empty returns → critical
REASONING_TRAIL_WINDOW = 10        # evaluate rate over last N queries (was 5 — too noisy)
REASONING_TRAIL_RATE = 0.4        # >40% reasoning-trail-only → loud
COOKIE_TTL_FLOOR_MIN = 10.0       # never let critical cookie TTL drop below this
KEEPER_COOLDOWN_S = 300.0         # don't re-fire the keeper within this window
KEEPER_THRASH_WINDOW_S = 900.0    # look-back window for keeper-thrash detection
KEEPER_THRASH_COUNT = 3           # keeper fired this many times in window + still short → upstream problem
ABORT_PERSIST_TICKS = 3           # advisory abort persists this many ticks → write handshake flag
CRITICAL_COOKIES = ("__cf_bm", "pplx.edge-sid", "pplx.session-id")

LEDGER_TERMINAL_STATUSES = {"COMPLETED", "ABORTED", "ABORTED-NETWORK", "INTERRUPTED"}
DEGRADED_CDP_PATHS = {"cdp_busy", "local_persistent", "local_nonpersistent"}

STATUS_SCHEMA = 1


# --------------------------------------------------------------------------- log

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(workdir: Path, line: str) -> None:
    """Append one timestamped line to ``{workdir}/monitor.log``. Never raises."""
    try:
        path = workdir / "monitor.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{_now_iso()} {line}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- io helpers

def _read_jsonl_tail(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Read JSONL records appended since byte ``offset``.

    Returns ``(records, new_offset)``. Drops a trailing partial line by only
    advancing the offset past the last complete newline. Fail-open: returns
    ``([], offset)`` on any error.
    """
    try:
        if not path.exists():
            return [], offset
        size = path.stat().st_size
        if size < offset:
            # File was truncated (e.g. 10 MB tail-rotate) — restart from 0.
            offset = 0
        if size == offset:
            return [], offset
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset  # no complete line yet
    complete = data[: last_nl + 1]
    new_offset = offset + len(complete)
    records: list[dict[str, Any]] = []
    for raw in complete.decode("utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records, new_offset


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _parse_ts(value: Any) -> float | None:
    """Parse an ISO-ish timestamp to epoch seconds. None on failure."""
    if not isinstance(value, str) or not value:
        return None
    txt = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(txt).timestamp()
    except ValueError:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return True  # unknown → assume alive, rely on other terminal signals
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- cookies

def _critical_cookie_ttl() -> dict[str, Any]:
    """Compute the minimum remaining TTL (minutes) across critical cookies.

    Returns ``{min_critical_minutes, stale_critical: [(name, minutes_left)]}``.
    ``min_critical_minutes`` is None when no critical cookie carries an expiry.
    Mirrors council_browser._check_session_freshness's cookie format.
    """
    out: dict[str, Any] = {"min_critical_minutes": None, "stale_critical": []}
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(data, list):
        return out
    now = time.time()
    min_left: float | None = None
    for cookie in data:
        if not isinstance(cookie, dict):
            continue
        if cookie.get("name") not in CRITICAL_COOKIES:
            continue
        exp = cookie.get("expires", -1)
        if not isinstance(exp, (int, float)) or exp <= 0:
            continue  # session cookie / no expiry
        minutes_left = (exp - now) / 60.0
        out["stale_critical"].append([cookie.get("name", ""), round(minutes_left, 1)])
        if min_left is None or minutes_left < min_left:
            min_left = minutes_left
    if min_left is not None:
        out["min_critical_minutes"] = round(min_left, 1)
    return out


def _trigger_keeper(workdir: Path) -> bool:
    """Fire the PerplexitySessionKeeper scheduled task (idempotent). Never raises."""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Start-ScheduledTask -TaskName 'PerplexitySessionKeeper'"],
            capture_output=True, timeout=20, check=False,
        )
        _log(workdir, "REMEDIATE triggered PerplexitySessionKeeper")
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        _log(workdir, f"REMEDIATE keeper trigger failed: {exc!r}")
        return False


# --------------------------------------------------------------------------- pushover

def _send_pushover(workdir: Path, title: str, body: str, priority: int) -> bool:
    """Minimal Pushover sender reusing ~/.claude/config/pushover.json. Fail-open."""
    try:
        cfg = json.loads(PUSHOVER_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    token, user = cfg.get("app_token"), cfg.get("user_key")
    if not token or not user:
        return False
    form = {
        "token": token, "user": user,
        "title": title[:250], "message": body[:1000],
        "priority": str(priority),
        "sound": cfg.get("default_sound", "incoming"),
    }
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json",
                                 data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = json.loads(resp.read()).get("status") == 1
        _log(workdir, f"PUSHOVER sent ok={ok} title={title!r}")
        return ok
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        _log(workdir, f"PUSHOVER failed: {exc!r}")
        return False


# --------------------------------------------------------------------------- state

@dataclass
class MonitorState:
    """Accumulated, bounded view of a run used to compute health each tick."""

    workdir: Path
    kind: str
    pid: int | None
    expected_run_min: float
    start_ts: float

    inst_offset: int = 0
    query_offset: int = 0
    trace_offset: int = 0

    pass_statuses: list[str] = field(default_factory=list)   # trailing window
    pass_empty: list[bool] = field(default_factory=list)     # parallel: was the pass empty?
    query_window: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=lambda: {
        "empty_returns": 0, "reasoning_trail": 0, "parse_failed": 0,
        "skipped_network": 0, "cdp_busy": 0, "local_fallback": 0,
        "result_suspect": 0,
    })
    passes_completed: int = 0
    last_event_ts: float = 0.0

    keeper_last_fired: float = 0.0
    keeper_fire_history: list[float] = field(default_factory=list)  # epoch times keeper fired
    cookie_auto_refreshed: bool = False
    cookie_last_refresh_iso: str | None = None

    abort_persist_ticks: int = 0   # consecutive ticks abort_recommended has been true
    notified_keys: set[str] = field(default_factory=set)
    last_health: str = "OK"


def _trailing_streak(statuses: list[str], target: str) -> int:
    """Count trailing consecutive entries equal to ``target``."""
    count = 0
    for status in reversed(statuses):
        if status == target:
            count += 1
        else:
            break
    return count


# --------------------------------------------------------------------------- evaluate

def evaluate(state: MonitorState, *, remediate: bool) -> dict[str, Any]:
    """Ingest new records, update counters, and return the status dict."""
    wd = state.workdir

    # 1. Ingest per-run pass instrumentation (extended-research only).
    inst_path = wd / "instrumentation.jsonl"
    recs, state.inst_offset = _read_jsonl_tail(inst_path, state.inst_offset)
    for rec in recs:
        status = str(rec.get("status") or "")
        is_empty = rec.get("raw_response_len_chars", 1) == 0 or status == "SKIPPED-NETWORK"
        state.pass_statuses.append(status)
        state.pass_empty.append(is_empty)
        ts = _parse_ts(rec.get("ts") or rec.get("timestamp"))
        if ts:
            state.last_event_ts = max(state.last_event_ts, ts)
        if status == "COMPLETED":
            state.passes_completed += 1
        if status == "PARSE-FAILED":
            state.counters["parse_failed"] += 1
        if status == "SKIPPED-NETWORK":
            state.counters["skipped_network"] += 1
        if is_empty:
            state.counters["empty_returns"] += 1
    state.pass_statuses = state.pass_statuses[-20:]
    state.pass_empty = state.pass_empty[-20:]

    # 2. Ingest global per-query instrumentation, filtered to this run's window.
    qrecs, state.query_offset = _read_jsonl_tail(GLOBAL_QUERY_INST, state.query_offset)
    for rec in qrecs:
        ts = _parse_ts(rec.get("ts"))
        if ts is not None and ts < state.start_ts - 5:
            continue  # belongs to a different/older run
        if ts:
            state.last_event_ts = max(state.last_event_ts, ts)
        state.query_window.append(rec)
        if rec.get("reasoning_trail_detection") == "flagged_blanked":
            state.counters["reasoning_trail"] += 1
        chrome = rec.get("chrome_path_used")
        if chrome == "cdp_busy":
            state.counters["cdp_busy"] += 1
        elif isinstance(chrome, str) and chrome.startswith("local"):
            state.counters["local_fallback"] += 1
        if rec.get("result_validation") == "suspect_truncated":
            state.counters["result_suspect"] += 1
    state.query_window = state.query_window[-REASONING_TRAIL_WINDOW:]

    # 3. Ingest autonomous-loop trace (loop kind only) for heartbeat. Accept
    #    --workdir pointing at either the project root or its .workflow dir.
    if state.kind == "autonomous-loop":
        trace_path = wd / ".workflow" / "trace.jsonl"
        if not trace_path.exists() and (wd / "trace.jsonl").exists():
            trace_path = wd / "trace.jsonl"
        # Heartbeat only: loop-level liveness comes from trace timestamps; the
        # Perplexity-call health (empty / reasoning-trail / cdp / cookie TTL) for
        # the loop's research calls comes from the global query log ingested above.
        trecs, state.trace_offset = _read_jsonl_tail(trace_path, state.trace_offset)
        for rec in trecs:
            ts = _parse_ts(rec.get("timestamp"))
            if ts:
                state.last_event_ts = max(state.last_event_ts, ts)

    # 4. Terminal detection.
    ledger = _read_json(wd / "ledger.json")
    done_sentinel = (wd / "runner.log.done").exists()
    ledger_status = str(ledger.get("status")) if ledger else None
    terminal = bool(
        done_sentinel
        or (ledger_status in LEDGER_TERMINAL_STATUSES)
        or not _pid_alive(state.pid)
    )

    # 5. Cookie TTL + proactive remediation.
    cookie = _critical_cookie_ttl()
    elapsed_min = (time.time() - state.start_ts) / 60.0
    remaining_run_min = max(0.0, state.expected_run_min - elapsed_min)
    ttl_floor = max(COOKIE_TTL_FLOOR_MIN, remaining_run_min)
    min_ttl = cookie.get("min_critical_minutes")
    cookie_short = isinstance(min_ttl, (int, float)) and min_ttl < ttl_floor
    if cookie_short and remediate and not terminal:
        if (time.time() - state.keeper_last_fired) > KEEPER_COOLDOWN_S:
            if _trigger_keeper(wd):
                now = time.time()
                state.keeper_last_fired = now
                state.keeper_fire_history.append(now)
                state.cookie_auto_refreshed = True
                state.cookie_last_refresh_iso = _now_iso()
    # Keeper-thrash: fired repeatedly in the look-back window but cookies are
    # STILL short → the problem has moved upstream (Perplexity policy / account)
    # and automation can no longer remediate it. Surface as a distinct class so
    # a successful-looking refresh doesn't mask a persistent failure.
    recent_fires = [t for t in state.keeper_fire_history
                    if (time.time() - t) <= KEEPER_THRASH_WINDOW_S]
    state.keeper_fire_history = recent_fires
    keeper_thrashing = cookie_short and len(recent_fires) >= KEEPER_THRASH_COUNT
    cookie_out = {
        "min_critical_minutes": min_ttl,
        "stale_critical": cookie.get("stale_critical", []),
        "auto_refreshed": state.cookie_auto_refreshed,
        "last_refresh_ts": state.cookie_last_refresh_iso,
    }

    # 6. Build alerts.
    alerts: list[dict[str, Any]] = []
    parse_streak = _trailing_streak(state.pass_statuses, "PARSE-FAILED")
    net_streak = _trailing_streak(state.pass_statuses, "SKIPPED-NETWORK")
    empty_streak = _trailing_streak(["EMPTY" if e else "X" for e in state.pass_empty], "EMPTY")
    abort_recommended = False
    abort_reason: str | None = None

    def add(signal: str, severity: str, detail: str) -> None:
        alerts.append({"signal": signal, "severity": severity,
                       "detail": detail, "ts": _now_iso()})

    if parse_streak >= PARSE_FAIL_STREAK_ABORT:
        abort_recommended, abort_reason = True, f"{parse_streak} consecutive PARSE-FAILED"
        add("parse_failed_streak", "critical", abort_reason + " (runner aborts)")
    elif parse_streak >= STREAK_WARN:
        add("parse_failed_streak", "warn",
            f"{parse_streak} consecutive PARSE-FAILED ({PARSE_FAIL_STREAK_ABORT - parse_streak} from abort)")

    if net_streak >= SKIPPED_NETWORK_STREAK_ABORT:
        abort_recommended, abort_reason = True, f"{net_streak} consecutive SKIPPED-NETWORK"
        add("skipped_network_streak", "critical", abort_reason + " (runner aborts)")
    elif net_streak >= STREAK_WARN:
        add("skipped_network_streak", "warn",
            f"{net_streak} consecutive SKIPPED-NETWORK ({SKIPPED_NETWORK_STREAK_ABORT - net_streak} from abort)")

    if empty_streak >= EMPTY_RETURN_LOUD:
        add("empty_return", "critical", f"{empty_streak} consecutive empty returns")
    elif empty_streak == 1:
        add("empty_return", "warn", "1 empty return")

    if state.query_window:
        rt = sum(1 for q in state.query_window
                 if q.get("reasoning_trail_detection") == "flagged_blanked")
        if rt / len(state.query_window) > REASONING_TRAIL_RATE:
            add("reasoning_trail", "critical",
                f"{rt}/{len(state.query_window)} recent queries reasoning-trail-only")
        # Empty-synthesis queries (disjoint from reasoning-trail). This is the
        # primary empty-return signal for autonomous loops, which have no
        # runner-side instrumentation.jsonl. Relies on council_browser now
        # recording exit_reason="empty_synthesis" / 0 chars instead of "completed".
        eq = sum(1 for q in state.query_window
                 if (q.get("exit_reason") == "empty_synthesis"
                     or q.get("extracted_synthesis_chars", 1) == 0)
                 and q.get("reasoning_trail_detection") != "flagged_blanked")
        if eq / len(state.query_window) > REASONING_TRAIL_RATE:
            add("empty_query", "critical",
                f"{eq}/{len(state.query_window)} recent queries returned empty synthesis")
        # Result-validation survivors: synthesis was stored but failed the
        # post-extraction completeness check (truncation / parse / UI regression).
        rs = sum(1 for q in state.query_window
                 if q.get("result_validation") == "suspect_truncated")
        if rs:
            sev = "critical" if rs / len(state.query_window) > REASONING_TRAIL_RATE else "warn"
            add("result_suspect", sev,
                f"{rs}/{len(state.query_window)} recent queries failed result-validation "
                "(suspect truncated/incomplete)")

    if state.query_window and state.query_window[-1].get("chrome_path_used") in DEGRADED_CDP_PATHS:
        add("cdp_degraded", "warn",
            f"chrome_path_used={state.query_window[-1].get('chrome_path_used')} (keeper cookies not in use)")

    if keeper_thrashing:
        add("keeper_thrashing", "critical",
            f"keeper fired {len(recent_fires)}x in {int(KEEPER_THRASH_WINDOW_S / 60)}min but "
            f"critical cookie TTL still {min_ttl}min — problem is upstream (Perplexity policy / "
            f"account); automation cannot remediate. Needs manual session re-login.")
    elif cookie_short:
        sev = "critical" if (state.cookie_auto_refreshed and isinstance(min_ttl, (int, float))
                             and min_ttl < COOKIE_TTL_FLOOR_MIN) else "warn"
        add("cookie_short_ttl", sev,
            f"critical cookie TTL {min_ttl}min < floor {round(ttl_floor, 1)}min"
            + (" (keeper fired)" if state.cookie_auto_refreshed else ""))

    seconds_since = (time.time() - state.last_event_ts) if state.last_event_ts else 0.0
    stalled = (not terminal and state.last_event_ts > 0
               and seconds_since > HEARTBEAT_STALE_S)
    if stalled:
        add("stalled", "critical", f"no new event in {int(seconds_since)}s")

    empty_result_run = bool(
        terminal and state.passes_completed > 0
        and state.counters["parse_failed"] + state.counters["skipped_network"] == 0
        and sum(1 for s in state.pass_statuses if s == "COMPLETED") > 0
        and ledger is not None
        and not (ledger.get("pass_log") and any(
            (p or {}).get("net_new_findings", 0) for p in ledger.get("pass_log", [])))
    )
    if empty_result_run:
        add("empty_result_run", "warn", "run completed with zero net findings")

    # 6b. Zombie handshake: if the advisory abort persists across several ticks
    #     while the runner has NOT self-aborted (its own breaker never fired —
    #     e.g. events stopped arriving so the streak counter is frozen), write a
    #     sentinel that Claude's orchestrator explicitly checks and acts on. The
    #     monitor still never kills the runner — this is a two-step handshake.
    if abort_recommended and not terminal:
        state.abort_persist_ticks += 1
    else:
        state.abort_persist_ticks = 0
    if state.abort_persist_ticks >= ABORT_PERSIST_TICKS:
        try:
            (wd / "monitor-abort-requested.flag").write_text(
                json.dumps({"ts": _now_iso(), "reason": abort_reason,
                            "persisted_ticks": state.abort_persist_ticks}),
                encoding="utf-8")
        except OSError as exc:
            _log(wd, f"abort-flag write failed: {exc!r}")

    # 7. Health rollup.
    if terminal:
        health = "DONE"
    elif stalled:
        health = "STALLED"
    elif any(a["severity"] == "critical" for a in alerts):
        health = "CRITICAL"
    elif alerts:
        health = "DEGRADED"
    else:
        health = "OK"

    status = {
        "schema": STATUS_SCHEMA,
        "run_kind": state.kind,
        "workdir": str(state.workdir),
        "pid": state.pid,
        "updated_ts": _now_iso(),
        "health": health,
        "last_event_ts": (datetime.fromtimestamp(state.last_event_ts, timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ") if state.last_event_ts else None),
        "seconds_since_last_event": int(seconds_since),
        "passes_completed": state.passes_completed,
        "counters": dict(state.counters),
        "cookie_ttl": cookie_out,
        "active_alerts": alerts,
        "abort_recommended": abort_recommended,
        "abort_reason": abort_reason,
        "abort_persist_ticks": state.abort_persist_ticks,
        "ledger_status": ledger_status,
        "terminal": terminal,
    }
    return status


# --------------------------------------------------------------------------- status io

def write_status(workdir: Path, status: dict[str, Any]) -> None:
    """Atomically write monitor-status.json. Never raises."""
    try:
        path = workdir / "monitor-status.json"
        tmp = workdir / "monitor-status.json.tmp"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        _log(workdir, f"write_status failed: {exc!r}")


def maybe_notify(state: MonitorState, status: dict[str, Any], *, enabled: bool) -> None:
    """Fire Pushover on health worsening or a new alert key (dedupe via state)."""
    if not enabled:
        return
    severity_rank = {"OK": 0, "DONE": 0, "DEGRADED": 1, "STALLED": 2, "CRITICAL": 2}
    health = status["health"]
    worsened = severity_rank.get(health, 0) > severity_rank.get(state.last_health, 0)
    current_keys = {f'{a["signal"]}:{a["severity"]}' for a in status["active_alerts"]}
    new_keys = [a for a in status["active_alerts"]
                if f'{a["signal"]}:{a["severity"]}' not in state.notified_keys]
    if worsened or new_keys:
        lines = [f'- {a["signal"]} [{a["severity"]}]: {a["detail"]}'
                 for a in (new_keys or status["active_alerts"])]
        priority = 1 if health in ("CRITICAL", "STALLED") else 0
        _send_pushover(
            state.workdir,
            f"[research-monitor] {health}: {state.kind}",
            "\n".join(lines) or f"health={health}",
            priority,
        )
    # Reset to exactly the currently-active keys: resolved alerts drop out (so a
    # recurrence re-fires), still-active alerts stay suppressed (no spam).
    state.notified_keys = current_keys
    state.last_health = health


# --------------------------------------------------------------------------- calibration

def _finalize_calibration(workdir: Path) -> None:
    """Run the existing calibration aggregator at terminal. Best-effort."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import calibration_log  # type: ignore
        summary = calibration_log.aggregate_run(Path(workdir))
        if summary and hasattr(calibration_log, "append_to_global_summary"):
            calibration_log.append_to_global_summary(summary)
        _log(workdir, "calibration aggregate_run done")
    except Exception as exc:  # noqa: BLE001 — best-effort, never block exit
        _log(workdir, f"calibration finalize skipped: {exc!r}")


# --------------------------------------------------------------------------- main

def _run_once(state: MonitorState, *, remediate: bool, notify: bool) -> dict[str, Any]:
    status = evaluate(state, remediate=remediate)
    write_status(state.workdir, status)
    maybe_notify(state, status, enabled=notify)
    return status


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Active monitor for Perplexity-browser runs.")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--kind", default="extended-research",
                        choices=["extended-research", "autonomous-loop"])
    parser.add_argument("--pid", type=int, default=None)
    parser.add_argument("--expected-run-min", type=float, default=40.0)
    parser.add_argument("--interval", type=float, default=12.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-pushover", action="store_true")
    parser.add_argument("--no-remediate", action="store_true")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).expanduser()
    state = MonitorState(
        workdir=workdir, kind=args.kind, pid=args.pid,
        expected_run_min=args.expected_run_min, start_ts=time.time(),
    )
    remediate = not args.no_remediate
    notify = not args.no_pushover
    _log(workdir, f"START kind={args.kind} pid={args.pid} once={args.once}")

    if args.once:
        _run_once(state, remediate=remediate, notify=notify)
        return 0

    while True:
        try:
            status = _run_once(state, remediate=remediate, notify=notify)
        except Exception as exc:  # noqa: BLE001 — fail-open monitor loop
            _log(workdir, f"tick error (continuing): {exc!r}")
            time.sleep(args.interval)
            continue
        if status.get("terminal"):
            _log(workdir, f"TERMINAL health={status['health']} "
                          f"ledger={status.get('ledger_status')}")
            _finalize_calibration(workdir)
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # noqa: BLE001 — must never crash into the parent
        try:
            _log(Path.cwd(), f"top_level_exception {type(exc).__name__}: {exc}")
        except Exception:
            pass
        sys.exit(0)

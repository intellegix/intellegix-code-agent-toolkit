"""queue_monitor.py -- active monitoring sidecar for the Perplexity research
FIFO queue (`research_queue.py`).

Watches the central queue snapshot (`perplexity-queue.json`) and activity log
(`perplexity-activity.jsonl`) that `research_queue.py` publishes, and alerts
on failures/stalls so the queue can be actively monitored while serializing
research runs across up to 9 concurrent local Claude Code sessions.

Design contract (mirrors research_monitor.py):
- **Fail-open.** Missing/torn snapshot or activity-log files never crash the
  poll loop -- they are treated as "no data this tick" and logged at debug.
- **Read-only.** This module never mutates queue state (tickets, locks,
  snapshot, activity log) -- it only reads research_queue.py's published
  outputs and (optionally) fires Pushover notifications / triggers the
  session keeper as a best-effort remediation.
- **REUSE, don't reimplement.** Pushover delivery and keeper-triggering are
  imported directly from research_monitor.py rather than duplicated.

Usage:
    python queue_monitor.py [--interval N] [--once] [--no-pushover]

``--once`` performs a single evaluation pass and exits (used by the unit
tests and for cron-style invocation); the default mode polls forever.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import research_queue

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Paths (monkeypatchable module globals -- resolved by name at call time so
# tests can safely do `monkeypatch.setattr(queue_monitor, "SNAPSHOT_PATH",
# tmp_path / "snapshot.json")` etc. without ever touching the real live
# queue state).
# --------------------------------------------------------------------------
SNAPSHOT_PATH: Path = research_queue.SNAPSHOT
ACTIVITY_LOG_PATH: Path = research_queue.ACTIVITY_LOG
LOG_WORKDIR: Path = Path.home() / ".claude" / "council-logs" / "queue-monitor"
# Daemon stdio sink: when run under pythonw.exe by Task Scheduler (--daemon),
# fds 1/2 are redirected here so the otherwise-discarded poll table + logging
# output stay diagnosable. Mirrors browser_bridge_keeper._harden_for_windows_daemon.
LOG_PATH: Path = Path.home() / ".claude" / "logs" / "queue_monitor.log"

# --------------------------------------------------------------------------
# Thresholds (monkeypatchable module globals -- see evaluate()).
# --------------------------------------------------------------------------
STALL_TTL_S = 120       # active.heartbeat_age_s past this -> holder may be dead/hung
DEEP_QUEUE_N = 6        # stats.depth (or len(queued)) past this -> congestion
LONG_RUN_S = 600        # active.elapsed_s past this -> stuck well past normal
SELF_HEAL_COOLDOWN_S = 300.0  # minimum gap between best-effort keeper triggers
# Stalled-near-empty detector (Jul 12-14 empirical failure mode): a run that
# burned a long wall-clock but returned almost nothing = a session/synthesis
# stall, NOT a legitimately short answer (those return fast). Both legs required
# so a fast "443" reply (<300s) never trips it. Perplexity-verified 2026-07-14.
STALL_EMPTY_S = 300     # elapsed_wall_s past this AND ...
STALL_EMPTY_CHARS = 300  # ... extracted_synthesis_chars below this -> stalled-empty

# Per-query instrumentation JSONL emitted by council_browser (carries
# elapsed_wall_s / extracted_synthesis_chars / exit_reason / min_critical_ttl_s,
# which the activity log does not). Monkeypatchable for tests.
INSTRUMENTATION_LOG_PATH: Path = (
    Path.home() / ".claude" / "council-cache" / "instrumentation-query.jsonl"
)

_COOKIE_FAILURE_SIGNATURE_RE = re.compile(
    r"cookie|session[-_ ]?(expired|stale)|unauthorized|re-?login|auth", re.IGNORECASE
)

# --------------------------------------------------------------------------
# Best-effort reuse of research_monitor's Pushover + keeper helpers. If
# research_monitor is unavailable (missing module, import error), degrade
# gracefully: log once and disable notifications/self-heal rather than
# crashing the queue monitor.
# --------------------------------------------------------------------------
try:
    import research_monitor as _rm

    _send_pushover = _rm._send_pushover
    _trigger_keeper = _rm._trigger_keeper
except Exception as exc:  # noqa: BLE001 -- must never block queue_monitor from running
    logger.warning(
        "queue_monitor: research_monitor unavailable, pushover/self-heal disabled (%s)", exc
    )
    _send_pushover = None
    _trigger_keeper = None


@dataclass(frozen=True)
class Alert:
    """One monitoring finding.

    `key` is stable across ticks for the same underlying condition/event, so
    callers can dedupe notifications without re-alerting on every poll.
    """

    key: str
    severity: str  # "info" | "warning" | "critical"
    message: str


# --------------------------------------------------------------------------
# Pure evaluation
# --------------------------------------------------------------------------
def evaluate(
    snapshot: Optional[Dict[str, Any]],
    recent_events: Sequence[Dict[str, Any]],
    recent_instrumentation: Sequence[Dict[str, Any]] = (),
) -> List[Alert]:
    """Compute alerts from a queue snapshot + a batch of activity-log events.

    `recent_events` is expected to be the events *new since the caller's last
    poll* (see `_tail_new_activity`) -- that is how cross-poll dedup of
    error/timeout alerts is achieved in `run_loop` without this function
    needing to hold any state itself. Within a single call, a duplicate
    run_id+event pair in `recent_events` is collapsed to one alert (this
    function stays a pure, stateless computation).

    Threshold globals (STALL_TTL_S, DEEP_QUEUE_N, LONG_RUN_S) are read by
    name at call time (never captured as default-arg values), so tests can
    `monkeypatch.setattr(queue_monitor, "STALL_TTL_S", ...)` and have it
    take effect immediately.
    """
    alerts: List[Alert] = []

    if snapshot:
        active = snapshot.get("active")
        if isinstance(active, dict):
            run_id = active.get("run_id", "?")
            session = active.get("session", "?")
            hb_age = active.get("heartbeat_age_s")
            if isinstance(hb_age, (int, float)) and hb_age > STALL_TTL_S:
                alerts.append(
                    Alert(
                        key=f"stalled:{run_id}",
                        severity="critical",
                        message=(
                            f"active run {run_id} (session={session}) heartbeat stale "
                            f"{hb_age}s > {STALL_TTL_S}s -- holder may be dead/hung"
                        ),
                    )
                )
            elapsed = active.get("elapsed_s")
            if isinstance(elapsed, (int, float)) and elapsed > LONG_RUN_S:
                alerts.append(
                    Alert(
                        key=f"long_run:{run_id}",
                        severity="warning",
                        message=(
                            f"active run {run_id} (session={session}) elapsed {elapsed}s "
                            f"> {LONG_RUN_S}s -- stuck well past normal"
                        ),
                    )
                )

        stats = snapshot.get("stats") or {}
        depth = stats.get("depth")
        if not isinstance(depth, (int, float)):
            depth = len(snapshot.get("queued") or [])
        if isinstance(depth, (int, float)) and depth > DEEP_QUEUE_N:
            alerts.append(
                Alert(
                    key="deep_queue",
                    severity="warning",
                    message=f"queue depth {depth} > {DEEP_QUEUE_N} -- congestion",
                )
            )

    seen_in_batch: Set[str] = set()
    for ev in recent_events or []:
        event = ev.get("event")
        if event not in ("error", "timeout"):
            continue
        run_id = ev.get("run_id", "?")
        dedupe_key = f"{run_id}:{event}"
        if dedupe_key in seen_in_batch:
            continue
        seen_in_batch.add(dedupe_key)
        session = ev.get("session", "?")
        detail = ev.get("error") or ev.get("query_preview") or ""
        severity = "critical" if event == "error" else "warning"
        alerts.append(
            Alert(
                key=f"event:{dedupe_key}",
                severity=severity,
                message=f"{event} run_id={run_id} session={session} {detail}".strip(),
            )
        )

    # Stalled-near-empty runs: completed but burned a long wall-clock for almost
    # no output = a session/synthesis stall (the Jul 12-14 real failure mode),
    # distinct from a legitimately short answer (which returns fast). The
    # `stalled:` key prefix makes _should_self_heal() trigger a keeper cookie
    # refresh for the NEXT run (the stalls correlate with low session-cookie TTL).
    seen_stall: Set[str] = set()
    for rec in recent_instrumentation or []:
        if rec.get("exit_reason") != "completed":
            continue
        elapsed = rec.get("elapsed_wall_s")
        chars = rec.get("extracted_synthesis_chars")
        if not isinstance(elapsed, (int, float)) or not isinstance(chars, (int, float)):
            continue
        if elapsed <= STALL_EMPTY_S or chars >= STALL_EMPTY_CHARS:
            continue
        run_id = rec.get("run_id", "?")
        if run_id in seen_stall:
            continue
        seen_stall.add(run_id)
        ttl = rec.get("min_critical_ttl_s")
        ttl_hint = (
            f", low session-cookie TTL {ttl:.0f}s at run"
            if isinstance(ttl, (int, float)) and ttl < 200
            else ""
        )
        alerts.append(
            Alert(
                key=f"stalled:empty:{run_id}",
                severity="critical",
                message=(
                    f"Perplexity stalled near-empty: run {run_id} took "
                    f"{elapsed:.0f}s but returned only {int(chars)} chars"
                    f"{ttl_hint} -- likely a session stall; refreshing cookies "
                    f"for the next run."
                ),
            )
        )

    return alerts


# --------------------------------------------------------------------------
# I/O helpers (fail-open)
# --------------------------------------------------------------------------
def _read_snapshot() -> Optional[Dict[str, Any]]:
    """Read + parse SNAPSHOT_PATH. Tolerates a missing or torn (partially
    written) file by returning None -- never raises into the poll loop.
    """
    try:
        text = SNAPSHOT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("_read_snapshot: %s unavailable (%s)", SNAPSHOT_PATH, exc)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.debug("_read_snapshot: %s is torn/invalid JSON, skipping this poll (%s)",
                     SNAPSHOT_PATH, exc)
        return None
    return data if isinstance(data, dict) else None


def _tail_new_activity(offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """Read JSONL activity records appended to ACTIVITY_LOG_PATH since byte
    `offset`. Returns `(records, new_offset)`. A trailing partial line is
    left unconsumed (offset only advances past the last complete newline).
    Fail-open: returns `([], offset)` on any I/O error, and restarts from 0
    if the file shrank (rotated/truncated) since the last poll.
    """
    try:
        if not ACTIVITY_LOG_PATH.exists():
            return [], offset
        size = ACTIVITY_LOG_PATH.stat().st_size
        if size < offset:
            offset = 0
        if size == offset:
            return [], offset
        with ACTIVITY_LOG_PATH.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError as exc:
        logger.debug("_tail_new_activity: could not read %s (%s)", ACTIVITY_LOG_PATH, exc)
        return [], offset

    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset  # no complete line yet
    complete = data[: last_nl + 1]
    new_offset = offset + len(complete)

    records: List[Dict[str, Any]] = []
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


def _tail_new_jsonl(path: Path, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """Generic sibling of `_tail_new_activity` for an arbitrary JSONL `path`.
    Same fail-open, shrink-detect, partial-final-line semantics. Used to tail
    the per-query instrumentation log without touching the activity-log reader.
    """
    try:
        if not path.exists():
            return [], offset
        size = path.stat().st_size
        if size < offset:
            offset = 0
        if size == offset:
            return [], offset
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError as exc:
        logger.debug("_tail_new_jsonl: %s unavailable (%s)", path, exc)
        return [], offset
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset  # no complete line yet
    complete = data[: last_nl + 1]
    new_offset = offset + len(complete)
    records: List[Dict[str, Any]] = []
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


def _tail_new_instrumentation(offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """Tail new per-query instrumentation records since byte `offset`."""
    return _tail_new_jsonl(INSTRUMENTATION_LOG_PATH, offset)


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------
def _format_table(snapshot: Optional[Dict[str, Any]], alerts: Sequence[Alert]) -> str:
    """Render a compact, human-readable live status table as a string."""
    lines = [f"--- queue_monitor {time.strftime('%Y-%m-%d %H:%M:%S')} ---"]
    if not snapshot:
        lines.append("  (no snapshot available)")
    else:
        active = snapshot.get("active")
        if isinstance(active, dict):
            lines.append(
                f"  ACTIVE  run={str(active.get('run_id', '?'))[:8]} "
                f"session={active.get('session', '?')} "
                f"elapsed={active.get('elapsed_s', '?')}s "
                f"hb_age={active.get('heartbeat_age_s', '?')}s "
                f"query={active.get('query_preview', '')!r}"
            )
        else:
            lines.append("  ACTIVE  (none)")

        queued = snapshot.get("queued") or []
        if queued:
            for q in queued[:5]:
                lines.append(
                    f"  QUEUED  #{q.get('position')} session={q.get('session', '?')} "
                    f"wait={q.get('wait_s', '?')}s query={q.get('query_preview', '')!r}"
                )
            if len(queued) > 5:
                lines.append(f"  ...     and {len(queued) - 5} more queued")
        else:
            lines.append("  QUEUED  (empty)")

        stats = snapshot.get("stats") or {}
        lines.append(
            f"  STATS   depth={stats.get('depth', '?')} "
            f"total_today={stats.get('total_today', '?')} "
            f"errors_today={stats.get('errors_today', '?')}"
        )

        recent = snapshot.get("recent") or []
        for r in recent[-3:]:
            lines.append(
                f"  RECENT  {str(r.get('event', '?')):9s} "
                f"run={str(r.get('run_id', '?'))[:8]} session={r.get('session', '?')}"
            )

    for a in alerts:
        lines.append(f"  ALERT   [{a.severity}] {a.message}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Notification + self-heal
# --------------------------------------------------------------------------
def _fire_alert(alert: Alert) -> None:
    """Send one Pushover notification for `alert`. Best-effort: logs and
    returns on any failure, never raises.
    """
    if _send_pushover is None:
        logger.info("queue_monitor: pushover unavailable, alert not sent: %s", alert.message)
        return
    priority = 1 if alert.severity == "critical" else 0
    try:
        ok = _send_pushover(
            LOG_WORKDIR, f"[queue_monitor] {alert.severity.upper()}", alert.message, priority
        )
    except Exception as exc:  # noqa: BLE001 -- notification failures must never crash the loop
        logger.warning("queue_monitor: _send_pushover raised for key=%s (%s)", alert.key, exc)
        return
    if not ok:
        logger.debug("queue_monitor: pushover send returned falsy for key=%s", alert.key)


def _should_self_heal(alerts: Sequence[Alert]) -> bool:
    """True if any current alert looks like a stalled-holder or a
    cookie/session-failure signature -- the two conditions a keeper refresh
    can plausibly help with.
    """
    for a in alerts:
        if a.key.startswith("stalled:"):
            return True
        if a.key.startswith("event:") and _COOKIE_FAILURE_SIGNATURE_RE.search(a.message):
            return True
    return False


def _maybe_self_heal(alerts: Sequence[Alert], last_fired_ts: float) -> float:
    """Best-effort keeper trigger, rate-limited by SELF_HEAL_COOLDOWN_S.
    Returns the (possibly updated) last-fired timestamp.
    """
    if _trigger_keeper is None:
        return last_fired_ts
    if not _should_self_heal(alerts):
        return last_fired_ts
    if (time.time() - last_fired_ts) <= SELF_HEAL_COOLDOWN_S:
        return last_fired_ts
    try:
        _trigger_keeper(LOG_WORKDIR)
    except Exception as exc:  # noqa: BLE001 -- self-heal must never crash the loop
        logger.warning("queue_monitor: _trigger_keeper raised (%s)", exc)
    return time.time()


# --------------------------------------------------------------------------
# Poll loop
# --------------------------------------------------------------------------
def _harden_for_windows_daemon() -> None:
    """Redirect fds 1/2 to LOG_PATH so pythonw Task Scheduler runs are diagnosable.

    Called from main() ONLY for --daemon (persistent background service), so an
    interactive `python queue_monitor.py` run still prints its table to the
    console. Mirrors browser_bridge_keeper._harden_for_windows_daemon.
    """
    if sys.platform != "win32":
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(str(LOG_PATH), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(log_fd)
        sys.stdout = os.fdopen(1, "w", buffering=1)
        sys.stderr = os.fdopen(2, "w", buffering=1)
    except (OSError, ValueError):
        pass
    for sig_name in ("SIGBREAK", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass


def run_loop(interval_s: float = 10.0, once: bool = False, use_pushover: bool = True) -> None:
    """Poll SNAPSHOT_PATH/ACTIVITY_LOG_PATH, evaluate, print, and notify.

    `once=True` performs a single pass and returns (used by tests and
    cron-style invocation). Notifications are deduped per alert `key`: a
    condition that stays active across polls only notifies once, but if it
    resolves (key drops out of the current alert set) and later recurs, it
    notifies again -- mirroring research_monitor.maybe_notify's
    reset-to-current-set pattern.
    """
    offset = 0
    # Seed the instrumentation tail at the CURRENT end-of-file so a freshly
    # started monitor alerts only on stalls that occur WHILE it is watching,
    # instead of replaying every historical stall in the log on startup.
    try:
        inst_offset = INSTRUMENTATION_LOG_PATH.stat().st_size
    except OSError:
        inst_offset = 0
    previously_active_keys: Set[str] = set()
    last_self_heal_ts = 0.0

    while True:
        snapshot = _read_snapshot()
        new_events, offset = _tail_new_activity(offset)
        new_inst, inst_offset = _tail_new_instrumentation(inst_offset)
        alerts = evaluate(snapshot, new_events, new_inst)

        print(_format_table(snapshot, alerts))

        current_keys = {a.key for a in alerts}
        new_keys = current_keys - previously_active_keys
        if new_keys:
            for a in alerts:
                if a.key in new_keys:
                    if use_pushover:
                        _fire_alert(a)
                    else:
                        logger.info(
                            "queue_monitor: alert suppressed (--no-pushover): [%s] %s",
                            a.severity, a.message,
                        )
        previously_active_keys = current_keys

        last_self_heal_ts = _maybe_self_heal(alerts, last_self_heal_ts)

        if once:
            return
        time.sleep(interval_s)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monitor the Perplexity research FIFO queue for stalls/errors/congestion."
    )
    parser.add_argument("--interval", type=float, default=10.0,
                        help="Poll interval in seconds (default: 10).")
    parser.add_argument("--once", action="store_true",
                        help="Perform a single evaluation pass and exit.")
    parser.add_argument("--no-pushover", action="store_true",
                        help="Disable Pushover notifications (still prints + logs).")
    parser.add_argument("--daemon", action="store_true",
                        help="Persistent background service mode: redirect stdout/stderr "
                             "to LOG_PATH (~/.claude/logs/queue_monitor.log) so a pythonw "
                             "Task Scheduler run stays diagnosable. Implies looping.")
    args = parser.parse_args(argv)

    if args.daemon:
        _harden_for_windows_daemon()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_loop(interval_s=args.interval, once=args.once, use_pushover=not args.no_pushover)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

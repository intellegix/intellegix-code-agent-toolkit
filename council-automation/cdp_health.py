"""Pre-attach health check for the keeper Chrome's CDP endpoint.

WHY THIS EXISTS (2026-08-22 outage, see PERPLEXITY-ACTIVATION-EVIDENCE-2026-08-22.md)
------------------------------------------------------------------------------------
Playwright's ``chromium.connect_over_cdp()`` does not merely open a websocket. After
the browser socket connects it issues ``Target.setAutoAttach`` and then *initialises
every attached page target* (Runtime.enable, Page.enable, ...). A single page whose
renderer process has hung answers none of that, and Playwright waits on it forever —
the whole connect blocks until its 180 s timeout, even though the browser process
itself is perfectly healthy and ``/json/list`` responds instantly.

On 2026-08-22 exactly one wedged ``https://www.perplexity.ai/`` tab in the keeper
Chrome took down the entire research pipeline for six hours:

    session_keeper.py   -> connect_over_cdp times out (53 consecutive attempts)
    council_browser.py  -> connect_over_cdp times out, falls back to a temp profile,
                           whose cookies are stale, so the fallback fires the keeper
                           to refresh -- which fails the same way -- and the run ends
                           on a not-logged-in browser where the /research slash
                           command does not exist, reported as the wholly misleading
                           "Failed to activate research mode".

The browser-process-level HTTP endpoints (``/json/list``, ``/json/close``) are served
by the *browser* process, not the renderer, so they keep working when a renderer is
wedged. That is the escape hatch this module uses: probe each page target on its own
websocket with a short budget, close the ones that do not answer, and only then let
Playwright attach.

Safe to call unconditionally and on every run: when the endpoint is healthy it costs
one HTTP GET plus a few concurrent websocket round-trips (~1 s) and closes nothing.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable

# A hung renderer answers nothing; a healthy one answers in single-digit ms even
# under load. 5 s is ~50x the observed healthy latency and still 36x cheaper than
# the 180 s Playwright timeout it exists to prevent.
LIVENESS_TIMEOUT_S = 5.0

# Total budget for the whole sweep. Never let the health check itself become the
# thing that makes a run slow -- if it cannot finish in time, give up and let
# Playwright try, which is exactly the old behaviour.
SWEEP_BUDGET_S = 30.0

# Above this many page targets, also reap known-leaked junk tabs. Chrome slows
# measurably past a few dozen targets and the 2026-08-22 wedge was found at 43.
PAGE_COUNT_REAP_THRESHOLD = 8

# Junk that accumulates in the keeper Chrome and is never load-bearing: the
# Perplexity news feed, blank tabs, and pages left behind by article-reading runs.
# A real query result lives under /search/ and is deliberately NOT in this list.
_JUNK_URL_MARKERS = ("/discover", "about:blank", "chrome://newtab")

# A page holding a live query result. Its renderer is the ONE most likely to be
# legitimately busy (deep-research streaming pins the JS main thread), so a single
# missed liveness probe is not evidence of a wedge. These get a second probe after
# a pause and are only closed if they miss both -- a renderer that is unreachable
# for ~20s straight will hang Playwright's attach anyway, so closing it is strictly
# better than the 180s stall it would otherwise cause.
_PROTECTED_URL_MARKERS = ("/search/",)

# Gap between the two probes given to a protected page before declaring it wedged.
PROTECTED_REPROBE_DELAY_S = 3.0


@dataclass
class CdpHealthReport:
    """Outcome of one pre-attach sweep."""

    reachable: bool = False
    page_targets: int = 0
    hung_closed: list[str] = field(default_factory=list)
    junk_closed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def closed_any(self) -> bool:
        return bool(self.hung_closed or self.junk_closed)

    def summary(self) -> str:
        """One-line, log-friendly summary. This is the diagnostic signal."""
        if not self.reachable:
            return f"cdp_health unreachable reason={self.skipped_reason or 'unknown'}"
        if self.skipped_reason:
            return (
                f"cdp_health skipped reason={self.skipped_reason} "
                f"pages={self.page_targets}"
            )
        return (
            f"cdp_health pages={self.page_targets} "
            f"hung_closed={len(self.hung_closed)} junk_closed={len(self.junk_closed)} "
            f"errors={len(self.errors)}"
        )


def _http_get_json(url: str, timeout: float = 5.0) -> object:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _http_get_text(url: str, timeout: float = 5.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _is_protected(url: str) -> bool:
    """True for pages that get a second chance before being declared wedged."""
    lowered = (url or "").lower()
    return any(marker in lowered for marker in _PROTECTED_URL_MARKERS)


def _is_junk(url: str) -> bool:
    lowered = (url or "").lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in _JUNK_URL_MARKERS)


async def _target_answers(ws_url: str, timeout: float = LIVENESS_TIMEOUT_S) -> bool:
    """Return True if this target's renderer answers a trivial Runtime.evaluate.

    Connects to the *target's own* websocket rather than the browser socket, so a
    hung renderer cannot stall the probe of any other target.
    """
    import websockets

    try:
        async with websockets.connect(
            ws_url, max_size=None, ping_interval=None, open_timeout=timeout
        ) as socket:
            await socket.send(
                json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": "1"}})
            )
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                message = json.loads(raw)
                if message.get("id") == 1:
                    return "result" in message
    except asyncio.TimeoutError:
        return False
    except Exception:
        # A target that refuses a websocket (already gone, or a type that does not
        # accept one) is not evidence of a wedge. Leave it alone.
        return True


def _close_target(http_endpoint: str, target_id: str) -> bool:
    """Close one target via the browser-process HTTP endpoint.

    Uses HTTP rather than CDP-over-websocket precisely because the browser process
    still answers when a renderer is wedged.
    """
    try:
        _http_get_text(f"{http_endpoint.rstrip('/')}/json/close/{target_id}", timeout=5.0)
        return True
    except Exception:
        return False


def _pick_keepers(alive: Iterable[dict]) -> set[str]:
    """Target ids that must never be reaped as junk.

    Keeps the first Perplexity page seen (the keeper's home tab) so the browser is
    never left with zero Perplexity pages, which is its own known failure mode.
    """
    keepers: set[str] = set()
    for target in alive:
        url = (target.get("url") or "").lower()
        if "perplexity.ai" in url and not _is_junk(url):
            keepers.add(target["id"])
            break
    return keepers


async def sweep_cdp_endpoint(
    http_endpoint: str,
    log: Callable[[str], None] | None = None,
    reap_junk: bool = True,
) -> CdpHealthReport:
    """Close wedged (and optionally leaked) page targets before Playwright attaches.

    Args:
        http_endpoint: CDP HTTP base, e.g. ``http://127.0.0.1:9223``.
        log: Optional single-argument logging callable.
        reap_junk: Also close known-leaked junk tabs once the page count exceeds
            ``PAGE_COUNT_REAP_THRESHOLD``. Never closes ``/search/`` result pages.

    Returns:
        A :class:`CdpHealthReport`. Never raises — a health check that fails must
        degrade to the previous behaviour, not break the run it is protecting.
    """
    emit = log or (lambda _message: None)
    report = CdpHealthReport()

    try:
        targets = _http_get_json(f"{http_endpoint.rstrip('/')}/json/list", timeout=5.0)
    except Exception as exc:
        report.skipped_reason = f"{type(exc).__name__}: {exc}"
        emit(report.summary())
        return report

    report.reachable = True
    if not isinstance(targets, list):
        report.skipped_reason = "malformed_json_list"
        emit(report.summary())
        return report

    pages = [
        t for t in targets
        if isinstance(t, dict)
        and t.get("type") == "page"
        and t.get("webSocketDebuggerUrl")
        and t.get("id")
    ]
    report.page_targets = len(pages)
    if not pages:
        emit(report.summary())
        return report

    async def classify(target: dict) -> tuple[dict, bool]:
        ws_url = target["webSocketDebuggerUrl"]
        if await _target_answers(ws_url):
            return target, True
        # Second chance for a page that may simply be busy streaming a result.
        if _is_protected(target.get("url", "")):
            await asyncio.sleep(PROTECTED_REPROBE_DELAY_S)
            return target, await _target_answers(ws_url)
        return target, False

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(classify(t) for t in pages), return_exceptions=True),
            timeout=SWEEP_BUDGET_S,
        )
    except asyncio.TimeoutError:
        report.skipped_reason = "sweep_budget_exceeded"
        emit(report.summary())
        return report
    except Exception as exc:
        report.skipped_reason = f"{type(exc).__name__}: {exc}"
        emit(report.summary())
        return report

    alive: list[dict] = []
    for outcome in results:
        if isinstance(outcome, BaseException):
            report.errors.append(f"{type(outcome).__name__}: {outcome}")
            continue
        target, responded = outcome
        if responded:
            alive.append(target)
            continue
        url = target.get("url", "")
        if _close_target(http_endpoint, target["id"]):
            report.hung_closed.append(url)
            emit(f"cdp_health CLOSED hung page url={url[:100]}")
        else:
            report.errors.append(f"close_failed hung {url[:100]}")

    # Leaked-tab reaping. Only above the threshold, only junk, and always keep at
    # least one Perplexity page so ensure_perplexity_tab's invariant still holds.
    if reap_junk and len(alive) > PAGE_COUNT_REAP_THRESHOLD:
        keepers = _pick_keepers(alive)
        for target in alive:
            if target["id"] in keepers:
                continue
            url = target.get("url", "")
            if not _is_junk(url):
                continue
            if _close_target(http_endpoint, target["id"]):
                report.junk_closed.append(url)
            else:
                report.errors.append(f"close_failed junk {url[:100]}")
        if report.junk_closed:
            emit(f"cdp_health CLOSED {len(report.junk_closed)} leaked junk tab(s)")

    emit(report.summary())
    return report


def sweep_cdp_endpoint_sync(
    http_endpoint: str,
    log: Callable[[str], None] | None = None,
    reap_junk: bool = True,
) -> CdpHealthReport:
    """Blocking wrapper for callers that are not already inside an event loop."""
    return asyncio.run(sweep_cdp_endpoint(http_endpoint, log=log, reap_junk=reap_junk))


if __name__ == "__main__":  # pragma: no cover - operational entry point
    import sys

    endpoint = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9223"
    result = sweep_cdp_endpoint_sync(endpoint, log=print)
    print(json.dumps({
        "reachable": result.reachable,
        "page_targets": result.page_targets,
        "hung_closed": result.hung_closed,
        "junk_closed": result.junk_closed,
        "errors": result.errors,
        "skipped_reason": result.skipped_reason,
    }, indent=2))
    sys.exit(0 if result.reachable else 1)

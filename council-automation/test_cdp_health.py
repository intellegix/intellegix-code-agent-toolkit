"""Tests for cdp_health — the pre-attach CDP wedge reaper (2026-08-22).

The property that matters most is NEGATIVE: this module runs on the hot path of
every research run, so it must never raise and must never close a page it has not
proved unresponsive. A bug here does not degrade research, it deletes work.
"""
from __future__ import annotations

import asyncio

import cdp_health


# --------------------------------------------------------------------------
# URL classification
# --------------------------------------------------------------------------
def test_junk_markers_match_the_tabs_that_actually_leaked():
    # These are the exact URLs reaped from the keeper Chrome on 2026-08-22.
    assert cdp_health._is_junk("https://www.perplexity.ai/discover")
    assert cdp_health._is_junk(
        "https://www.perplexity.ai/discover/you/un-security-council-holds-seco-SuSl"
    )
    assert cdp_health._is_junk("about:blank")
    assert cdp_health._is_junk("")


def test_query_results_and_the_home_tab_are_never_junk():
    # Closing either of these would destroy a user's actual work.
    assert not cdp_health._is_junk("https://www.perplexity.ai/search/abc-123")
    assert not cdp_health._is_junk("https://www.perplexity.ai/")


def test_search_pages_are_protected_from_a_single_missed_probe():
    """A streaming deep-research page pins its JS thread; one miss is not a wedge."""
    assert cdp_health._is_protected("https://www.perplexity.ai/search/abc-123")
    assert not cdp_health._is_protected("https://www.perplexity.ai/discover")


def test_pick_keepers_always_spares_one_perplexity_home_tab():
    alive = [
        {"id": "t1", "url": "https://www.perplexity.ai/discover"},
        {"id": "t2", "url": "https://www.perplexity.ai/"},
        {"id": "t3", "url": "https://www.perplexity.ai/"},
    ]
    keepers = cdp_health._pick_keepers(alive)
    assert keepers == {"t2"}, "must keep exactly the first non-junk Perplexity page"


def test_pick_keepers_with_no_perplexity_page_returns_empty():
    alive = [{"id": "t1", "url": "https://example.com/"}]
    assert cdp_health._pick_keepers(alive) == set()


# --------------------------------------------------------------------------
# Failure containment
# --------------------------------------------------------------------------
def test_unreachable_endpoint_degrades_instead_of_raising():
    """If the health check itself fails, the run must proceed as it always did."""
    report = cdp_health.sweep_cdp_endpoint_sync("http://127.0.0.1:1")
    assert report.reachable is False
    assert report.closed_any is False
    assert report.skipped_reason
    assert "unreachable" in report.summary()


def test_malformed_json_list_is_not_treated_as_targets(monkeypatch):
    monkeypatch.setattr(cdp_health, "_http_get_json", lambda *a, **k: {"not": "a list"})
    report = cdp_health.sweep_cdp_endpoint_sync("http://127.0.0.1:9223")
    assert report.reachable is True
    assert report.skipped_reason == "malformed_json_list"
    assert report.closed_any is False


def test_healthy_endpoint_closes_nothing(monkeypatch):
    targets = [
        {"type": "page", "id": "t1", "url": "https://www.perplexity.ai/",
         "webSocketDebuggerUrl": "ws://x/1"},
        {"type": "page", "id": "t2", "url": "https://www.perplexity.ai/search/a",
         "webSocketDebuggerUrl": "ws://x/2"},
    ]
    closed: list[str] = []
    monkeypatch.setattr(cdp_health, "_http_get_json", lambda *a, **k: targets)
    monkeypatch.setattr(cdp_health, "_close_target",
                        lambda ep, tid: closed.append(tid) or True)

    async def always_alive(ws_url, timeout=5.0):
        return True

    monkeypatch.setattr(cdp_health, "_target_answers", always_alive)
    report = cdp_health.sweep_cdp_endpoint_sync("http://127.0.0.1:9223")
    assert report.page_targets == 2
    assert closed == [], "a healthy endpoint must not lose a single tab"


def test_hung_page_is_closed_and_named(monkeypatch):
    targets = [
        {"type": "page", "id": "good", "url": "https://www.perplexity.ai/",
         "webSocketDebuggerUrl": "ws://x/good"},
        {"type": "page", "id": "wedged", "url": "https://www.perplexity.ai/",
         "webSocketDebuggerUrl": "ws://x/wedged"},
    ]
    closed: list[str] = []
    monkeypatch.setattr(cdp_health, "_http_get_json", lambda *a, **k: targets)
    monkeypatch.setattr(cdp_health, "_close_target",
                        lambda ep, tid: closed.append(tid) or True)

    async def one_wedge(ws_url, timeout=5.0):
        return "wedged" not in ws_url

    monkeypatch.setattr(cdp_health, "_target_answers", one_wedge)
    report = cdp_health.sweep_cdp_endpoint_sync("http://127.0.0.1:9223")
    assert closed == ["wedged"]
    assert report.hung_closed == ["https://www.perplexity.ai/"]
    assert report.closed_any is True


def test_a_busy_search_page_that_recovers_is_not_closed(monkeypatch):
    """The regression this guards: killing an in-flight deep-research query."""
    targets = [
        {"type": "page", "id": "busy", "url": "https://www.perplexity.ai/search/abc",
         "webSocketDebuggerUrl": "ws://x/busy"},
    ]
    closed: list[str] = []
    calls = {"n": 0}
    monkeypatch.setattr(cdp_health, "_http_get_json", lambda *a, **k: targets)
    monkeypatch.setattr(cdp_health, "_close_target",
                        lambda ep, tid: closed.append(tid) or True)
    monkeypatch.setattr(cdp_health, "PROTECTED_REPROBE_DELAY_S", 0.01)

    async def slow_then_alive(ws_url, timeout=5.0):
        calls["n"] += 1
        return calls["n"] > 1  # misses the first probe, answers the second

    monkeypatch.setattr(cdp_health, "_target_answers", slow_then_alive)
    report = cdp_health.sweep_cdp_endpoint_sync("http://127.0.0.1:9223")
    assert calls["n"] == 2, "a protected page must get a second probe"
    assert closed == [], "a busy-but-alive query page must survive"
    assert report.hung_closed == []


def test_junk_is_only_reaped_above_the_threshold(monkeypatch):
    """Below the threshold a few leaked tabs are harmless; do not touch them."""
    targets = [
        {"type": "page", "id": f"t{i}", "url": "https://www.perplexity.ai/discover",
         "webSocketDebuggerUrl": f"ws://x/{i}"}
        for i in range(3)
    ]
    closed: list[str] = []
    monkeypatch.setattr(cdp_health, "_http_get_json", lambda *a, **k: targets)
    monkeypatch.setattr(cdp_health, "_close_target",
                        lambda ep, tid: closed.append(tid) or True)

    async def always_alive(ws_url, timeout=5.0):
        return True

    monkeypatch.setattr(cdp_health, "_target_answers", always_alive)
    report = cdp_health.sweep_cdp_endpoint_sync("http://127.0.0.1:9223")
    assert closed == []
    assert report.junk_closed == []


def test_target_answers_returns_false_when_the_renderer_never_replies(monkeypatch):
    """The core discriminator: silence within the budget means wedged."""

    class NeverReplies:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, _payload):
            return None

        async def recv(self):
            await asyncio.sleep(10)  # outlives the timeout

    import websockets

    monkeypatch.setattr(websockets, "connect", lambda *a, **k: NeverReplies())
    result = asyncio.run(cdp_health._target_answers("ws://x/1", timeout=0.05))
    assert result is False


def test_target_answers_is_forgiving_when_the_socket_itself_refuses(monkeypatch):
    """A target that cannot take a websocket is not evidence of a wedge."""
    import websockets

    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(websockets, "connect", boom)
    assert asyncio.run(cdp_health._target_answers("ws://x/1", timeout=0.05)) is True

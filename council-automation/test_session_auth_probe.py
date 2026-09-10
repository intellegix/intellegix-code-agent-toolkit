"""Regression tests for the 2026-08-22 Perplexity session-keeper faults.

Two distinct bugs are pinned here.

1. The keeper injected the saved cookie jar into the live browser context BEFORE
   checking whether that context was signed in. With a dead jar on disk that
   overwrote a healthy session with expired cookies, observed the resulting
   signed-out state, and saved it -- a loop that destroyed the session while
   logging "Refresh complete" every cycle. Ordering is the whole fix, so the
   ordering is what these tests assert.

2. Auth was inferred from the presence of `#ask-input`, which Perplexity renders
   to signed-out visitors. The probe now asks next-auth's session endpoint, and
   must treat an empty body, a past `expires`, and an unreachable endpoint as
   three different answers.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import re
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import session_keeper  # noqa: E402


# --------------------------------------------------------------------------
# 1. Ordering: probe the live context before injecting anything into it.
# --------------------------------------------------------------------------

def _keeper_source() -> str:
    return (HERE / "session_keeper.py").read_text(encoding="utf-8")


def test_saved_cookies_are_not_injected_before_the_auth_probe():
    """add_cookies must never run before the first _navigate_and_warm call.

    This is the exact fault: injecting a stale jar into a live context is
    destructive, and it can only be judged safe after the context has been shown
    to be signed out.
    """
    source = _keeper_source()
    first_warm = source.index("await _navigate_and_warm(page)")
    injection = source.index("await context.add_cookies(old_cookies)")
    assert injection > first_warm, (
        "session_keeper injects the saved cookie jar before probing the live "
        "context. That overwrites a healthy session with whatever is on disk -- "
        "the 2026-08-22 outage. Probe first; inject only to repair a context "
        "already proven signed out."
    )


def test_injection_is_guarded_by_a_signed_out_branch():
    """The injection must sit on the failure branch, not run unconditionally."""
    source = _keeper_source()
    injection = source.index("await context.add_cookies(old_cookies)")
    window = source[max(0, injection - 600):injection]
    assert re.search(r"if logged_in:", window), (
        "The cookie injection is no longer guarded by a signed-in check."
    )


def test_cookies_are_only_persisted_when_logged_in():
    """A failed auth check must abort before _save_cookies_and_storage runs."""
    source = _keeper_source()
    abort = source.index("if not logged_in:")
    save = source.index("await _save_cookies_and_storage(context, page)")
    assert abort < save, (
        "The keeper can reach the cookie-save with logged_in False, which is how "
        "a dead jar overwrote a good one."
    )


# --------------------------------------------------------------------------
# 2. The auth probe itself.
# --------------------------------------------------------------------------

class FakePage:
    """Minimal stand-in for a Playwright page whose evaluate() is scripted."""

    def __init__(self, result=None, raises: Exception | None = None):
        self._result = result
        self._raises = raises
        self.urls: list[str] = []

    async def evaluate(self, _script, arg):
        self.urls.append(arg)
        if self._raises is not None:
            raise self._raises
        return self._result


def _probe(page):
    return asyncio.run(session_keeper._ask_perplexity_whether_signed_in(page))


def _iso(delta: dt.timedelta) -> str:
    return (dt.datetime.now(dt.timezone.utc) + delta).isoformat().replace("+00:00", "Z")


def test_empty_session_object_is_signed_out():
    """next-auth answers {} for an anonymous visitor. That is a hard False."""
    assert _probe(FakePage({})) is False


def test_populated_unexpired_session_is_signed_in():
    assert _probe(FakePage({"user": {"id": "x"}, "expires": _iso(dt.timedelta(days=7))})) is True


def test_expired_session_is_signed_out_even_though_the_body_is_populated():
    """A JWT session carries an `expires` computed at issuance and can be past.

    Testing truthiness alone would call this signed in.
    """
    assert _probe(FakePage({"user": {"id": "x"}, "expires": _iso(dt.timedelta(days=-1))})) is False


def test_unreachable_endpoint_is_unverified_not_signed_out():
    """None and False must stay distinct: 'I could not tell' is not 'signed out'.

    Conflating them would either discard a good cookie jar or certify a dead one.
    """
    assert _probe(FakePage(raises=RuntimeError("net::ERR_CONNECTION_RESET"))) is None


def test_non_ok_response_is_unverified():
    assert _probe(FakePage(None)) is None


def test_unparseable_expires_is_unverified_rather_than_assumed_good():
    assert _probe(FakePage({"user": {"id": "x"}, "expires": "not-a-date"})) is None


def test_probe_url_is_cache_busted():
    """A CDN or service worker keys on URL, so the URL itself must vary.

    `cache: 'no-store'` alone only governs the browser's own HTTP cache.
    """
    page = FakePage({})
    _probe(page)
    source = _keeper_source()
    assert "_cb=" in source and "Date.now()" in source, (
        "The auth probe no longer varies its URL, so a cached 200 could replay a "
        "session that has since been revoked."
    )
    assert "cache: 'no-store'" in source


def test_probe_targets_the_auth_endpoint_not_a_dom_selector():
    page = FakePage({})
    _probe(page)
    assert page.urls == [session_keeper.AUTH_SESSION_URL]


def test_selector_probe_is_only_a_fallback():
    """The selector loop may remain, but only below the endpoint check.

    Those selectors render for signed-out visitors, which is what made them
    useless as the primary signal. Anchor on the `for selector in (` loop rather
    than on a bare selector string -- the selector names also appear in comments
    explaining this very fault, and matching those would test nothing.
    """
    source = _keeper_source()
    endpoint_check = source.index("signed_in = await _ask_perplexity_whether_signed_in(page)")
    selector_loop = source.index("for selector in (")
    assert selector_loop > endpoint_check, (
        "The DOM selector loop is consulted before the authoritative endpoint "
        "check, so a signed-out session can be certified healthy again."
    )
    assert "#ask-input" in source[selector_loop:selector_loop + 200]

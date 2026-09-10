"""Non-interactive Perplexity session refresher.

Loads existing cookies from playwright-session.json, launches Chromium (headful
by default — Cloudflare blocks headless), navigates to Perplexity, waits for
auth hydration, then saves fresh cookies and localStorage back to disk.

Usage:
    python refresh_session.py              # headful refresh (~6s)
    python refresh_session.py --headless   # headless (may fail on Cloudflare)
    python refresh_session.py --validate   # refresh + run a test query
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Reuse paths from council_config to stay in sync
try:
    from council_config import (
        BROWSER_LOCALSTORAGE_PATH,
        BROWSER_SESSION_PATH,
        SEMAPHORE_WAIT_TIMEOUT,
    )
    from council_browser import PerplexityCouncil, SessionSemaphore, BrowserBusyError
    _HAS_COUNCIL = True
except ImportError:
    # Fallback if run outside council-automation directory
    BROWSER_SESSION_PATH = Path.home() / ".claude" / "config" / "playwright-session.json"
    BROWSER_LOCALSTORAGE_PATH = Path.home() / ".claude" / "config" / "playwright-localstorage.json"
    SEMAPHORE_WAIT_TIMEOUT = 60
    _HAS_COUNCIL = False


def _log(msg: str) -> None:
    print(f"[refresh_session] {msg}", flush=True)


import datetime as _dt

AUTH_SESSION_URL = "https://www.perplexity.ai/api/auth/session"


async def _ask_perplexity_whether_signed_in(page) -> bool | None:
    """Ask Perplexity's own auth endpoint whether this session is signed in.

    next-auth returns an empty object for an anonymous visitor and a populated
    user object for a real session, so this is authoritative and immune to UI
    drift -- unlike looking for a composer element, which is rendered to signed
    out visitors too.

    Returns:
        True or False when the endpoint answers, None when it cannot be reached
        or returns something unrecognised (caller decides what to do).
    """
    try:
        payload = await page.evaluate(
            """async (url) => {
                // Vary the URL, not just the cache mode: `cache: 'no-store'`
                // only governs the browser's own HTTP cache, while a CDN edge
                // cache or a service worker's Cache Storage keys on the URL and
                // would happily replay a stale 200 for either.
                const bust = url + (url.includes('?') ? '&' : '?') + '_cb=' + Date.now();
                const response = await fetch(bust, {
                    credentials: 'include',
                    cache: 'no-store',
                });
                if (!response.ok) return null;
                return await response.json();
            }""",
            AUTH_SESSION_URL,
        )
    except Exception as exc:  # network error, navigation mid-flight, CSP, ...
        _log(f"Auth endpoint unreachable ({type(exc).__name__}: {exc})")
        return None
    if not isinstance(payload, dict):
        return None
    if not payload:
        return False

    # A non-empty body is not proof of a live session. next-auth computes
    # `expires` from the JWT at issuance, so it can already be in the past.
    expires = payload.get("expires")
    if isinstance(expires, str):
        try:
            deadline = _dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            _log(f"Auth endpoint returned an unparseable expires ({expires!r}); "
                 f"treating the session as UNVERIFIED rather than guessing.")
            return None
        if deadline <= _dt.datetime.now(_dt.timezone.utc):
            _log(f"Auth endpoint returned a session that expired at {expires}.")
            return False
    return True


async def _navigate_and_check_auth(context) -> tuple:
    """Navigate to Perplexity and check for logged-in state.

    2026-08-22: this used to return True as soon as it found '#ask-input'. That
    element is present on the SIGNED OUT homepage too, so the check reported
    "Auth confirmed" on a completely dead session, saved the dead cookies, and
    every run afterwards failed with "session expired or not logged in". A
    refresh that reports success while nothing works is worse than one that
    fails, so the authoritative auth endpoint is asked first and the selector is
    only a fallback for when that endpoint cannot be reached.

    Returns (page, logged_in) tuple.
    """
    page = await context.new_page()
    _log("Navigating to perplexity.ai...")
    await page.goto("https://www.perplexity.ai/", wait_until="domcontentloaded", timeout=30000)

    _log("Waiting for auth hydration...")
    await page.wait_for_timeout(3000)

    signed_in = await _ask_perplexity_whether_signed_in(page)
    if signed_in is True:
        _log("Auth confirmed: Perplexity's auth endpoint reports an active session")
        return page, True
    if signed_in is False:
        _log(
            "Auth check FAILED: SIGNED OUT. Perplexity's auth endpoint reports no "
            "session, so the stored cookies are dead server-side. Saving them again "
            "cannot help -- a human must sign in interactively."
        )
        _log("MONITOR-SIGNAL perplexity_signed_out")
        return page, False

    # Endpoint indeterminate. Fall back to the old selector check, but say plainly
    # that it is weak evidence rather than logging "Auth confirmed".
    for selector in ["#ask-input", "textarea[placeholder]", "[data-testid='ask-input']"]:
        try:
            await page.wait_for_selector(selector, timeout=10000)
            _log(
                f"Auth UNVERIFIED: auth endpoint unreachable; found '{selector}', which "
                f"is also present when signed out. Proceeding, but do not treat this as "
                f"proof the session is alive."
            )
            return page, True
        except Exception:
            continue

    _log("Auth check failed: no input element found")
    return page, False


def _report_session_info(cookies: list[dict], localstorage: dict[str, str]) -> None:
    """Log key session details."""
    session_token = next(
        (c for c in cookies if c.get("name") == "__Secure-next-auth.session-token"),
        None,
    )
    if session_token:
        _log("Session token present (httpOnly, secure)")
    else:
        _log("WARNING: No __Secure-next-auth.session-token found in cookies")

    pplx_session = localstorage.get("pplx-next-auth-session")
    if pplx_session:
        try:
            sess_data = json.loads(pplx_session)
            expires = sess_data.get("expires", "unknown")
            user = sess_data.get("user", {}).get("name", "unknown")
            tier = sess_data.get("user", {}).get("subscription_tier", "unknown")
            _log(f"Logged in as: {user} ({tier}), expires: {expires}")
        except Exception:
            _log("localStorage session present but unparseable")


async def refresh_session(
    headless: bool = False,
    session_path: Path | None = None,
    localstorage_path: Path | None = None,
) -> bool:
    """Refresh Perplexity session cookies non-interactively.

    Returns True if session was refreshed and validated, False otherwise.
    """
    from playwright.async_api import async_playwright

    sess_path = session_path or BROWSER_SESSION_PATH
    ls_path = localstorage_path or BROWSER_LOCALSTORAGE_PATH

    if not sess_path.exists():
        _log(f"ERROR: No session file at {sess_path}")
        _log("Run `python council_browser.py --save-session` interactively first.")
        return False

    # Load existing cookies
    try:
        old_cookies = json.loads(sess_path.read_text(encoding="utf-8"))
        if not isinstance(old_cookies, list):
            _log("ERROR: Session file is not in Playwright-native format (expected JSON array)")
            return False
        _log(f"Loaded {len(old_cookies)} existing cookies from {sess_path.name}")
    except Exception as e:
        _log(f"ERROR: Failed to read session file: {e}")
        return False

    # Load existing localStorage
    old_localstorage: dict[str, str] = {}
    if ls_path.exists():
        try:
            old_localstorage = json.loads(ls_path.read_text(encoding="utf-8"))
            _log(f"Loaded {len(old_localstorage)} localStorage items from {ls_path.name}")
        except Exception:
            _log("WARNING: Failed to read localStorage file, continuing without it")

    stealth_js = """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        if (!window.chrome) window.chrome = { runtime: {} };
        delete window.__playwright;
        delete window.__pw_manual;
    """

    storage_state: dict = {"cookies": old_cookies, "origins": []}
    if old_localstorage:
        storage_state["origins"] = [{
            "origin": "https://www.perplexity.ai",
            "localStorage": [
                {"name": k, "value": v} for k, v in old_localstorage.items()
            ],
        }]

    # Acquire semaphore slot to prevent conflicts with concurrent sessions
    semaphore = None
    if _HAS_COUNCIL:
        semaphore = SessionSemaphore()
        try:
            semaphore.acquire(SEMAPHORE_WAIT_TIMEOUT)
        except BrowserBusyError as e:
            _log(f"ERROR: {e}")
            return False

    pw = await async_playwright().start()
    browser = None
    context = None
    temp_profile_dir: str | None = None

    try:
        # --- Attempt 1: non-persistent context (fast, supports concurrency) ---
        chrome_args = PerplexityCouncil._chrome_args(instance_id=0) if _HAS_COUNCIL else [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        # NOTE 2026-05-20: tried hiding the headful Chrome window via
        # `--window-position=-2000,-2000` and `--start-minimized` — both tripped
        # Cloudflare/Perplexity bot detection (cookies refreshed successfully, but
        # subsequent headless council_browser validation failed: "Session invalid:
        # input element not found"). Reverted to NO window-hiding args; accepting
        # the visible Chrome pop-up during refresh as the cost of reliable cookies.
        # The pop-up is brief (~6s, auto-closes) and only fires when critical
        # cookies are stale (once per ~30min during active use).
        browser = await pw.chromium.launch(
            channel="chrome",
            headless=headless,
            args=chrome_args,
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            storage_state=storage_state,
        )
        await context.add_init_script(stealth_js)

        page, logged_in = await _navigate_and_check_auth(context)

        # --- Attempt 2: Cloudflare fallback — persistent context with temp profile ---
        if not logged_in:
            _log("Non-persistent context blocked (Cloudflare), trying persistent fallback...")
            await page.close()
            await context.close()
            await browser.close()
            browser = None
            context = None

            temp_profile_dir = tempfile.mkdtemp(prefix="refresh_session_")
            _log(f"Using temp profile: {temp_profile_dir}")

            context = await pw.chromium.launch_persistent_context(
                user_data_dir=temp_profile_dir,
                channel="chrome",
                headless=headless,
                args=chrome_args,
                viewport={"width": 1280, "height": 900},
            )
            await context.add_init_script(stealth_js)

            # Inject cookies into persistent context
            await context.add_cookies(old_cookies)

            page, logged_in = await _navigate_and_check_auth(context)

        if not logged_in:
            _log("ERROR: Not logged in after both attempts")
            _log("Run `python council_browser.py --save-session` interactively to re-authenticate.")
            return False

        # Give the page a moment to settle (background API calls, token refresh)
        await page.wait_for_timeout(2000)

        # Extract fresh cookies
        fresh_cookies = await context.cookies()
        _log(f"Extracted {len(fresh_cookies)} fresh cookies")

        # Extract fresh localStorage
        fresh_localstorage = await page.evaluate("""() => {
            const items = {};
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                items[key] = localStorage.getItem(key);
            }
            return items;
        }""")
        _log(f"Extracted {len(fresh_localstorage)} localStorage items")

        # Filter out cookies that are already expired or expiring within 60 seconds
        # (per 2026-05-20 production audit + smoke-test correction). Originally tried
        # a 1-hour threshold but that dropped freshly-issued __cf_bm / pplx.edge-sid
        # cookies (their natural TTL is ~30min, so 29min remaining is NORMAL, not
        # near-death). The auto-refresh-on-stale-detect hook in council_browser.py
        # handles runtime freshness; this filter only catches imminently-expiring or
        # already-dead entries that survived context.cookies() somehow. Session
        # cookies (expires == -1 or 0) are always kept.
        now = time.time()
        SHORT_TTL_DROP_THRESHOLD_S = 60  # drop only cookies < 60s from expiry
        before = len(fresh_cookies)
        filtered_cookies = []
        dropped: list[str] = []
        for c in fresh_cookies:
            exp = c.get("expires", -1)
            if isinstance(exp, (int, float)) and exp > 0 and (exp - now) < SHORT_TTL_DROP_THRESHOLD_S:
                ttl_min = (exp - now) / 60
                ttl_label = f"{ttl_min:.1f}m" if ttl_min >= 0 else "EXPIRED"
                dropped.append(f"{c.get('name')}({ttl_label})")
                continue
            filtered_cookies.append(c)
        if dropped:
            _log(f"Dropped {len(dropped)} imminently-expiring cookies: {', '.join(dropped[:8])}{'...' if len(dropped) > 8 else ''}")
        else:
            _log("No cookies dropped (all fresh or session-scoped)")
        _log(f"Saving {len(filtered_cookies)}/{before} cookies after short-TTL filter")

        # Save cookies in Playwright-native format
        sess_path.parent.mkdir(parents=True, exist_ok=True)
        sess_path.write_text(
            json.dumps(filtered_cookies, indent=2, default=str),
            encoding="utf-8",
        )
        _log(f"Saved {len(filtered_cookies)} cookies to {sess_path}")

        # Save localStorage
        if fresh_localstorage:
            ls_path.write_text(
                json.dumps(fresh_localstorage, indent=2, default=str),
                encoding="utf-8",
            )
            _log(f"Saved {len(fresh_localstorage)} localStorage items to {ls_path}")

        # Report key session info
        _report_session_info(fresh_cookies, fresh_localstorage)

        _log("Session refresh complete.")
        return True

    except Exception as e:
        _log(f"ERROR: {e}")
        return False

    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        await pw.stop()
        if semaphore:
            semaphore.release()
        if temp_profile_dir and Path(temp_profile_dir).exists():
            shutil.rmtree(temp_profile_dir, ignore_errors=True)
            _log(f"Cleaned up temp profile: {temp_profile_dir}")


async def validate_with_query() -> bool:
    """Run a simple test query via council_browser to confirm session works."""
    import subprocess

    _log("Running validation query: 'What is 2+2?'")
    script_dir = Path(__file__).parent
    council_script = script_dir / "council_browser.py"

    if not council_script.exists():
        _log(f"ERROR: council_browser.py not found at {council_script}")
        return False

    try:
        result = subprocess.run(
            [sys.executable, str(council_script), "--headful", "--perplexity-mode", "research", "What is 2+2?"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(script_dir),
        )

        if result.returncode == 0:
            try:
                output = json.loads(result.stdout)
                if output.get("error"):
                    _log(f"Validation FAILED: {output['error']}")
                    return False
                synthesis = output.get("synthesis", "")
                if synthesis and len(synthesis) > 10:
                    _log(f"Validation PASSED: got {len(synthesis)} char response")
                    return True
                _log("Validation UNCLEAR: response was too short")
                return False
            except json.JSONDecodeError:
                _log(f"Validation FAILED: non-JSON output: {result.stdout[:200]}")
                return False
        else:
            _log(f"Validation FAILED (exit {result.returncode}): {result.stderr[:200]}")
            return False

    except subprocess.TimeoutExpired:
        _log("Validation FAILED: timed out after 120s")
        return False
    except Exception as e:
        _log(f"Validation FAILED: {e}")
        return False


async def main() -> None:
    parser = argparse.ArgumentParser(description="Non-interactive Perplexity session refresh")
    parser.add_argument("--headless", action="store_true", help="Run headless (may fail on Cloudflare)")
    parser.add_argument("--validate", action="store_true", help="Run a test query after refresh")
    parser.add_argument("--session-path", type=str, help="Override session file path")
    args = parser.parse_args()

    session_path = Path(args.session_path) if args.session_path else None

    start = time.monotonic()
    success = await refresh_session(
        headless=args.headless,
        session_path=session_path,
    )
    elapsed = time.monotonic() - start
    _log(f"Refresh {'succeeded' if success else 'FAILED'} in {elapsed:.1f}s")

    if not success:
        sys.exit(1)

    if args.validate:
        _log("--- Validation ---")
        valid = await validate_with_query()
        if not valid:
            sys.exit(2)
        _log("Full pipeline validated.")


if __name__ == "__main__":
    asyncio.run(main())

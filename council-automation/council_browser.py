"""Standalone Playwright script for Perplexity council mode automation.

Runs the full council workflow autonomously:
  navigate -> activate council -> submit query -> wait -> extract -> return JSON

Usage:
    python council_browser.py "What architecture for X?"
    python council_browser.py --headful "Debug query"
    python council_browser.py --save-session   # headful login, save state
"""

import argparse
import asyncio
import base64
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import shutil
import subprocess
import tempfile
import urllib.parse

from council_config import (
    BROWSER_HEADLESS,
    BROWSER_HEADLESS_FALLBACK,
    BROWSER_LABS_TIMEOUT,
    BROWSER_LOCALSTORAGE_PATH,
    BROWSER_POLL_INTERVAL,
    BROWSER_POLL_INTERVAL_RESEARCH,
    BROWSER_SESSION_PATH,
    BROWSER_SESSIONS_DIR,
    BROWSER_STABLE_MS,
    BROWSER_STABLE_MS_LABS,
    BROWSER_STABLE_MS_RESEARCH,
    BROWSER_RESEARCH_TIMEOUT,
    BROWSER_TIMEOUT,
    BROWSER_DOM_MIN_ELAPSED_RESEARCH,
    BROWSER_DOM_MIN_ELAPSED_LABS,
    BROWSER_DOM_MIN_TEXT_LENGTH,
    BROWSER_DOM_CONFIRM_WAIT,
    BROWSER_TYPE_DELAY,
    BROWSER_USER_DATA_DIR,
    BROWSER_STOP_BUTTON_POLL_MS,
    BROWSER_STOP_BUTTON_DEBOUNCE_MS,
    BROWSER_MIN_GENERATION_TIME_MS,
    BROWSER_CONFIRMATION_WINDOW_MS,
    BROWSER_MUTATION_STABILITY_MS,
    INSTANCE_GPU_CUTOFF,
    INSTANCE_LANGUAGES,
    INSTANCE_VIEWPORTS,
    MAX_CONCURRENT_SESSIONS,  # noqa: F401 — used transitively via submission_lock
    SELECTORS_PATH,
    SEMAPHORE_TTL,
    SEMAPHORE_WAIT_TIMEOUT,
    VISION_ENABLED,
    VISION_JPEG_QUALITY,
    VISION_MAX_TOKENS,
    VISION_MODEL,
    VISION_POLL_INTERVAL_MODELS,
    VISION_POLL_INTERVAL_SYNTHESIS,
)

import research_queue
from submission_lock import get_submit_lock, start_lock_heartbeat

# Env flag values (case-insensitive) treated as "enabled". A bare
# `os.environ.get(...)` truthiness check would treat FLAG=0/FLAG=false
# as enabled too (any non-empty string is truthy) -- _flag_enabled()
# normalizes that footgun for every RESEARCH_QUEUE_* flag read below.
_TRUTHY_FLAG_VALUES = {"1", "true", "yes", "on"}


def _flag_enabled(env_var: str) -> bool:
    """Return True only if `env_var` is set to a recognized truthy value.

    Recognized truthy values (case-insensitive): "1", "true", "yes",
    "on". Unset, empty, or any other value (including "0"/"false") is
    treated as disabled.
    """
    value = os.environ.get(env_var)
    if value is None:
        return False
    return value.strip().lower() in _TRUTHY_FLAG_VALUES


class BrowserBusyError(Exception):
    """Raised when another browser automation session holds the profile lock."""
    pass


class SessionStaleError(Exception):
    """Raised when critical Perplexity cookies are EXPIRED and no refresh path
    could renew them, so submitting the query would be knowingly doomed.

    Replaces the old `keeper-timeout-stale-proceed` behaviour. Proceeding on
    expired cookies converted a clean, diagnosable "the session is dead" into a
    5-8 minute mid-run death that also consumed a slot on a serialized queue
    (2026-08-08 outage: two consecutive runs burned ~9 minutes each this way).
    Failing here costs seconds and names the fix. Set COUNCIL_ALLOW_STALE=1 to
    restore the old proceed-anyway behaviour.
    """
    pass


class SessionSemaphore:
    """File-based named-slot semaphore for concurrent browser sessions.

    Uses named slots (slot-0.lock through slot-N.lock) instead of PID-named files
    for deterministic instance_id assignment. Each slot file contains "PID TIMESTAMP".
    acquire() returns the slot number as instance_id for fingerprint diversification.
    Stale sessions are cleaned via PID liveness check + TTL expiry.
    """

    def __init__(
        self,
        max_sessions: int = MAX_CONCURRENT_SESSIONS,
        ttl: int = SEMAPHORE_TTL,
        sessions_dir: Path | None = None,
    ):
        self.max_sessions = max_sessions
        self.ttl = ttl
        self.sessions_dir = sessions_dir or BROWSER_SESSIONS_DIR
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._session_file: Path | None = None
        self.instance_id: int = 0

    def _cleanup_stale(self) -> int:
        """Remove slot files for dead PIDs or expired TTL. Returns count removed."""
        removed = 0
        now = time.time()
        for f in self.sessions_dir.glob("slot-*.lock"):
            try:
                content = f.read_text(encoding="utf-8").strip()
                parts = content.split()
                pid = int(parts[0])
                ts = float(parts[1]) if len(parts) > 1 else 0
            except (ValueError, IndexError, OSError):
                # Corrupt file — remove it
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
                continue

            # Check PID liveness (Windows os.kill can raise SystemError)
            pid_alive = True
            try:
                os.kill(pid, 0)
            except (OSError, SystemError):
                pid_alive = False

            # Remove if PID is dead or TTL expired
            if not pid_alive or (self.ttl and now - ts > self.ttl):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass

        return removed

    def _cleanup_orphaned_temp_dirs(self) -> None:
        """Remove orphaned council temp profile dirs older than 10 minutes."""
        import glob as glob_mod
        tmp = tempfile.gettempdir()
        for pattern in ["council_np_*", "council_cf_*"]:
            for d in glob_mod.glob(os.path.join(tmp, pattern)):
                try:
                    if not os.path.isdir(d):
                        continue
                    age = time.time() - os.path.getmtime(d)
                    if age > 600:  # 10 minutes
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass

    def _count_active(self) -> int:
        """Count active slot files (after cleanup)."""
        return len(list(self.sessions_dir.glob("slot-*.lock")))

    def acquire(self, wait_timeout: float = SEMAPHORE_WAIT_TIMEOUT) -> int:
        """Acquire a named session slot. Waits up to wait_timeout seconds.

        Returns the slot number (0..max_sessions-1) as instance_id.
        Raises BrowserBusyError if no slot becomes available.
        """
        self._cleanup_orphaned_temp_dirs()
        start = time.time()
        pid = os.getpid()

        while True:
            self._cleanup_stale()
            for slot in range(self.max_sessions):
                slot_file = self.sessions_dir / f"slot-{slot}.lock"
                try:
                    # Atomic create — O_CREAT|O_EXCL fails if file already exists
                    fd = os.open(
                        str(slot_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                    )
                    os.write(fd, f"{pid} {time.time():.0f}\n".encode("utf-8"))
                    os.close(fd)
                    self._session_file = slot_file
                    self.instance_id = slot
                    return slot
                except OSError:
                    continue  # Slot already claimed by another process

            elapsed = time.time() - start
            if elapsed >= wait_timeout:
                raise BrowserBusyError(
                    f"All {self.max_sessions} browser session slots are in use. "
                    f"Waited {wait_timeout}s. Wait for a session to finish or use --mode api."
                )

            time.sleep(1)

    def release(self) -> None:
        """Release the session slot by deleting the slot file."""
        if self._session_file and self._session_file.exists():
            try:
                self._session_file.unlink()
            except OSError:
                pass
            self._session_file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


# DEPRECATED: BrowserLock is replaced by SessionSemaphore (counting semaphore, max 3).
# Kept for one release cycle for backward compatibility.
class BrowserLock:
    """DEPRECATED — Use SessionSemaphore instead.

    Cross-platform file lock for Playwright browser profile serialization.
    On Windows uses msvcrt.locking(), on Unix uses fcntl.flock().
    Non-blocking: raises BrowserBusyError immediately if lock is held.
    """
    LOCK_PATH = Path.home() / ".claude" / "config" / "council_browser.lock"

    def __init__(self):
        self.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._fd = None

    def acquire(self):
        try:
            self._fd = open(self.LOCK_PATH, 'w')
            self._fd.write(f"{os.getpid()} {time.time():.0f}\n")
            self._fd.flush()
            self._fd.seek(0)
        except PermissionError:
            self._fd = None
            raise BrowserBusyError(
                "Another council/research browser session is already running. "
                "Wait for it to finish (~1-3 min) or use --mode api."
            )
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            self._fd.close()
            self._fd = None
            raise BrowserBusyError(
                "Another council/research browser session is already running. "
                "Wait for it to finish (~1-3 min) or use --mode api."
            )

    def release(self):
        if self._fd:
            try:
                self._fd.seek(0)
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self._fd.close()
            self._fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


# Perplexity slash-command UI commit key. As of 2026-05-05 Perplexity's
# command palette commits on Space (not Enter — Enter submits the literal
# "/research" string as a regular search query). If Perplexity changes
# this again, edit ONLY this constant + the matching one in
# ~/.claude/mcp-servers/browser-bridge/server.js. Don't grep-and-hunt.
PERPLEXITY_COMMIT_KEY = "Space"

# Basename of the profile dir session_keeper.py launches Chrome with (see
# session_keeper.py: keeper_profile_dir). Used to prove a CDP endpoint really
# belongs to the keeper and not to another app squatting the same port.
KEEPER_PROFILE_DIRNAME = "session_keeper_profile"
KEEPER_PROFILE_DIR = Path.home() / ".claude" / "config" / KEEPER_PROFILE_DIRNAME

# CDP port the keeper owns. Historically 9222 -- which is ALSO the port
# ~/.claude/browser-relay/relay.mjs (the /takeover phone relay) binds, and
# whichever process boots first wins. That collision produced the 2026-08-07
# outage (runner attached to the relay's cookie-less Chrome) and again the
# 2026-08-08 outage (relay held 9222, so the freshness guard below believed
# "the keeper is alive" and fired a refresh that could never land). The keeper
# now gets a DEDICATED port so the two can never contend. Override with
# COUNCIL_KEEPER_CDP_PORT if 9223 ever conflicts. See memory port-registry.
KEEPER_CDP_PORT = int(os.environ.get("COUNCIL_KEEPER_CDP_PORT", "9223"))

# Cookies that only exist in a browser profile actually logged in to Perplexity.
# Their absence proves the attached CDP context is not a usable keeper browser.
PERPLEXITY_AUTH_COOKIES = {
    "__Secure-next-auth.session-token",
    "pplx.session-id",
}


def _log(msg: str) -> None:
    """Log to stderr (stdout reserved for JSON result).

    flush=True ensures real-time observability when stderr is redirected
    to a file (Python's text-mode default block-buffers redirected stderr
    even with python -u, which only unbuffers the binary layer below).
    Real-time logs are essential for diagnosing the submit_lock + activate_mode
    hang pattern under concurrent /research-perplexity load.
    """
    print(f"  [browser] {msg}", file=sys.stderr, flush=True)


def _load_selectors() -> dict:
    """Load CSS selectors from perplexity-selectors.json."""
    if SELECTORS_PATH.exists():
        return json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))
    _log(f"WARNING: selectors file not found at {SELECTORS_PATH}, using defaults")
    return {
        "textarea": "#ask-input",
        "responseContainer": ".prose",
        "councilSynthesis": ".prose:first-of-type",
        "councilModelRow": "[class*='interactable'][class*='appearance-none']",
        "councilCompletedIndicator": "[class*='Completed'], svg[class*='check']",
        "councilPanelClose": "button[aria-label='Close']",
    }



# Phase 3 (2026-05-29 follow-ups) per-query instrumentation. Mirrors the
# extended_research_runner._emit_pass_instrumentation pattern: write to
# ~/.claude/council-cache/instrumentation-query.jsonl with 10 MB tail-truncate
# at write time. Fail-open — never raises.
_QUERY_INST_LOG = Path.home() / ".claude" / "council-cache" / "instrumentation-query.jsonl"
_QUERY_INST_CAP_BYTES = 10 * 1024 * 1024

# 2026-06-15 session-freshness guard. A controlled A/B smoke proved both the
# empty-synthesis (~7%) and the mid-run 360s-timeout-abort failures correlate
# with a degraded Perplexity session: the identical query returned empty on a
# 2.6-min-TTL session and correct synthesis on a 9.8-min one. Perplexity issues
# pplx.session-id TTLs of 3-10 min; the keeper only refreshes every ~20 min, so
# queries land on a dying session mid-cycle. Refresh proactively when a critical
# cookie's remaining TTL drops below the floor. Floor = 360s worst-case query +
# ~90s keeper/reload buffer; revisit if Perplexity shortens TTLs further.
SESSION_FRESHNESS_THRESHOLD_S = 480
SESSION_KEEPER_WAIT_S = 120  # max wait for the keeper to bump the cookies-file mtime


def _emit_query_instrumentation(record: dict) -> None:
    """Append one per-query JSONL record with 10 MB tail-truncate at write time."""
    try:
        path = _QUERY_INST_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _QUERY_INST_CAP_BYTES:
            data = path.read_bytes()[-(8 * 1024 * 1024):]
            first_nl = data.find(b"\n")
            if first_nl > 0:
                data = data[first_nl + 1:]
            path.write_bytes(data)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def _cf_hostname_match(frame_url: str) -> bool:
    """Return True if a frame URL's host is the Cloudflare challenge host.

    Matches ``challenges.cloudflare.com`` or any ``*.cloudflare.com`` subdomain
    via a structured hostname check (``urlparse().hostname``) rather than
    substring-matching the domain inside raw HTML. The substring form is both
    unreliable (an arbitrary page can embed the literal string) and flagged by
    CodeQL ``py/incomplete-url-substring-sanitization``.
    """
    from urllib.parse import urlparse
    try:
        host = (urlparse(frame_url or "").hostname or "").lower()
    except ValueError:
        return False
    return host == "challenges.cloudflare.com" or host.endswith(".cloudflare.com")


def _is_reasoning_trail_only(text: str) -> bool:
    """Heuristic: response contains only Perplexity's pre-synthesis thought bubbles.

    On large-artifact research-mode queries (>20 KB input), Perplexity's browser
    UI often renders only the reasoning trail ("Looking up X", "Checking Y") and
    never mounts the synthesis body. The extractor reads the .prose container,
    which contains this trail. Detecting that case lets callers trigger
    PARSE-FAILED early instead of feeding garbage to the JSON parser.
    """
    if not text:
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return True
    reasoning_starts = (
        "Looking up", "Looking into", "Checking ", "Preparing ",
        "Searching for", "Analyzing", "Reviewing", "Validating ",
    )
    reasoning_count = sum(1 for ln in lines if ln.startswith(reasoning_starts))
    # 2026-05-29 INTERIM PENDING PHASE 4 CALIBRATION.
    # Raised from 1500 -> 2500 per /extended-research audit verdict — partial-mount
    # stubs and legitimately concise research outputs can land in the 1500-2500
    # band, and the old 1500 threshold likely caused false-positive blanking on
    # valid responses.
    #
    # COUPLED ROLLBACK NOTE: this threshold widens the zone where the parser
    # provisions in extended_research_runner.py:extract_json are responsible for
    # catching wrapper-extraction issues. If those provisions are reverted, this
    # threshold MUST also revert to <=1500 in the same change — banned rollback
    # combo is "parser provisions out, threshold raised".
    return (
        reasoning_count >= 3
        and reasoning_count / max(len(lines), 1) > 0.5
        and len(text) < 2500
    )


# A synthesis at/above this length is treated as a complete answer, never a
# failure worth flagging: the truncation heuristic (rule (d)) is unreliable at
# this size (reasoning-trail-inflated .prose peak). Chosen from the empirical
# 197-vs-6379-char gap (Jul 12-14): 8x above the failure ceiling, ~2200-char
# buffer below the smallest known-good output. Perplexity-verified 2026-07-14.
SUBSTANTIAL_SYNTHESIS_CHARS = 2500


def _validate_result(text: str, peak_len: int | None = None) -> str:
    """Post-extraction semantic-completeness check (Perplexity #4, 2026-06-15).

    NON-BLOCKING diagnostic — returns "ok" | "empty" | "suspect_truncated"; the
    caller instruments + alerts but still returns the synthesis. High-precision by
    design: a false "suspect" on a valid answer is worse than missing a rare
    truncation, so we only fire on signals that virtually never end a complete
    answer.

    Signals (verified via Perplexity review):
      (d) LENGTH REGRESSION — strongest, causal: final extraction is >15% shorter
          than the peak .prose length seen while streaming (content that existed is
          now gone = truncation). Independent of length, catches the cases the
          shape rules miss (e.g. a cut mid-sentence on a proper noun).
      (a) unclosed code fence (odd ``` count).
      (b) substantial text (>200 chars) ending on a dangling token (— – : , ;).
    Rule (c) from the review (dangling function-word after citation-strip) was
    DROPPED in implementation: the only viable citation-strip regex is greedy and
    eats real prose words, which would make (c) MISS the truncations it targets —
    net negative for a precision-first flag. (d) covers that gap causally.
    """
    s = (text or "").strip()
    if not s:
        return "empty"
    # (d) length regression vs streaming peak — GATED to non-substantial outputs.
    # On large structured answers the streaming .prose peak is inflated by
    # transient reasoning-trail text that collapses on final extraction, so a
    # COMPLETE 6-13K-char answer trips the >15% regression check. Empirically
    # (Jul 12-14 instrumentation) this was the SOLE false-positive source: every
    # false "truncated" flag was on an output >=6000 chars, while every genuine
    # near-empty failure was <300 chars. Gate (d) below SUBSTANTIAL_SYNTHESIS_CHARS
    # so it still catches truncation of short/medium answers (where the .prose
    # peak is a reliable baseline). Rules (a)/(b) stay UNCONDITIONAL so a
    # structural cut mid-fence / mid-token is still caught at any size
    # (per Perplexity plan-verification 2026-07-14).
    if len(s) < SUBSTANTIAL_SYNTHESIS_CHARS:
        if isinstance(peak_len, (int, float)) and peak_len >= 100:
            if (peak_len - len(s)) / peak_len > 0.15:
                return "suspect_truncated"
    # (a) unclosed code fence.
    if s.count("```") % 2 == 1:
        return "suspect_truncated"
    # (b) dangling terminal token on substantial text.
    if len(s) > 200:
        tail = s.rstrip().rstrip('"”’\'')
        if tail and tail[-1] in ("—", "–", ":", ",", ";"):
            return "suspect_truncated"
    return "ok"


class PerplexityCouncil:
    """Autonomous Playwright-based Perplexity automation.

    Supports three Perplexity modes:
      - "council": /council slash command (multi-model, 3 AI responses + synthesis)
      - "research": /research slash command (deep research, single synthesized response)
      - "labs": /labs slash command (experimental labs mode, longer timeout)
    """

    def __init__(
        self,
        headless: bool = BROWSER_HEADLESS,
        session_path: Path | None = None,
        timeout: int = BROWSER_TIMEOUT,
        save_artifacts: bool = False,
        perplexity_mode: str = "council",
        use_persistent: bool = False,
        headless_fallback: bool = BROWSER_HEADLESS_FALLBACK,
        instance_id: int = 0,
    ):
        self.headless = headless
        self.headless_fallback = headless_fallback
        self.session_path = session_path or BROWSER_SESSION_PATH
        # Research/labs modes get longer timeouts
        if timeout == BROWSER_TIMEOUT and perplexity_mode == "research":
            self.timeout = BROWSER_RESEARCH_TIMEOUT
        elif timeout == BROWSER_TIMEOUT and perplexity_mode == "labs":
            self.timeout = BROWSER_LABS_TIMEOUT
        else:
            self.timeout = timeout
        self.save_artifacts = save_artifacts
        self.perplexity_mode = perplexity_mode
        self.use_persistent = use_persistent
        self.instance_id = instance_id
        self.selectors = _load_selectors()
        self.playwright = None
        self._browser = None  # Separate browser object (non-persistent mode)
        self.context = None
        # CDP attach state (2026-05-21): set True when we've attached to a running
        # session_keeper.py's headful Chrome via connect_over_cdp() instead of
        # launching our own browser. In that mode, stop() must NOT close the
        # context or kill the remote browser — we only own the pages we open.
        self._cdp_attached = False
        self._cdp_owned_pages: list = []
        self.page = None
        self._artifact_count = 0
        self._artifact_dir: Path | None = None
        self._temp_profile_dir: str | None = None  # Cloudflare fallback temp dir

    def _init_artifact_dir(self, query: str) -> None:
        """Create run artifact directory based on timestamp + query slug."""
        slug = re.sub(r"[^a-z0-9]+", "-", query[:40].lower()).strip("-") or "query"
        run_id = f"{time.strftime('%Y%m%d_%H%M')}_{slug[:30]}"
        self._artifact_dir = Path("~/.claude/council-logs/runs").expanduser() / run_id
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._artifact_count = 0

    async def _save_artifact(self, page, label: str) -> None:
        """Capture screenshot + HTML as forensic artifacts. Non-fatal, capped at 10."""
        if not self.save_artifacts or not self._artifact_dir:
            return
        if self._artifact_count >= 10:
            return
        try:
            self._artifact_count += 1
            # Screenshot
            jpg_path = self._artifact_dir / f"{label}.jpg"
            screenshot = await page.screenshot(type="jpeg", quality=80)
            jpg_path.write_bytes(screenshot)
            # Page HTML
            html_path = self._artifact_dir / f"{label}.html"
            html = await page.content()
            html_path.write_text(html, encoding="utf-8")
            _log(f"Artifact saved: {self._artifact_dir.name}/{label} (screenshot + html)")
        except Exception as e:
            _log(f"WARNING: Failed to save artifact '{label}': {e}")

    @staticmethod
    def _build_storage_state(
        session_path: Path, localstorage_path: Path | None = None
    ) -> dict | None:
        """Build a Playwright storage_state dict from session + localStorage files.

        Returns None if no session file exists.
        """
        if not session_path.exists():
            return None

        try:
            data = json.loads(session_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        cookies = []
        if isinstance(data, list):
            # Playwright-native format: list of cookie dicts
            cookies = data
        elif isinstance(data, dict):
            # Legacy format: {cookies: "name=val; ...", localStorage: {...}}
            cookies = PerplexityCouncil._parse_cookie_string(data.get("cookies", ""))

        if not cookies:
            return None

        storage_state: dict = {"cookies": cookies, "origins": []}

        # Merge localStorage if available
        ls_path = localstorage_path or BROWSER_LOCALSTORAGE_PATH
        if ls_path.exists():
            try:
                ls_data = json.loads(ls_path.read_text(encoding="utf-8"))
                if isinstance(ls_data, dict) and ls_data:
                    storage_state["origins"] = [{
                        "origin": "https://www.perplexity.ai",
                        "localStorage": [
                            {"name": k, "value": v} for k, v in ls_data.items()
                        ],
                    }]
            except Exception:
                pass

        return storage_state

    @staticmethod
    def _get_instance_fingerprint(instance_id: int = 0) -> dict:
        """Deterministic per-instance fingerprint for Cloudflare evasion."""
        import hashlib
        seed = hashlib.md5(f"council_{instance_id}".encode()).hexdigest()
        vp = INSTANCE_VIEWPORTS[instance_id % len(INSTANCE_VIEWPORTS)]
        lang = INSTANCE_LANGUAGES[instance_id % len(INSTANCE_LANGUAGES)]
        return {"viewport": vp, "language": lang, "seed": seed}

    @staticmethod
    def _chrome_args(instance_id: int = 0) -> list[str]:
        """Shared Chrome launch arguments for all launch methods.

        Per-instance: window offset, resource limits, GPU control.
        """
        fp = PerplexityCouncil._get_instance_fingerprint(instance_id)
        vp_w, vp_h = fp["viewport"]
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            # Extended --disable-features set: original isolation flags + four
            # features that suppress UI overlays / profile-teardown races which
            # can hold the temp user_data_dir locked across concurrent runs and
            # trigger ProcessSingleton false-positive matches.
            "--disable-features=IsolateOrigins,site-per-process,DestroyProfileOnBrowserClose,ChromeWhatsNewUI,DownloadBubble,DownloadBubbleV2",
            f"--window-size={vp_w},{vp_h}",
            # Resource-saving (all instances)
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-sync",
            "--disable-translate",
            "--metrics-recording-only",
            "--no-report-upload",
            "--disk-cache-size=10485760",  # 10MB disk cache per instance
            # Isolation hardening (2026-05-13 — see ~/.claude/plans/lexical-toasting-babbage.md).
            # Suppress crash-recovery dialog, prevent crash-reporter file locks,
            # disable background service registration, and keep credentials out
            # of the OS keychain so concurrent instances don't contend on it.
            "--disable-session-crashed-bubble",
            "--disable-breakpad",
            "--no-service-autorun",
            "--password-store=basic",
            # Per-instance window offset
            f"--window-position={100 + (instance_id * 60)},{100 + (instance_id * 40)}",
        ]
        if instance_id >= INSTANCE_GPU_CUTOFF:
            args.extend(["--disable-gpu", "--use-angle=swiftshader"])
        return args

    @staticmethod
    def _stealth_scripts(fingerprint: dict | None = None) -> str:
        """Return JavaScript to reduce automation detection.

        Masks: webdriver flag, chrome.runtime/csi/loadTimes, Playwright globals,
        navigator.plugins, navigator.languages, WebGL vendor/renderer.
        When fingerprint is provided, appends per-instance canvas noise and language override.
        """
        base_scripts = """
            // Hide webdriver flag
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // Chrome object stubs
            if (!window.chrome) window.chrome = {};
            if (!window.chrome.runtime) window.chrome.runtime = {};
            if (!window.chrome.csi) window.chrome.csi = function() {
                return { startE: Date.now(), onloadT: Date.now() + 100, pageT: 300, tran: 15 };
            };
            if (!window.chrome.loadTimes) window.chrome.loadTimes = function() {
                return {
                    commitLoadTime: Date.now() / 1000,
                    connectionInfo: 'h2',
                    finishDocumentLoadTime: Date.now() / 1000 + 0.1,
                    finishLoadTime: Date.now() / 1000 + 0.2,
                    firstPaintAfterLoadTime: 0,
                    firstPaintTime: Date.now() / 1000 + 0.05,
                    navigationType: 'Other',
                    npnNegotiatedProtocol: 'h2',
                    requestTime: Date.now() / 1000 - 0.3,
                    startLoadTime: Date.now() / 1000 - 0.3,
                    wasAlternateProtocolAvailable: false,
                    wasFetchedViaSpdy: true,
                    wasNpnNegotiated: true,
                };
            };

            // Remove Playwright globals
            delete window.__playwright;
            delete window.__pw_manual;

            // navigator.plugins — return a non-empty PluginArray-like object
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
                          description: 'Portable Document Format', length: 1 },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                          description: '', length: 1 },
                        { name: 'Native Client', filename: 'internal-nacl-plugin',
                          description: '', length: 2 },
                    ];
                    plugins.refresh = () => {};
                    Object.setPrototypeOf(plugins, PluginArray.prototype);
                    return plugins;
                },
            });

            // navigator.languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });

            // WebGL vendor/renderer masking
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(param) {
                // UNMASKED_VENDOR_WEBGL
                if (param === 37445) return 'Google Inc. (NVIDIA)';
                // UNMASKED_RENDERER_WEBGL
                if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter.call(this, param);
            };
            const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {
                if (param === 37445) return 'Google Inc. (NVIDIA)';
                if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter2.call(this, param);
            };
        """

        if fingerprint:
            seed = fingerprint["seed"]
            lang = fingerprint.get("language", "en-US,en")
            extra = f"""
            // Per-instance canvas noise (fingerprint diversification)
            const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
            CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {{
                const data = _origGetImageData.call(this, x, y, w, h);
                const noise = parseInt('{seed[:4]}', 16) % 5;
                for (let i = 0; i < data.data.length; i += 100) {{
                    data.data[i] = (data.data[i] + noise) % 256;
                }}
                return data;
            }};
            // Per-instance language override
            Object.defineProperty(navigator, 'languages', {{
                get: () => '{lang}'.split(',').map(l => l.split(';')[0].trim()),
            }});
            """
            return base_scripts + extra

        return base_scripts

    async def _detect_cloudflare(self, page) -> bool:
        """Check if the current page is a Cloudflare challenge/block page."""
        try:
            title = await page.title()
            content = await page.evaluate("document.body.innerText.substring(0, 500)")
            indicators = [
                "Just a moment" in title,
                "Verify you are human" in content,
                "Checking your browser" in content,
                "cf-challenge" in (await page.content())[:2000],
                # Cloudflare managed-challenge / Turnstile loads an iframe from
                # challenges.cloudflare.com — detect it by structured frame host
                # (the JS-challenge path with no iframe is still caught by the
                # text indicators above, e.g. "cf-challenge").
                any(_cf_hostname_match(f.url) for f in page.frames),
            ]
            return any(indicators)
        except Exception:
            return False

    async def start(self) -> None:
        """Launch browser. Uses non-persistent context by default (supports concurrency).

        If headless_fallback is True, launches headless first, navigates to Perplexity,
        and if Cloudflare blocks the page, closes and re-launches in headful mode.
        """
        from council_config import USE_REBROWSER

        if USE_REBROWSER:
            from rebrowser_playwright.async_api import async_playwright
        else:
            from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()

        # 2026-05-21 (Q5 architecture fix): try CDP attach to session_keeper.py first.
        # When the keeper is running it owns a long-lived headful Chrome that Cloudflare
        # trusts. Borrowing its context instead of launching our own browser eliminates
        # the "cookies issued headful, headless can't validate" fingerprint asymmetry
        # entirely — every research call uses the keeper's exact browser fingerprint.
        if await self._start_via_cdp():
            _log("CDP-attached to session_keeper; skipping local browser launch")
            if hasattr(self, "_query_inst"):
                self._query_inst["chrome_path_used"] = "cdp_attached"
                self._query_inst["cdp_keeper_alive_at_start"] = True
            # CDP-attach skips _load_session entirely, so this is the ONLY freshness
            # guard for the dominant path — refresh the keeper's in-place session if
            # a critical cookie is expiring before this query could finish.
            await self._ensure_fresh_session("cdp-attach")
            return

        if self.headless_fallback and self.headless:
            # Try headless first
            _log("Headless-fallback: trying headless launch first...")
            await self._start_non_persistent()
            page = await self.context.new_page()
            try:
                await page.goto(
                    "https://www.perplexity.ai/",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                await page.wait_for_timeout(3000)
                if await self._detect_cloudflare(page):
                    _log("Headless-fallback: Cloudflare detected, switching to headful...")
                    await page.close()
                    await self._cleanup_browser()
                    self.headless = False
                    await self._start_non_persistent()
                else:
                    _log("Headless-fallback: no Cloudflare detected, proceeding headless")
                    await page.close()
            except Exception as e:
                _log(f"Headless-fallback: navigation error ({e}), switching to headful...")
                try:
                    await page.close()
                except Exception:
                    pass
                await self._cleanup_browser()
                self.headless = False
                await self._start_non_persistent()
        elif self.use_persistent:
            await self._start_persistent()
        else:
            await self._start_non_persistent()

    @staticmethod
    def _cdp_port_owner_cmdline(port: int) -> str | None:
        """Return the command line of the process LISTENING on `port`, or None.

        Windows-only (uses Get-NetTCPConnection + Win32_Process). Returns None
        when the owner cannot be determined — callers must treat None as
        "unknown", never as "not the keeper", so a failed probe degrades to the
        post-attach cookie gate rather than blocking a healthy keeper.
        """
        if sys.platform != "win32":
            return None
        ps = (
            f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen "
            f"-ErrorAction SilentlyContinue | Select-Object -First 1; "
            f"if ($c) {{ (Get-CimInstance Win32_Process -Filter "
            f"\"ProcessId=$($c.OwningProcess)\").CommandLine }}"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as e:
            _log(f"CDP owner probe failed ({type(e).__name__}: {e}) — owner unknown")
            return None
        cmdline = (out.stdout or "").strip()
        return cmdline or None

    @classmethod
    def _cdp_endpoint_is_keeper(cls, endpoint: str, recorded_pid: int | None) -> tuple[bool, str]:
        """Verify a live CDP endpoint actually belongs to the session keeper.

        A reachable CDP port is NOT proof the keeper is alive. Port 9222 is also
        used by `~/.claude/browser-relay/relay.mjs` (the /takeover phone relay),
        which launches Chrome on a throwaway `C:\\Temp\\igx-cdp-profile` with no
        Perplexity cookies. On 2026-08-07 the keeper had been disabled since
        07-30 while a *stale* session_keeper.cdp from 08-02 still pointed at
        9222; when the relay claimed that port at 12:57 PT, every research run
        attached to the relay's cookie-less Chrome and died ~128s later with
        "Target page, context or browser has been closed". Four consecutive
        failures, machine-wide research outage.

        Returns (is_keeper, reason). `is_keeper=False` means "definitely not the
        keeper — do not attach". An indeterminate probe returns True with a
        reason noting the uncertainty; the post-attach cookie gate is the
        backstop for that case.
        """
        # Gate A — the PID recorded alongside the endpoint must still be alive.
        # A dead PID means the .cdp file is a leftover from a previous boot and
        # whatever answers on that port now is somebody else's Chrome.
        if recorded_pid and recorded_pid > 0 and not cls._pid_alive(recorded_pid):
            return False, f"recorded keeper pid {recorded_pid} is dead (stale .cdp file)"

        port = KEEPER_CDP_PORT
        try:
            port = int(urllib.parse.urlparse(endpoint).port or KEEPER_CDP_PORT)
        except Exception:
            pass

        # Gate B — DevToolsActivePort match. Chrome writes "<port>\n<ws-guid-path>"
        # into its own --user-data-dir at startup. Comparing that GUID against the
        # webSocketDebuggerUrl the endpoint reports is the canonical way to prove
        # "this endpoint is the Chrome launched with THAT profile" — any process
        # can serve plausible JSON on a port, so the response alone proves nothing.
        # Portable (no process introspection) and decisive when the file exists.
        active_port_file = KEEPER_PROFILE_DIR / "DevToolsActivePort"
        try:
            lines = active_port_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        if len(lines) >= 2 and lines[0].strip().isdigit():
            keeper_port = int(lines[0].strip())
            keeper_guid = lines[1].strip()
            if keeper_port != port:
                return False, (
                    f"keeper's DevToolsActivePort says port {keeper_port}, "
                    f"but endpoint points at {port}"
                )
            try:
                import urllib.request as _ur
                with _ur.urlopen(f"{endpoint}/json/version", timeout=3) as _r:
                    ws = json.loads(_r.read().decode()).get("webSocketDebuggerUrl", "")
            except Exception as e:
                return True, f"DevToolsActivePort read but /json/version probe failed ({type(e).__name__})"
            if keeper_guid and keeper_guid in ws:
                return True, f"DevToolsActivePort GUID matches endpoint on port {port}"
            return False, (
                f"endpoint on port {port} reports a different DevTools GUID than the "
                f"keeper's profile — a foreign Chrome is serving this port"
            )

        # Gate C — no DevToolsActivePort (keeper not running, or its profile was
        # cleaned). Fall back to checking that the process listening on the port
        # is running the keeper's profile dir.
        cmdline = cls._cdp_port_owner_cmdline(port)
        if cmdline is None:
            return True, "port owner unknown (probe unavailable) — deferring to cookie gate"
        if KEEPER_PROFILE_DIRNAME in cmdline:
            return True, f"port {port} owned by keeper profile ({KEEPER_PROFILE_DIRNAME})"
        foreign = (
            "browser-relay /takeover"
            if "igx-cdp-profile" in cmdline
            else "unidentified app"
        )
        return False, (
            f"port {port} is owned by a FOREIGN Chrome ({foreign}) — "
            f"no '{KEEPER_PROFILE_DIRNAME}' in its command line"
        )

    @classmethod
    def _keeper_cdp_alive(cls) -> tuple[bool, str]:
        """True only if the keeper's OWN Chrome is serving CDP on KEEPER_CDP_PORT.

        This is the identity-gated twin of the check `_start_via_cdp` performs.
        It exists because the 2026-08-08 outage came from the *unguarded* copy of
        this probe in `_ensure_fresh_session`: that one asked only "does the port
        answer /json/version?", concluded the keeper was alive when the /takeover
        relay held 9222, and so routed every refresh down the keeper branch --
        which then could not possibly land. Reachability is not identity; prove
        ownership before believing a port belongs to us.
        """
        endpoint = f"http://127.0.0.1:{KEEPER_CDP_PORT}"
        try:
            import urllib.request as _ur
            with _ur.urlopen(f"{endpoint}/json/version", timeout=2) as _r:
                _ = _r.read()
        except Exception as e:
            return False, f"no CDP on port {KEEPER_CDP_PORT} ({type(e).__name__})"
        return cls._cdp_endpoint_is_keeper(endpoint, None)

    @staticmethod
    def _keeper_task_state() -> str:
        """State of the PerplexitySessionKeeper scheduled task.

        Returns one of "Ready" | "Running" | "Disabled" | "Missing" | "Unknown".

        Firing `Start-ScheduledTask` at a **Disabled** task is a silent no-op:
        it neither errors usefully nor runs anything. The task had been Disabled
        since 2026-07-30, so every `_ensure_fresh_session` keeper refresh since
        then burned the full SESSION_KEEPER_WAIT_S and then proceeded stale --
        the 120s of dead wall-clock at the head of every doomed run on 08-08.
        Check the state first and skip the branch when it cannot possibly work.
        """
        if sys.platform != "win32":
            return "Unknown"
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$t = Get-ScheduledTask -TaskName 'PerplexitySessionKeeper' "
                 "-ErrorAction SilentlyContinue; "
                 "if ($t) { $t.State } else { 'Missing' }"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as e:
            _log(f"Keeper task-state probe failed ({type(e).__name__}: {e})")
            return "Unknown"
        state = (out.stdout or "").strip()
        return state or "Unknown"

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """True if `pid` names a live process. Windows-safe (see _is_session_keeper_running)."""
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True  # exists but inaccessible
        except (ProcessLookupError, OSError, SystemError):
            if sys.platform == "win32":
                try:
                    import ctypes
                    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                    h = ctypes.windll.kernel32.OpenProcess(
                        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
                    )
                    if h:
                        ctypes.windll.kernel32.CloseHandle(h)
                        return True
                except Exception:
                    pass
            return False

    async def _cdp_context_has_perplexity_session(self) -> bool:
        """True if the attached CDP context carries a Perplexity login cookie.

        Backstop for `_cdp_endpoint_is_keeper` when the port-owner probe is
        unavailable: a foreign/throwaway Chrome profile has zero perplexity.ai
        cookies, so this separates "the keeper" from "somebody else's browser"
        without depending on Windows process introspection. It also catches a
        live keeper whose login has actually expired.
        """
        try:
            cookies = await self.context.cookies()
        except Exception as e:
            _log(f"CDP cookie gate: could not read cookies ({type(e).__name__}) — allowing attach")
            return True  # don't block on an unexpected Playwright error
        names = {
            c.get("name")
            for c in cookies
            if "perplexity.ai" in (c.get("domain") or "")
        }
        if names & PERPLEXITY_AUTH_COOKIES:
            return True
        _log(
            f"CDP cookie gate: attached context has {len(names)} perplexity.ai cookie(s), "
            f"none of {sorted(PERPLEXITY_AUTH_COOKIES)} — not a logged-in keeper browser"
        )
        return False

    async def _start_via_cdp(self) -> bool:
        """Attach to a running session_keeper.py via Chrome DevTools Protocol.

        Returns True on successful attach (caller skips local browser launch),
        False to fall through to the launch path. Reads the keeper's CDP endpoint
        from `~/.claude/config/session_keeper.cdp` and connects via
        `chromium.connect_over_cdp()`. Reuses the keeper's existing context
        (contexts[0]) which has the human-issued cookies + warm Cloudflare state.

        We track pages we open in `self._cdp_owned_pages` so stop() can close
        ONLY our pages, leaving the keeper's home tab intact.
        """
        # The CHROME subprocess that the keeper launched (with DETACHED_PROCESS) can
        # outlive the keeper python — especially during Task Scheduler restart-on-
        # failure cycles. What we actually need for CDP attach is "is the CDP port
        # reachable", not "is the keeper python alive". Check the endpoint file
        # first, then probe the port directly. Treat keeper-python liveness as a
        # secondary signal (informational only).
        cdp_file = Path.home() / ".claude" / "config" / "session_keeper.cdp"

        # Fast path: if Chrome is already serving CDP on the default port,
        # synthesize the endpoint file so we can attach. Covers the case where
        # the keeper's Chrome is alive but the .cdp file was inadvertently
        # removed (observed 2026-05-25 — user closed Chrome window, the file
        # got cleaned up by some path, but Chrome's child processes kept
        # serving the port).
        # NOTE: "port 9222 answers" is NOT the same as "the keeper is up" — the
        # /takeover browser-relay serves CDP on the same port. Only synthesize
        # the endpoint file once the listening process is confirmed to be running
        # the keeper's own profile, otherwise we manufacture a valid-looking
        # endpoint pointing at somebody else's Chrome (the 2026-08-07 outage).
        if not cdp_file.exists():
            try:
                import urllib.request as _ur
                with _ur.urlopen(f"http://127.0.0.1:{KEEPER_CDP_PORT}/json/version", timeout=2) as _r:
                    _ = _r.read()
                owner = self._cdp_port_owner_cmdline(KEEPER_CDP_PORT)
                if owner is not None and KEEPER_PROFILE_DIRNAME not in owner:
                    _log(
                        f"CDP-attach: port {KEEPER_CDP_PORT} is serving CDP but is NOT the keeper "
                        f"(no '{KEEPER_PROFILE_DIRNAME}' in owner cmdline) — refusing to "
                        "synthesize an endpoint file for a foreign browser"
                    )
                    raise RuntimeError("foreign CDP owner")
                # Port is up — write a fresh CDP file so the rest of the path
                # can use it. The keeper would have written this normally.
                cdp_file.parent.mkdir(parents=True, exist_ok=True)
                cdp_file.write_text(
                    json.dumps({"port": KEEPER_CDP_PORT,
                                "endpoint": f"http://127.0.0.1:{KEEPER_CDP_PORT}"}),
                    encoding="utf-8",
                )
                _log(f"CDP-attach: Chrome alive on {KEEPER_CDP_PORT} but .cdp file "
                     f"missing — synthesized it")
            except Exception:
                pass  # port not reachable / not the keeper; fall through

        # Auto-start the keeper task if CDP still isn't reachable. Avoids the
        # headful local-launch fallback that creates focus-stealing popups.
        if not cdp_file.exists():
            _log("CDP not reachable — triggering PerplexitySessionKeeper scheduled task...")
            try:
                import subprocess
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Start-ScheduledTask -TaskName 'PerplexitySessionKeeper'"],
                    capture_output=True, text=True, timeout=10,
                )
                # Poll for CDP file (keeper writes it after initial warm — ~5-8s).
                import time as _time
                deadline = _time.time() + 20
                while _time.time() < deadline:
                    if cdp_file.exists():
                        _log("Keeper task came up — CDP file present, proceeding")
                        break
                    _time.sleep(1)
            except Exception as e:
                _log(f"Keeper auto-start failed: {type(e).__name__}: {e}")

        if not cdp_file.exists():
            keeper_alive = self._is_session_keeper_running()
            if keeper_alive:
                _log("CDP-attach skipped: keeper alive but session_keeper.cdp missing (still warming up?)")
            else:
                _log("CDP-attach skipped: session_keeper not running and auto-start did not produce CDP endpoint")
            return False
        # Probe the CDP endpoint directly — works even if keeper python is mid-restart.
        try:
            data = json.loads(cdp_file.read_text(encoding="utf-8"))
            endpoint_probe = data.get("endpoint", "")
            if endpoint_probe:
                import urllib.request as _ur
                try:
                    with _ur.urlopen(f"{endpoint_probe}/json/version", timeout=3) as _r:
                        _ = _r.read()  # consume to verify CDP responds
                except Exception as e:
                    _log(f"CDP-attach skipped: endpoint {endpoint_probe} unreachable ({type(e).__name__}); falling back to launch")
                    return False
                # Reachable is not enough — prove it is the keeper's Chrome.
                # A stale .cdp file plus a foreign app on the same port is the
                # exact combination that produced the 2026-08-07 outage.
                recorded_pid = data.get("pid")
                ok, why = self._cdp_endpoint_is_keeper(
                    endpoint_probe,
                    recorded_pid if isinstance(recorded_pid, int) else None,
                )
                if not ok:
                    _log(f"CDP-attach REFUSED: {why} — falling back to local launch")
                    try:
                        cdp_file.unlink()
                        _log("Removed stale session_keeper.cdp so later runs re-probe cleanly")
                    except Exception:
                        pass
                    return False
                _log(f"CDP identity OK: {why}")
        except Exception:
            pass  # let the connect_over_cdp below try and produce its own error
        try:
            data = json.loads(cdp_file.read_text(encoding="utf-8"))
            endpoint = data.get("endpoint")
            if not endpoint:
                _log("CDP file present but no endpoint key — falling back to launch")
                return False
        except Exception as e:
            _log(f"CDP file unreadable ({type(e).__name__}: {e}) — falling back to launch")
            return False

        try:
            _log(f"Attaching to session_keeper via CDP at {endpoint} ...")
            self._browser = await self.playwright.chromium.connect_over_cdp(endpoint)
            contexts = self._browser.contexts
            if not contexts:
                _log("CDP attach: no contexts available (keeper not ready); fall back")
                # IMPORTANT: do NOT call browser.close() on a CDP-connected
                # browser — it sends Browser.close CDP which kills the remote
                # Chrome (and the keeper). Just release the reference.
                self._browser = None
                return False
            self.context = contexts[0]

            # Backstop identity gate: a browser that is not logged in to
            # Perplexity is useless to us, and a foreign/throwaway profile has
            # no perplexity.ai cookies at all. Catches the wrong-browser case
            # even when the Windows port-owner probe was unavailable, and also
            # catches a genuinely expired keeper login — both of which used to
            # present as a ~128s hang ending in "browser has been closed".
            if not await self._cdp_context_has_perplexity_session():
                _log("CDP attach REFUSED by cookie gate — falling back to local launch")
                self.context = None
                self._browser = None  # never .close() a CDP browser: kills remote Chrome
                return False

            self._cdp_attached = True
            existing_count = len(self.context.pages)
            _log(f"CDP attach OK: {len(contexts)} context(s); using contexts[0] with {existing_count} existing page(s)")

            # Auto-cleanup stale /search/ pages from prior runs (orphans accumulate
            # because killed/timed-out council_browser invocations don't get to run
            # their CDP-aware stop() that closes _cdp_owned_pages). Past a certain
            # count Chrome resources slow down and validate_session can silently
            # exit. Keep the keeper's home tab(s) intact; close /search/ pages.
            stale_closed = 0
            for p in list(self.context.pages):
                if "/search/" in (p.url or ""):
                    try:
                        await p.close()
                        stale_closed += 1
                    except Exception:
                        pass
            if stale_closed:
                _log(f"CDP cleanup: closed {stale_closed} stale /search/ page(s) from prior runs")
            return True
        except Exception as e:
            _log(f"CDP attach failed: {type(e).__name__}: {e}")
            # IMPORTANT: do NOT call browser.close() — would kill remote Chrome.
            self._browser = None
            self._cdp_attached = False
            return False

    async def _start_non_persistent(self) -> None:
        """Launch browser with an isolated temp profile directory.

        Each session gets its own user-data-dir via launch_persistent_context()
        to prevent Chrome SingletonLock conflicts when multiple instances run
        concurrently. Cookies injected via _load_session() after launch.
        Per-instance fingerprint diversification applied via instance_id.
        """
        # Pre-launch jitter: prevents two concurrent Claude sessions from
        # racing into chromium.launch_persistent_context within microseconds
        # of each other. Chrome's global Local\ChromeProcessSingletonStartup!
        # mutex serialises singleton-window creation; under zero-stagger
        # concurrency, the FindRunningChromeWindow lookup can briefly see
        # the other instance's not-yet-titled window as a match candidate.
        # 0-500ms jitter eliminates the zero-stagger race. The submit_lock
        # below (acquired by run() before self.start()) is the primary
        # mechanism; this jitter is belt-and-suspenders defense.
        await asyncio.sleep(random.uniform(0, 0.5))
        # Canonicalise the temp dir: resolve symlinks, fix case, strip trailing
        # separator. Defeats Chrome's FindRunningChromeWindow false-positive title
        # match on Windows (mixed-case drives, 8.3 short paths, trailing seps),
        # which was causing concurrent /research-perplexity calls to share state
        # via WM_COPYDATA forwarding into an existing chrome.exe instance.
        raw_temp_dir = tempfile.mkdtemp(prefix="council_np_")
        self._temp_profile_dir = os.path.realpath(raw_temp_dir).rstrip(os.sep)
        fp = self._get_instance_fingerprint(self.instance_id)
        vp_w, vp_h = fp["viewport"]
        _log(f"Non-persistent: instance={self.instance_id} profile={self._temp_profile_dir} viewport={vp_w}x{vp_h}")
        if hasattr(self, "_query_inst"):
            self._query_inst["chrome_path_used"] = "local_nonpersistent"

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self._temp_profile_dir,
            channel="chrome",
            headless=self.headless,
            args=self._chrome_args(instance_id=self.instance_id),
            viewport={"width": vp_w, "height": vp_h},
        )

        if self.session_path.exists():
            await self._load_session()

        # Apply stealth scripts with per-instance fingerprint
        await self.context.add_init_script(self._stealth_scripts(fingerprint=fp))

    async def _start_persistent(self) -> None:
        """Launch with persistent context (used for --save-session only).

        Always uses instance_id=0 (login sessions don't need fingerprint diversification).
        """
        BROWSER_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        if hasattr(self, "_query_inst"):
            self._query_inst["chrome_path_used"] = "local_persistent"

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_USER_DATA_DIR),
            channel="chrome",
            headless=self.headless,
            args=self._chrome_args(instance_id=0),
            viewport={"width": 1920, "height": 1080},
        )

        if self.session_path.exists():
            await self._load_session()

        await self.context.add_init_script(self._stealth_scripts())

    async def _start_with_temp_profile(self) -> None:
        """Cloudflare fallback: persistent context with a temp profile directory.

        Uses a unique temp dir per session — no SingletonLock conflicts.
        Cookies injected via _load_session() after launch.
        Per-instance fingerprint diversification applied via instance_id.
        """
        # Pre-launch jitter (see _start_non_persistent for full rationale).
        await asyncio.sleep(random.uniform(0, 0.5))
        # Canonicalise the temp dir — same rationale as _start_non_persistent:
        # defeats Chrome's FindRunningChromeWindow false-positive title match.
        raw_cf_dir = tempfile.mkdtemp(prefix="council_cf_")
        self._temp_profile_dir = os.path.realpath(raw_cf_dir).rstrip(os.sep)
        fp = self._get_instance_fingerprint(self.instance_id)
        vp_w, vp_h = fp["viewport"]
        _log(f"Cloudflare fallback: instance={self.instance_id} profile={self._temp_profile_dir} viewport={vp_w}x{vp_h}")

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self._temp_profile_dir,
            channel="chrome",
            headless=self.headless,
            args=self._chrome_args(instance_id=self.instance_id),
            viewport={"width": vp_w, "height": vp_h},
        )

        if self.session_path.exists():
            await self._load_session()

        await self.context.add_init_script(self._stealth_scripts(fingerprint=fp))

    async def _ensure_fresh_session(self, reason: str = "") -> None:
        """Refresh the Perplexity session if a critical cookie is expired or
        expiring within SESSION_FRESHNESS_THRESHOLD_S, BEFORE submitting a query.

        Both the empty-synthesis and the mid-run 360s-timeout-abort failures were
        proven (A/B smoke, 2026-06-15) to correlate with a degraded session. The
        existing auto-refresh lived only in _load_session, which the CDP-attach
        path (the dominant path — every observed failure was chrome_path_used=
        cdp_attached) never calls. This guard runs on BOTH paths.

        Fires the keeper task (idempotent — Task Scheduler refuses a 2nd instance
        of the one-shot, so no lock is needed) and waits up to SESSION_KEEPER_WAIT_S
        for the cookies-file mtime to bump, then falls back to refresh_session.py.
        Because we CDP-attach to the same keeper Chrome that the keeper refreshes
        in-place, the live attached session picks up the new cookies.

        Raises SessionStaleError if a critical cookie is still EXPIRED after both
        refresh paths. This reverses the pre-2026-08-08 policy of always
        proceeding: the old rationale ("a stale session still usually succeeds")
        holds for cookies that are merely *expiring soon*, which is why only
        hard-expired cookies abort here. It does not hold for expired ones —
        those produced a Cloudflare wall and a guaranteed mid-run death 5-8
        minutes later, on a serialized queue, with no error ever logged.
        """
        freshness = self._check_session_freshness(self.session_path)
        if hasattr(self, "_query_inst"):
            self._query_inst["cookies_stale_critical"] = freshness.get("stale_critical", [])
            # Pre-guard min critical-cookie TTL — the value the guard decided on,
            # which is what lets calibration tune SESSION_FRESHNESS_THRESHOLD_S.
            self._query_inst["min_critical_ttl_s"] = freshness.get("min_critical_ttl_s")
        stale = freshness.get("stale_critical") or []
        soon = freshness.get("expiring_soon") or []
        if not stale and not soon:
            return  # session healthy — no-op

        min_ttl = freshness.get("min_critical_ttl_s")
        detail = (", ".join(f"{n}({age}m ago)" for n, age in stale)
                  or ", ".join(f"{n}({s}s left)" for n, s in soon))
        _log(f"Session-freshness guard ({reason}): critical cookies low [{detail}] "
             f"min_ttl={int(min_ttl) if isinstance(min_ttl, (int, float)) else 'expired'}s — refreshing")
        _log(f"MONITOR-SIGNAL cookie_stale {detail}")

        auto_refresh_env = os.environ.get("COUNCIL_AUTO_REFRESH", "").lower()
        if auto_refresh_env in ("0", "false", "no", "off"):
            _log("COUNCIL_AUTO_REFRESH=0 (opt-out) — proceeding with stale session")
            if hasattr(self, "_query_inst"):
                self._query_inst["auto_refresh_path"] = "no_refresh"
            return

        # Prefer refreshing THROUGH the keeper task (no popup) when the KEEPER'S
        # OWN Chrome is serving CDP; else fall back to refresh_session.py.
        #
        # Two gates, both added 2026-08-08 after this block caused a machine-wide
        # research outage:
        #
        #   1. IDENTITY, not reachability. This used to be a bare urlopen against
        #      port 9222 -- the exact "a reachable port is a trusted port" mistake
        #      that `_start_via_cdp` was hardened against on 08-07. The /takeover
        #      relay held 9222, so `chrome_alive` came back True, the keeper branch
        #      was taken, and the refresh could never land. Note the two paths
        #      disagreed: `_start_via_cdp` correctly REFUSED the same port seconds
        #      earlier and fell back to a local launch. Fixing one call site and
        #      not its twin is what turned a closed bug back into an open one.
        #
        #   2. TASK RUNNABILITY. `Start-ScheduledTask` against a Disabled task is a
        #      silent no-op. PerplexitySessionKeeper had been Disabled since
        #      2026-07-30, so the wait below could only ever time out. Every run
        #      paid SESSION_KEEPER_WAIT_S for nothing before proceeding doomed.
        keeper_ok, keeper_why = self._keeper_cdp_alive()
        task_state = self._keeper_task_state() if keeper_ok else "n/a"
        keeper_usable = keeper_ok and task_state in ("Ready", "Running", "Unknown")
        refreshed_via_keeper = False

        if keeper_ok and not keeper_usable:
            _log(f"Keeper Chrome present ({keeper_why}) but its scheduled task is "
                 f"{task_state} — firing it would be a no-op; using refresh_session.py")
        elif not keeper_ok:
            _log(f"Keeper CDP not usable: {keeper_why} — using refresh_session.py")

        if keeper_usable:
            _log(f"Keeper verified on CDP ({keeper_why}); task={task_state}; "
                 f"firing PerplexitySessionKeeper to refresh in-place...")
            if hasattr(self, "_query_inst"):
                self._query_inst["auto_refresh_path"] = "keeper_task"
            try:
                import subprocess as _subprocess
                import time as _time
                mtime_before = self.session_path.stat().st_mtime if self.session_path.exists() else 0.0
                _subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Start-ScheduledTask -TaskName 'PerplexitySessionKeeper'"],
                    capture_output=True, text=True, timeout=10,
                )
                deadline = _time.time() + SESSION_KEEPER_WAIT_S
                while _time.time() < deadline:
                    try:
                        mtime_now = self.session_path.stat().st_mtime
                    except OSError:
                        mtime_now = 0.0
                    if mtime_now > mtime_before:
                        _log("Keeper refreshed cookies (mtime bumped); session fresh")
                        refreshed_via_keeper = True
                        break
                    _time.sleep(1)
                if not refreshed_via_keeper:
                    # Most likely the keeper's own ~20-min auto-cycle is mid-run,
                    # so Task Scheduler refused our 2nd instance. Do NOT proceed
                    # on stale cookies -- fall through to the direct refresher.
                    _log(f"WARN: keeper did not bump cookie mtime in "
                         f"{SESSION_KEEPER_WAIT_S}s — falling back to refresh_session.py")
            except Exception as e:
                _log(f"Keeper-task refresh failed: {type(e).__name__}: {e} — "
                     f"falling back to refresh_session.py")

        if not refreshed_via_keeper:
            if hasattr(self, "_query_inst"):
                self._query_inst["auto_refresh_path"] = (
                    "keeper_then_refresh_session" if keeper_usable else "refresh_session_fallback"
                )
            _log("Auto-refresh ON — invoking refresh_session.py ...")
            refreshed = await self._auto_refresh_session()
            _log("Auto-refresh succeeded" if refreshed else "WARNING: auto-refresh failed")

        # Fail loud rather than submitting a doomed run. Only HARD-EXPIRED
        # critical cookies abort: `expiring_soon` is a soft signal (the guard
        # fires at SESSION_FRESHNESS_THRESHOLD_S = 8 min of remaining TTL, and a
        # session with 7 minutes left still answers fine), whereas an expired
        # pplx.session-id / __cf_bm means Cloudflare will serve a bot challenge
        # Playwright cannot pass and the run WILL die -- just 5-8 minutes later,
        # after it has consumed a slot on the serialized queue.
        post = self._check_session_freshness(self.session_path)
        still_expired = post.get("stale_critical") or []
        if hasattr(self, "_query_inst"):
            self._query_inst["cookies_stale_critical"] = still_expired
        if not still_expired:
            return
        if os.environ.get("COUNCIL_ALLOW_STALE", "").lower() in ("1", "true", "yes", "on"):
            _log("COUNCIL_ALLOW_STALE=1 — proceeding on expired cookies anyway")
            return
        detail_expired = ", ".join(f"{n}({age}m ago)" for n, age in still_expired)
        _log(f"MONITOR-SIGNAL session_stale_abort {detail_expired}")
        raise SessionStaleError(
            f"Perplexity session cookies are EXPIRED and could not be refreshed "
            f"[{detail_expired}]. Refresh path tried: "
            f"keeper={'usable' if keeper_usable else f'unusable ({keeper_why}, task={task_state})'}, "
            f"refresh_session.py=failed. Aborting before submit instead of burning a "
            f"queue slot on a run that cannot succeed. Fix: run /cache-perplexity-session, "
            f"or check that PerplexitySessionKeeper is Enabled and owns CDP port "
            f"{KEEPER_CDP_PORT}."
        )

    async def _load_session(self) -> None:
        """Load session from playwright-session.json + playwright-localstorage.json.

        Pre-flight: detect stale critical cookies (Cloudflare __cf_bm has a ~30min
        TTL — if expired, Cloudflare serves a bot challenge that Playwright can't
        pass, and the synthesis comes back 0 bytes silently). When detected:
          - Always log a WARN with cookie names + minutes-since-expiry.
          - If COUNCIL_AUTO_REFRESH=1, invoke refresh_session.py inline + reload.
          - Otherwise continue with stale cookies (caller may still succeed if
            Cloudflare is lenient now) but the WARN is the postmortem signal.
        """
        # Proactive session-freshness guard (refactored 2026-06-15 into
        # _ensure_fresh_session, shared with the CDP-attach path in start()).
        await self._ensure_fresh_session("local-launch")

        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))

            # Playwright-native format: list of cookie dicts
            if isinstance(data, list):
                await self.context.add_cookies(data)
                _log(f"Loaded {len(data)} cookies from {self.session_path.name}")

            # Legacy format from /cache-perplexity-session: {cookies: "str", localStorage: {}}
            elif isinstance(data, dict):
                cookies = self._parse_cookie_string(data.get("cookies", ""))
                if cookies:
                    await self.context.add_cookies(cookies)
                    _log(f"Converted and loaded {len(cookies)} cookies from legacy format")

        except Exception as e:
            _log(f"WARNING: Failed to load cookies: {e}")

    @staticmethod
    def _check_session_freshness(session_path: Path) -> dict:
        """Return {stale_critical: [(name, minutes_expired)], all_count: N, expired_count: N}.

        Critical cookies are Cloudflare/Perplexity short-TTL ones that, when expired,
        produce silent 0-byte synthesis responses from Perplexity research mode.
        """
        CRITICAL = {"__cf_bm", "pplx.edge-sid", "pplx.session-id"}
        result = {"stale_critical": [], "expiring_soon": [], "min_critical_ttl_s": None,
                  "all_count": 0, "expired_count": 0}
        try:
            data = json.loads(session_path.read_text(encoding="utf-8"))
        except Exception:
            return result
        if not isinstance(data, list):
            return result
        now = time.time()
        min_ttl = None
        for c in data:
            result["all_count"] += 1
            exp = c.get("expires", -1)
            if not isinstance(exp, (int, float)) or exp <= 0:
                continue  # session cookie or no expiry
            name = c.get("name", "")
            if name not in CRITICAL:
                continue
            if exp < now:
                result["expired_count"] += 1
                result["stale_critical"].append((name, int((now - exp) / 60)))
            else:
                ttl = exp - now
                if min_ttl is None or ttl < min_ttl:
                    min_ttl = ttl
                if ttl < SESSION_FRESHNESS_THRESHOLD_S:
                    # 2026-06-15: flag expiring-soon (not just already-expired) so the
                    # freshness guard refreshes BEFORE the session dies mid-query.
                    result["expiring_soon"].append((name, int(ttl)))
        result["min_critical_ttl_s"] = min_ttl
        return result

    @staticmethod
    def _is_session_keeper_running() -> bool:
        """Detect a running session_keeper.py via its PID file.

        Windows: os.kill(pid, 0) for a non-existent PID raises generic OSError
        (not ProcessLookupError as on Unix). PermissionError on either OS means
        the process exists but is inaccessible — treat as alive. All other
        OSError or ProcessLookupError → treat as stale and clear the file.
        """
        pid_file = Path.home() / ".claude" / "config" / "session_keeper.pid"
        if not pid_file.exists():
            return False
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return False
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True  # process exists, just inaccessible (cross-user on Windows)
        except (ProcessLookupError, OSError, SystemError):
            # Windows signal 0 isn't valid for TerminateProcess — os.kill raises
            # OSError(WinError 87) which CPython surfaces as SystemError. Fall
            # back to a Windows-native check via ctypes.OpenProcess before
            # treating the PID as stale.
            if sys.platform == "win32":
                try:
                    import ctypes
                    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                    handle = ctypes.windll.kernel32.OpenProcess(
                        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
                    )
                    if handle:
                        ctypes.windll.kernel32.CloseHandle(handle)
                        return True
                except Exception:
                    pass
            try:
                pid_file.unlink()
            except FileNotFoundError:
                pass
            return False

    async def _auto_refresh_session(self) -> bool:
        """Invoke refresh_session.py via subprocess.run (in a thread) to refresh stale cookies."""
        import shutil as _shutil
        import subprocess as _subprocess
        refresh_script = Path(__file__).parent / "refresh_session.py"
        if not refresh_script.exists():
            _log(f"refresh_session.py not found at {refresh_script}")
            return False
        python_path = _shutil.which("python") or sys.executable
        argv = [python_path, str(refresh_script)]
        def _run() -> tuple[int, str]:
            try:
                proc = _subprocess.run(argv, capture_output=True, text=True, timeout=60)
                return proc.returncode, (proc.stderr or "")[-300:]
            except _subprocess.TimeoutExpired:
                return -1, "refresh_session.py timed out after 60s"
        try:
            rc, stderr_tail = await asyncio.to_thread(_run)
            if rc != 0:
                _log(f"refresh_session.py exit={rc}; stderr tail: {stderr_tail}")
                return False
            return True
        except Exception as e:
            _log(f"refresh_session.py invoke failed: {type(e).__name__}: {e}")
            return False

        # Inject localStorage from companion file (critical for pplx-next-auth-session)
        ls_path = self.session_path.parent / "playwright-localstorage.json"
        if ls_path.exists():
            await self._inject_local_storage(ls_path)

    async def _inject_local_storage(self, ls_path: Path) -> None:
        """Inject localStorage items into Perplexity origin."""
        try:
            local_storage = json.loads(ls_path.read_text(encoding="utf-8"))
            if not local_storage:
                return
            page = await self.context.new_page()
            await page.goto("https://www.perplexity.ai/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            for key, value in local_storage.items():
                await page.evaluate(
                    f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)})"
                )
            await page.close()
            _log(f"Injected {len(local_storage)} localStorage items")
        except Exception as e:
            _log(f"WARNING: Failed to inject localStorage: {e}")

    @staticmethod
    def _parse_cookie_string(cookie_str: str) -> list[dict]:
        """Parse semicolon-delimited cookie string into Playwright cookie dicts."""
        if not cookie_str:
            return []
        cookies = []
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".perplexity.ai",
                "path": "/",
            })
        return cookies

    async def validate_session(self) -> bool:
        """Check if we're logged in to Perplexity."""
        page = await self.context.new_page()
        if self._cdp_attached:
            self._cdp_owned_pages.append(page)
        try:
            await page.goto("https://www.perplexity.ai/", wait_until="domcontentloaded", timeout=30000)
            # Wait a moment for JS to hydrate
            await page.wait_for_timeout(2000)

            textarea = self.selectors.get("textarea", "#ask-input")
            try:
                await page.wait_for_selector(textarea, timeout=10000)
                _log("Session valid: found input element")
                return True
            except Exception:
                _log("Session invalid: input element not found (not logged in?)")
                await self._save_artifact(page, "validate_failure")
                return False
        finally:
            await page.close()

    async def _acquire_submit_lock(self):
        """Acquire the cross-process Perplexity-submit lock async-safely.

        Two concurrent Claude Code sessions launching Chrome 4 seconds apart
        on Windows can race on OS-level keyboard focus during slash-command
        typing (the second `chromium.launch_persistent_context` calls
        SetForegroundWindow / BringWindowToTop, stealing focus from the
        first session mid-keystroke). The result: keystrokes go to the wrong
        target, the slash-command palette closes prematurely, the query
        submits in Search mode instead of Research, .prose comes back
        empty, and `server.js` retries-once → new subprocess → new Chrome
        window. User sees "browsers cancelling each other and trying."

        The fix is a cross-process file lock around the focus-sensitive
        critical section. `FileLock.acquire` is blocking; we offload to a
        worker thread so the asyncio event loop keeps pumping (network IO,
        timer callbacks) while we wait our turn.

        Under RESEARCH_QUEUE_STOPGAP (see `run()`), submit_lock is held
        for the whole run (minutes, not the ~10-25s submit window this
        lock was designed for). Two adjustments only apply under that
        flag: (1) acquire with a long timeout (RESEARCH_QUEUE_MAX_WAIT,
        default 1200s) so a waiting session queues instead of erroring
        out on the short default timeout; (2) start a background
        heartbeat that refreshes the lock file's mtime so the 180s
        stale-reclaim in `get_submit_lock()` never mistakes a live
        long-held lock for a crashed one. Default (flag unset) behavior
        is unchanged -- default timeout, no heartbeat.
        """
        stopgap = _flag_enabled("RESEARCH_QUEUE_STOPGAP")
        timeout = None
        if stopgap:
            try:
                timeout = float(os.environ.get("RESEARCH_QUEUE_MAX_WAIT", "1200"))
            except ValueError:
                timeout = 1200.0
        lock = get_submit_lock(timeout=timeout)
        _log(
            f"submit_lock waiting timeout_s={lock.timeout:.0f} "
            f"path={lock.lock_file}"
        )
        await asyncio.to_thread(lock.acquire)
        _log(f"submit_lock acquired path={lock.lock_file}")
        if stopgap:
            self._submit_lock_heartbeat = start_lock_heartbeat(lock)
            _log("submit_lock heartbeat started (RESEARCH_QUEUE_STOPGAP)")
        return lock

    def _stop_submit_lock_heartbeat(self) -> None:
        """Stop and clear the submit-lock heartbeat thread, if running.

        Idempotent and safe to call unconditionally: when
        RESEARCH_QUEUE_STOPGAP is unset (default), the heartbeat is
        never started, `self._submit_lock_heartbeat` stays unset/None,
        and this is a silent no-op.
        """
        heartbeat = getattr(self, "_submit_lock_heartbeat", None)
        if heartbeat is not None:
            try:
                heartbeat.stop()
            except Exception:
                pass
            self._submit_lock_heartbeat = None

    async def activate_mode(self, page) -> bool:
        """Activate the configured Perplexity mode via slash command.

        Supports: /council (multi-model) and /research (deep research).

        Emits structured `[browser] activate_mode ...` log lines at every
        decision point so a Perplexity UI change (e.g. commit-key swap) is
        diagnosable from a single log entry instead of a multi-day arc.
        """
        slash_cmd = f"/{self.perplexity_mode}"
        textarea = self.selectors.get("textarea", "#ask-input")
        t0 = time.perf_counter()
        _log(
            f"activate_mode start mode={self.perplexity_mode} "
            f"slash_cmd={slash_cmd} commit_key={PERPLEXITY_COMMIT_KEY} "
            f"textarea={textarea}"
        )

        # Focus the input
        try:
            await page.click(textarea)
            await page.wait_for_timeout(500)
        except Exception as e:
            _log(
                f"activate_mode FAIL step=focus mode={self.perplexity_mode} "
                f"textarea={textarea} error={e!r}"
            )
            return False

        # Type the slash command
        await page.keyboard.type(slash_cmd, delay=BROWSER_TYPE_DELAY)
        await page.wait_for_timeout(1500)  # Wait for command palette
        _log(
            f"activate_mode step=palette_typed mode={self.perplexity_mode} "
            f"chars={len(slash_cmd)} elapsed_ms={int((time.perf_counter() - t0) * 1000)}"
        )

        # Press the Perplexity commit key (Space — see PERPLEXITY_COMMIT_KEY).
        await page.keyboard.press(PERPLEXITY_COMMIT_KEY)
        await page.wait_for_timeout(1500)  # Wait for activation
        _log(
            f"activate_mode step=commit_pressed mode={self.perplexity_mode} "
            f"key={PERPLEXITY_COMMIT_KEY} "
            f"elapsed_ms={int((time.perf_counter() - t0) * 1000)}"
        )

        # Verify activation based on mode
        if self.perplexity_mode == "council":
            ok = await self._verify_council_activation(page)
        elif self.perplexity_mode == "research":
            ok = await self._verify_research_activation(page)
        elif self.perplexity_mode == "labs":
            ok = await self._verify_labs_activation(page)
        else:
            _log(
                f"activate_mode step=verify_skipped mode={self.perplexity_mode} "
                f"reason=unknown_mode"
            )
            return True

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if ok:
            _log(
                f"activate_mode SUCCESS mode={self.perplexity_mode} "
                f"key={PERPLEXITY_COMMIT_KEY} elapsed_ms={elapsed_ms}"
            )
        else:
            # Capture the current URL on failure. If it's /search/<uuid>,
            # Perplexity treated the slash as a search submit — almost
            # certainly the commit key is wrong (Perplexity UI changed).
            # If it's the homepage, the verifier just couldn't find the
            # indicator and we're in optimistic-proceed territory.
            try:
                current_url = page.url
            except Exception:
                current_url = "<unknown>"
            _log(
                f"activate_mode FAIL step=verify mode={self.perplexity_mode} "
                f"key={PERPLEXITY_COMMIT_KEY} elapsed_ms={elapsed_ms} "
                f"url={current_url}"
            )
        return ok

    async def _verify_council_activation(self, page) -> bool:
        """Verify Council mode activated via 2-tier selector cascade.

        Tier 1: stable aria-label selector for the '3 models' dropdown.
        Tier 2: text-scan for 'Model council' (tolerates DOM drift).
        Both miss → SELECTOR_DRIFT_DETECTED, return False (was: optimistic True).
        """
        # Tier 0 (2026-07-22): aria-label + aria-pressed on the icon-only mode
        # button (same Perplexity redesign that broke the research verifier).
        try:
            aria_found = await page.evaluate("""() => {
                const els = document.querySelectorAll('[aria-label]');
                for (const el of els) {
                    const label = (el.getAttribute('aria-label') || '').trim().toLowerCase();
                    if ((label === 'model council' || label === 'council')
                        && el.getAttribute('aria-pressed') === 'true') return true;
                }
                return false;
            }""")
            if aria_found:
                _log("activate_mode verify=OK mode=council indicator=tier0_aria_pressed")
                return True
        except Exception as _e0:
            _log(f"activate_mode verify=tier0_ERROR mode=council exception={_e0!r}")

        # Tier 1: stable aria-label selector
        try:
            three_models = self.selectors.get("threeModelsDropdown", "button[aria-label='3 models']")
            await page.wait_for_selector(three_models, timeout=5000)
            _log("activate_mode verify=OK mode=council indicator=tier1_aria_label")
            return True
        except Exception as e:
            _log(f"activate_mode verify=tier1_MISS mode=council exception={e!r}")

        # Tier 2: fallback — text scan for 'Model council'
        try:
            council_text = await page.evaluate(
                "!!document.querySelector('button')?.textContent?.includes('Model council')"
            )
            if council_text:
                _log("activate_mode verify=OK mode=council indicator=tier2_text_scan")
                return True
        except Exception as e:
            _log(f"activate_mode verify=tier2_ERROR mode=council exception={e!r}")

        # Both tiers missed — selector drift is the most likely cause if
        # this fires repeatedly. The structured log line is the signal.
        _log("activate_mode verify=FAIL mode=council indicator=SELECTOR_DRIFT_DETECTED")
        return False

    async def _verify_research_activation(self, page) -> bool:
        """Verify Research mode activated via 2-tier selector cascade.

        Tier 0 (2026-07-22): aria-label + aria-pressed on the toolbar mode
        button. Perplexity moved the indicator from a TEXT pill to an
        ICON-ONLY button (aria-label="Deep research", aria-pressed="true")
        with EMPTY textContent, so the tier1/tier2 text scans below can no
        longer see it and falsely report SELECTOR_DRIFT. Verified via live DOM
        probe. aria-pressed is the reliable active-state discriminator.
        Tier 1: exact-text match for the activated mode pill ("Deep research"
        or "Research" exactly). Catches the canonical activated state.
        Tier 2: case-insensitive contains scan for 'deep research' or
        exact 'research'. Tolerates minor Perplexity UI tweaks.
        All miss → SELECTOR_DRIFT_DETECTED, return False (was: optimistic True).
        """
        # Tier 0: aria-label + aria-pressed on the icon-only mode button
        try:
            aria_found = await page.evaluate("""() => {
                const els = document.querySelectorAll('[aria-label]');
                for (const el of els) {
                    const label = (el.getAttribute('aria-label') || '').trim().toLowerCase();
                    if ((label === 'deep research' || label === 'research')
                        && el.getAttribute('aria-pressed') === 'true') return true;
                }
                return false;
            }""")
            if aria_found:
                _log("activate_mode verify=OK mode=research indicator=tier0_aria_pressed")
                return True
        except Exception as e:
            _log(f"activate_mode verify=tier0_ERROR mode=research exception={e!r}")

        # Tier 1: exact-text match on the toolbar mode pill
        try:
            primary_found = await page.evaluate("""() => {
                const candidates = document.querySelectorAll('button, [role="button"], div[data-state]');
                for (const el of candidates) {
                    const text = (el.textContent || '').trim();
                    if (text === 'Deep research' || text === 'Research') return true;
                }
                return false;
            }""")
            if primary_found:
                _log("activate_mode verify=OK mode=research indicator=tier1_exact_match")
                return True
        except Exception as e:
            _log(f"activate_mode verify=tier1_ERROR mode=research exception={e!r}")

        # Tier 2: looser case-insensitive contains-scan as DOM-drift fallback
        try:
            fallback_found = await page.evaluate("""() => {
                const candidates = document.querySelectorAll('button, [role="button"], div[data-state], span[class*="pill"], span[class*="badge"]');
                for (const el of candidates) {
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (text.includes('deep research') || text === 'research') return true;
                }
                return false;
            }""")
            if fallback_found:
                _log("activate_mode verify=OK mode=research indicator=tier2_contains_match")
                return True
        except Exception as e:
            _log(f"activate_mode verify=tier2_ERROR mode=research exception={e!r}")

        # Both tiers missed — selector drift is the most likely cause if
        # this fires repeatedly.
        _log("activate_mode verify=FAIL mode=research indicator=SELECTOR_DRIFT_DETECTED")
        return False

    async def _verify_labs_activation(self, page) -> bool:
        """Verify Labs mode activated via 2-tier selector cascade.

        Tier 0 (2026-07-22): aria-label + aria-pressed on the icon-only mode
        button (same Perplexity redesign that broke the research verifier).
        Tier 1: exact-text 'Labs' on a toolbar pill.
        Tier 2: case-insensitive contains 'labs' (looser fallback).
        All miss → SELECTOR_DRIFT_DETECTED, return False (was: optimistic True).
        """
        # Tier 0: aria-label + aria-pressed on the icon-only mode button
        try:
            aria_found = await page.evaluate("""() => {
                const els = document.querySelectorAll('[aria-label]');
                for (const el of els) {
                    const label = (el.getAttribute('aria-label') || '').trim().toLowerCase();
                    if (label === 'labs' && el.getAttribute('aria-pressed') === 'true') return true;
                }
                return false;
            }""")
            if aria_found:
                _log("activate_mode verify=OK mode=labs indicator=tier0_aria_pressed")
                return True
        except Exception as e:
            _log(f"activate_mode verify=tier0_ERROR mode=labs exception={e!r}")

        # Tier 1: exact-text match on the toolbar mode pill
        try:
            primary_found = await page.evaluate("""() => {
                const candidates = document.querySelectorAll('button, [role="button"], div[data-state]');
                for (const el of candidates) {
                    const text = (el.textContent || '').trim();
                    if (text === 'Labs') return true;
                }
                return false;
            }""")
            if primary_found:
                _log("activate_mode verify=OK mode=labs indicator=tier1_exact_match")
                return True
        except Exception as e:
            _log(f"activate_mode verify=tier1_ERROR mode=labs exception={e!r}")

        # Tier 2: case-insensitive contains
        try:
            fallback_found = await page.evaluate("""() => {
                const candidates = document.querySelectorAll('button, [role="button"], div[data-state], span[class*="pill"], span[class*="badge"]');
                for (const el of candidates) {
                    const text = (el.textContent || '').trim().toLowerCase();
                    if (text.includes('labs')) return true;
                }
                return false;
            }""")
            if fallback_found:
                _log("activate_mode verify=OK mode=labs indicator=tier2_contains_match")
                return True
        except Exception as e:
            _log(f"activate_mode verify=tier2_ERROR mode=labs exception={e!r}")

        _log("activate_mode verify=FAIL mode=labs indicator=SELECTOR_DRIFT_DETECTED")
        return False

    async def _detect_dom_completion(self, page) -> dict:
        """Check Perplexity DOM for completion signals (research/labs modes)."""
        return await page.evaluate("""() => {
            // Signal 1: No streaming/loading indicators (includes Perplexity-specific selectors)
            const streaming = document.querySelectorAll(
                '[class*="streaming"], [class*="loading"], [class*="generating"], '
                + '[class*="animate-pulse"], [class*="animate-spin"], '
                + '[class*="cursor"], [class*="typing"], [class*="progress"], '
                + '.animate-blink, [data-testid*="loading"]'
            );

            // Signal 2: Sources/citations section visible
            const sources = document.querySelector(
                '[class*="source"], [class*="citation"], [data-testid*="source"]'
            );

            // Signal 3: Share/copy/rewrite action buttons (appear after completion)
            const actions = document.querySelectorAll(
                'button[aria-label*="Share"], button[aria-label*="Copy"], '
                + 'button[aria-label*="Rewrite"]'
            );

            // Signal 4: Follow-up input re-enabled
            const followUp = document.querySelector(
                'textarea:not([disabled]), #ask-input'
            );

            // Signal 5: "Related" section at bottom
            const related = document.querySelector(
                '[class*="related"], [data-testid*="related"]'
            );

            // Signal 6: Stop/Cancel button present = still generating
            const stopBtn = document.querySelector(
                'button[aria-label*="Stop"], button[aria-label*="Cancel"], '
                + 'button[class*="stop"], [data-testid*="stop"]'
            );

            return {
                isStreaming: streaming.length > 0,
                hasSources: !!sources,
                hasActionButtons: actions.length >= 2,
                hasFollowUp: !!followUp,
                hasRelated: !!related,
                hasStopButton: !!stopBtn,
            };
        }""")

    async def _get_text_length(self, page) -> int:
        """Get current text length of the main response element."""
        try:
            return await page.evaluate("""() => {
                const report = document.querySelector('div.prose.max-w-none');
                if (report) return report.innerText.length;
                const proses = Array.from(document.querySelectorAll('div.prose'));
                if (proses.length === 0) return 0;
                proses.sort((a, b) => b.innerText.length - a.innerText.length);
                return proses[0].innerText.length;
            }""")
        except Exception:
            return 0

    async def activate_council(self, page) -> bool:
        """Activate council mode. Delegates to activate_mode()."""
        return await self.activate_mode(page)

    async def submit_query(self, page, query: str) -> None:
        """Type and submit the query.

        Mode activation (/research, /council, /labs) is already completed
        before this method is called, so the mode is locked in. Native
        setter (fast paste) is safe here — it sets the query text without
        affecting the already-activated mode.
        """
        textarea = self.selectors.get("textarea", "#ask-input")

        # Try native setter first (preserves newlines), fall back to page.fill()
        try:
            filled = await page.evaluate(
                """([sel, text]) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    // Try textarea/input native setter (React-compatible)
                    const proto = el.tagName === 'TEXTAREA'
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) {
                        setter.call(el, text);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        return true;
                    }
                    return false;
                }""",
                [textarea, query],
            )
            if not filled:
                raise ValueError("Native setter failed")
        except Exception:
            _log("Native setter unavailable, using page.fill()")
            await page.fill(textarea, query)
        await page.wait_for_timeout(500)

        # Submit via Enter
        await page.keyboard.press("Enter")
        _log(f"Query submitted ({len(query)} chars)")

        # Wait for response to start appearing
        response_sel = self.selectors.get("responseContainer", ".prose")
        try:
            await page.wait_for_selector(response_sel, timeout=30000)
            _log("Response generation started")
        except Exception:
            _log("WARNING: Response container not detected within 30s")

    async def _analyze_screenshot(self, screenshot_bytes: bytes) -> dict:
        """Send screenshot to Claude Haiku for page state analysis.

        Returns dict with:
            models_completed: int (0-3)
            synthesis_visible: bool
            loading_active: bool
            page_state: "loading" | "generating" | "synthesizing" | "complete" | "error"
            error_text: str (empty if no error)
        """
        import anthropic

        b64 = base64.b64encode(screenshot_bytes).decode()

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

        response = client.messages.create(
            model=VISION_MODEL,
            max_tokens=VISION_MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Analyze this Perplexity AI council query page screenshot. "
                            "Return ONLY valid JSON (no markdown, no explanation):\n"
                            '{"models_completed":<0-3>,"synthesis_visible":<bool>,'
                            '"loading_active":<bool>,"page_state":"<state>",'
                            '"error_text":"<text or empty>"}\n\n'
                            "IMPORTANT: Perplexity council has TWO phases:\n"
                            "Phase 1: Individual model responses (shown as expandable rows with checkmarks)\n"
                            "Phase 2: A SEPARATE synthesis/summary section BELOW the model rows. "
                            "This is the main response text that streams AFTER all models finish.\n\n"
                            "page_state values:\n"
                            '- "loading": page is loading, no model responses yet\n'
                            '- "generating": models are actively generating (streaming text, spinners, pulsing)\n'
                            '- "synthesizing": all 3 models have checkmarks BUT the synthesis text below '
                            "is still streaming (text is appearing, cursor/caret visible, content growing)\n"
                            '- "complete": synthesis text is FULLY rendered AND sources/citations section '
                            "is visible at the very bottom of the page. No streaming, no pulsing, no loading.\n"
                            '- "error": error message, red/orange banner, or "try again" button visible\n\n'
                            "CRITICAL: Do NOT report 'complete' just because 3 model checkmarks are visible. "
                            "The synthesis section below must ALSO be fully done with sources visible at bottom."
                        ),
                    },
                ],
            }],
            timeout=15,
        )

        text = response.content[0].text.strip()
        # Strip markdown code fences if Haiku wraps in ```json
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        return json.loads(text)

    # --- Smart Completion Detection methods (Phase 1-2, 5) ---

    async def _stop_button_present(self, page) -> bool:
        """True if Perplexity's generation stop/cancel button is currently in the
        DOM (a response is still streaming). Reuses the exact selector used by
        _wait_for_stop_button_cycle. Fail-open: returns False on probe error so a
        transient evaluate failure can't deadlock a caller's wait loop.
        """
        stop_selectors = (
            'button[aria-label*="Stop"], button[aria-label*="Cancel"], '
            '[data-testid*="stop"], button:has(svg circle[stroke-dasharray]), '
            'button[class*="stop"]'
        )
        try:
            return bool(await page.evaluate(
                f"() => !!document.querySelector('{stop_selectors}')"
            ))
        except Exception:
            return False

    async def _wait_for_stop_button_cycle(self, page, timeout: int, start: float) -> bool:
        """Wait for stop button to appear then disappear (with debounce).

        Returns True if the stop button completed a full cycle (appeared → disappeared).
        Returns False if the stop button never appeared within 30s.
        """
        stop_selectors = (
            'button[aria-label*="Stop"], button[aria-label*="Cancel"], '
            '[data-testid*="stop"], button:has(svg circle[stroke-dasharray]), '
            'button[class*="stop"]'
        )
        poll_s = BROWSER_STOP_BUTTON_POLL_MS / 1000
        debounce_s = BROWSER_STOP_BUTTON_DEBOUNCE_MS / 1000

        # Phase 1: Wait for stop button to appear (confirms generation started)
        _log("Smart: waiting for stop button to appear...")
        appear_deadline = start + 30  # 30s to detect stop button
        appeared = False
        while time.time() < appear_deadline and (time.time() - start) * 1000 < timeout:
            try:
                has_stop = await page.evaluate(f"""() => {{
                    return !!document.querySelector('{stop_selectors}');
                }}""")
                if has_stop:
                    appeared = True
                    _log(f"Smart: stop button appeared ({time.time() - start:.1f}s)")
                    break
            except Exception:
                pass
            await asyncio.sleep(poll_s)

        if not appeared:
            _log("Smart: stop button never appeared (30s), falling back")
            return False

        # Phase 2: Wait for stop button to disappear
        _log("Smart: waiting for stop button to disappear...")
        while (time.time() - start) * 1000 < timeout:
            try:
                has_stop = await page.evaluate(f"""() => {{
                    return !!document.querySelector('{stop_selectors}');
                }}""")
                if not has_stop:
                    _log(f"Smart: stop button disappeared ({time.time() - start:.1f}s), debouncing {debounce_s}s...")
                    # Debounce: re-check after delay to handle inter-section flickers
                    await asyncio.sleep(debounce_s)
                    try:
                        reappeared = await page.evaluate(f"""() => {{
                            return !!document.querySelector('{stop_selectors}');
                        }}""")
                    except Exception:
                        reappeared = False
                    if reappeared:
                        _log("Smart: stop button reappeared during debounce, re-entering wait loop")
                        continue
                    _log(f"Smart: stop button confirmed gone ({time.time() - start:.1f}s)")
                    return True
            except Exception:
                pass
            await asyncio.sleep(poll_s)

        _log(f"Smart: timed out waiting for stop button to disappear ({time.time() - start:.1f}s)")
        return False

    async def _inject_mutation_observer(self, page) -> None:
        """Inject a MutationObserver on the .prose content area.

        Tracks window.__mutationState = { lastMutationTime, isStable, stableForMs }.
        Stability = BROWSER_MUTATION_STABILITY_MS of zero mutations.
        """
        stability_ms = BROWSER_MUTATION_STABILITY_MS
        await page.evaluate(f"""() => {{
            window.__mutationState = {{
                lastMutationTime: Date.now(),
                isStable: false,
                stableForMs: 0,
            }};
            const target = document.querySelector('.prose') ||
                           document.querySelector('div.prose.max-w-none') ||
                           document.body;
            const observer = new MutationObserver((mutations) => {{
                if (mutations.length > 0) {{
                    window.__mutationState.lastMutationTime = Date.now();
                    window.__mutationState.isStable = false;
                    window.__mutationState.stableForMs = 0;
                }}
            }});
            observer.observe(target, {{
                childList: true,
                characterData: true,
                subtree: true,
            }});
            // Periodic stability check
            setInterval(() => {{
                const elapsed = Date.now() - window.__mutationState.lastMutationTime;
                window.__mutationState.stableForMs = elapsed;
                window.__mutationState.isStable = elapsed >= {stability_ms};
            }}, 500);
        }}""")
        _log("Smart: MutationObserver injected on .prose content area")

    async def _check_mutation_stability(self, page) -> bool:
        """Check if the MutationObserver reports stable (no mutations for threshold)."""
        try:
            state = await page.evaluate("() => window.__mutationState || {}")
            return bool(state.get("isStable", False))
        except Exception:
            return False

    async def _check_for_error_state(self, page) -> bool:
        """Check for error indicators after stop button disappears.

        Returns True if an error was detected.
        """
        try:
            error = await page.evaluate("""() => {
                // Check for error text
                const body = document.body.innerText || '';
                const errorPatterns = [
                    'Something went wrong',
                    'Rate limit',
                    'Error generating',
                    'An error occurred',
                    'Please try again',
                ];
                for (const pattern of errorPatterns) {
                    if (body.includes(pattern)) return pattern;
                }
                // Check for error-styled elements
                const errorEl = document.querySelector('[class*="error"]');
                if (errorEl && errorEl.textContent.trim().length > 5) {
                    return errorEl.textContent.trim().substring(0, 100);
                }
                return null;
            }""")
            if error:
                _log(f"Smart: error state detected: {error}")
                return True
        except Exception:
            pass
        return False

    async def _wait_content_stability(
        self,
        page,
        stable_s: int,
        min_chars: int,
        max_s: int,
        start: float,
        label: str = "stability",
    ) -> str:
        """Content-stability completion poll for Perplexity research/labs mode.

        Polls div.prose.max-w-none every 500ms. Declares STABLE when content
        length is >= min_chars AND has not changed for stable_s consecutive
        seconds. Declares NO_GROWTH if no content appears within the first
        30s (caller should fall back to stop-button detection). Declares
        GROWING if max_s elapsed while content is still changing — caller
        should switch to a slower detector with a longer ceiling.

        Returns one of: "STABLE" | "GROWING" | "NO_GROWTH".
        2026-05-22 Q5 followup: catches ultra-short research-mode queries
        whose answer is rendered before the stop button becomes visible.
        """
        import hashlib as _hashlib
        # PATCH 2026-05-22 (Perplexity DOM drift): research mode now renders the
        # answer in `div.prose` (no `.max-w-none` modifier). The old selector
        # was matching a small loading-skeleton placeholder div with ~3 chars
        # and never falling through to the longer real-content div. Fix: always
        # pick the LONGEST .prose element — robust to skeleton placeholders +
        # multiple prose blocks (intro summary + main answer).
        POLL_INTERVAL_S = 0.5
        NO_GROWTH_GRACE_S = 30  # if zero content by this point, give up to caller

        last_len = 0
        last_hash = ""
        stable_since: float | None = None
        stop_absent_since: float | None = None  # debounce clock for stop-button-gone
        deadline = start + max_s
        no_growth_deadline = start + NO_GROWTH_GRACE_S

        while time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                text = await page.evaluate("""
                    () => {
                        // 1. Longest .prose element — handles long-form research answers,
                        //    DECOMPOSE-style JSON outputs, etc. (Perplexity DOM as of 2026-05-22)
                        const proses = document.querySelectorAll('div.prose, .prose');
                        let best = '';
                        for (const e of proses) {
                            const t = e.innerText || '';
                            if (t.length > best.length) best = t;
                        }
                        if (best.length > 50) return best;

                        // 2. Fallback: <main> element with UI chrome stripped.
                        //    Short research answers (e.g. "What is 300+300?" → "600") render
                        //    inside <main> but NOT inside any .prose div. Strip the static
                        //    tab labels and footer controls that appear around the answer.
                        const main = document.querySelector('main');
                        if (!main) return best;
                        const raw = main.innerText || '';
                        const chrome = new Set([
                            'Answer', 'Links', 'Images', 'Share', 'Sources',
                            'Ask a follow-up', 'Search', 'Model', 'Steps', 'Tasks',
                        ]);
                        const cleaned = raw
                            .split('\\n')
                            .map(s => s.trim())
                            .filter(s => s && !chrome.has(s))
                            .join('\\n');
                        return cleaned.length > best.length ? cleaned : best;
                    }
                """)
            except Exception:
                text = ""

            cur_len = len(text or "")
            cur_hash = _hashlib.md5((text or "").encode("utf-8", errors="replace")).hexdigest()
            # Track the running peak .prose length for the result-validation
            # length-regression check (truncation = final << peak).
            if cur_len > getattr(self, "_peak_prose_chars", 0):
                self._peak_prose_chars = cur_len

            # NO_GROWTH path: nothing rendered after the grace window → bail to caller.
            if cur_len == 0 and time.time() > no_growth_deadline:
                _log(f"Smart[{label}]: no content growth in {NO_GROWTH_GRACE_S}s — yielding to caller")
                return "NO_GROWTH"

            if cur_len != last_len or cur_hash != last_hash:
                # Content changed — stream resumed; reset BOTH windows.
                stable_since = None
                stop_absent_since = None
                last_len = cur_len
                last_hash = cur_hash
            else:
                # Content unchanged this tick.
                if cur_len >= min_chars:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= stable_s:
                        # 2026-06-15 truncation guard: "unchanged for stable_s" can be a
                        # mid-stream PAUSE (Perplexity /research streams in chunks with
                        # gaps), which previously declared STABLE and extracted a
                        # truncated ~210-char fragment. Only complete when the generation
                        # stop button has been ABSENT for a debounce window — content
                        # stability alone doesn't cover an inter-section flicker where the
                        # button briefly disappears between streamed sections. (Mirrors
                        # _wait_for_stop_button_cycle's debounce.) Ultra-short queries that
                        # never render a stop button still complete after the debounce.
                        if await self._stop_button_present(page):
                            stop_absent_since = None  # still streaming — keep waiting
                            continue
                        if stop_absent_since is None:
                            stop_absent_since = time.time()  # start debounce clock
                            continue
                        if (time.time() - stop_absent_since) >= (BROWSER_STOP_BUTTON_DEBOUNCE_MS / 1000):
                            _log(f"Smart[{label}]: stable {stable_s}s @ {cur_len} chars, "
                                 f"stop button absent (debounced) — complete")
                            return "STABLE"

        _log(f"Smart[{label}]: max_s={max_s} reached, content still growing or below threshold ({last_len} chars) — yielding to caller")
        return "GROWING"

    async def _wait_research_smart(self, page, timeout: int, start: float) -> bool:
        """Smart completion detection for research/labs modes.

        Signal hierarchy (revised 2026-05-22):
        0. NEW Fast-path: content-stability poll of div.prose.max-w-none. Catches
           ultra-short queries (e.g. "What is 2+2?") that resolve in <10s and
           never render a visible stop button. ~60s ceiling. If content keeps
           growing past the threshold, falls through to (1).
        1. Primary: stop button cycle (appeared → disappeared with debounce)
        2. Confirming: MutationObserver stability OR text stability (10s window)
        3. Fallback: existing _wait_research_fallback() with reduced guards
        """
        min_gen_s = BROWSER_MIN_GENERATION_TIME_MS / 1000
        confirm_s = BROWSER_CONFIRMATION_WINDOW_MS / 1000
        text_stable_s = 8  # seconds of unchanged text for confirmation

        # Inject MutationObserver early
        await self._inject_mutation_observer(page)

        # NEW (2026-05-22 Q5 followup): content-stability fast-path for short queries.
        # Tries SIMPLE profile (3s stable / 10+ chars / 60s ceiling). If content
        # grows AND stabilizes inside that window, return immediately. If content
        # is still growing at 60s, fall through to the existing detector (handles
        # deep research). If zero growth seen, also fall through.
        fast_path = await self._wait_content_stability(
            page, stable_s=3, min_chars=10, max_s=60, start=start, label="fast-path"
        )
        if fast_path == "STABLE":
            # 2026-05-27 synthesis-mount guard: stability alone is insufficient.
            # Perplexity can leave a partial-mount shell stable at <200 chars
            # while only the reasoning trail rendered (the synthesis body never
            # mounts on large-artifact queries). Wait briefly for real content
            # to appear before declaring complete.
            try:
                # Mount-readiness gate: wait for real synthesis (>200 chars) in
                # .prose before declaring complete. Kept at 20s — a controlled A/B
                # smoke (2026-06-15) DISPROVED the "needs more time" hypothesis:
                # the empty-synthesis failures correlate with SESSION DEGRADATION
                # (short pplx.session-id TTL), not a client-side skeleton race
                # (identical query → empty on a 2.6min-TTL session, correct on a
                # 9.9min one). Extending the wait only added latency to legitimate
                # short answers (<200 chars never satisfy the gate). The real B fix
                # is session freshness (keeper / pre-query TTL guard), not time here.
                await page.wait_for_function(
                    "() => (document.querySelector('.prose')?.innerText?.length ?? 0) > 200",
                    timeout=20000,
                )
                _log("Smart: synthesis content mounted (>200 chars in .prose); complete")
                if hasattr(self, "_query_inst"):
                    self._query_inst["synthesis_mount_wait_outcome"] = "success"
            except Exception:
                # NOTE: also fires for legitimate short answers (<200 chars), so
                # "timeout" here is NOT a clean failure signal — the authoritative
                # empty signal is extracted_synthesis_chars==0 / exit_reason==empty_synthesis.
                _log("Smart: .prose stable but never exceeded 200 chars in 20s — short/reasoning-only/empty; proceeding to extraction")
                if hasattr(self, "_query_inst"):
                    self._query_inst["synthesis_mount_wait_outcome"] = "timeout"
            return True
        # else GROWING or NO_GROWTH → fall through to stop-button cycle

        # Primary signal: stop button cycle
        stop_cycle = await self._wait_for_stop_button_cycle(page, timeout, start)

        if not stop_cycle:
            # Stop button never appeared — fall through to existing fallback
            _log("Smart: no stop button detected, using fallback completion detection")
            return await self._wait_research_fallback(page, timeout, start)

        elapsed = time.time() - start

        # Check for error state after stop button disappears
        if await self._check_for_error_state(page):
            _log("Smart: error detected after stop button disappeared")
            return False

        # Suspiciously fast? Wait for confirming signal
        if elapsed < min_gen_s:
            _log(f"Smart: stop button gone at {elapsed:.1f}s (< {min_gen_s}s), waiting for confirmation...")
            confirm_start = time.time()
            text_snapshot = await self._get_text_length(page)
            text_stable_since = time.time()

            while (time.time() - confirm_start) < confirm_s:
                # Check mutation stability
                if await self._check_mutation_stability(page):
                    _log(f"Smart: confirmed via MutationObserver stability ({time.time() - start:.1f}s)")
                    return True
                # Check text stability
                current_len = await self._get_text_length(page)
                if current_len != text_snapshot:
                    text_snapshot = current_len
                    text_stable_since = time.time()
                elif (time.time() - text_stable_since) >= text_stable_s:
                    _log(f"Smart: confirmed via text stability ({text_stable_s}s, {time.time() - start:.1f}s)")
                    return True
                await asyncio.sleep(1)

            _log(f"Smart: no confirming signal in {confirm_s}s, falling back to CSS detection")
            return await self._wait_research_fallback(page, timeout, start)

        # Normal timing — brief confirmation phase (10s max)
        _log(f"Smart: stop button gone at {elapsed:.1f}s, running brief confirmation...")
        confirm_start = time.time()
        text_snapshot = await self._get_text_length(page)
        text_stable_since = time.time()

        while (time.time() - confirm_start) < confirm_s:
            # Check mutation stability
            if await self._check_mutation_stability(page):
                _log(f"Smart: confirmed via MutationObserver stability ({time.time() - start:.1f}s)")
                return True
            # Check text stability
            current_len = await self._get_text_length(page)
            if current_len != text_snapshot:
                text_snapshot = current_len
                text_stable_since = time.time()
            elif (time.time() - text_stable_since) >= text_stable_s:
                _log(f"Smart: confirmed via text stability ({text_stable_s}s, {time.time() - start:.1f}s)")
                return True
            await asyncio.sleep(1)

        # Confirmation window expired but stop button is still gone — trust it
        _log(f"Smart: confirmation window expired, trusting stop button signal ({time.time() - start:.1f}s)")
        return True

    async def wait_for_completion(self, page, timeout: int | None = None) -> bool:
        """Wait for all model responses and synthesis to complete.

        Research/labs: Smart detection (stop button + multi-signal confirmation).
        Council: Vision-based detection via Haiku screenshot analysis.
        Fallback: CSS selector + stability polling (when ANTHROPIC_API_KEY not set).
        """
        timeout = timeout or self.timeout
        start = time.time()

        # Research/labs: always use smart detection (stop button + multi-signal)
        # regardless of vision availability. Vision is deprecated for research/labs.
        if self.perplexity_mode in ("research", "labs"):
            return await self._wait_research_smart(page, timeout, start)

        # Council mode: vision-based or CSS fallback
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        use_vision = bool(api_key) and VISION_ENABLED

        if use_vision:
            return await self._wait_vision(page, timeout, start)
        else:
            _log("Vision monitoring unavailable (no ANTHROPIC_API_KEY), using CSS fallback")
            return await self._wait_css_fallback(page, timeout, start)

    async def _wait_vision(self, page, timeout: int, start: float) -> bool:
        """Vision-based completion detection using Haiku screenshots.

        Enforces state machine: generating -> synthesizing -> complete.
        Requires seeing 'synthesizing' before trusting 'complete', and
        requires 2 consecutive 'complete' polls for confidence.
        """
        # Note: research/labs now use _wait_research_smart() (routed in wait_for_completion)
        # This method is only called for council mode.

        poll_interval = VISION_POLL_INTERVAL_MODELS
        all_models_done = False
        seen_synthesizing = False
        consecutive_complete = 0

        _log("Vision monitoring: polling with Haiku screenshot analysis...")

        while (time.time() - start) * 1000 < timeout:
            try:
                screenshot = await page.screenshot(type="jpeg", quality=VISION_JPEG_QUALITY)
                state = await self._analyze_screenshot(screenshot)

                models_done = state.get("models_completed", 0)
                page_state = state.get("page_state", "unknown")
                _log(f"  Vision: {models_done}/3 models, state={page_state}")

                if page_state == "error":
                    error = state.get("error_text", "unknown error")
                    _log(f"Vision: error detected: {error}")
                    return False

                if page_state == "synthesizing":
                    seen_synthesizing = True
                    consecutive_complete = 0
                    if not all_models_done:
                        all_models_done = True
                        poll_interval = VISION_POLL_INTERVAL_SYNTHESIS
                        _log("  Synthesis phase detected, switching to faster polling")

                if page_state == "complete":
                    if not seen_synthesizing:
                        # Haiku likely confused "3 checkmarks" with "complete"
                        # Force at least one synthesizing cycle
                        _log("  Vision reported 'complete' but no synthesizing seen yet — treating as synthesizing")
                        seen_synthesizing = True
                        if not all_models_done:
                            all_models_done = True
                            poll_interval = VISION_POLL_INTERVAL_SYNTHESIS
                    else:
                        consecutive_complete += 1
                        if consecutive_complete >= 2:
                            _log(f"Vision: page complete (confirmed 2x) ({time.time() - start:.1f}s)")
                            return True
                        _log(f"  Vision: complete (need 1 more confirmation)")
                else:
                    consecutive_complete = 0

                # Switch to faster polling once all models done
                if models_done >= 3 and not all_models_done:
                    all_models_done = True
                    poll_interval = VISION_POLL_INTERVAL_SYNTHESIS
                    _log("  All models done, switching to faster polling")

            except json.JSONDecodeError as e:
                _log(f"  Vision: failed to parse Haiku response: {e}")
            except Exception as e:
                _log(f"  Vision: analysis error: {e}")

            await asyncio.sleep(poll_interval)

        _log(f"Vision: timed out after {time.time() - start:.1f}s")
        return False

    async def _analyze_research_screenshot(self, screenshot_bytes: bytes) -> dict:
        """Send screenshot to Claude Haiku for research/labs page state analysis."""
        import anthropic

        b64 = base64.b64encode(screenshot_bytes).decode()

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

        response = client.messages.create(
            model=VISION_MODEL,
            max_tokens=VISION_MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Analyze this Perplexity research/labs page screenshot. "
                            "Return ONLY valid JSON (no markdown, no explanation):\n"
                            '{"page_state":"<state>","loading_active":<bool>,'
                            '"error_text":"<text or empty>"}\n\n'
                            "page_state values:\n"
                            '- "loading": page is loading, no response yet\n'
                            '- "generating": response is actively streaming '
                            "(text appearing, cursor visible, content growing)\n"
                            '- "complete": response is FULLY rendered AND '
                            "sources/citations visible at bottom. No streaming.\n"
                            '- "error": error message visible\n\n'
                            "CRITICAL: Do NOT report 'complete' if text is still "
                            "appearing or growing."
                        ),
                    },
                ],
            }],
            timeout=15,
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        return json.loads(text)

    async def _wait_vision_research(self, page, timeout: int, start: float) -> bool:
        """Vision-based completion detection for research/labs modes.

        Simplified state machine: generating -> complete (no model checkmarks).
        Requires 2 consecutive 'complete' polls for confidence.
        """
        poll_interval = VISION_POLL_INTERVAL_MODELS
        seen_generating = False
        consecutive_complete = 0

        _log("Vision monitoring (research/labs): polling with Haiku screenshot analysis...")

        while (time.time() - start) * 1000 < timeout:
            try:
                screenshot = await page.screenshot(type="jpeg", quality=VISION_JPEG_QUALITY)
                state = await self._analyze_research_screenshot(screenshot)

                page_state = state.get("page_state", "unknown")
                _log(f"  Vision (research): state={page_state}")

                if page_state == "error":
                    error = state.get("error_text", "unknown error")
                    _log(f"Vision: error detected: {error}")
                    return False

                if page_state == "generating":
                    seen_generating = True
                    consecutive_complete = 0
                    poll_interval = VISION_POLL_INTERVAL_SYNTHESIS

                if page_state == "complete":
                    if not seen_generating:
                        _log("  Vision reported 'complete' but no generating seen yet — treating as generating")
                        seen_generating = True
                        poll_interval = VISION_POLL_INTERVAL_SYNTHESIS
                    else:
                        consecutive_complete += 1
                        if consecutive_complete >= 2:
                            elapsed = time.time() - start
                            min_elapsed = BROWSER_DOM_MIN_ELAPSED_LABS / 1000 if self.perplexity_mode == "labs" else BROWSER_DOM_MIN_ELAPSED_RESEARCH / 1000
                            if elapsed < min_elapsed:
                                _log(f"  Vision: ignoring early complete ({elapsed:.0f}s < {min_elapsed:.0f}s min)")
                                consecutive_complete = 0
                            else:
                                _log(f"Vision (research): page complete (confirmed 2x) ({elapsed:.1f}s)")
                                return True
                        else:
                            _log("  Vision (research): complete (need 1 more confirmation)")
                else:
                    consecutive_complete = 0

            except json.JSONDecodeError as e:
                _log(f"  Vision: failed to parse Haiku response: {e}")
            except Exception as e:
                _log(f"  Vision: analysis error: {e}")

            await asyncio.sleep(poll_interval)

        _log(f"Vision (research): timed out after {time.time() - start:.1f}s")
        return False

    async def _wait_css_fallback(self, page, timeout: int, start: float) -> bool:
        """CSS selector + stability fallback (original implementation)."""
        # Phase A: Wait for model completion indicators
        completion_sel = self.selectors.get(
            "councilCompletedIndicator", "[class*='Completed'], svg[class*='check']"
        )
        _log("Phase A: Waiting for model completions...")

        phase_a_timeout = min(90000, timeout)
        try:
            await page.wait_for_function(
                f"""() => {{
                    const indicators = document.querySelectorAll("{completion_sel}");
                    return indicators.length >= 3;
                }}""",
                timeout=phase_a_timeout,
            )
            _log(f"Phase A complete: all models finished ({time.time() - start:.1f}s)")
        except Exception:
            try:
                count = await page.evaluate(
                    f'document.querySelectorAll("{completion_sel}").length'
                )
                _log(f"Phase A timeout: {count}/3 models completed, proceeding to Phase B")
            except Exception:
                _log("Phase A timeout: couldn't check completion count, proceeding")

        # Phase B: Wait for synthesis stability
        synthesis_sel = self.selectors.get("councilSynthesis", ".prose:first-of-type")
        _log("Phase B: Waiting for synthesis stability...")

        remaining = timeout - int((time.time() - start) * 1000)
        if remaining < 5000:
            _log("WARNING: Very little time remaining for stability check")
            remaining = 10000

        last_content = ""
        stable_since = time.time()
        poll_interval = BROWSER_POLL_INTERVAL / 1000
        stable_threshold = BROWSER_STABLE_MS / 1000

        while (time.time() - start) * 1000 < timeout:
            try:
                current = await page.evaluate(
                    f'document.querySelector("{synthesis_sel}")?.textContent || ""'
                )
                if current and current == last_content:
                    if time.time() - stable_since >= stable_threshold:
                        _log(f"Phase B complete: synthesis stable for {stable_threshold}s ({time.time() - start:.1f}s total)")
                        return True
                else:
                    last_content = current
                    stable_since = time.time()
            except Exception:
                pass

            await asyncio.sleep(poll_interval)

        _log(f"Completion wait timed out after {time.time() - start:.1f}s")
        return False

    async def _wait_research_fallback(self, page, timeout: int, start: float) -> bool:
        """Completion detection for research/labs modes (no model cards).

        Unlike council CSS fallback, this:
        - Skips Phase A (no model checkmarks in research/labs)
        - Uses longer stability threshold (50-60s vs 8s)
        - Checks DOM signals with guards (min elapsed time + min text length + confirmation)
        - Tracks text growth to prevent false stability on pauses
        """
        # Mode-aware thresholds
        if self.perplexity_mode == "labs":
            stable_ms = BROWSER_STABLE_MS_LABS
            dom_min_elapsed = BROWSER_DOM_MIN_ELAPSED_LABS / 1000
        else:
            stable_ms = BROWSER_STABLE_MS_RESEARCH
            dom_min_elapsed = BROWSER_DOM_MIN_ELAPSED_RESEARCH / 1000
        poll_interval = BROWSER_POLL_INTERVAL_RESEARCH / 1000  # 3s
        stable_threshold = stable_ms / 1000  # 50s or 60s
        dom_min_text = BROWSER_DOM_MIN_TEXT_LENGTH
        dom_confirm_wait = BROWSER_DOM_CONFIRM_WAIT / 1000  # 10s

        last_text_len = 0
        stable_since = time.time()
        _log(f"Research/labs fallback: polling with {stable_threshold}s stability, "
             f"{dom_min_elapsed}s DOM guard, {dom_confirm_wait}s growth-polling confirm, "
             f"{dom_min_text} char minimum...")

        while (time.time() - start) * 1000 < timeout:
            elapsed = time.time() - start

            # Layer 1: DOM signals (guarded — skip early in generation)
            if elapsed >= dom_min_elapsed:
                try:
                    current_len_check = await self._get_text_length(page)
                    if current_len_check >= dom_min_text:
                        dom = await self._detect_dom_completion(page)
                        if (not dom['isStreaming'] and not dom.get('hasStopButton', False)
                                and dom['hasActionButtons'] and (dom['hasSources'] or dom['hasRelated'])):
                            # Growth-polling confirmation: check every 5s during confirm window
                            _log(f"DOM signals detected at {elapsed:.0f}s ({current_len_check} chars), "
                                 f"verifying with {dom_confirm_wait}s growth check...")
                            growth_detected = False
                            check_interval = 5  # seconds
                            checks = int(dom_confirm_wait / check_interval)
                            prev_len = current_len_check
                            for check_i in range(checks):
                                await asyncio.sleep(check_interval)
                                new_len = await self._get_text_length(page)
                                if new_len != prev_len:
                                    growth_detected = True
                                    _log(f"  Text grew during confirm check {check_i+1}/{checks}: {prev_len} → {new_len}")
                                    break
                                prev_len = new_len
                            if not growth_detected:
                                _log(f"Completion confirmed via DOM signals + {dom_confirm_wait}s growth polling "
                                     f"(sources={dom['hasSources']}, actions={dom['hasActionButtons']}, {prev_len} chars)")
                                return True
                            else:
                                _log(f"DOM signals were premature — text still growing, resetting stability timer")
                                stable_since = time.time()  # Reset stability timer
                except Exception:
                    pass

            # Layer 2: Text growth tracking
            current_len = await self._get_text_length(page)
            if current_len != last_text_len:
                last_text_len = current_len
                stable_since = time.time()  # Reset — content still growing

            # Layer 3: Stability timeout (mode-aware, requires substantial text + min elapsed)
            # Guard: don't trust stability before dom_min_elapsed — Perplexity pauses 60-120s
            # between "thinking" phases, so early stability is almost certainly a false positive.
            if (elapsed >= dom_min_elapsed
                    and current_len >= dom_min_text
                    and (time.time() - stable_since) >= stable_threshold):
                _log(f"Completion via text stability ({stable_threshold}s, {current_len} chars, {elapsed:.0f}s elapsed)")
                return True

            await asyncio.sleep(poll_interval)

        _log(f"Research/labs fallback timed out after {time.time() - start:.1f}s")
        return False

    async def _find_model_cards(self, page) -> list:
        """Find the 3 model card elements using JS evaluation (more reliable than CSS selectors).

        Strategy 1: querySelectorAll for model card containers (overflow-hidden rounded-xl)
        Strategy 2: Text-walk heuristic — find model name text, walk up to card boundary.
        Both run in page JS context to avoid Playwright CSS selector quirks.
        """
        # Strategy 1: direct querySelectorAll in page JS
        card_count = await page.evaluate("""() => {
            return document.querySelectorAll(
                'div[class*="overflow-hidden"][class*="rounded-xl"][class*="border-subtler"]'
            ).length;
        }""")
        _log(f"Model card JS querySelectorAll count: {card_count}")

        if card_count >= 2:
            # Use Playwright locator which supports auto-waiting
            cards = await page.query_selector_all(
                'div[class*="overflow-hidden"][class*="rounded-xl"][class*="border-subtler"]'
            )
            if len(cards) >= 2:
                _log(f"Found {len(cards)} model cards via primary selector")
                return cards

        # If CSS selector didn't work but JS found them, use evaluate_handle
        if card_count >= 2:
            handles = []
            for i in range(card_count):
                h = await page.evaluate_handle(
                    f"""() => document.querySelectorAll(
                        'div[class*="overflow-hidden"][class*="rounded-xl"][class*="border-subtler"]'
                    )[{i}]"""
                )
                handles.append(h.as_element())
            handles = [h for h in handles if h is not None]
            if len(handles) >= 2:
                _log(f"Found {len(handles)} model cards via evaluate_handle")
                return handles

        # Strategy 2: heuristic — walk text nodes for model names, find card boundaries
        model_names = ["GPT", "Claude", "Gemini"]
        card_indices = await page.evaluate("""(modelNames) => {
            const cards = [];
            const allDivs = document.querySelectorAll('div');
            // Build index of divs with class containing 'rounded-xl'
            const roundedDivs = [];
            allDivs.forEach((div, idx) => {
                const cls = div.className?.toString() || '';
                if (cls.includes('rounded-xl') && cls.includes('border')) {
                    const text = div.textContent || '';
                    if (text.length > 20 && text.length < 50000) {
                        roundedDivs.push({ idx, text: text.substring(0, 300), cls: cls.substring(0, 200) });
                    }
                }
            });
            // Filter to those containing model name text
            for (const name of modelNames) {
                const match = roundedDivs.find(d =>
                    d.text.includes(name) && !cards.some(c => c.idx === d.idx)
                );
                if (match) cards.push(match);
            }
            return cards;
        }""", model_names)

        if card_indices and len(card_indices) >= 2:
            _log(f"Found {len(card_indices)} model cards via heuristic (names: {[c.get('text', '')[:30] for c in card_indices]})")
            # Get element handles by re-querying
            handles = []
            for card_info in card_indices:
                cls_prefix = card_info.get("cls", "")[:40]
                if cls_prefix:
                    h = await page.evaluate_handle(
                        """(clsPrefix) => {
                            const divs = document.querySelectorAll('div');
                            for (const d of divs) {
                                if ((d.className?.toString() || '').startsWith(clsPrefix)) {
                                    return d;
                                }
                            }
                            return null;
                        }""",
                        cls_prefix,
                    )
                    el = h.as_element()
                    if el:
                        handles.append(el)
            if handles:
                return handles

        # 0 model cards is normal — Perplexity may use single-model mode for simpler queries
        _log(f"No model cards found (council may have used single-model mode)")
        return []

    async def _extract_model_name(self, card) -> str:
        """Extract the clean model name from a model card element."""
        name = await card.evaluate("""el => {
            // Look for the model name text element (font-medium, text-xs)
            const nameEl = el.querySelector(
                'div[class*="font-medium"][class*="text-xs"][class*="text-foreground"]'
            );
            if (nameEl) return nameEl.textContent.trim();
            // Fallback: first short text child
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const t = walker.currentNode.textContent.trim();
                if (t.length > 2 && t.length < 60) return t;
            }
            return '';
        }""")
        # Clean up: strip "Thinking", "X steps", etc. suffixes
        if name:
            for suffix in [" Thinking", " Writing", " Searching"]:
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
        return (name or "Unknown Model")[:50]

    async def _extract_panel_response(self, page) -> str:
        """Extract the response text from the currently active model panel."""
        # The panel slides in with data-state="active" and contains .prose content
        panel_prose_sel = self.selectors.get(
            "councilModelPanelProse", "div[data-state='active'] .prose"
        )
        try:
            text = await page.evaluate(
                f'document.querySelector("{panel_prose_sel}")?.innerText || ""'
            )
            if text:
                return text
        except Exception:
            pass

        # Fallback: data-state="active" with h-full class, extract all text
        panel_sel = self.selectors.get(
            "councilModelPanel", "div[data-state='active'].h-full"
        )
        try:
            text = await page.evaluate(
                f'document.querySelector("{panel_sel}")?.innerText || ""'
            )
            return text or ""
        except Exception:
            return ""

    async def extract_results(self, page) -> dict:
        """Extract synthesis and per-model responses from the page.

        DOM structure (validated 2026-02-16):
          Council mode:
            - Synthesis: first div.prose.inline element
            - Model cards: 3x div.overflow-hidden.rounded-xl.border-subtler
          Research mode:
            - Full report: div.prose.max-w-none (right panel with detailed sections)
            - Intro summary: first div.prose.inline (left panel, shorter)
        """
        results = {
            "synthesis": "",
            "models": {},
            "citations": [],
        }

        # Extract synthesis/report text — different selectors per mode
        if self.perplexity_mode in ("research", "labs"):
            # Research mode: try longest .prose first (long-form answers), then
            # fall back to <main> with UI chrome stripped (short answers like
            # "What is 500+500?" → "1000" which Perplexity renders OUTSIDE any
            # .prose container as of 2026-05-22 DOM drift).
            try:
                text = await page.evaluate("""() => {
                    // 1. Longest .prose element — handles long-form answers.
                    const proses = Array.from(document.querySelectorAll('div.prose, .prose'));
                    let best = '';
                    for (const e of proses) {
                        const t = e.innerText || '';
                        if (t.length > best.length) best = t;
                    }
                    if (best.length > 50) return best;
                    // 2. Fallback: <main> innerText with UI chrome stripped.
                    const main = document.querySelector('main');
                    if (!main) return best;
                    const raw = main.innerText || '';
                    const chrome = new Set([
                        'Answer', 'Links', 'Images', 'Share', 'Sources',
                        'Ask a follow-up', 'Search', 'Model', 'Steps', 'Tasks',
                    ]);
                    const cleaned = raw
                        .split('\\n')
                        .map(s => s.trim())
                        .filter(s => s && !chrome.has(s))
                        .join('\\n');
                    return cleaned.length > best.length ? cleaned : best;
                }""")
                # 2026-05-27 reasoning-trail guard: on large-artifact queries,
                # Perplexity sometimes leaves only "Looking up X / Checking Y"
                # in .prose with no synthesis body. Detect and blank out so
                # downstream JSON parsing fails cleanly and retry-once fires.
                if _is_reasoning_trail_only(text):
                    _log(f"WARNING: extracted text appears reasoning-trail-only ({len(text)} chars) — treating as empty for clean retry-once")
                    _log(f"MONITOR-SIGNAL reasoning_trail {self.perplexity_mode}")
                    results["synthesis"] = ""
                    if hasattr(self, "_query_inst"):
                        self._query_inst["reasoning_trail_detection"] = "flagged_blanked"
                        self._query_inst["extracted_synthesis_chars"] = 0
                else:
                    results["synthesis"] = text
                    _log(f"Extracted research report: {len(results['synthesis'])} chars")
                    if hasattr(self, "_query_inst"):
                        self._query_inst["extracted_synthesis_chars"] = len(text)
            except Exception as e:
                _log(f"WARNING: Failed to extract research report: {e}")
        else:
            # Council mode: synthesis is in div.prose.inline
            synthesis_sel = self.selectors.get("councilSynthesis", "div.prose.inline")
            synthesis_fallback = self.selectors.get("councilSynthesisFallback", ".prose:first-of-type")
            try:
                text = await page.evaluate(
                    f'document.querySelector("{synthesis_sel}")?.innerText || ""'
                )
                if not text:
                    text = await page.evaluate(
                        f'document.querySelector("{synthesis_fallback}")?.innerText || ""'
                    )
                # 2026-06-13: extend reasoning-trail + empty-synthesis detection to
                # council mode (previously research/labs only) so council responses
                # that render only the reasoning trail are blanked + flagged instead
                # of silently parsed as content.
                if not text:
                    _log("WARNING: council synthesis empty")
                    results["synthesis"] = ""  # exit_reason/marker set by post-extraction empty-check
                elif _is_reasoning_trail_only(text):
                    _log(f"WARNING: council synthesis appears reasoning-trail-only ({len(text)} chars) — blanking")
                    _log("MONITOR-SIGNAL reasoning_trail council")
                    results["synthesis"] = ""
                    if hasattr(self, "_query_inst"):
                        self._query_inst["reasoning_trail_detection"] = "flagged_blanked"
                        self._query_inst["extracted_synthesis_chars"] = 0
                else:
                    results["synthesis"] = text
                    _log(f"Extracted synthesis: {len(results['synthesis'])} chars")
                    if hasattr(self, "_query_inst"):
                        self._query_inst["extracted_synthesis_chars"] = len(text)
            except Exception as e:
                _log(f"WARNING: Failed to extract synthesis: {e}")

        # 2026-06-14 hardening: a 0-char extraction (empty synthesis, reasoning-trail
        # blanked, or an extraction exception) must NOT be recorded as exit_reason
        # "completed" — that masked the most common silent failure (see the 12/202
        # empty_synthesis records). Mark it explicitly so per-query instrumentation,
        # calibration, and research_monitor.py all see the silent-empty failure. The
        # bottom finally only sets "completed" when exit_reason is still None.
        if not (results.get("synthesis") or "").strip():
            _log(f"MONITOR-SIGNAL empty_synthesis {self.perplexity_mode}")
            if hasattr(self, "_query_inst"):
                self._query_inst["exit_reason"] = "empty_synthesis"
                self._query_inst["extracted_synthesis_chars"] = 0

        # End-to-end result validation (#4, 2026-06-15) — non-blocking: flag
        # truncation/parse survivors (incl. length-regression vs streaming peak) so
        # a plausible-but-incomplete answer is never silently stored as valid. The
        # synthesis is still returned; the signal drives observation + monitor alerts.
        if hasattr(self, "_query_inst"):
            peak = getattr(self, "_peak_prose_chars", 0)
            self._query_inst["peak_prose_chars"] = peak
            _syn = results.get("synthesis") or ""
            if _syn.strip():
                _verdict = _validate_result(_syn, peak)
                self._query_inst["result_validation"] = _verdict
                if _verdict == "suspect_truncated":
                    _log(f"MONITOR-SIGNAL result_suspect {self.perplexity_mode} "
                         f"chars={len(_syn)} peak={peak}")

        # Find model cards (council mode only — research mode has no model cards)
        cards = []
        if self.perplexity_mode not in ("research", "labs"):
            cards = await self._find_model_cards(page)
            _log(f"Found {len(cards)} model cards")

        # Extract per-model responses by clicking each card
        for i, card in enumerate(cards):
            try:
                model_name = await self._extract_model_name(card)

                # Click the card header to expand the model panel
                clickable = await card.query_selector(
                    self.selectors.get(
                        "councilModelClickableRow",
                        "div[class*='cursor-pointer'][class*='p-3']",
                    )
                )
                target = clickable or card
                await target.click()
                await page.wait_for_timeout(1500)

                # Extract the response from the active panel
                response_text = await self._extract_panel_response(page)

                if response_text:
                    results["models"][model_name] = {"response": response_text}
                    _log(f"  Model '{model_name}': {len(response_text)} chars")
                else:
                    _log(f"  Model '{model_name}': no response text in panel")
                    await self._save_artifact(page, f"model_{i}_empty_panel")

                # Close the panel (Escape or close button)
                close_sel = self.selectors.get("councilPanelClose", "button[aria-label='Close']")
                try:
                    close_btn = await page.query_selector(close_sel)
                    if close_btn:
                        await close_btn.click()
                        await page.wait_for_timeout(500)
                    else:
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(500)
                except Exception:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(500)

            except Exception as e:
                _log(f"  WARNING: Failed to extract model {i}: {e}")
                await self._save_artifact(page, f"model_{i}_error")

        # Extract citations
        try:
            citations = await page.evaluate("""() => {
                const links = document.querySelectorAll('.prose a[href]');
                return Array.from(links).map(a => ({
                    url: a.href,
                    text: a.textContent?.trim() || ''
                })).filter(c => c.url && !c.url.startsWith('javascript:'));
            }""")
            results["citations"] = citations[:50]  # Cap at 50
            _log(f"Extracted {len(results['citations'])} citations")
        except Exception as e:
            _log(f"WARNING: Failed to extract citations: {e}")

        return results

    async def _cleanup_browser(self) -> None:
        """Close current browser/context without stopping Playwright."""
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

    async def run(self, query: str) -> dict:
        """Full pipeline, FIFO-serialized: research_queue slot -> semaphore ->
        start -> validate -> query -> wait -> extract.

        The research_queue slot is the OUTERMOST layer -- it wraps the
        entire existing pipeline in `_run_impl` (semaphore, Chrome
        launch, submit_lock, extract), so acquisition order is always
        queue -> submit_lock, never reversed, and at most one run()
        across ALL concurrent Claude Code sessions is ever inside
        `_run_impl` at a time.

        `acquire_slot()` is a SYNC context manager whose __enter__ blocks
        in a poll-wait loop (up to research_queue.MAX_WAIT_S) -- entered
        and exited via `asyncio.to_thread` (same pattern as
        `_acquire_submit_lock`'s `asyncio.to_thread(lock.acquire)`) so a
        waiting run() never blocks this process's asyncio event loop.

        Kill switch: `RESEARCH_QUEUE_ENABLED=0` makes `acquire_slot()` a
        true no-op (see research_queue.py) -- `run()` then behaves
        byte-for-byte as it did before this wiring (no ticket/log/
        snapshot files, no serialization).

        `_run_impl` swallows almost all failures internally and returns
        an `{"error": ...}` dict rather than raising (see its own
        `except Exception` block), so a real exception essentially never
        reaches this wrapper. To make the central
        `perplexity-activity.jsonl` record "error" (not "completed") for
        those logical failures too, a synthetic exc_info is passed into
        `slot_cm.__exit__` when the result dict carries an "error" key --
        this is NOT re-raised; run()'s external contract (always returns
        a dict, never raises for expected failures) is unchanged.
        """
        session_id = f"{Path.cwd().name}:{os.getpid()}"
        slot_cm = research_queue.acquire_slot(
            session=session_id, query_preview=query, mode=self.perplexity_mode
        )
        slot = await asyncio.to_thread(slot_cm.__enter__)
        self._run_id = slot.get("run_id")

        _exc_info: tuple = (None, None, None)
        try:
            result = await self._run_impl(query)
            if isinstance(result, dict) and result.get("error"):
                _exc_info = (
                    RuntimeError,
                    RuntimeError(str(result.get("error"))[:200]),
                    None,
                )
            return result
        except BaseException as e:
            _exc_info = (type(e), e, e.__traceback__)
            raise
        finally:
            await asyncio.to_thread(lambda: slot_cm.__exit__(*_exc_info))

    async def _run_impl(self, query: str) -> dict:
        """Full pipeline: semaphore -> start -> validate -> query -> wait -> extract.

        Called only from `run()`, which wraps this entire method in the
        research_queue FIFO slot. `self._run_id` (stamped by `run()`
        before this method starts) is threaded into `self._query_inst`
        and the returned `results` dict below, so
        `instrumentation-query.jsonl` / `runs.jsonl` share the same
        run_id used in the central `perplexity-activity.jsonl`.
        """
        start_time = time.time()
        self._init_artifact_dir(query)
        self._semaphore = SessionSemaphore()

        # Phase 3 (2026-05-29 follow-ups) — initialize per-query instrumentation
        # BEFORE _semaphore.acquire() so even the BrowserBusyError early-exit
        # produces a record with chrome_path_used="cdp_busy". Step 7 critique Q3:
        # the high-value cdp_busy signal would be lost if init waited until
        # after the semaphore. Hook sites mutate self._query_inst along the way;
        # emit happens at every exit path (early returns + bottom finally).
        self._query_inst: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "run_id": getattr(self, "_run_id", None),
            "query_chars": len(query) if query else 0,
            "perplexity_mode": getattr(self, "perplexity_mode", None),
            "chrome_path_used": None,
            "chrome_headless": bool(getattr(self, "headless", True)),
            "cdp_keeper_alive_at_start": None,
            "cookies_stale_critical": [],
            "auto_refresh_path": "none",
            "synthesis_mount_wait_outcome": "not_triggered",
            "reasoning_trail_detection": "not_flagged",
            "extracted_synthesis_chars": 0,
            "peak_prose_chars": 0,
            "result_validation": "ok",
            "min_critical_ttl_s": None,   # pre-guard min critical-cookie TTL (calibration #3)
            "elapsed_wall_s": None,       # submit->final wall time (calibration #3)
            "exit_reason": None,
            "inst_emitted": False,
        }
        self._query_inst_emitted = False
        self._peak_prose_chars = 0  # running max .prose length seen while streaming

        try:
            instance_id = self._semaphore.acquire(SEMAPHORE_WAIT_TIMEOUT)
            self.instance_id = instance_id
        except BrowserBusyError as e:
            self._query_inst["chrome_path_used"] = "cdp_busy"
            self._query_inst["exit_reason"] = "browser_busy"
            if not self._query_inst_emitted:
                _emit_query_instrumentation(self._query_inst)
                self._query_inst_emitted = True
            return {
                "error": str(e),
                "code": "BROWSER_BUSY",
                "step": "lock",
            }

        # Acquire the cross-process submit lock BEFORE self.start() so
        # chromium.launch_persistent_context calls are serialised across
        # concurrent Claude Code sessions. Without this, two Chrome
        # processes can start within microseconds of each other, race
        # through Chrome's global Local\ChromeProcessSingletonStartup!
        # mutex, and one suicides with exit code 21 (ProcessSingleton
        # collision). Released right after submit_query completes
        # (~10-15s) — wait_for_completion runs outside the lock so
        # other sessions can submit while this one collects results.
        # The defensive release in the outer finally below covers any
        # exception path that exits before the explicit release.
        submit_lock = None
        submit_lock_released = False
        try:
            submit_lock = await self._acquire_submit_lock()
        except Exception as lock_err:
            self._semaphore.release()
            self._query_inst["exit_reason"] = f"submit_lock_failed:{type(lock_err).__name__}"
            if not self._query_inst_emitted:
                _emit_query_instrumentation(self._query_inst)
                self._query_inst_emitted = True
            return {
                "error": f"submit_lock acquire failed: {lock_err!r}",
                "step": "submit_lock",
            }

        try:
            _log("Starting Playwright browser...")
            try:
                await self.start()
            except SessionStaleError as e:
                # Abort cleanly instead of submitting a run that cannot succeed.
                # Surfaced to the caller as a coded error (same shape as
                # BROWSER_BUSY) so the MCP layer and the queue both see a real
                # failure rather than a silent 5-8 minute death.
                self._query_inst["chrome_path_used"] = "aborted_session_stale"
                self._query_inst["exit_reason"] = "session_stale"
                if not self._query_inst_emitted:
                    _emit_query_instrumentation(self._query_inst)
                    self._query_inst_emitted = True
                _log(f"ABORT session_stale: {e}")
                return {
                    "error": str(e),
                    "code": "SESSION_STALE",
                    "step": "session_freshness",
                }

            # CDP-attached sessions share the keeper's Chrome — no per-session
            # browser was launched, so the semaphore slot isn't actually
            # serving its purpose (which is to limit concurrent local Chrome
            # launches). Release it immediately so other sessions can attach
            # without hitting BROWSER_BUSY when slots are full of stale entries
            # from crashed prior runs. submission_lock still serializes the
            # focus-sensitive submit step across all sessions.
            if self._cdp_attached and self._semaphore is not None:
                try:
                    self._semaphore.release()
                    _log(f"CDP-attached; released semaphore slot {self.instance_id} (other sessions can attach freely)")
                except Exception as _sem_rel_err:
                    _log(f"WARN: semaphore release on CDP-attach failed: {_sem_rel_err}")
                self._semaphore = None
                self.instance_id = -1

            _log("Validating session...")
            if not await self.validate_session():
                # Cloudflare may have blocked non-persistent context — retry with temp profile
                if not self.use_persistent:
                    _log("Non-persistent context failed validation, trying Cloudflare fallback...")
                    await self._cleanup_browser()
                    await self._start_with_temp_profile()
                    if not await self.validate_session():
                        return {
                            "error": "Session expired or not logged in. Run: python council_browser.py --save-session",
                            "step": "validate",
                        }
                else:
                    return {
                        "error": "Session expired or not logged in. Run: python council_browser.py --save-session",
                        "step": "validate",
                    }

            # Open a new page for the query
            page = await self.context.new_page()
            if self._cdp_attached:
                self._cdp_owned_pages.append(page)

            try:
                _log("Navigating to Perplexity...")
                await page.goto(
                    "https://www.perplexity.ai/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await page.wait_for_timeout(2000)

                # submit_lock was acquired BEFORE self.start() (above) so
                # Chrome launches ARE inside the lock — this prevents the
                # ProcessSingleton race between concurrent Claude sessions
                # that was causing "browsers close each other out".
                # Release happens immediately after submit_query returns so
                # wait_for_completion + extract_results run OUTSIDE the lock
                # (fully parallel across sessions) -- THIS IS THE DEFAULT
                # (RESEARCH_QUEUE_STOPGAP unset) BEHAVIOR ONLY. Under
                # RESEARCH_QUEUE_STOPGAP, that early release is skipped
                # (see the guard below) and submit_lock stays held all the
                # way through wait_for_completion + extract_results,
                # released only in the outer `finally`.
                _log(f"Activating {self.perplexity_mode} mode...")
                if not await self.activate_mode(page):
                    await self._save_artifact(page, "activate_failure")
                    return {"error": f"Failed to activate {self.perplexity_mode} mode", "step": "activate"}

                _log(f"Submitting query: {query[:80]}...")
                await self.submit_query(page, query)

                # Release submit_lock NOW — submission landed (.prose
                # appeared inside submit_query). Defensive release in the
                # outer finally covers any exception path that bypasses
                # this explicit release.
                #
                # Phase 0 stopgap (RESEARCH_QUEUE_STOPGAP): when set, skip
                # this early release so submit_lock stays held for the
                # WHOLE run — the existing defensive release in the outer
                # `finally` below (guarded by `submit_lock_released`)
                # becomes the sole release point, serializing entire runs
                # instead of just the submit window. Default (flag unset)
                # behavior is unchanged.
                if not _flag_enabled("RESEARCH_QUEUE_STOPGAP"):
                    try:
                        submit_lock.release()
                        submit_lock_released = True
                        _log("submit_lock released")
                    except Exception as e:
                        _log(f"submit_lock release error={e!r}")
                    finally:
                        # No-op when the flag is off (heartbeat is only
                        # ever started under RESEARCH_QUEUE_STOPGAP in
                        # _acquire_submit_lock), kept here for symmetry.
                        self._stop_submit_lock_heartbeat()

                _log("Waiting for completion...")
                completed = await self.wait_for_completion(page, self.timeout)
                if not completed:
                    _log("WARNING: Timed out waiting for completion, extracting partial results")
                    await self._save_artifact(page, "timeout")

                _log("Extracting results...")
                results = await self.extract_results(page)

                elapsed = int((time.time() - start_time) * 1000)
                results["query"] = query
                results["mode"] = "browser"
                results["completed"] = completed
                results["execution_time_ms"] = elapsed
                results["run_id"] = getattr(self, "_run_id", None)
                _log(f"Done in {elapsed/1000:.1f}s")

                return results

            finally:
                await page.close()

        except Exception as e:
            # DIAG (2026-05-20): surface full traceback so silent-swallow failures
            # (Chrome profile collision under concurrent runners, ProcessSingleton races,
            # Cloudflare bot-challenge during launch) leave evidence in stderr instead of
            # just an opaque str(e). Investigation 2026-05-20 showed Pattern B's 2-second
            # exit was masked here when PID 17996 contended for submit_lock + Chrome profile.
            import traceback as _tb_run
            _log(f"council.run UNHANDLED {type(e).__name__}: {e}")
            _tb_run.print_exc(file=sys.stderr)
            sys.stderr.flush()
            # Try to capture artifact on unhandled exception
            if self.context:
                try:
                    pages = self.context.pages
                    if pages:
                        await self._save_artifact(pages[-1], "unhandled_exception")
                except Exception:
                    pass
            return {
                "error": str(e),
                "error_type": type(e).__name__,
                "step": "unknown",
                "execution_time_ms": int((time.time() - start_time) * 1000),
            }
        finally:
            # Defensive submit_lock release. Only fires if the explicit
            # release after submit_query didn't run (e.g., exception
            # before submit_query completed). Double-release is harmless
            # with thread_local=False — release() checks is_locked first
            # and short-circuits if already released.
            if submit_lock is not None and not submit_lock_released:
                try:
                    submit_lock.release()
                    _log("submit_lock released (defensive)")
                except Exception:
                    pass
                finally:
                    # Under RESEARCH_QUEUE_STOPGAP this is the ONLY
                    # release point (the mid-run release above is
                    # skipped under the flag), so it must also be where
                    # the heartbeat thread started in
                    # _acquire_submit_lock() gets stopped. No-op when
                    # the flag is off / heartbeat was never started.
                    self._stop_submit_lock_heartbeat()
            # Phase 3 (2026-05-29): emit per-query instrumentation once per
            # run() invocation regardless of exit path (normal return, raised
            # exception, mid-pipeline failure). Idempotent via _query_inst_emitted.
            if not getattr(self, "_query_inst_emitted", False):
                try:
                    if self._query_inst.get("exit_reason") is None:
                        self._query_inst["exit_reason"] = "completed"
                    self._query_inst["elapsed_wall_s"] = round(time.time() - start_time, 1)
                    _emit_query_instrumentation(self._query_inst)
                    self._query_inst_emitted = True
                except Exception:
                    pass
            # Guard: _semaphore may have been released + nulled earlier in this
            # method (CDP-attached path releases right after self.start()).
            if self._semaphore is not None:
                try:
                    self._semaphore.release()
                except Exception:
                    pass

    async def save_session(self) -> None:
        """Save current browser session for future headless use."""
        if not self.context:
            _log("ERROR: No browser context to save from")
            return

        cookies = await self.context.cookies()

        # Save cookies in Playwright-native format
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(
            json.dumps(cookies, indent=2, default=str),
            encoding="utf-8",
        )
        _log(f"Saved {len(cookies)} cookies to {self.session_path}")

        # Also capture localStorage from Perplexity page
        try:
            pages = self.context.pages
            pplx_page = None
            for p in pages:
                if "perplexity.ai" in (p.url or ""):
                    pplx_page = p
                    break
            if not pplx_page and pages:
                pplx_page = pages[0]

            if pplx_page:
                ls_data = await pplx_page.evaluate("""() => {
                    const items = {};
                    for (let i = 0; i < localStorage.length; i++) {
                        const key = localStorage.key(i);
                        items[key] = localStorage.getItem(key);
                    }
                    return items;
                }""")
                if ls_data:
                    ls_path = BROWSER_LOCALSTORAGE_PATH
                    ls_path.write_text(
                        json.dumps(ls_data, indent=2, default=str),
                        encoding="utf-8",
                    )
                    _log(f"Saved {len(ls_data)} localStorage items to {ls_path.name}")
        except Exception as e:
            _log(f"WARNING: Failed to capture localStorage: {e}")

    async def stop(self) -> None:
        """Close browser, Playwright, and clean up temp resources.

        CDP-attached mode (2026-05-21): we're sharing session_keeper.py's
        long-lived headful Chrome. Closing the context would kill the keeper's
        whole browser window — instead close ONLY the pages we opened (tracked
        in self._cdp_owned_pages), then detach Playwright from the remote Chrome
        via browser.close() which only severs the connection, not the process.
        """
        if self._cdp_attached:
            # CDP-attached cleanup: close only pages we opened; leave the keeper's
            # context + remote Chrome alive. Critical correction (2026-05-21):
            # browser.close() on a CDP-connected browser sends Browser.close CDP
            # command which TERMINATES the remote Chrome process (and kills the
            # keeper). Skip it. Just stop our Playwright client; the keeper's
            # own Playwright connection survives unaffected.
            for page in list(self._cdp_owned_pages):
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass
            self._cdp_owned_pages.clear()
            self.context = None
            self._browser = None  # release ref but DO NOT call .close()
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
            self._cdp_attached = False
            return

        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        # Clean up temp profile dir (Cloudflare fallback)
        if self._temp_profile_dir and Path(self._temp_profile_dir).exists():
            try:
                shutil.rmtree(self._temp_profile_dir, ignore_errors=True)
                _log(f"Cleaned up temp profile: {self._temp_profile_dir}")
            except Exception:
                pass
            self._temp_profile_dir = None
        # Safety net: release semaphore if still held
        if hasattr(self, '_semaphore') and self._semaphore:
            self._semaphore.release()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Perplexity council browser automation")
    parser.add_argument("query", nargs="?", help="The question to ask the council")
    parser.add_argument("--headful", action="store_true", help="Run with visible browser")
    parser.add_argument("--save-session", action="store_true", help="Login and save session")
    parser.add_argument("--timeout", type=int, default=BROWSER_TIMEOUT, help="Timeout in ms")
    parser.add_argument("--session-path", type=str, help="Path to session file")
    parser.add_argument("--save-artifacts", action="store_true", default=False,
        help="Save screenshots/HTML on failure (default: True when --opus-synthesis)")
    parser.add_argument("--perplexity-mode", choices=["council", "research", "labs"], default="council",
        help="Perplexity slash command: /council (multi-model), /research (deep research), or /labs (experimental labs)")
    parser.add_argument("--headless-fallback", action="store_true",
        help="Try headless first, fall back to headful if Cloudflare blocks")

    args = parser.parse_args()

    # --headless-fallback implies starting headless (overrides --headful)
    headless = not args.headful
    headless_fallback = args.headless_fallback
    if headless_fallback:
        headless = True  # Start headless, auto-switch if blocked

    session_path = Path(args.session_path) if args.session_path else None
    council = PerplexityCouncil(
        headless=headless,
        session_path=session_path,
        timeout=args.timeout,
        save_artifacts=args.save_artifacts,
        perplexity_mode=args.perplexity_mode,
        use_persistent=args.save_session,  # Persistent context only for --save-session
        headless_fallback=headless_fallback,
    )

    if args.save_session:
        semaphore = SessionSemaphore()
        try:
            instance_id = semaphore.acquire(SEMAPHORE_WAIT_TIMEOUT)
            council.instance_id = instance_id
            await council.start()
            _log("Browser opened. Log in to Perplexity in the browser window.")
            _log("Press Enter here when done...")
            # Use asyncio-compatible input
            await asyncio.get_event_loop().run_in_executor(None, input)
            await council.save_session()
            await council.stop()
            _log("Session saved. You can now run queries in headless mode.")
        finally:
            semaphore.release()
        return

    if not args.query:
        parser.error("Query is required unless using --save-session")

    try:
        result = await council.run(args.query)
        print(json.dumps(result, indent=2, default=str))
    finally:
        await council.stop()


if __name__ == "__main__":
    asyncio.run(main())

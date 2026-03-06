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
import re
import sys
import time
from pathlib import Path

import shutil
import tempfile

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
    MAX_CONCURRENT_SESSIONS,
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


class BrowserBusyError(Exception):
    """Raised when another browser automation session holds the profile lock."""
    pass


class SessionSemaphore:
    """File-based counting semaphore for concurrent browser sessions.

    Each active session creates a PID-named file in BROWSER_SESSIONS_DIR.
    Supports wait-with-timeout instead of immediate failure.
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

    def _cleanup_stale(self) -> int:
        """Remove session files for dead PIDs or expired TTL. Returns count removed."""
        removed = 0
        now = time.time()
        for f in self.sessions_dir.glob("session-*.lock"):
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

    def _count_active(self) -> int:
        """Count active session files (after cleanup)."""
        return len(list(self.sessions_dir.glob("session-*.lock")))

    def acquire(self, wait_timeout: float = SEMAPHORE_WAIT_TIMEOUT) -> None:
        """Acquire a session slot. Waits up to wait_timeout seconds.

        Raises BrowserBusyError if no slot becomes available.
        """
        start = time.time()
        pid = os.getpid()

        while True:
            self._cleanup_stale()
            active = self._count_active()

            if active < self.max_sessions:
                # Claim a slot
                self._session_file = self.sessions_dir / f"session-{pid}.lock"
                self._session_file.write_text(
                    f"{pid} {time.time():.0f}\n", encoding="utf-8"
                )
                return

            elapsed = time.time() - start
            if elapsed >= wait_timeout:
                raise BrowserBusyError(
                    f"All {self.max_sessions} browser session slots are in use. "
                    f"Waited {wait_timeout}s. Wait for a session to finish or use --mode api."
                )

            time.sleep(1)

    def release(self) -> None:
        """Release the session slot by deleting the session file."""
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


def _log(msg: str) -> None:
    """Log to stderr (stdout reserved for JSON result)."""
    print(f"  [browser] {msg}", file=sys.stderr)


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
        self.selectors = _load_selectors()
        self.playwright = None
        self._browser = None  # Separate browser object (non-persistent mode)
        self.context = None
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
    def _chrome_args() -> list[str]:
        """Shared Chrome launch arguments for all launch methods."""
        return [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--disable-features=IsolateOrigins,site-per-process",
            "--window-size=1920,1080",
        ]

    @staticmethod
    def _stealth_scripts() -> str:
        """Return JavaScript to reduce automation detection.

        Masks: webdriver flag, chrome.runtime/csi/loadTimes, Playwright globals,
        navigator.plugins, navigator.languages, WebGL vendor/renderer.
        """
        return """
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
                "challenges.cloudflare.com" in (await page.content())[:2000],
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

    async def _start_non_persistent(self) -> None:
        """Launch browser with an isolated temp profile directory.

        Each session gets its own user-data-dir via launch_persistent_context()
        to prevent Chrome SingletonLock conflicts when multiple instances run
        concurrently. Cookies injected via _load_session() after launch.
        """
        self._temp_profile_dir = tempfile.mkdtemp(prefix="council_np_")
        _log(f"Non-persistent: using isolated profile {self._temp_profile_dir}")

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self._temp_profile_dir,
            channel="chrome",
            headless=self.headless,
            args=self._chrome_args(),
            viewport={"width": 1920, "height": 1080},
        )

        if self.session_path.exists():
            await self._load_session()

        # Apply stealth scripts
        await self.context.add_init_script(self._stealth_scripts())

    async def _start_persistent(self) -> None:
        """Launch with persistent context (used for --save-session only)."""
        BROWSER_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_USER_DATA_DIR),
            channel="chrome",
            headless=self.headless,
            args=self._chrome_args(),
            viewport={"width": 1920, "height": 1080},
        )

        if self.session_path.exists():
            await self._load_session()

        await self.context.add_init_script(self._stealth_scripts())

    async def _start_with_temp_profile(self) -> None:
        """Cloudflare fallback: persistent context with a temp profile directory.

        Uses a unique temp dir per session — no SingletonLock conflicts.
        Cookies injected via _load_session() after launch.
        """
        self._temp_profile_dir = tempfile.mkdtemp(prefix="council_cf_")
        _log(f"Cloudflare fallback: using temp profile {self._temp_profile_dir}")

        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self._temp_profile_dir,
            channel="chrome",
            headless=self.headless,
            args=self._chrome_args(),
            viewport={"width": 1920, "height": 1080},
        )

        if self.session_path.exists():
            await self._load_session()

        await self.context.add_init_script(self._stealth_scripts())

    async def _load_session(self) -> None:
        """Load session from playwright-session.json + playwright-localstorage.json."""
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

    async def activate_mode(self, page) -> bool:
        """Activate the configured Perplexity mode via slash command.

        Supports: /council (multi-model) and /research (deep research).
        """
        slash_cmd = f"/{self.perplexity_mode}"
        _log(f"Activating {self.perplexity_mode} mode via {slash_cmd}...")
        textarea = self.selectors.get("textarea", "#ask-input")

        # Focus the input
        try:
            await page.click(textarea)
            await page.wait_for_timeout(500)
        except Exception as e:
            _log(f"Failed to focus input: {e}")
            return False

        # Type the slash command
        await page.keyboard.type(slash_cmd, delay=BROWSER_TYPE_DELAY)
        await page.wait_for_timeout(1500)  # Wait for command palette

        # Press Enter to activate
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(1500)  # Wait for activation

        # Verify activation based on mode
        if self.perplexity_mode == "council":
            return await self._verify_council_activation(page)
        elif self.perplexity_mode == "research":
            return await self._verify_research_activation(page)
        elif self.perplexity_mode == "labs":
            return await self._verify_labs_activation(page)
        else:
            _log(f"Unknown mode '{self.perplexity_mode}', proceeding optimistically")
            return True

    async def _verify_council_activation(self, page) -> bool:
        """Verify council mode activated (look for '3 models' indicator)."""
        try:
            three_models = self.selectors.get("threeModelsDropdown", "button[aria-label='3 models']")
            await page.wait_for_selector(three_models, timeout=5000)
            _log("Council mode activated (found '3 models' indicator)")
            return True
        except Exception:
            try:
                council_text = await page.evaluate(
                    "!!document.querySelector('button')?.textContent?.includes('Model council')"
                )
                if council_text:
                    _log("Council mode activated (found 'Model council' text)")
                    return True
            except Exception:
                pass
            _log("WARNING: Could not verify council activation, proceeding anyway")
            return True  # Proceed optimistically

    async def _verify_research_activation(self, page) -> bool:
        """Verify research mode activated (look for research indicators)."""
        try:
            # Research mode shows a "Research" or "Deep Research" indicator
            found = await page.evaluate("""() => {
                const buttons = document.querySelectorAll('button, [role="button"], div[data-state]');
                for (const b of buttons) {
                    const text = (b.textContent || '').trim().toLowerCase();
                    if (text.includes('research') || text.includes('deep research')) return true;
                }
                return false;
            }""")
            if found:
                _log("Research mode activated (found research indicator)")
                return True
        except Exception:
            pass
        _log("WARNING: Could not verify research activation, proceeding anyway")
        return True  # Proceed optimistically

    async def _verify_labs_activation(self, page) -> bool:
        """Verify labs mode activated (look for labs indicators)."""
        try:
            found = await page.evaluate("""() => {
                const buttons = document.querySelectorAll('button, [role="button"], div[data-state]');
                for (const b of buttons) {
                    const text = (b.textContent || '').trim().toLowerCase();
                    if (text.includes('labs')) return true;
                }
                return false;
            }""")
            if found:
                _log("Labs mode activated (found labs indicator)")
                return True
        except Exception:
            pass
        _log("WARNING: Could not verify labs activation, proceeding anyway")
        return True  # Proceed optimistically

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

    async def _wait_research_smart(self, page, timeout: int, start: float) -> bool:
        """Smart completion detection for research/labs modes.

        Signal hierarchy:
        1. Primary: stop button cycle (appeared → disappeared with debounce)
        2. Confirming: MutationObserver stability OR text stability (10s window)
        3. Fallback: existing _wait_research_fallback() with reduced guards

        If the stop button disappears suspiciously fast (<30s), waits for
        a confirming signal before accepting. If stop button never appears,
        falls through to the CSS/text-stability fallback.
        """
        min_gen_s = BROWSER_MIN_GENERATION_TIME_MS / 1000
        confirm_s = BROWSER_CONFIRMATION_WINDOW_MS / 1000
        text_stable_s = 8  # seconds of unchanged text for confirmation

        # Inject MutationObserver early
        await self._inject_mutation_observer(page)

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
            # Research mode: full report is in the right panel (prose.max-w-none)
            try:
                text = await page.evaluate("""() => {
                    // Primary: right panel with full research report
                    const report = document.querySelector('div.prose.max-w-none');
                    if (report && report.innerText.length > 100) return report.innerText;
                    // Fallback: find the largest prose element on the page
                    const proses = Array.from(document.querySelectorAll('div.prose'));
                    if (proses.length === 0) return '';
                    proses.sort((a, b) => b.innerText.length - a.innerText.length);
                    return proses[0].innerText || '';
                }""")
                results["synthesis"] = text
                _log(f"Extracted research report: {len(results['synthesis'])} chars")
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
                results["synthesis"] = text
                _log(f"Extracted synthesis: {len(results['synthesis'])} chars")
            except Exception as e:
                _log(f"WARNING: Failed to extract synthesis: {e}")

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
        """Full pipeline: semaphore -> start -> validate -> query -> wait -> extract."""
        start_time = time.time()
        self._init_artifact_dir(query)
        self._semaphore = SessionSemaphore()

        try:
            self._semaphore.acquire(SEMAPHORE_WAIT_TIMEOUT)
        except BrowserBusyError as e:
            return {
                "error": str(e),
                "code": "BROWSER_BUSY",
                "step": "lock",
            }

        try:
            _log("Starting Playwright browser...")
            await self.start()

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

            try:
                _log("Navigating to Perplexity...")
                await page.goto(
                    "https://www.perplexity.ai/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await page.wait_for_timeout(2000)

                _log(f"Activating {self.perplexity_mode} mode...")
                if not await self.activate_mode(page):
                    await self._save_artifact(page, "activate_failure")
                    return {"error": f"Failed to activate {self.perplexity_mode} mode", "step": "activate"}

                _log(f"Submitting query: {query[:80]}...")
                await self.submit_query(page, query)

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
                _log(f"Done in {elapsed/1000:.1f}s")

                return results

            finally:
                await page.close()

        except Exception as e:
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
                "step": "unknown",
                "execution_time_ms": int((time.time() - start_time) * 1000),
            }
        finally:
            self._semaphore.release()

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
        """Close browser, Playwright, and clean up temp resources."""
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
        await council.start()
        _log("Browser opened. Log in to Perplexity in the browser window.")
        _log("Press Enter here when done...")
        # Use asyncio-compatible input
        await asyncio.get_event_loop().run_in_executor(None, input)
        await council.save_session()
        await council.stop()
        _log("Session saved. You can now run queries in headless mode.")
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

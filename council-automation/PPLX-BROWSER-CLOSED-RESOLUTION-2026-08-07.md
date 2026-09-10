# RESOLVED — PPLX runner "Target page, context or browser has been closed"

**Resolved:** 2026-08-07 14:2x PT. Companion to `PPLX-BROWSER-CLOSED-EVIDENCE-2026-08-07.md`.
**Status:** FIXED and verified with real successful `research_query` calls.

## Root cause

The runner attached to **the wrong Chrome**.

`PerplexityCouncil._start_via_cdp()` decided the session keeper was alive using a single
signal: *does `http://127.0.0.1:9222/json/version` respond?* That is not proof of identity —
any process can serve plausible JSON on a port. Three independent facts lined up:

1. **The keeper was not running.** The `PerplexitySessionKeeper` scheduled task has been
   **Disabled since 2026-07-30** (`LastRunTime 7/30 12:31`, 1451 missed runs). Research still
   worked because CDP attach quietly failed and the runner fell back to its local-launch path.
2. **A stale endpoint file pointed at 9222.** `~/.claude/config/session_keeper.cdp` was last
   written **2026-08-02 06:34** and records `pid 175804` — a PID that has not existed since the
   reboot. Nothing ever invalidated it.
3. **Another app took port 9222.** `~/.claude/browser-relay/relay.mjs` (the `/takeover` phone
   relay) launches Chrome with `--remote-debugging-port=9222` on
   `--user-data-dir=C:\Temp\igx-cdp-profile`. It started **12:57:20 PT**, seven minutes after
   the ~12:50 reboot.

From 12:57 onward the stale `.cdp` file's health check *passed* — against the relay's Chrome.
Every run then attached to `contexts[0]` of a throwaway profile with **zero Perplexity
cookies**, navigated to a logged-out wall, and was torn down ~128s later:
`Page.wait_for_timeout: Target page, context or browser has been closed`.

**The timeline is exact:** last success **11:49** (pre-reboot, launch path). Relay claims 9222
at **12:57**. First failure **13:19**. Four consecutive failures, all ~128s.

This also explains the two misleading secondary symptoms:
- **`BROWSER_BUSY` with `active: null`** — the semaphore is released early for CDP-attached
  runs, so slots leak from runs that die inside the doomed attach.
- **"a dead browser that killed two other sessions' queries"** — on attach the runner closes
  every `/search/` page in the shared context, so each new doomed run tore down the pages of
  the other doomed runs sharing the relay's Chrome.

The reboot was a trigger, not the cause. The cause is that a *reachable* port was treated as a
*trusted* port.

## Fix

### 1. CDP identity gate (`council_browser.py`) — the actual fix

`_start_via_cdp()` now proves the endpoint is the keeper's Chrome before attaching. Layered so
an indeterminate probe degrades to the next check rather than blocking a healthy keeper:

- **Gate A — recorded PID liveness.** A dead `pid` in `session_keeper.cdp` means the file is a
  leftover from a previous boot. The stale file is deleted so later runs re-probe cleanly.
- **Gate B — `DevToolsActivePort` match.** Chrome writes `<port>\n<ws-guid-path>` into its own
  `--user-data-dir`. We compare that GUID against the endpoint's reported
  `webSocketDebuggerUrl`. This is the canonical launch-time↔discovery-time match; portable and
  decisive. (Independently confirmed by Perplexity research during this fix — skipping it is a
  known SSRF-style attack vector against CDP clients, not just an ops bug.)
- **Gate C — port-owner command line.** If no `DevToolsActivePort` exists, the process
  listening on the port must have `session_keeper_profile` in its command line.
- **Gate D — post-attach cookie check.** The attached context must carry a Perplexity auth
  cookie (`__Secure-next-auth.session-token` / `pplx.session-id`). Backstop when process
  introspection is unavailable, and it *also* catches a live keeper whose login has expired
  (hypothesis 2 in the evidence file) — previously indistinguishable from this failure.

The "synthesize a `.cdp` file because 9222 answers" fast path — the specific line that turned a
squatter into a false positive — is now gated behind the same ownership check.

Any refusal falls back to the local-launch path, which is what was working before 12:57.

### 2. Orphaned-result recovery (`council_query.py`)

`invocation_id` is a fresh UUID per call, so when a caller dies the completed result on disk
becomes unreachable — the work succeeded, only delivery was lost
(`council_fae5c9e0.json`, `intellegix-business-projections`).

Results now carry a `query_fingerprint` (sha256 of mode+context+query). `run_browser_query`
checks the cache for a matching, usable, recent result before spending minutes of browser
automation. Details:

- Age is computed from the result's **recorded `timestamp`**, not file mtime — recovering an
  orphan re-saves it under a new invocation id, which would otherwise reset mtime and let one
  stale result be re-served forever.
- Window: 2h (`ORPHAN_RESULT_MAX_AGE_S`). Scan bounded to 200 newest files.
- Error stubs and empty syntheses are never served (`_result_is_usable`).
- Pre-fingerprint results already on disk are matched on stored query text, so existing
  orphans are recoverable.
- Opt out with `COUNCIL_NO_ORPHAN_RECOVERY=1`.

## Verification

Queue log, `~/.claude/council-logs/perplexity-queue.json` — patch landed 14:07:55 PT:

| seq | time | duration | result |
|---|---|---|---|
| 905 | 13:50 | 128.2s | ❌ browser closed |
| 906 | 14:01 | 128.1s | ❌ browser closed |
| **908** | **14:08** | **159.5s** | ✅ **completed** (first post-patch run) |

- Gate unit-tested against live machine state: correctly **refuses** the relay's Chrome by both
  stale-PID and foreign-owner paths; correctly **refuses** wrong-GUID and wrong-port; correctly
  **accepts** a simulated healthy keeper (so the CDP path is not broken for when the keeper
  returns).
- Orphan recovery tested against the real `council_fae5c9e0.json`: recovers the identical
  synthesis, respects the 2h window, is immune to mtime reset, and returns `None` for an unseen
  query.
- End-to-end through the MCP tool: a repeated query returned **instantly** from cache; new
  queries run and complete normally.

## Follow-up worth owning (NOT changed here)

1. **The keeper task is still Disabled** (since 07-30). Research works without it via the
   launch path. Re-enable deliberately, not as part of this fix.
2. **Port 9222 is contended.** `session_keeper.py` and `browser-relay/relay.mjs` both want it,
   and the relay's header documents 9222 as its takeover contract. If the keeper is ever
   re-enabled while the relay is up, the keeper will fail to bind. Per the port-registry rules
   this pair needs an explicit assignment — it is a real collision, currently latent.
3. **`BROWSER_BUSY` vs `BROWSER_DEAD`** are still not distinguished in the caller-facing error.
   The gate removes the cause that made it misleading today, but the semaphore can still leak a
   slot on a crashed run and report contention that does not exist.

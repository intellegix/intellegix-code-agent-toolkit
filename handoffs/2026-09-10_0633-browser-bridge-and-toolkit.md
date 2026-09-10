# Browser bridge honesty fix, and the toolkit repo reconciled

Two commits already public in this repository's history carry Austin's name, role and three email addresses, and one of them also names the local folder where his API keys are kept, so that needs his attention before anything else in this document. No key values were exposed and nothing has been rewritten. Separately, the browser bridge now declares when it can only see one of two browsers instead of returning a bare tab list, and the eight unpushed local commits are reconciled and waiting on his review in PR #92.

---

## 1. The thing to read first: what is already public

**No credentials were found anywhere.** Not in the working tree, not in the ten stale
`.bak` copies, not in any of 256 commits. `MEMORY.md`, `API_KEY_REGISTRY.md` and
`settings.json` have never been committed here.

What was found is PII and one signpost, both **already on `origin/master`**, both
verified as ancestors of it with `git merge-base --is-ancestor`:

| Commit | Date | What it publishes | Public for |
|---|---|---|---|
| `3bd5b0b` | 2026-02-20 | Historical `CLAUDE.md`: full name, employer, job title, and **one line naming the local directory where API keys are stored** | ~6 months 3 weeks |
| `5622cca` | 2026-06-09 | Historical `CLAUDE.md`: three email addresses | ~3 months |

The exact path is deliberately **not reproduced in this file**, because this file lives
in the public repository and restating it would create a fresh, prominent pointer to
where the keys are. It is written out in full, with the reasoning and the remediation
options, in a private note outside this repo:

```
~/.claude/SECURITY-FINDING-2026-09-10-public-history-pii.md
```

`CLAUDE.md` is no longer in the tree at the tip — commit `25eddb6` untracked it and the
repo now ships `CLAUDE.md.example` only. **That fixed the tip and does nothing to
history.**

**Nothing was force-pushed, rewritten or deleted**, per the brief. There is also a
practical reason a rewrite is not remediation here: this repository has **57 stars and
14 forks**. Rewriting upstream history does not touch a fork, and fork-network objects
stay reachable by SHA. Anyone who cloned in the last six months already has it.

**Two decisions are Austin's and were left to him.** Whether this repository should be
public at all — the brief says that has never actually been decided — and whether to
rotate on the basis that the keys folder's location has been public for six months.

---

## 2. Job one — the browser bridge

### What actually happened on 2026-09-10, evidenced

The incident report's headline hypothesis was that the tab list was stale. **It was
not.** `handleGetTabs` in `extension/background.js` does a live `chrome.tabs.query({})`
on every call, and a live reproduction this session showed a newly created tab appear in
the very next `browser_get_tabs`. The list was accurate. The tab had been **destroyed**.

Root cause, from `~/.claude/mcp-debug.log`:

```
12:39:22  PID 10776  sessionId=04cd2eca  project=intellegix-relay
12:43:06  relay stdin closed — parent exited
12:43:06  relay session orphaned: 04cd2eca
12:43:08  PID 22680  sessionId=a643b291
```

Each bridge process gets a fresh random `sessionId` (`server.js:80`), tabs are grouped by
it, and a relay disconnect made `websocket-bridge.js` broadcast `session_cleanup`
**immediately** — closing every tab in that group. The lane's process exited at 12:43:06
and its replacement connected 2.3 seconds later under a new id, so the restart could not
reclaim its own tabs. That is 05:39 / 05:43 PT, exactly the incident window. It also
matches the "stuck tabs disappear between attempts" symptom in
`NAVIGATE-HANG-EVIDENCE-2026-08-29.md`.

### Corrections to the brief

The brief's hypotheses were explicitly marked unverified. Two do not survive:

- **"Tab list is stale" — REFUTED.** Live query, verified by reproduction. The real
  fault was destruction, not staleness.
- **"relay.mjs throws away a 7-vs-5 discrepancy" — REFUTED as described.** `relay.mjs:194`
  filters on `^https?:` to build the takeover picker; `chrome://newtab` is not
  controllable by design. No second-browser signal is being discarded there.
- **"Navigate silently retargets" — CONFIRMED** (`background.js:656-668`), reproduced.
- **"Blind to other browsers, reports partial state as complete" — CONFIRMED**, and this
  is the design flaw the brief singled out. Fixed.

### The fix

Everything is server-side. **No tool was added, renamed or re-described, and no tool
description changed** — a tool-list change invalidates the prompt cache at position 0 for
every lane on this machine. No fleet restart was needed or performed; the new code takes
effect as each lane restarts its own bridge process naturally. The session-keeper Chrome
on port 9223 was never touched.

- **`lib/browser-discovery.js`** (new) — enumerates Chromium browser *roots* on Windows
  via `Get-CimInstance Win32_Process` (`wmic` is gone in 24H2), identifies them by the
  absence of a `--type=` switch, reads `DevToolsActivePort`, and does a bounded loopback
  CDP probe with an explicit `Host` header. **Detect and declare only — it never attaches
  to another browser.** 15-second cache.
- **`browser_get_tabs`** now returns a `coverage` envelope before the data: `status`
  (`COMPLETE` / `PARTIAL` / `SCOPED` / `UNAVAILABLE`), `negativeEvidence`
  (`SAFE_WITHIN_DECLARED_SCOPE` / `UNSAFE` / `UNKNOWN`), `observedCount` vs
  `detectedCount`, and `unobserved[]` naming the CDP endpoint to try. When a negative
  conclusion would be invalid it prepends a banner: `RESULT STATUS: PARTIAL — NEGATIVE
  EVIDENCE UNSAFE`, and the summary names the invalid inference in words rather than
  hedging: *this is NOT evidence that the tab is not open*. The envelope is **always
  present**, so the ordinary complete case is an explicit "complete", not silence.
- **`browser_navigate`** now returns `requestedTabId`, `retargeted` and `retargetReason`
  instead of `success: true` on a tab the caller never asked for.
- **`websocket-bridge.js` + `config.js`** — `session_cleanup` is deferred by
  `sessionCleanupGrace` (45s) and **cancelled** if a relay reconnects for the same project
  path. A genuinely dead session still gets cleaned up, 45 seconds later.

Design informed by two Perplexity passes, each authored through the `prompt-engineering`
skill with real code and real symptoms attached. The precedents it surfaced and the fix
follows: Elasticsearch `_shards` / `timed_out`, GraphQL returning `data` *and* `errors`,
the DNS truncation bit, and HTTP 206 declaring its range. All four say the same thing —
**a partial result is a success that states its own scope**, not an error, and the scope
goes before the data.

### Verified

- 18 new tests in `test-coverage-honesty.js`, all passing; they fail against the pre-fix
  code. Proven by running the backed-up original: it sends `session_cleanup`
  synchronously and has no `_scheduleSessionCleanup`; the new code sends none and cancels
  on reconnect.
- 10/10 `npm test`; 60/60 across the existing handler, context-manager and reliability
  suites.
- Live census: 382ms, correctly detected both Chromes and named
  `http://127.0.0.1:9223` / `session_keeper_profile`.
- Full end-to-end over real MCP stdio: the `PARTIAL` / `UNSAFE` banner appears and the
  retarget disclosure is correct.
- **Perplexity verified working through the patched server** (`PPLX_BLOCKS=1`,
  `PPLX_ISERROR=false`), because the research pipeline for every lane runs through this
  bridge.

One bug in my own fix was caught by that end-to-end run and is worth recording:
`observedCount` was 0 for every lane, because relay-mode processes have an empty
`browserClients` map and **every lane runs in relay mode**. Fixed by querying the
primary's `/health` for `bridge.browserCount`. The unit tests all passed while this was
broken; only the end-to-end run found it.

---

## 3. Job two — the repo

`origin` was 25 commits ahead, local had 8 `origin` had never seen, and an untracked
`__pycache__` sat in the tree.

**PR #92** — all checks green, labelled `needs-austin`. It carries the content of those
8 commits, the browser-bridge fix, and the dependency work. Two things were deliberately
left out:

- **The CRLF flip.** Local commit `0ec3912` rewrote 211 of 218 tracked text files from LF
  to CRLF without changing one line of content (`+61050/-61050`). All 218 text blobs on
  `origin/master` were checked and every one is LF-only. The files in #92 are staged as
  LF, so its diff is content only: 22 files, not 227. A new `.gitattributes` pins
  `* text=auto` — a no-op against the current tree, there purely to stop the next
  automated sweep repeating it.
- **Ten `.bak-2026-08-22-*` snapshots** (~14k lines of stale duplicates). Verified every
  one still exists on disk under `~/.claude` before excluding them, so nothing is lost,
  and git already holds the prior versions.

### The five Dependabot PRs — the reason not to bulk-merge on title

Three of the five were **failing CI and could not have passed**, which the titles do not
show. `github/codeql-action/{init,autobuild,analyze,upload-sarif}` is one action that
refuses to run at mixed versions. With no `groups` config, Dependabot opened a separate PR
per sub-action, so merging any single one leaves the workflow half-bumped:

```
Loaded a configuration file for version '4.37.9', but running version '4.37.6'
```

- **#87, #88, #90** — failing `Analyze (Python)` for that reason. Superseded by #92, which
  moves all four references together. Commented, left open until #92 lands.
- **#89** — green only because `scorecard.yml`'s `upload-sarif` is the sole
  `codeql-action` step in that file. Rolled into #92 anyway so the repo is never mixed.
- **#91** (pydantic `>=2.13.4` → `>=2.13.5`) — diff read line by line, and 2.13.5 confirmed
  on PyPI: uploaded 2026-08-28, not yanked, currently latest. Green and ready.

The SHA was verified **against the upstream repository, not the PR title**: the annotated
tag `v4.37.9` in `github/codeql-action` resolves to `cdf488f5…`, and the SHA being replaced
resolves to `v4.37.6`. `.github/dependabot.yml` now groups the family so the split cannot
recur. PR #92 passing `Analyze (Python)` is the proof the combined bump works where the
split ones could not.

### Why nothing merged

`master` requires **one approving review** (`required_approving_review_count: 1`,
read from the branch-protection API, not inferred from the refusal message). That is
deliberate on a public repo and I did not use `--admin` to go around it. All five
Dependabot PRs plus #92 are labelled `needs-austin` with the decision stated in plain
language in a comment.

### Local checkout

Working tree is clean; the `__pycache__` is gone and `.gitignore` gains a
`**/__pycache__/` catch-all in #92. Local `master` is intentionally **left as it was**
until #92 merges — resetting it now would discard content `origin` does not yet have. A
safety branch `backup/local-master-pre-sync-2026-09-10` was created first and holds all 8
commits, including the three `.bak` blobs that exist only in git and not on disk. Once
#92 merges, the whole reconciliation is one command:

```
git checkout master && git reset --hard origin/master
```

---

## 4. Other findings, with confidence and severity

| Finding | Confidence | Severity |
|---|---|---|
| **The Chrome extension exists in three copies and the live one matches neither source.** `background.js`: repo 1835 lines, `~/.claude` 2016, the Dropbox copy Chrome actually loads 1862. `content.js` runs the other way — the live copy is 572 against 593 in both sources. Chrome loads the Dropbox copy. Not touched: deploying an unreviewed merge could break the bridge for the whole fleet. | CONFIRMED | **High** — the code being audited is not the code running |
| `sessionId` is regenerated per process (`server.js:80`), so a restart can never reclaim its own tab group by identity. The 45s grace works around this; keying on project path would fix it properly. | CONFIRMED | Medium |
| CI runs `automated-loop`, `health-check` and `minecraft` only. The three new `council-automation` test modules and the 18 browser-bridge node tests are **not run by CI**. Not wired up here — they need a browser and would make CI flaky. | CONFIRMED | Medium |
| `.bak-*` files accumulate inside a public repo through an automated commit sweep with nobody reading them. Ten were about to be pushed. | CONFIRMED | Medium |
| The `browser_get_tabs` **tool description** was deliberately left unchanged, so a model that never reads the response body has no advance warning the envelope exists. Changing it would invalidate the prompt cache fleet-wide. Worth batching into the next unavoidable tool-list change. | CONFIRMED | Low, but a real gap |
| PR #52 is from an outside contributor (`mouse-value-add`), open since 2026-07-03. Not reviewed — out of scope, and it is a product call on a repo whose visibility is unresolved. | CONFIRMED | Informational |
| Benign scanner hits at the tip, checked individually and all fine: docker-compose example creds in `agents/devops.md:122`, `automated-loop/.env.example`, a deliberately fake truncated key in `automated-loop/tests/test_log_redactor.py:19`, a `nonexistent.invalid` URL in `health-check/tests/test_hc_db_pg.py:73`, and "BAD!" doc examples in `patterns/SECURITY_CHECKLIST.md:42` and `rules/python-scripts.md:109`. | CONFIRMED | None |

---

## 5. Claims table

| Claim | Evidence | Status |
|---|---|---|
| Tab list is live, not stale | `extension/background.js` `handleGetTabs` → `chrome.tabs.query({})`; live repro, new tab appeared in the next call | CONFIRMED |
| Tabs were destroyed by `session_cleanup` on relay disconnect | `~/.claude/mcp-debug.log` 12:39:22 / 12:43:06 / 12:43:08 sequence | CONFIRMED |
| `sessionId` is fresh per process | `server.js:80` `this.sessionId = randomUUID()` | CONFIRMED |
| Navigate silently retargets | `extension/background.js:656-668`; reproduced 1435686256 → 1435686303 with `success: true` | CONFIRMED |
| relay.mjs's "7 tabs, 5 controllable" is a URL-scheme filter, not a coverage signal | `relay.mjs:194` `/^https?:/i` filter, printed at line 200 | CONFIRMED — refutes the brief |
| Two Chrome instances were running; only one was reachable | live census, 382ms, found `session_keeper_profile` on `127.0.0.1:9223` | CONFIRMED |
| The fix does not change the tool list | no add/rename/description change in `server.js`; all edits are response fields and `lib/*` | CONFIRMED |
| Perplexity still works after the change | end-to-end through the patched server, `PPLX_BLOCKS=1`, `PPLX_ISERROR=false` | CONFIRMED |
| Nothing about to be pushed contains a credential | scan of the exact staged bytes of all 21 files, 14 credential patterns, **with a positive control proving the scanner was reading them** | CONFIRMED |
| An earlier version of that scan was worthless | it reported 0 hits while `grep` found matches in the same bytes; re-run with a positive control | CONFIRMED — my own error, caught before the push |
| `3bd5b0b` and `5622cca` are ancestors of `origin/master` | `git merge-base --is-ancestor`, both exit 0 | CONFIRMED |
| Repo is public with 57 stars and 14 forks | `gh repo view --json isPrivate,visibility,createdAt,stargazerCount,forkCount` | CONFIRMED |
| `origin/master` is entirely LF | all 218 text blobs read and counted; 0 contain CRLF | CONFIRMED |
| `0ec3912` is line-ending noise | 227 files differ, 22 differ under `--ignore-cr-at-eol` | CONFIRMED |
| Three codeql PRs cannot pass CI individually | run 33384590794 error line, quoted above; PR #92 with all four bumped passes the same check | CONFIRMED |
| `cdf488f5…` really is codeql-action v4.37.9 | `gh api repos/github/codeql-action/commits/v4.37.9` | CONFIRMED |
| pydantic 2.13.5 is real and not yanked | PyPI JSON API, uploaded 2026-08-28 | CONFIRMED |
| `master` requires 1 approving review | `gh api .../branches/master/protection` → `required_approving_review_count: 1` | CONFIRMED |
| Keying tab groups on project path instead of `sessionId` would remove the need for the grace period | reasoning from the code, not implemented or tested | HYPOTHESIS |
| The extension three-way drift is causing other unexplained bridge behaviour | drift is measured; a causal link to any specific bug is not | HYPOTHESIS |

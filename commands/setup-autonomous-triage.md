# /setup-autonomous-triage — Install the autonomous triage loop in a host app

Stand up the always-on error + inefficiency triage pattern from `patterns/AUTONOMOUS_TRIAGE_PATTERN.md` inside the current working directory's host app. Interactive: asks about the host app's stack, prereqs, and admin identity, then walks through the 9 phases in order with checkpoints between each.

**Time budget:** ~1-2 hours for a Next.js + Prisma + Vercel host app (the reference stack). ~3-4 hours for a stack that needs adaptation (Python, Rails, Go).

**Cost per host app after setup:** ~$0.25-$5.60/month depending on user count (see the pattern doc's cost model).

**Non-goal:** this command does not deploy or admin-merge anything. It writes code, drops schema, wires env stubs, and hands off for you to review + push.

---

## Prerequisites — the receiving Claude MUST verify these before starting

Skim the host app before writing anything. Report back to the user which prereqs are satisfied and which will need stubs.

- [ ] **Per-user audit event stream** — any of: a `audit_events`-shaped table, Sentry SDK with `beforeSend` hook, Segment/Posthog with per-user identifiers, or a custom client-side logger.
- [ ] **Cron scheduler** — Vercel Cron, GitHub Actions cron, Cloudflare Cron Triggers, AWS EventBridge, or an external pinger.
- [ ] **Object storage** — Vercel Blob (private), S3 (with signed URL support), R2, or writeable local disk.
- [ ] **Admin auth gate** — some way to check "is this request from an admin?" server-side.
- [ ] **`GITHUB_PAT` env** — a Personal Access Token with `repo` scope. This is the least portable prereq and is required.
- [ ] **`ANTHROPIC_API_KEY` env** — Claude API access.
- [ ] **Per-user notification abstraction** — a function or endpoint that takes `(userId, message)` and delivers to that specific user. Bell feeds, Slack DMs, Pushover with per-user keys, email — any single channel is enough.

If any of these is missing, note the gap in your Phase 1 output and use `TODO(prereq: X)` stubs in the affected file. Do NOT halt setup.

---

## Read this first — the pattern doc

Before any file writes: read `patterns/AUTONOMOUS_TRIAGE_PATTERN.md` end-to-end. It's ~700 lines, ~30 minutes of reading. The command below assumes you have that context.

---

## Phase 1 — Scope + adaptation planning

Ask the user (via `AskUserQuestion` or similar):

1. **Host app path** — confirm the working directory. Report what you see: framework, ORM, DB, cron system, blob system, auth.
2. **User count + rough error volume** — needed to set the initial per-user error cluster threshold. Under 20 users, use `COUNT >= 3`. 20-100 users, `COUNT >= 5`. Over 100, `COUNT >= 10`.
3. **Admin identity for the mirror** — the user_id whose bell will receive a copy of every notification (matches the reference-impl's `ADMIN_MIRROR_USER_ID` in `notify.ts`).
4. **Notification channel choice** — which per-user pipe to wire (bell + Web Push, or email, or Slack, or Pushover). The pattern is channel-agnostic; the setup wires one.
5. **Vercel Cron plan tier** — Hobby caps at once-per-day; sub-daily needs Pro. If Hobby, either upgrade or fall back to GitHub Actions cron.

Report the adaptation matrix — copy the rows from `patterns/AUTONOMOUS_TRIAGE_PATTERN.md` §"Adaptation matrix" that apply, then confirm with the user before proceeding.

---

## Phase 2 — Schema

Drop `reference-impl/autonomous-triage/schema/triage_tables.prisma` (or its adapted equivalent) into the host app's schema. Two additive models:
- `triage_tickets` — one row per unique detected pattern; state machine `open → in_progress → resolved | skipped | archived`.
- `system_state` — key/value store for cron watermarks.

**Adaptation checkpoints:**
- Prisma → Drizzle/TypeORM/SQLAlchemy: regenerate from the Prisma DSL.
- Postgres arrays → JSON if the target is MySQL or SQLite.
- Rename `pplx_response` if you want — it's the historical column name from the initial Perplexity gate design. Keeping it saves migration effort.

**Deploy step:** the receiving Claude runs the schema migration but DOES NOT pass `--accept-data-loss` (or the ORM equivalent) without listing what would drop first. This is a hard rule — the reference-impl session accidentally dropped 14 legacy rows this way; don't repeat.

**Confirm with the user before running the migration on a production DB.**

---

## Phase 3 — Env vars

Set two new env vars on the host's Prod + Preview:

```bash
# Random 32-byte hex, guards /admin/triage mutation endpoints
TRIAGE_ADMIN_KEY=<generate: openssl rand -hex 32>

# Existing gh CLI token or a fresh PAT with `repo` scope
GITHUB_PAT=<gh auth token>
```

Two more are typically already there:
- `ANTHROPIC_API_KEY` — from the host app's existing Claude usage
- `CRON_SECRET` — cron endpoint bearer auth

If the host app already had a Perplexity API key set, ignore it — this pattern's runtime does NOT use Perplexity (see the pattern doc for the reasoning). Perplexity via `research_query` remains valuable at design time.

---

## Phase 4 — Detection cron (auto-triage Stage A)

Drop these files under the host app's route structure:
- `src/lib/triage/signals.ts` — the two SQL detectors + `computeFingerprint()`.
- `src/app/api/cron/auto-triage/route.ts` — Stage A only for this phase; Stages B and C are added in Phase 5-6.

**Adaptation checkpoints:**
- Postgres INTERVAL syntax + regex functions (`~*`, `regexp_replace(...)`) — if the target DB is MySQL/SQLite, port each raw SQL block.
- The `audit_events` column list (`event_type`, `route`, `element_text`, `fetch_url`, `fetch_method`, `fetch_status`, `error_msg`, `user_id`, `session_id`, `occurred_at`) — if the host app's audit stream uses different column names, rewrite the queries.
- If the host app has no session concept in its audit stream, the rage-click detector doesn't work — omit `detectInefficiency` and ship error clustering only.
- Threshold: use the number picked in Phase 1.

Register the cron entry in `vercel.json` (or the host's cron config): `*/15 * * * *`. `maxDuration = 30`.

**Test manually:**
```bash
curl -H "Authorization: Bearer $CRON_SECRET" https://<host>.vercel.app/api/cron/auto-triage
# Expect: { stageA: { opened, updated, errors } }
```

---

## Phase 5 — Sonnet gate + blast-radius classifier (Stage B)

Drop:
- `src/lib/triage/sonnet-gate.ts` — Sonnet 4.5 with structured JSON output. Never throws.
- `src/lib/triage/blast-radius.ts` — pure static rule table. First-match strictness wins.
- `tests/unit/lib/triage/blast-radius.test.ts` — 19 unit tests.

Wire Stage B into the cron. Refer to `reference-impl/autonomous-triage/src/app/api/cron/auto-triage/route.ts` for the exact structure — Stage B is a bounded loop (10 tickets/run) that runs classifyTicket, applies classifyBlastRadius as authoritative veto, persists, and skips-or-continues.

**Adaptation checkpoints:**
- The blast-radius rule table's paths are Next.js/Prisma-shaped. Swap for the target stack — the STRUCTURE (four levels, first-match, authoritative-veto semantics) stays; the paths change.
- Sonnet call uses raw `fetch`; works in any JS runtime. Python port: `httpx` + `pydantic`.

**Test the classifier:**
```bash
npx jest tests/unit/lib/triage/blast-radius.test.ts
# Expect: 19 passed
```

**Test the gate end-to-end:** manually insert a fake `triage_tickets` row, hit the cron, verify `pplx_response` populates and the ticket transitions correctly.

---

## Phase 6 — Patch generator (Stage C)

Drop:
- `src/lib/triage/patch-generator.ts` — GitHub API + Sonnet + Vercel Blob upload.

**Hardcodes to extract at drop time:**
```typescript
const REPO_OWNER = 'intellegix';               // → env var HOST_GITHUB_OWNER
const REPO_NAME = 'ASR-PO-System-Enterprise';  // → env var HOST_GITHUB_REPO
```

Set both env vars on Prod + Preview.

**Adaptation checkpoints:**
- Vercel Blob → S3/R2/local: rewrite the `put()` call. The dashboard's blob proxy (Phase 8) needs a matching `head()` equivalent.
- If the host app can't fetch from GitHub Contents API (private repo without PAT, etc.), the patch generator can't work. This is a hard-block on the pattern.

Wire Stage C into the cron similarly to Stage B — bounded to 3 tickets/run.

---

## Phase 7 — Admin dashboard

Drop the four files:
- `src/app/(admin)/admin/triage/page.tsx` — MUI Table + row-expand + actions.
- `src/app/api/admin/triage/route.ts` — list + counts.
- `src/app/api/admin/triage/[id]/action/route.ts` — approve/skip/archive.
- `src/app/api/admin/triage/[id]/blob/route.ts` — admin-authed blob proxy.

**Adaptation checkpoints:**
- Auth: replace `getServerSession(authOptions)` + `isAdmin(role)` with the host app's admin check.
- MUI + React Query: if the host uses Tailwind or another UI kit, port the page.tsx — API endpoints stay the same.
- Add a nav link to the admin sidebar. Reference-impl uses `HealingIcon` from `@mui/icons-material`.

**Test:** sign in as admin → sidebar has `/admin/triage` → any in_progress ticket shows an Approve button that copies the apply command block to clipboard.

---

## Phase 8 — Close-loop + cleanup crons

Drop:
- `src/lib/triage/close-loop.ts` — GH PR poll, watermark, resolve-and-notify.
- `src/app/api/cron/triage-close-loop/route.ts` — every 5 min.
- `src/app/api/cron/triage-cleanup/route.ts` — nightly 03:00 UTC.

**Adaptation checkpoints:**
- The close-loop cron calls `notifyUser(userId, {...})`. Wire this to the host app's per-user notification channel picked in Phase 1. If the host doesn't have one yet, wire a simple email fallback.
- Extract the GH owner/name hardcodes to env vars (same as Phase 6).
- Register both cron entries in the host's cron config.

**Test the close-loop:**
```bash
curl -H "Authorization: Bearer $CRON_SECRET" https://<host>.vercel.app/api/cron/triage-close-loop
# Expect: { priorWatermark, newWatermark, seen: 0, resolved: 0, ... }
```

---

## Phase 9 — Wrap + hand off

Update the host app's `CLAUDE.md` with a note pointing at the new dashboard and describing the operational shape. Suggested paragraph:

```markdown
## Autonomous triage

- `/admin/triage` — human-in-loop review of Sonnet-drafted patches for errors + rage-click inefficiency detected in `audit_events`.
- Cron `*/15` → detect + gate; cron `*/5` → close-loop; cron nightly → cleanup.
- Approve action copies a `git apply` command sequence for local execution.
- Cost: ~$0.25-$1/mo baseline for internal-app scale (see patterns/AUTONOMOUS_TRIAGE_PATTERN.md).
```

Update the host app's project MEMORY.md (if it uses the toolkit's memory system) with a topic entry pointing at the pattern doc.

Open a single PR per phase, OR a single wrapped PR — user's preference. Reference-impl was 6 PRs (#49-54) across 9 phases for clean review.

Hand off to the user: "Everything's shipped. Watch `/admin/triage` for the first ticket."

---

## Failure modes to expect + recover from

- **Sonnet responds with prose instead of JSON.** The Zod validator returns `sonnet_parse_fail`. Ticket stays open, retried next cron. If persistent, the prompt drifted — check what the host app's error patterns look like in Sonnet's context.
- **GitHub API rate-limited.** Watermark still advances (so we don't reprocess). Next 5-min cycle picks up. If persistent, the PAT hit its limit — get a fresh one.
- **Vercel Blob unavailable.** Patch generation skips with `blob_upload_failed`. Ticket transitions to `skipped`. Retryable manually via the dashboard's re-run action (not built in MVP; add if this becomes common).
- **Ticket table growing unbounded.** Check the nightly cleanup cron is firing. If it's not — check `CRON_SECRET` and the cron config.
- **Admin never approves anything.** Check Pushover / notification setup; the batched end-of-run ping is a soft nudge, not a required signal. Consider a stronger reminder if tickets pile up.

---

## Related toolkit resources

- `patterns/AUTONOMOUS_TRIAGE_PATTERN.md` — architecture, rationale, adaptation matrix.
- `patterns/API_PATTERNS.md` — API design patterns; useful when adapting the cron endpoints.
- `patterns/SECURITY_CHECKLIST.md` — apply to the blob proxy + admin routes.
- `patterns/TESTING_PATTERNS.md` — the pattern's test suite is minimal (19 tests, blast-radius only); expand per host app's testing norms.

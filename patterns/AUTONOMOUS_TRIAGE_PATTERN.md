# AUTONOMOUS_TRIAGE_PATTERN.md

An always-on error + inefficiency triage loop for a small-team internal app. Detects real user friction in production, has an LLM propose a fix, applies static-rules safety checks over the proposal, drafts a patch, waits for a human to approve + merge, then notifies the affected users the moment their issue is fixed and live.

Human-in-loop at the MERGE step only. Everything upstream (detection, classification, patch draft) is autonomous.

**Reference implementation:** `reference-impl/autonomous-triage/` (Next.js 16 + Prisma 5 + Neon Postgres + Vercel Blueprint, shipped 2026-07-10, ~2200 LOC).

**When to use this pattern:** an internal app with <20 users, a per-user audit stream, a cron scheduler, and an admin who can approve merges within a day. Cost at 8-user signal density: ~$0.32/month.

**When NOT to use:** consumer-scale apps (this is not a distributed telemetry system), regulated environments where autonomous code drafting requires ISO 27001 attestation, or teams without an admin willing to be the merge gate.

---

## The pipeline in one picture

```
Cron (*/15 min)                Cron (*/5 min)             Cron (nightly)
      │                              │                         │
      ▼                              ▼                         ▼
┌───────────────┐             ┌────────────────┐        ┌────────────────┐
│ auto-triage   │             │ close-loop     │        │ triage-cleanup │
│ Stage A: SQL  │             │ Poll GH PRs    │        │ 30d prune +    │
│ Stage B: gate │             │ Match          │        │ orphan sweep   │
│ Stage C: patch│             │ triage/<uuid>  │        │                │
└───────────────┘             │ → notify users │        └────────────────┘
      │                       └────────────────┘
      ▼                              ▲
 triage_tickets                Admin merges PR
      │                              ▲
      ▼                              │
 /admin/triage — review, approve locally, apply patch, push, PR
```

The three crons + one admin page is the whole system. The rest is code that fills those slots.

---

## Prerequisites in the host app

The pattern's runtime depends on **7 things** the host app must expose. If any is missing, the setup command emits `TODO(prereq: X)` stubs and the receiving Claude fills them in.

| # | Prereq | Ref-impl uses | Portable substitute |
|---|---|---|---|
| 1 | Per-user audit event stream | `audit_events` table with `event_type`, `route`, `element_text`, `fetch_url`, `fetch_method`, `fetch_status`, `error_msg`, `user_id`, `session_id`, `occurred_at` | Any per-user click/fetch/error log. If the host app has none, this pattern isn't yet applicable — first ship a client-side audit stream (see `patterns/API_PATTERNS.md` for shape) |
| 2 | Cron scheduler | `vercel.json` cron entries | Any per-schedule executor: Vercel Cron, GitHub Actions cron, Cloudflare Cron Triggers, systemd timers, or a `cron`-labelled endpoint hit by an external pinger |
| 3 | Object storage for patch artifacts | Vercel Blob (`@vercel/blob`, `access: 'private'`) | S3 + signed URLs, R2, or a local `/tmp/triage-patches/` dir for single-node deployments |
| 4 | Admin auth gate | NextAuth 4 + `isAdmin(user.role)` | Any admin check exposed to server code |
| 5 | GitHub PAT with `repo` scope | `GITHUB_PAT` env var | Same — the PAT is the least-portable piece and is required for repo access |
| 6 | Anthropic API key | `ANTHROPIC_API_KEY` env var | Same |
| 7 | Per-user notification channel | `user_notifications` bell row + Web Push via `notifyUser()` helper | Any per-user pipe: SES, Pushover with per-user keys, Slack DMs, Discord webhooks, or a bell-feed table |

**Explicit non-prerequisite:** the host app does NOT need Prisma. The reference-impl uses Prisma because the host does; port the two Prisma models to whatever ORM/SQL the target uses. Same for MUI on the dashboard — the pattern is React-optional.

---

## Component-by-component walkthrough

Each subsection maps directly to a file under `reference-impl/autonomous-triage/`. Read the file and this section side-by-side.

### Schema (`schema/triage_tables.prisma`)

Two models. `triage_tickets` is the state store for one row per unique detected pattern. `system_state` is a key/value scratch space for cron watermarks.

**Portability notes:**
- `id` uses Postgres `gen_random_uuid()`. Swap for SQLite `randomblob(16)` or app-side UUID.
- `String[]` and `Int[]` are Postgres native arrays. On MySQL/SQLite, use `Json`.
- `pplx_response Json?` — the column name is a historical artifact of an initial Perplexity design; kept for schema stability during the pivot to Sonnet. Rename freely.
- Indexes optimize for `WHERE status = 'open' AND pplx_response IS NULL` (Stage B pickup) and `WHERE status = 'in_progress' AND patch_blob_pathname IS NULL` (Stage C pickup). Keep both if you keep the pipeline.

### Signal detection (`src/lib/triage/signals.ts`)

Two exported functions, both raw SQL over `audit_events`:
- `detectErrorClusters(prisma)` — per-user URL+status bucketing (COUNT >= 3) UNION cross-user error_msg pattern matching (DISTINCT user_id >= 2).
- `detectInefficiency(prisma)` — rage-click detection via SQL window function.

**Threshold calibration is empirical.** The `COUNT >= 3` per-user threshold was chosen after measuring 30 days of the reference-impl's actual signal (5 real active users, ~7 clusters/month). At different scales:
- 100 users → keep 3 or lift to 5
- 1000 users → lift to 10 and add a cross-user weight
- Under 10 users → the pattern is signal-poor; consider a rules-only version without the LLM gate

**Portability notes:**
- The SQL uses Postgres `INTERVAL` syntax and regex functions (`~*`, `regexp_replace(...)`). SQLite / MySQL need equivalents.
- "User reached goal" definition: any 2xx `POST`/`PATCH`/`PUT` fetch in the same `session_id` within the window. If the host app has a different mutation pattern, adjust.
- `element_text` for rage-click identification is the button label. Ensure the audit stream captures this and not just an anonymous DOM path.

### Sonnet triage gate (`src/lib/triage/sonnet-gate.ts`)

Sends a triage_ticket snapshot to Claude Sonnet 4.5 with structured JSON output (Zod-validated). Returns:

```typescript
{ fixable, severity, approach, blast_radius, estimated_users_affected, file_hints[], skip_reason? }
```

**Never throws.** Any failure — timeout, non-2xx, empty response, parse failure, schema mismatch — returns `{ fixable: false, skip_reason: 'sonnet_...' }` so the caller proceeds uniformly.

**Why Sonnet and not Perplexity, GPT-4, or Gemini:**
- Perplexity's browser-driven API (via Playwright in the toolkit's `research_query`) can't be called from a server cron.
- Sonnet's structured-JSON adherence with `response_format` + Zod is more reliable than GPT-4's function-calling at this size.
- Cost per gate call is ~$0.005; at 7 tickets/month that's $0.04/month baseline.

**Portability:** the SDK client is a raw `fetch` (not the Anthropic SDK) so this works in any JS runtime. Port to Python by lifting the prompt + Zod schema into pydantic.

### Blast-radius classifier (`src/lib/triage/blast-radius.ts`)

Pure static rule table. First-match-wins on file paths. Has **authoritative veto** over the Sonnet gate — can only make Sonnet's verdict MORE restrictive, never less.

The rules ranked strictest-to-loosest:
- `HARD_BLOCK`: `prisma/schema.prisma`, `prisma/migrations/`, `lib/auth/`, `middleware.ts`, `.env*`, `**/credential*|secret|token|apikey*`
- `HARD_BLOCK` (with diff content): any `**/route.ts` that adds a `prisma.*.{create|update|delete|upsert|executeRaw}` call
- `HIGH`: `next.config.*`, `package.json`, `package-lock.json`
- `MEDIUM`: `**/api/**/route.ts` (no DB write), `**/components/**/*.tsx` with logic
- `LOW`: `**/*.tsx` (JSX text-only), `**/lib/**/*.ts` (Zod tightening, null-checks, prompt strings), test files
- Default: `MEDIUM` (conservative)

**Called twice:**
1. After the Sonnet gate, over Sonnet's `file_hints[]` — cheap pre-filter before we pay for patch generation.
2. After patch generation, over the ACTUAL `+++ b/<path>` lines from the diff PLUS the diff content — the belt-and-suspenders check.

**Portability:** these rules are 100% host-agnostic in structure but Next.js/Prisma-shaped by content. Swap file paths for the target stack (`app/routes/` in Rails, `pkg/api/` in Go, etc.).

### Patch generator (`src/lib/triage/patch-generator.ts`)

For a ticket where Sonnet said `fixable=true` and the classifier didn't `HARD_BLOCK`:
1. Fetch current master SHA via `GET /repos/{owner}/{name}/git/refs/heads/master` with the `GITHUB_PAT`.
2. Fetch each `file_hint`'s contents at that SHA. Hallucinated hints (404) are silently dropped. If < 1 real file, skip.
3. Sonnet drafts a unified diff + summary using the two-section fence:
   ```
   --- summary ---
   ...
   --- diff ---
   ...
   ```
4. Post-generation guard: run the classifier over the ACTUAL `+++ b/<path>` diff paths + diff content. If HARD_BLOCK, don't upload.
5. Upload `summary.md` + `suggested-fix.patch` to private blob storage.
6. Mark ticket `patch_blob_pathname`, `patch_summary`, `master_sha`, `status='in_progress'`.

**Prompt budgets baked in:** `MAX_FILE_HINTS = 5`, `MAX_FILE_LINES = 400`, `MAX_TOTAL_FILE_CHARS = 40_000`, `PATCH_TIMEOUT_MS = 40_000`.

**Cost:** ~$0.05-0.15 per patch call. At 2 patches/month, ~$0.20/month.

**Portability:**
- GitHub owner + repo name are hardcoded at the top of the file. Extract to env vars for portability.
- Vercel Blob upload → S3 with a `contentType` header and signed download URL for the dashboard proxy.
- The `two-section fence` output format is important — the parser splits on it. Keep it if you swap models.

### Admin dashboard (`src/app/(admin)/admin/triage/page.tsx` + `src/app/api/admin/triage/*`)

MUI Table with row-expand, streams diff from private blob via admin-authed proxy, three actions:
- **Approve** — marks `approved_by_user_id` + `approved_at`; copies a full `git checkout <sha> && git checkout -b triage/<uuid> && curl ... && git apply ...` command to the clipboard. Ticket stays `in_progress`.
- **Skip** — opens a reason dialog; marks `status='skipped'`.
- **Archive** — hides from default view.

**The admin never touches git through the dashboard.** The apply command runs on their laptop in their own Claude Code session. The dashboard is a review-and-approve UI, not an executor.

**Portability:**
- The Table is MUI + React Query. Port to any UI kit.
- Auth gate is `getServerSession(authOptions)` + `isAdmin(user.role)`. Use the host app's admin check.
- Blob proxy: `head()` the private blob to get its signed URL, then `fetch()` and stream through. Any S3-equivalent works.

### Close-loop cron (`src/app/api/cron/triage-close-loop/route.ts` + `src/lib/triage/close-loop.ts`)

Every 5 minutes:
1. Read watermark from `system_state.triage_close_loop.value.last_processed_pr_number`.
2. `GET /repos/{owner}/{name}/pulls?state=closed&sort=updated&direction=desc&per_page=50&base=master` with the PAT.
3. Filter for `merged_at != null` AND `number > watermark`.
4. For each PR whose head branch matches `triage/<uuid>`:
   - Look up the ticket.
   - If `status='in_progress' AND resolved_at IS NULL`, use `updateMany` with those conditions in the WHERE — count=0 means another cron pass already claimed it and we skip.
   - For each `affected_user_id`, call `notifyUser({ title, body, kind, url })` which writes the bell row + sends Web Push + mirrors to admin.
5. Advance the watermark to the highest PR number seen (even for non-triage PRs).

**Portability:**
- GH poll: same as patch-gen, hardcode → env var.
- The `notifyUser` helper is the host app's per-user notification abstraction — bell + push + email + whatever. Its interface is `notifyUser(userId, { title, body, url?, kind })`. Ship this ABSTRACTION expectation, not a specific implementation.

### Cleanup cron (`src/app/api/cron/triage-cleanup/route.ts`)

Nightly at 03:00 UTC:
1. `DELETE FROM triage_tickets WHERE status IN ('resolved','archived','skipped') AND updated_at < NOW() - INTERVAL '30 days'`.
2. Raw SQL: strip orphan user_ids from `affected_user_ids` arrays and recompute `estimated_users`.

Tiny, uneventful. Not strictly required, but keeps the table lean.

---

## Adaptation matrix — what to swap for a different stack

The reference-impl's stack (Next.js 16 + Prisma 5 + Neon + Vercel Blueprint + NextAuth 4) is one point in the design space. Below is the matrix of substitutions for other host apps.

| Component | Ref-impl | If host app uses… | Change |
|---|---|---|---|
| ORM | Prisma 5 | Drizzle | Regenerate schema; the raw SQL in `signals.ts` works unchanged |
| ORM | Prisma 5 | TypeORM / Sequelize | Rewrite `.upsert()`, `.updateMany()`, `.findUnique()` calls to that ORM's idioms |
| ORM | Prisma 5 | Python + SQLAlchemy | Full port; the SQL survives, the wrapper doesn't |
| DB | Neon Postgres | Postgres self-hosted | No change |
| DB | Neon Postgres | MySQL 8 | Replace `INTERVAL '15 minutes'` with `INTERVAL 15 MINUTE`, replace `~*` regex with `REGEXP`, replace `String[]` with JSON columns |
| DB | Neon Postgres | SQLite | Replace INTERVAL / window functions with app-side computation over recent rows; SQLite lacks arrays entirely |
| Cron | Vercel Cron | GitHub Actions | Each cron becomes a `.github/workflows/cron-XXX.yml` calling the API endpoint |
| Cron | Vercel Cron | AWS EventBridge → Lambda | Wrap each route in a Lambda handler |
| Cron | Vercel Cron | Cloudflare Workers Cron Triggers | Direct swap; adjust the `maxDuration` semantics |
| Blob | Vercel Blob (private) | S3 | `put()` → `s3.putObject()`; `head()` → `s3.getObject()` + signed URL |
| Blob | Vercel Blob (private) | Cloudflare R2 | Same as S3 with R2 endpoint |
| Blob | Vercel Blob (private) | Local disk | Write to `/tmp/triage-patches/{date}/{id}/`; scale limit is single-node |
| Auth | NextAuth 4 sessions | Clerk / Auth0 / Supabase Auth | Swap `getServerSession(authOptions)` for the host's session helper |
| Auth | `isAdmin(role)` | Anything | Swap the predicate |
| Notification | `user_notifications` + Web Push + mirror | Slack DMs | Replace `notifyUser` with a Slack webhook per user |
| Notification | `user_notifications` + Web Push + mirror | Email only | Replace with `sendEmail({to, subject, body})` per user |
| Notification | `user_notifications` + Web Push + mirror | Pushover per-user | Replace with `sendPushover({token, user_key, message})` per user |
| Frontend | MUI + React Query | Tailwind + SWR | Rewrite `page.tsx`; API endpoints unchanged |
| Frontend | MUI + React Query | Vue / Svelte / HTMX | Same — API endpoints are the contract |
| LLM gate | Sonnet 4.5 via fetch | GPT-4o with function calling | Replace `fetch` + JSON parsing with the OpenAI function-call adapter |
| LLM gate | Sonnet 4.5 via fetch | Anthropic SDK | Direct swap; keep the Zod validation |

---

## Cost model (portable)

Applies across stacks. Signal density is what varies.

| Signal density | Tickets/mo | Sonnet gate | Sonnet patch | Total /mo |
|---|---|---|---|---|
| 5-10 real users, low friction | ~5-10 | ~$0.03 | ~$0.20 | **~$0.25** |
| 25 users | ~20-40 | ~$0.15 | ~$1.00 | **~$1.15** |
| 100 users | ~100-200 | ~$0.60 | ~$5.00 | **~$5.60** |
| 500+ users | Threshold-lifted; add cross-user weight | — | — | Reassess architecture — this pattern targets internal apps |

**Threshold recommendation as user count scales:** lift the per-user COUNT threshold linearly with user count. At 100 users, `COUNT >= 10` per-user. Cross-user distinct-user threshold can stay at 2 or lift to 3.

---

## Human-in-loop guarantees

The pattern makes these hard promises:

1. **Nothing auto-merges.** The dashboard's Approve action marks a ticket approved and copies an apply command; the admin runs it. No workflow bypasses this.
2. **HARD_BLOCK is absolute.** Once the classifier returns HARD_BLOCK, no path downstream can un-block. The rule table can only be edited in code.
3. **No autonomous DB writes.** The `route.ts` + `prisma.*.{create,update,delete,upsert}` combination is a HARD_BLOCK content rule. Sonnet cannot draft a fix that adds a new DB write in an API route.
4. **User-visible notifications are gated on ticket resolution.** The close-loop cron only fires `notifyUser` when a `triage/<uuid>` PR is merged. Notifications never fire on a Sonnet skip, a classifier veto, or an admin manual skip.

Break any of these and you're outside the pattern; document the deviation prominently.

---

## Reading order for the receiving Claude

1. This file end-to-end (~700 lines) — architecture + component walkthrough + adaptation matrix.
2. `commands/setup-autonomous-triage.md` — the interactive playbook.
3. `reference-impl/autonomous-triage/ADAPTATION.md` — quickstart cheatsheet.
4. `reference-impl/autonomous-triage/src/lib/triage/` files — the runtime logic, best read in order: signals → sonnet-gate → blast-radius → patch-generator → close-loop.
5. `reference-impl/autonomous-triage/src/app/api/cron/auto-triage/route.ts` — the orchestrator that ties them together.
6. The dashboard + tests as needed.

---

## Provenance

Extracted 2026-07-11 from the ASR PO System Enterprise implementation (`intellegix/ASR-PO-System-Enterprise`, PRs #49-54, shipped 2026-07-10). Reference-impl files are copied verbatim; do not edit them without also updating the source repo, or they'll drift.

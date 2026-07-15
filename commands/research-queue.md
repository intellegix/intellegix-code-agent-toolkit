# /research-queue — Live Perplexity Research Queue Status

Show the current state of the global Perplexity research FIFO queue — the cross-session
serialization layer that all concurrent `/research-perplexity` runs pass through. Use this to see
what's running, who's waiting and where, and whether anything is failing.

## What to do

1. **Read the live snapshot:** `~/.claude/council-logs/perplexity-queue.json` (atomically written;
   has a monotonic `version` — if you read it twice, trust the higher `version`).
   - If the file does not exist: no research has run through the queue yet — say so and stop.

2. **Render a compact status:**
   - **Active:** the currently-running run — `session`, query preview, `elapsed_s`, `heartbeat_age_s`.
     If `active` is null → "idle (no run in progress)".
   - **Queued (FIFO order):** each waiting run — `position`, `session`, query preview, `wait_s`.
     Empty list → "none waiting".
   - **Recent:** the last few `completed`/`error`/`timeout` runs with `duration_s` and status.
   - **Stats:** `depth` (currently waiting), `total_today`, `errors_today`.

3. **For deeper history**, read the tail of `~/.claude/council-logs/perplexity-activity.jsonl` —
   the append-only event log (`enqueued`/`started`/`completed`/`error`/`timeout`), each line carrying
   a shared `run_id` that links to `instrumentation-query.jsonl` and `runs.jsonl`.

4. **Flag anything unhealthy and suggest a fix:**
   - Active run with `heartbeat_age_s` > 120s → likely stalled/dead holder (the queue auto-reclaims
     via PID-liveness + TTL, but surface it).
   - Deep queue (many waiting) → congestion; expected under heavy concurrency.
   - Recent `error`/`timeout` events → check the keeper/session health.
   - If the queue is misbehaving, the instant kill-switch is `RESEARCH_QUEUE_ENABLED=0` (runs then
     pass straight through, no queuing), and `RESEARCH_QUEUE_MAX_WAIT` (default 1200s) bounds the wait.

## Notes
- Source of truth is the ticket dir `~/.claude/config/research-queue/tickets/`; the JSON is a
  derived snapshot. If the JSON looks stale, list the ticket dir to cross-check.
- The `research_queue_status` MCP tool (browser-bridge) returns the same snapshot programmatically.
- Architecture + operations: `~/.claude/docs/plans/2026-07-11-perplexity-research-queue-design.md`.

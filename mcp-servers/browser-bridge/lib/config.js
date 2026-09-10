/**
 * Configuration constants and debug logging for Claude Browser Bridge.
 */

import { join } from 'node:path';
import { homedir } from 'node:os';
import { appendFileSync, existsSync, statSync, unlinkSync, renameSync } from 'node:fs';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

export const CONFIG = {
  wsPort: parseInt(process.env.MCP_WS_PORT || '8765', 10),
  wsHost: '127.0.0.1',
  healthPort: parseInt(process.env.MCP_HEALTH_PORT || '8766', 10),
  requestTimeout: 15_000,
  heartbeatTimeout: 120_000,
  heartbeatCheck: 45_000,
  maxMessageSize: 50_000_000, // 50 MB — screenshots can be large
  dbPath: process.env.MCP_DB_PATH || ':memory:',
  cleanupInterval: 300_000, // 5 min
  relayIdleTtl: 900_000, // 15 minutes — evict idle relay clients

  // Tool broadcast timeouts (ms)
  timeouts: {
    quick: 5_000,              // get_context, session_cleanup, close_tabs, clear_console
    interactive: 10_000,       // handle_dialog
    councilExec: 10_000,       // execFileSync for council_metrics, council_read, session_context
    councilUi: 15_000,         // activate_council, export_council_md
    space: 20_000,             // add_to_space
    heavy: 30_000,             // screenshot_element, evaluate, insertText
    fullPage: 120_000,         // screenshot_full_page
    councilApi: 150_000,       // council_query --mode api
    councilBrowser: 210_000,   // council_query --mode browser
    councilResearch: 540_000,  // research_query (9 min — Perplexity can take up to 7 min)
    councilLabs: 900_000,      // labs_query (15 min)
    perplexityAuto: 600_000,   // automate_perplexity_task (10 min max)
  },

  // Relay-specific timeouts.
  // relayReconnectDelay is the BASE of a full-jitter exponential backoff, not a
  // fixed sleep. It was a constant 3_000 until 2026-08-22, which made every
  // relay client retry in the same millisecond — ~20 processes logging the
  // identical ECONNREFUSED 10,415 times in 24h. Each of those drops removed and
  // re-added this server's tools, and a tool-list change invalidates the entire
  // prompt cache, so the herd was costing ~$400/day. Small base = fast recovery
  // once an owner exists; the cap bounds the retry rate while none does.
  relayReconnectDelay: 250,
  relayReconnectCapMs: 8_000,
  ppidPollInterval: 10_000,

  // WS bridge zombie/reconnect detection
  appMsgTimeout: 45_000,         // zombie detection: 2 missed 20s keepalives
  waitForBrowserTimeout: 5_000,  // max wait for browser client reconnect

  // Grace period before a disconnected relay's tabs are closed.
  // A relay disconnect used to close that session's tabs IMMEDIATELY. Measured
  // 2026-09-10: the intellegix-relay lane's server process exited at 12:43:06 and
  // a replacement connected 2.3s later, so the cleanup destroyed a tab the caller
  // was still working with. The caller then read the (accurate) tab list, did not
  // find its tab, and concluded the page was never open — which is the incident in
  // STALE-TABS-AND-SINGLE-BROWSER-BLINDNESS-EVIDENCE-2026-09-10.md. Deferring the
  // cleanup lets a restart reclaim its own tabs. A genuinely-gone session just has
  // its tabs closed this many ms later.
  sessionCleanupGrace: 45_000,
};

// ---------------------------------------------------------------------------
// Debug logging — writes to ~/.claude/mcp-debug.log for crash diagnostics
// ---------------------------------------------------------------------------

const _debugLogPath = join(homedir(), '.claude', 'mcp-debug.log');

// Rotate log if > 5MB (runs once per startup)
const _MAX_LOG_SIZE = 5 * 1024 * 1024;
try {
  if (existsSync(_debugLogPath) && statSync(_debugLogPath).size > _MAX_LOG_SIZE) {
    const oldPath = _debugLogPath + '.old';
    if (existsSync(oldPath)) unlinkSync(oldPath);
    renameSync(_debugLogPath, oldPath);
  }
} catch (_) { /* ignore rotation errors */ }

export function _debugLog(msg) {
  try {
    appendFileSync(_debugLogPath, `${new Date().toISOString()} [PID:${process.pid}] ${msg}\n`);
  } catch (_) { /* ignore */ }
}

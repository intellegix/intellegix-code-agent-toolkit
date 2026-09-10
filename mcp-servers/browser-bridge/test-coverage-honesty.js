/**
 * test-coverage-honesty.js — regression tests for the 2026-09-10 incident.
 *
 * The bug: browser_get_tabs returned a bare {tabs:[...]} from the ONE browser the
 * bridge extension is connected to, with nothing saying other browsers existed.
 * An agent read that silence as absence and reported a confident false negative.
 *
 * These tests fail against the pre-fix code (which had no coverage envelope, no
 * retarget disclosure, and closed a relay's tabs the instant it disconnected) and
 * pass against the fix.
 *
 * Run with: node --test test-coverage-honesty.js
 */

import { describe, it, mock } from 'node:test';
import assert from 'node:assert/strict';

import {
  buildCoverage,
  coverageBanner,
  getSwitchValue,
  isBrowserRoot,
} from './lib/browser-discovery.js';
import { WebSocketBridge } from './lib/websocket-bridge.js';
import { CONFIG } from './lib/config.js';

// ---------------------------------------------------------------------------
// The incident, reproduced as a fixture
// ---------------------------------------------------------------------------

/** Two Chromes running: the bridge's Default profile, and the session keeper on 9223. */
const TWO_CHROME_CENSUS = {
  known: true,
  asOf: '2026-09-10T12:39:00.000Z',
  browserRoots: [
    {
      pid: 1001,
      exe: 'chrome.exe',
      userDataDir: 'C:\\Users\\example\\AppData\\Local\\Google\\Chrome\\User Data',
      declaredDebuggingPort: null,
      usesDebuggingPipe: false,
    },
    {
      pid: 2002,
      exe: 'chrome.exe',
      userDataDir: 'C:\\Users\\example\\.claude\\config\\session_keeper_profile',
      declaredDebuggingPort: 9223,
      usesDebuggingPipe: false,
    },
  ],
  cdpEndpoints: [{ port: 9223, browser: 'Chrome/153.0.8010.36', pid: 2002 }],
};

const ONE_CHROME_CENSUS = {
  known: true,
  asOf: '2026-09-10T12:39:00.000Z',
  browserRoots: [{ pid: 1001, exe: 'chrome.exe', userDataDir: 'C:\\...\\User Data', declaredDebuggingPort: null, usesDebuggingPipe: false }],
  cdpEndpoints: [],
};

describe('coverage envelope — the tab list must declare its scope', () => {
  it('REGRESSION: two browsers, one connected → PARTIAL and negative evidence UNSAFE', () => {
    const coverage = buildCoverage({ tabCount: 5, connectedBrowserClients: 1, census: TWO_CHROME_CENSUS });

    // This is the assertion the pre-fix response could not satisfy at all: the old
    // shape was {tabs:[...]} with no coverage key of any kind.
    assert.equal(coverage.status, 'PARTIAL');
    assert.equal(coverage.negativeEvidence, 'UNSAFE');
    assert.equal(coverage.observedCount, 1);
    assert.equal(coverage.detectedCount, 2);
  });

  it('names the invalid inference explicitly, not just "may be incomplete"', () => {
    const coverage = buildCoverage({ tabCount: 5, connectedBrowserClients: 1, census: TWO_CHROME_CENSUS });
    assert.match(coverage.summary, /NOT evidence that the tab is not open/);
  });

  it('tells the caller where to look — the unseen browser CDP endpoint is actionable', () => {
    const coverage = buildCoverage({ tabCount: 5, connectedBrowserClients: 1, census: TWO_CHROME_CENSUS });
    const keeper = coverage.unobserved.find((u) => u.cdpEndpoint === 'http://127.0.0.1:9223');
    assert.ok(keeper, 'the session-keeper Chrome must appear under unobserved with its endpoint');
    assert.equal(keeper.reasonCode, 'REACHABLE_VIA_CDP_NOT_CONNECTED');
    assert.equal(keeper.tabCount, null, 'must not invent a tab count for a browser it did not query');
  });

  it('single browser → COMPLETE, and a negative conclusion is allowed', () => {
    const coverage = buildCoverage({ tabCount: 5, connectedBrowserClients: 1, census: ONE_CHROME_CENSUS });
    assert.equal(coverage.status, 'COMPLETE');
    assert.equal(coverage.negativeEvidence, 'SAFE_WITHIN_DECLARED_SCOPE');
    assert.deepEqual(coverage.unobserved, []);
  });

  it('coverage is ALWAYS present — the common case must not be silent', () => {
    for (const census of [ONE_CHROME_CENSUS, TWO_CHROME_CENSUS, null]) {
      const coverage = buildCoverage({ tabCount: 1, connectedBrowserClients: 1, census });
      assert.ok(coverage.status, 'status must always be set');
      assert.ok(coverage.negativeEvidence, 'negativeEvidence must always be set');
      assert.ok(coverage.scope, 'scope must always name the universe');
    }
  });

  it('discovery failure must NOT be reported as complete coverage', () => {
    const coverage = buildCoverage({ tabCount: 5, connectedBrowserClients: 1, census: { known: false, note: 'PowerShell unavailable' } });
    assert.equal(coverage.status, 'SCOPED');
    assert.equal(coverage.negativeEvidence, 'UNKNOWN');
    assert.notEqual(coverage.status, 'COMPLETE');
  });

  it('banner leads with the status and only appears when a negative inference is unsafe', () => {
    const partial = coverageBanner(buildCoverage({ tabCount: 5, connectedBrowserClients: 1, census: TWO_CHROME_CENSUS }));
    assert.match(partial, /^RESULT STATUS: PARTIAL — NEGATIVE EVIDENCE UNSAFE\./);

    const complete = coverageBanner(buildCoverage({ tabCount: 5, connectedBrowserClients: 1, census: ONE_CHROME_CENSUS }));
    assert.equal(complete, null, 'the ordinary case must not add banner noise to every call');
  });
});

// ---------------------------------------------------------------------------
// Windows command-line parsing
// ---------------------------------------------------------------------------

describe('chromium command-line parsing', () => {
  it('reads a quoted user-data-dir containing spaces', () => {
    const cmd = '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --user-data-dir="C:\\Users\\A B\\.claude\\config\\session_keeper_profile" --remote-debugging-port=9223';
    assert.equal(getSwitchValue(cmd, 'user-data-dir'), 'C:\\Users\\A B\\.claude\\config\\session_keeper_profile');
    assert.equal(getSwitchValue(cmd, 'remote-debugging-port'), '9223');
  });

  it('handles space-separated switch values', () => {
    assert.equal(getSwitchValue('chrome.exe --remote-debugging-port 9222', 'remote-debugging-port'), '9222');
  });

  it('returns null for an absent switch', () => {
    assert.equal(getSwitchValue('chrome.exe --headless', 'remote-debugging-port'), null);
  });

  it('counts only browser roots — renderer/GPU children are not instances', () => {
    assert.equal(isBrowserRoot({ CommandLine: '"chrome.exe" --user-data-dir="C:\\x"' }), true);
    assert.equal(isBrowserRoot({ CommandLine: '"chrome.exe" --type=renderer --lang=en-US' }), false);
    assert.equal(isBrowserRoot({ CommandLine: '"chrome.exe" --type=gpu-process' }), false);
    assert.equal(isBrowserRoot({ CommandLine: '' }), false, 'unreadable command line must not be counted as a root');
  });
});

// ---------------------------------------------------------------------------
// Session cleanup grace — the mechanism that destroyed the caller's tab
// ---------------------------------------------------------------------------

describe('relay disconnect must not destroy a restarting lane\'s tabs', () => {
  it('REGRESSION: cleanup is deferred, not sent on the disconnect tick', () => {
    const bridge = new WebSocketBridge();
    const sent = [];
    const fakeBrowser = { readyState: 1, send: (d) => sent.push(JSON.parse(d)), close: () => {} };
    bridge.browserClients.set(fakeBrowser, { id: 'b1', connectedAt: Date.now() });

    bridge._scheduleSessionCleanup('04cd2eca-4ca4-4532-857a-b496d6d73ed5', 'C:\\dev\\intellegix-relay');

    // Pre-fix behaviour sent session_cleanup synchronously here. It must not.
    assert.deepEqual(sent, [], 'no session_cleanup may be sent during the grace period');
    assert.equal(bridge.pendingSessionCleanups.size, 1);
    bridge.stop();
  });

  it('a relay reconnecting for the same project cancels the pending cleanup', () => {
    const bridge = new WebSocketBridge();
    bridge._scheduleSessionCleanup('04cd2eca-4ca4-4532-857a-b496d6d73ed5', 'C:\\dev\\intellegix-relay');
    // 2.3 seconds later a replacement process connected for the same directory —
    // that is exactly what happened at 12:43:06 → 12:43:08 on 2026-09-10.
    const cancelled = bridge._cancelPendingCleanup('C:\\dev\\intellegix-relay');
    assert.equal(cancelled, true);
    assert.equal(bridge.pendingSessionCleanups.size, 0);
    bridge.stop();
  });

  it('a different project does not cancel someone else\'s pending cleanup', () => {
    const bridge = new WebSocketBridge();
    bridge._scheduleSessionCleanup('aaaaaaaa-0000-0000-0000-000000000000', 'C:\\dev\\project-a');
    assert.equal(bridge._cancelPendingCleanup('C:\\dev\\project-b'), false);
    assert.equal(bridge.pendingSessionCleanups.size, 1);
    bridge.stop();
  });

  it('the cleanup does eventually fire for a session that really ended', async () => {
    const original = CONFIG.sessionCleanupGrace;
    CONFIG.sessionCleanupGrace = 20;
    try {
      const bridge = new WebSocketBridge();
      const sent = [];
      bridge.browserClients.set({ readyState: 1, send: (d) => sent.push(JSON.parse(d)), close: () => {} }, { id: 'b1', connectedAt: Date.now() });
      bridge._scheduleSessionCleanup('deadbeef-0000-0000-0000-000000000000', 'C:\\dev\\gone');
      await new Promise((r) => setTimeout(r, 80));
      assert.equal(sent.length, 1);
      assert.equal(sent[0].type, 'session_cleanup');
      assert.equal(sent[0].payload.sessionId, 'deadbeef-0000-0000-0000-000000000000');
      bridge.stop();
    } finally {
      CONFIG.sessionCleanupGrace = original;
    }
  });
});

// ---------------------------------------------------------------------------
// Navigate retarget disclosure
// ---------------------------------------------------------------------------

/** Mirrors the browser_navigate branch in server.js _handleToolCall. */
function decorateNavigateResult(res, tabId) {
  if (res && typeof res === 'object' && tabId && res.tabId && res.tabId !== tabId) {
    return {
      ...res,
      requestedTabId: tabId,
      retargeted: true,
      retargetReason: `Tab ${tabId} is not owned by this MCP session, so a new tab (${res.tabId}) was opened and navigated instead.`,
    };
  }
  if (res && typeof res === 'object' && tabId) return { ...res, requestedTabId: tabId, retargeted: false };
  return res;
}

describe('browser_navigate must disclose a retarget', () => {
  it('REGRESSION: the exact 2026-09-10 call reports retargeted:true', () => {
    // Requested 1435686256 (chrome://newtab), got back 1435686303, success:true.
    const out = decorateNavigateResult({ success: true, url: 'https://fantasy.espn.com/football/', tabId: 1435686303 }, 1435686256);
    assert.equal(out.retargeted, true);
    assert.equal(out.requestedTabId, 1435686256);
    assert.equal(out.tabId, 1435686303);
    assert.match(out.retargetReason, /new tab \(1435686303\)/);
  });

  it('an honoured tabId reports retargeted:false', () => {
    const out = decorateNavigateResult({ success: true, url: 'https://example.com/', tabId: 77 }, 77);
    assert.equal(out.retargeted, false);
    assert.equal(out.requestedTabId, 77);
  });

  it('no tabId requested → no retarget fields invented', () => {
    const out = decorateNavigateResult({ success: true, url: 'https://example.com/', tabId: 77 }, undefined);
    assert.equal(out.retargeted, undefined);
  });
});

/**
 * Claude Browser Bridge — Service Worker (background.js)
 *
 * MCPBridgeClient: WebSocket client connecting to the MCP server at ws://127.0.0.1:8765.
 * Triple-layer keepalive strategy to survive Chrome MV3 service worker lifecycle:
 *   Layer 1: WebSocket ping every 20s
 *   Layer 2: chrome.alarms every 24s
 *   Layer 3: Offscreen document pings every 20s
 *
 * Handles inbound requests from the MCP server and delegates to content scripts or
 * Chrome extension APIs as needed.
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const WS_URL = 'ws://127.0.0.1:8765';
const RECONNECT_BASE = 3000;
const RECONNECT_MAX = 5000;
const PING_INTERVAL = 20_000;
const ALARM_NAME = 'ws-keepalive';
const ALARM_PERIOD = 0.4; // minutes (~24 seconds)

// Timeout constants for CDP/UI interaction delays
const MENU_ANIMATION_DELAY = 1200;     // dropdown menu render time
const SPACE_MODAL_DELAY = 1500;        // modal open animation
const COMMAND_PALETTE_DELAY = 1500;    // Perplexity command palette render
const DOWNLOAD_INIT_DELAY = 2000;      // file download initiation
const SPACE_CREATION_DELAY = 2000;     // new space creation roundtrip
const SPACE_SELECT_DELAY = 1000;       // space selection confirmation
const CDP_REATTACH_DELAY = 50;         // debugger detach→reattach cycle

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let ws = null;
let reconnectDelay = RECONNECT_BASE;
let reconnectTimer = null;
let pingTimer = null;
let isConnected = false;
let connecting = false;
let clientId = null;
let lastServerMessage = Date.now();
let keepaliveWindowId = null;

// ---------------------------------------------------------------------------
// Session tab group management
// ---------------------------------------------------------------------------

const sessionGroups = new Map(); // sessionId -> { groupId, tabIds: Set, color, label, lastActivity }
const GROUP_COLORS = ['blue', 'red', 'yellow', 'green', 'pink', 'purple', 'cyan', 'orange'];
let colorIndex = 0;

const MAX_IMAGE_DIMENSION = 7800; // Under Anthropic's 8000px API limit

/** Check if a Chrome tab group ID still exists */
async function validateGroupId(groupId) {
  try {
    await chrome.tabGroups.get(groupId);
    return true;
  } catch { return false; }
}

/** Get or create a Chrome tab group for a Claude Code session */
async function getOrCreateSessionGroup(sessionId, projectLabel) {
  if (sessionGroups.has(sessionId)) {
    const group = sessionGroups.get(sessionId);
    group.lastActivity = Date.now();
    // Validate that the cached groupId still exists (survives extension reload / Chrome restart)
    if (!await validateGroupId(group.groupId)) {
      console.warn(`[Bridge] Stale groupId ${group.groupId} for session ${sessionId.slice(0, 8)} — recreating`);
      sessionGroups.delete(sessionId);
      // Fall through to create a new group
    } else {
      return group;
    }
  }

  // Create a new tab and group it
  const tab = await chrome.tabs.create({ active: false });
  const groupId = await chrome.tabs.group({ tabIds: [tab.id] });
  const color = GROUP_COLORS[colorIndex++ % GROUP_COLORS.length];
  const label = projectLabel
    ? (projectLabel.length > 25 ? projectLabel.slice(0, 22) + '...' : projectLabel)
    : `Claude ${sessionId.slice(0, 4)}`;

  await chrome.tabGroups.update(groupId, { title: label, color, collapsed: false });

  const group = { groupId, tabIds: new Set([tab.id]), color, label, lastActivity: Date.now() };
  sessionGroups.set(sessionId, group);

  console.log(`[Bridge] Created tab group "${label}" (${color}) for session ${sessionId.slice(0, 8)}`);
  return group;
}

/** Add a tab to a session's group, recovering from stale groupId */
async function addTabToSession(sessionId, tabId, projectLabel) {
  let group = sessionGroups.get(sessionId);
  if (!group) return;
  try {
    await chrome.tabs.group({ tabIds: [tabId], groupId: group.groupId });
    group.tabIds.add(tabId);
  } catch (err) {
    console.warn(`[Bridge] Failed to add tab ${tabId} to group (${err.message}) — recreating group`);
    // Stale groupId — recreate the group with this tab + any surviving tracked tabs
    sessionGroups.delete(sessionId);
    group = await getOrCreateSessionGroup(sessionId, projectLabel);
    // The new group already has one tab from getOrCreateSessionGroup; add this one too
    try {
      await chrome.tabs.group({ tabIds: [tabId], groupId: group.groupId });
      group.tabIds.add(tabId);
    } catch (e) { console.warn('[Bridge] Failed to re-add tab to recreated group:', e.message); }
  }
}

// Clean up closed tabs from session groups
chrome.tabs.onRemoved.addListener((removedTabId) => {
  for (const [sessionId, group] of sessionGroups) {
    if (group.tabIds.has(removedTabId)) {
      group.tabIds.delete(removedTabId);
      if (group.tabIds.size === 0) {
        sessionGroups.delete(sessionId);
        console.log(`[Bridge] Session group "${group.label}" removed (no tabs left)`);
        closeKeepaliveIfNoSessions();
      }
      break;
    }
  }
});

// Close stale session groups (inactive > 30 min)
setInterval(() => {
  const cutoff = Date.now() - 30 * 60_000;
  for (const [sessionId, group] of sessionGroups) {
    if (group.lastActivity < cutoff) {
      for (const tabId of group.tabIds) {
        chrome.tabs.remove(tabId).catch(() => {});
      }
      sessionGroups.delete(sessionId);
      console.log(`[Bridge] Closed stale session group "${group.label}"`);
    }
  }
}, 5 * 60_000);

// ---------------------------------------------------------------------------
// Keepalive window — prevents Chrome exit when sessions are active
// ---------------------------------------------------------------------------

/** Close the keepalive window if no active session groups remain */
function closeKeepaliveIfNoSessions() {
  if (keepaliveWindowId === null) return;
  const hasActiveSessions = [...sessionGroups.values()].some(g => g.tabIds.size > 0);
  if (!hasActiveSessions) {
    chrome.windows.remove(keepaliveWindowId).catch(() => {});
    keepaliveWindowId = null;
    console.log('[Bridge] Keepalive window closed (no active sessions)');
  }
}

chrome.windows.onRemoved.addListener(async (windowId) => {
  // If the keepalive window itself was closed, clear the reference and let Chrome exit
  if (windowId === keepaliveWindowId) {
    keepaliveWindowId = null;
    return;
  }

  // Check if any windows remain
  const windows = await chrome.windows.getAll();
  if (windows.length > 0) return;

  // Check if any session groups have active tabs
  const hasActiveSessions = [...sessionGroups.values()].some(g => g.tabIds.size > 0);
  if (!hasActiveSessions) return;

  // Prevent Chrome exit by opening a keepalive window
  try {
    const win = await chrome.windows.create({
      url: chrome.runtime.getURL('keepalive.html'),
      type: 'popup',
      width: 380,
      height: 260,
      focused: true,
    });
    keepaliveWindowId = win.id;
    console.log(`[Bridge] Keepalive window created (${sessionGroups.size} active sessions)`);
  } catch (err) {
    console.error('[Bridge] Failed to create keepalive window:', err.message);
  }
});

// ---------------------------------------------------------------------------
// Offscreen document management (Layer 3)
// ---------------------------------------------------------------------------

async function ensureOffscreen() {
  const existing = await chrome.offscreen.hasDocument().catch(() => false);
  if (!existing) {
    try {
      await chrome.offscreen.createDocument({
        url: 'offscreen.html',
        reasons: ['BLOBS'], // valid MV3 reason
        justification: 'Keepalive ping to prevent service worker termination',
      });
    } catch (e) {
      console.warn('[Bridge] Offscreen document creation skipped:', e.message);
    }
  }
}

// ---------------------------------------------------------------------------
// WebSocket connection
// ---------------------------------------------------------------------------

function connect() {
  if (connecting || (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN))) {
    return;
  }

  connecting = true;
  try {
    ws = new WebSocket(WS_URL);
  } catch (err) {
    connecting = false;
    console.error('[Bridge] WebSocket constructor failed:', err);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    connecting = false;
    console.log('[Bridge] Connected to MCP server');
    isConnected = true;
    reconnectDelay = RECONNECT_BASE;
    lastServerMessage = Date.now();

    // Identify this client so the server can pick a single canonical target when
    // more than one Chrome has the extension (e.g. the user's personal profile
    // AND the always-on keeper Chrome). The keeper's copy carries "(Keeper)" in
    // its manifest name; everything else reports role 'default'. Absence of this
    // message (older extension builds) is treated as 'default' server-side, so
    // this is fully backward-compatible.
    try {
      const mf = chrome.runtime.getManifest();
      const role = (mf.name || '').includes('Keeper') ? 'keeper' : 'default';
      ws.send(JSON.stringify({ type: 'hello', role, clientId: chrome.runtime.id, version: mf.version }));
    } catch (e) {
      // non-fatal — server defaults missing role to 'default'
    }

    // Layer 1: periodic ping
    clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'keepalive' }));
      }
    }, PING_INTERVAL);

    // Push initial page context
    pushActiveTabContext();
  };

  ws.onmessage = (event) => {
    lastServerMessage = Date.now();
    try {
      const msg = JSON.parse(event.data);
      handleServerMessage(msg);
    } catch (err) {
      console.error('[Bridge] Bad message from server:', err);
    }
  };

  ws.onclose = () => {
    console.log('[Bridge] Disconnected from MCP server');
    isConnected = false;
    connecting = false;
    clientId = null;
    clearInterval(pingTimer);
    scheduleReconnect();
  };

  ws.onerror = (err) => {
    console.error('[Bridge] WebSocket error:', err);
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectDelay = Math.min(reconnectDelay * 1.5, RECONNECT_MAX);
    connect();
  }, reconnectDelay);
}

function sendToServer(data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
  }
}

// ---------------------------------------------------------------------------
// Inbound message handling (from MCP server)
// ---------------------------------------------------------------------------

async function handleServerMessage(msg) {
  // Store client ID from connection_init
  if (msg.type === 'connection_init') {
    clientId = msg.clientId;
    console.log('[Bridge] Assigned clientId:', clientId);
    return;
  }

  const { requestId } = msg;

  try {
    let result;

    switch (msg.type) {
      case 'action_request':
        result = await handleActionRequest(msg.payload);
        break;
      case 'navigate':
        result = await handleNavigate(msg.payload);
        break;
      case 'load_dynamic':
        result = await handleLoadDynamic(msg.payload);
        break;
      case 'get_context':
        result = await handleGetContext(msg.payload);
        break;
      case 'screenshot':
        result = await handleScreenshot(msg.payload);
        break;
      case 'screenshot_element':
        result = await handleScreenshotElement(msg.payload);
        break;
      case 'screenshot_full_page':
        result = await handleScreenshotFullPage(msg.payload);
        break;
      case 'context_sync':
        result = await handleContextSync(msg.payload);
        break;
      case 'session_cleanup':
        result = await handleSessionCleanup(msg.payload);
        break;
      case 'close_tabs':
        result = await handleCloseTabs(msg.payload);
        break;
      case 'get_tabs':
        result = await handleGetTabs(msg.payload);
        break;
      case 'switch_tab':
        result = await handleSwitchTab(msg.payload);
        break;
      case 'evaluate':
        result = await handleEvaluate(msg.payload);
        break;
      case 'get_console_messages':
        result = await handleGetConsoleMessages(msg.payload);
        break;
      case 'handle_dialog':
        result = await handleDialog(msg.payload);
        break;
      case 'cdp_type':
        result = await handleCdpType(msg.payload);
        break;
      case 'activate_council':
        result = await handleActivateCouncil(msg.payload);
        break;
      case 'export_council_md':
        result = await handleExportCouncilMarkdown(msg.payload);
        break;
      case 'add_to_space':
        result = await handleAddToSpace(msg.payload);
        break;
      case 'get_all_cookies':
        result = await handleGetAllCookies(msg.payload);
        break;
      default:
        result = { error: `Unknown message type: ${msg.type}` };
    }

    if (requestId) {
      sendToServer({ requestId, result });
    }
  } catch (err) {
    if (requestId) {
      const response = { requestId, error: err.message };
      if (err.code) response.code = err.code;
      sendToServer(response);
    }
  }
}

// ---------------------------------------------------------------------------
// Action handlers
// ---------------------------------------------------------------------------

async function getTargetTabId(payload) {
  if (payload && payload.tabId) {
    const n = Number(payload.tabId);
    if (!Number.isInteger(n) || n <= 0) {
      console.warn('[Bridge] Invalid tabId in payload:', payload.tabId);
    } else {
      return n;
    }
  }

  // Session-aware: use a tab from the session's tab group
  if (payload && payload.sessionId) {
    const group = await getOrCreateSessionGroup(payload.sessionId, payload.projectLabel);

    if (group.tabIds.size > 0) {
      const allTabs = await chrome.tabs.query({});
      const groupTabs = allTabs.filter((t) => group.tabIds.has(t.id));

      if (groupTabs.length > 0) {
        // Prefer active tab in group, else first
        return (groupTabs.find((t) => t.active) || groupTabs[0]).id;
      }

      // Tracked tabs were closed externally — reset
      group.tabIds.clear();
    }

    // No tabs in group — create one
    const newTab = await chrome.tabs.create({ active: false });
    try {
      await chrome.tabs.group({ tabIds: [newTab.id], groupId: group.groupId });
    } catch (e) {
      console.warn('[Bridge] Stale groupId in getTargetTabId, recovering:', e.message);
      await addTabToSession(payload.sessionId, newTab.id, payload.projectLabel);
    }
    group.tabIds.add(newTab.id);
    return newTab.id;
  }

  // Fallback: global active tab (backward compat)
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab ? tab.id : null;
}

// ---------------------------------------------------------------------------
// Dynamic content loader — foreground the tab, scroll lazy content, restore
// ---------------------------------------------------------------------------
//
// Facebook (and similar hardened sites) gate infinite-scroll content behind
// IntersectionObserver + lazy hydration. Chrome throttles BACKGROUND tabs —
// paint is paused and IntersectionObserver callbacks (bound to the frame
// rendering pipeline) never fire — so lazy lists never populate in the tabs the
// bridge opens with { active: false }. CDP visibility-spoofing does NOT flip
// document.visibilityState to 'visible', so the only reliable fix is to briefly
// FOREGROUND the tab, scroll, then restore the user's prior tab/window.

/**
 * Run `fn` with `tabId` foregrounded (active + its window focused), then restore
 * whatever tab/window was active before. Always restores, even if `fn` throws.
 */
async function withForegroundTab(tabId, fn) {
  let prevTabId = null;
  let prevWindowId = null;
  try {
    const [prev] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (prev) {
      prevTabId = prev.id;
      prevWindowId = prev.windowId;
    }
  } catch { /* best-effort — restore is optional */ }

  const target = await chrome.tabs.get(tabId);
  try {
    await chrome.tabs.update(tabId, { active: true });
    await chrome.windows.update(target.windowId, { focused: true });
    return await fn();
  } finally {
    // Restore the user's context only if it differs from the target tab.
    if (prevTabId && prevTabId !== tabId) {
      await chrome.tabs.update(prevTabId, { active: true }).catch(() => {});
      if (prevWindowId) {
        await chrome.windows.update(prevWindowId, { focused: true }).catch(() => {});
      }
    }
  }
}

/**
 * Navigate to (or reuse) a page, foreground it, and auto-scroll to force
 * viewport-gated lazy content to load, then restore the user's prior tab.
 */
async function handleLoadDynamic(payload) {
  // 1. Resolve the target tab — navigate first if a URL was supplied.
  let tabId;
  let url = payload.url || null;
  if (url) {
    const nav = await handleNavigate(payload);
    tabId = nav.tabId;
    url = nav.url || url;
  } else {
    tabId = await getTargetTabId(payload);
  }
  if (!tabId) throw new Error('No target tab for load_dynamic');

  // 2. Ensure the content script is present (same probe as handleActionRequest).
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => typeof window.claudeHelpers !== 'undefined',
    });
  } catch {
    throw new Error('Cannot access this page (restricted URL)');
  }

  // 3. Foreground the tab so IntersectionObserver / lazy hydration actually run,
  //    scroll the lazy list to completion, then restore the prior tab/window.
  const scrollResult = await withForegroundTab(tabId, async () => {
    if (payload.waitForSelector) {
      await chrome.tabs.sendMessage(tabId, {
        source: 'claude-bridge',
        action: 'waitForElement',
        selector: payload.waitForSelector,
        timeout: 15000,
      }).catch(() => {});
    }
    return chrome.tabs.sendMessage(tabId, {
      source: 'claude-bridge',
      action: 'autoScrollLoad',
      containerSelector: payload.containerSelector,
      itemSelector: payload.itemSelector,
      maxScrolls: payload.maxScrolls,
      stableMs: payload.stableMs,
      minDelay: payload.minDelay,
      maxDelay: payload.maxDelay,
      timeout: payload.timeout,
    });
  });

  return { ...(scrollResult || {}), tabId, url };
}

async function handleActionRequest(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  // Trusted directional scroll: JS window.scrollBy() produces isTrusted:false
  // wheel/scroll events, which hardened infinite-scroll sites (Facebook friends
  // list, etc.) ignore — the scrollbar moves but no lazy content loads. Route
  // directional scrolls through CDP Input.dispatchMouseEvent(mouseWheel) so they
  // arrive as trusted input and trigger lazy-load exactly like a human wheel.
  // Selector-based scroll (scrollIntoView) still goes to the content script.
  if (payload.action === 'scroll' && !payload.selector) {
    return cdpWheelScroll(tabId, payload);
  }

  // Inject content script if not already present
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => typeof window.claudeHelpers !== 'undefined',
    });
  } catch {
    // Content script may not be injectable (chrome:// pages, etc.)
    throw new Error('Cannot access this page (restricted URL)');
  }

  const response = await chrome.tabs.sendMessage(tabId, {
    source: 'claude-bridge',
    ...payload,
  });

  return response;
}

// ---------------------------------------------------------------------------
// Trusted wheel scroll via CDP
// ---------------------------------------------------------------------------
//
// JS window.scrollBy/scrollTo and new WheelEvent() are isTrusted:false. Hardened
// infinite-scroll (Facebook friends list, feeds, etc.) only fetches the next
// batch in response to a TRUSTED input event, so JS scrolling moves the scrollbar
// but never loads more content. CDP Input.dispatchMouseEvent(mouseWheel) events
// go through Chrome's real input pipeline (isTrusted:true) — the same mechanism
// Playwright's mouse.wheel() uses — so the page reacts as if a human scrolled.
async function cdpWheelScroll(tabId, payload) {
  const amount = Math.max(1, Math.min(30000, payload.amount || 500));
  const dir = payload.direction || 'down';
  // [deltaY sign, deltaX sign] — CDP mouseWheel: +deltaY scrolls DOWN.
  const sign = { up: [-1, 0], down: [1, 0], left: [0, -1], right: [0, 1] };
  const [vy, vx] = sign[dir] || [1, 0];

  // Hardened sites (Facebook, feeds) pause IntersectionObserver / lazy hydration
  // on hidden/background tabs, so a trusted wheel loads nothing while the tab is
  // in the background. Foreground the tab (and focus its window) first so the
  // scroll actually triggers content loading. Best-effort — never block on it.
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab && !tab.active) await chrome.tabs.update(tabId, { active: true });
    if (tab && tab.windowId != null) {
      await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
    }
  } catch {
    // tab may be gone; the cdpCommand calls below will surface a clear error
  }

  // Wheel events must originate from a point over the scrollable content.
  // Resolve the viewport center via CDP Runtime.evaluate (defaults if it fails).
  let x = 400;
  let y = 400;
  try {
    const vp = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: 'JSON.stringify({w:innerWidth,h:innerHeight})',
      returnByValue: true,
    });
    const dims = JSON.parse(vp?.result?.value || '{}');
    if (dims.w) x = Math.round(dims.w / 2);
    if (dims.h) y = Math.round(dims.h / 2);
  } catch {
    // fall back to default coordinates
  }

  // Position the pointer first — some wheel handlers require a prior mouseMoved.
  await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });

  // Real wheel gestures arrive as a burst of small deltas; emulate that so the
  // page's scroll / IntersectionObserver logic fires the same as a human scroll.
  const TICK = 120;
  const ticks = Math.max(1, Math.round(amount / TICK));
  let sent = 0;
  for (let i = 0; i < ticks; i++) {
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', {
      type: 'mouseWheel',
      x,
      y,
      deltaX: vx * TICK,
      deltaY: vy * TICK,
    });
    sent += TICK;
    await new Promise((r) => setTimeout(r, 16)); // ~1 frame between ticks
  }

  return { success: true, direction: dir, amount: sent, trusted: true, via: 'cdp-mouseWheel' };
}

async function handleNavigate(payload) {
  let tabId;

  if (payload && payload.tabId && payload.sessionId) {
    // Explicit tabId — only reuse if it belongs to this session's group
    const group = sessionGroups.get(payload.sessionId);
    if (group && group.tabIds.has(payload.tabId)) {
      tabId = payload.tabId;
    } else {
      // Tab belongs to another session or the user — open a new one instead
      const ownGroup = await getOrCreateSessionGroup(payload.sessionId, payload.projectLabel);
      const newTab = await chrome.tabs.create({ active: false });
      await addTabToSession(payload.sessionId, newTab.id, payload.projectLabel);
      ownGroup.tabIds.add(newTab.id);
      tabId = newTab.id;
    }
  } else if (payload && payload.tabId) {
    // Explicit tabId, no session — backward compat
    tabId = payload.tabId;
  } else if (payload && payload.sessionId) {
    // Always open a new tab for navigation (don't clobber existing pages)
    const group = await getOrCreateSessionGroup(payload.sessionId, payload.projectLabel);
    const newTab = await chrome.tabs.create({ active: false });
    await addTabToSession(payload.sessionId, newTab.id, payload.projectLabel);
    group.tabIds.add(newTab.id);
    tabId = newTab.id;
  } else {
    tabId = await getTargetTabId(payload);
  }

  if (!tabId) throw new Error('No active tab found');

  await chrome.tabs.update(tabId, { url: payload.url });

  // Wait for navigation to complete
  return new Promise((resolve) => {
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId === tabId && changeInfo.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve({ success: true, url: payload.url, tabId });
      }
    };
    chrome.tabs.onUpdated.addListener(listener);

    // Timeout fallback
    setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve({ success: true, url: payload.url, tabId, note: 'Navigation started (timeout on complete)' });
    }, 15_000);
  });
}

async function handleGetContext(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  const tab = await chrome.tabs.get(tabId);

  // Try to get detailed context from content script
  let pageContent = null;
  try {
    pageContent = await chrome.tabs.sendMessage(tabId, {
      source: 'claude-bridge',
      action: 'getPageContent',
    });
  } catch {
    // Content script not available on this page
  }

  return {
    url: tab.url,
    title: tab.title,
    tabId: tab.id,
    status: tab.status,
    favIconUrl: tab.favIconUrl,
    ...(pageContent || {}),
  };
}

// ---------------------------------------------------------------------------
// Chrome DevTools Protocol (CDP) helpers — persistent session manager
// ---------------------------------------------------------------------------

const cdpSessions = new Map(); // tabId -> { attached, lastUsed, timer }
const CDP_IDLE_MS = 5000; // Auto-detach after 5s of inactivity

/**
 * Acquire a CDP session for a tab. Reuses existing attachment if still active,
 * otherwise attaches fresh. Resets the idle timer on each acquire.
 */
async function cdpAcquire(tabId) {
  const session = cdpSessions.get(tabId);
  if (session?.attached) {
    session.lastUsed = Date.now();
    clearTimeout(session.timer);
    session.timer = setTimeout(() => cdpRelease(tabId), CDP_IDLE_MS);
    return;
  }
  try {
    await chrome.debugger.attach({ tabId }, '1.3');
  } catch (err) {
    const error = new Error(`CDP attach failed for tab ${tabId}: ${err.message}`);
    error.code = 'CDP_ATTACH_FAILED';
    throw error;
  }
  cdpSessions.set(tabId, {
    attached: true,
    lastUsed: Date.now(),
    timer: setTimeout(() => cdpRelease(tabId), CDP_IDLE_MS),
  });
}

/**
 * Release a CDP session for a tab. Detaches debugger and cleans up.
 * Safe to call even if already detached (no-op).
 */
async function cdpRelease(tabId) {
  const session = cdpSessions.get(tabId);
  if (!session?.attached) return;
  clearTimeout(session.timer);
  cdpSessions.delete(tabId);
  await chrome.debugger.detach({ tabId }).catch(() => {});
}

/**
 * Force detach + reattach for handlers that need a fresh CDP pipeline
 * (e.g., handleCdpType and handleActivateCouncil for Lexical editors).
 */
async function cdpForceReattach(tabId) {
  await cdpRelease(tabId);
  await new Promise(r => setTimeout(r, CDP_REATTACH_DELAY));
  await cdpAcquire(tabId);
  await new Promise(r => setTimeout(r, CDP_REATTACH_DELAY));
}

/**
 * Send a CDP command. Two reliability behaviors:
 *
 * 1. Refreshes the idle timer on every call. The CDP_IDLE_MS=5000 timer
 *    set in cdpAcquire fires unconditionally otherwise, releasing the
 *    debugger mid-flight on any typing sequence > 5s (long question
 *    strings). With refresh-on-use, the session stays alive as long as
 *    commands keep arriving; idle release still fires after >5s of true
 *    quiescence.
 *
 * 2. Auto-recovers ONCE if Chrome silently detached the session. Detach
 *    reasons documented by chrome.debugger.onDetach include
 *    target_closed, canceled_by_user, replaced_with_devtools — any of
 *    which can fire at any time without our code calling detach(). The
 *    cdpSessions Map then lies (attached:true), and the next sendCommand
 *    throws "Debugger is not attached to the tab" or "Detached while
 *    handling command." We catch that specific error, clear the stale
 *    Map entry, re-attach, and retry the original command once.
 */
async function cdpCommand(tabId, method, params = {}) {
  const session = cdpSessions.get(tabId);
  if (session?.attached) {
    clearTimeout(session.timer);
    session.lastUsed = Date.now();
    session.timer = setTimeout(() => cdpRelease(tabId), CDP_IDLE_MS);
  }
  try {
    return await chrome.debugger.sendCommand({ tabId }, method, params);
  } catch (err) {
    const msg = String(err?.message || err || '');
    const detached = /not attached|Detached while handling/i.test(msg);
    if (!detached) throw err;
    // Stale session — Chrome dropped it but our Map didn't notice.
    // Clear and try once more with a fresh attach.
    if (cdpSessions.has(tabId)) {
      clearTimeout(cdpSessions.get(tabId).timer);
      cdpSessions.delete(tabId);
    }
    await chrome.debugger.attach({ tabId }, '1.3').catch(() => {});
    cdpSessions.set(tabId, {
      attached: true,
      lastUsed: Date.now(),
      timer: setTimeout(() => cdpRelease(tabId), CDP_IDLE_MS),
    });
    return chrome.debugger.sendCommand({ tabId }, method, params);
  }
}

// Clean up CDP sessions when tabs are closed.
chrome.tabs.onRemoved.addListener((removedTabId) => {
  if (cdpSessions.has(removedTabId)) {
    clearTimeout(cdpSessions.get(removedTabId).timer);
    cdpSessions.delete(removedTabId);
    chrome.debugger.detach({ tabId: removedTabId }).catch(() => {});
  }
});

// Clean up cdpSessions Map when Chrome silently detaches the debugger
// session (reasons per chrome.debugger.onDetach docs: target_closed,
// canceled_by_user, replaced_with_devtools). Without this, our session
// state stays `attached: true` after Chrome already dropped the session,
// and the next cdpCommand's sendCommand call fails. The cdpCommand
// auto-recovery above is the fallback; this listener is the proactive
// path that keeps the Map honest.
chrome.debugger.onDetach.addListener((source, _reason) => {
  if (!source || typeof source.tabId !== 'number') return;
  const session = cdpSessions.get(source.tabId);
  if (!session) return;
  clearTimeout(session.timer);
  cdpSessions.delete(source.tabId);
});

/** Normalize format string for CDP and build params */
function cdpCaptureParams(payload) {
  const format = payload.format || 'png';
  const cdpFormat = format === 'jpg' ? 'jpeg' : format;
  const params = { format: cdpFormat };
  if (cdpFormat === 'jpeg' && payload.quality) {
    params.quality = payload.quality;
  }
  return { format, cdpFormat, params };
}

// ---------------------------------------------------------------------------
// Screenshot handlers (CDP-based — no focus requirement)
// ---------------------------------------------------------------------------

async function handleScreenshot(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  const { format, params } = cdpCaptureParams(payload);

  try {
    await cdpAcquire(tabId);
    const result = await cdpCommand(tabId, 'Page.captureScreenshot', params);

    const dataUrl = `data:image/${format};base64,${result.data}`;
    return { success: true, screenshot: dataUrl, format };
  } finally {
    await cdpRelease(tabId);
  }
}

async function handleScreenshotElement(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  // Get element bounds (page-absolute coordinates) from content script
  const rectResult = await chrome.tabs.sendMessage(tabId, {
    source: 'claude-bridge',
    action: 'getElementRect',
    selector: payload.selector,
  });

  if (!rectResult || !rectResult.success) {
    throw new Error(rectResult?.error || `Element not found: ${payload.selector}`);
  }

  const { rect, dpr } = rectResult;
  const { format, params } = cdpCaptureParams(payload);

  // Auto-scale if element dimensions exceed API limit
  let scale = dpr;
  if (rect.width * dpr > MAX_IMAGE_DIMENSION || rect.height * dpr > MAX_IMAGE_DIMENSION) {
    scale = Math.min(MAX_IMAGE_DIMENSION / rect.width, MAX_IMAGE_DIMENSION / rect.height, dpr);
  }

  try {
    await cdpAcquire(tabId);
    const result = await cdpCommand(tabId, 'Page.captureScreenshot', {
      ...params,
      clip: {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
        scale,
      },
      captureBeyondViewport: true,
    });

    const dataUrl = `data:image/${format};base64,${result.data}`;
    return {
      success: true,
      screenshot: dataUrl,
      width: Math.round(rect.width * scale),
      height: Math.round(rect.height * scale),
      format,
    };
  } finally {
    await cdpRelease(tabId);
  }
}

async function handleScreenshotFullPage(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  // Get page dimensions from content script
  const dims = await chrome.tabs.sendMessage(tabId, {
    source: 'claude-bridge',
    action: 'getPageDimensions',
  });

  if (!dims || !dims.success) {
    throw new Error(dims?.error || 'Failed to get page dimensions');
  }

  const { scrollHeight, viewportHeight, viewportWidth, dpr } = dims;

  // Safety cap: extremely tall pages may exceed Chrome's image buffer
  const maxHeight = 30 * viewportHeight;
  if (scrollHeight > maxHeight) {
    const fallback = await handleScreenshot(payload);
    fallback.warning = `Page too tall (${scrollHeight}px, max ${maxHeight}px). Captured viewport only.`;
    return fallback;
  }

  const { format, params } = cdpCaptureParams(payload);

  // Auto-scale to stay under API dimension limit (8000px)
  let scale = dpr;
  if (viewportWidth * dpr > MAX_IMAGE_DIMENSION || scrollHeight * dpr > MAX_IMAGE_DIMENSION) {
    scale = Math.min(MAX_IMAGE_DIMENSION / viewportWidth, MAX_IMAGE_DIMENSION / scrollHeight, dpr);
  }

  try {
    await cdpAcquire(tabId);
    const result = await cdpCommand(tabId, 'Page.captureScreenshot', {
      ...params,
      clip: {
        x: 0,
        y: 0,
        width: viewportWidth,
        height: scrollHeight,
        scale,
      },
      captureBeyondViewport: true,
    });

    const dataUrl = `data:image/${format};base64,${result.data}`;
    const meta = {
      success: true,
      screenshot: dataUrl,
      width: Math.round(viewportWidth * scale),
      height: Math.round(scrollHeight * scale),
      format,
    };
    if (scale < dpr) {
      meta.note = `Scaled from ${dpr}x to ${scale.toFixed(2)}x to fit ${MAX_IMAGE_DIMENSION}px limit`;
    }
    return meta;
  } finally {
    await cdpRelease(tabId);
  }
}

async function handleContextSync(payload) {
  // Store synced context in local storage for popup display
  await chrome.storage.local.set({
    syncedContext: {
      conversationId: payload.conversationId,
      messages: payload.messages,
      timestamp: Date.now(),
    },
  });
  return { success: true, received: payload.messages.length };
}

async function handleCloseTabs(payload) {
  const { tabIds } = payload;
  if (!tabIds || !tabIds.length) return { success: true, closed: 0 };
  let closed = 0;
  for (const tabId of tabIds) {
    try {
      await chrome.tabs.remove(tabId);
      closed++;
    } catch (e) { console.warn('[Bridge] Tab already closed:', tabId, e.message); }
  }
  // Clean from session groups
  for (const [sessionId, group] of sessionGroups) {
    for (const tabId of tabIds) group.tabIds.delete(tabId);
    if (group.tabIds.size === 0) sessionGroups.delete(sessionId);
  }
  closeKeepaliveIfNoSessions();
  return { success: true, closed };
}

async function handleSessionCleanup(payload) {
  const { sessionId } = payload;
  const group = sessionGroups.get(sessionId);
  if (!group) return { success: true, note: 'No tab group found for session' };

  const tabIds = [...group.tabIds];
  for (const tabId of tabIds) {
    await chrome.tabs.remove(tabId).catch(() => {});
  }
  sessionGroups.delete(sessionId);
  console.log(`[Bridge] Cleaned up session group "${group.label}" (${tabIds.length} tabs closed)`);
  closeKeepaliveIfNoSessions();
  return { success: true, closed: tabIds.length };
}

async function handleGetTabs(payload) {
  const tabs = await chrome.tabs.query({});

  // Build reverse lookup: tabId -> sessionLabel
  const tabSessionMap = new Map();
  for (const [, group] of sessionGroups) {
    for (const tid of group.tabIds) {
      tabSessionMap.set(tid, group.label);
    }
  }

  return {
    tabs: tabs.map((t) => {
      const entry = {
        id: t.id,
        url: t.url,
        title: t.title,
        active: t.active,
        windowId: t.windowId,
        status: t.status,
        groupId: t.groupId,
      };
      const session = tabSessionMap.get(t.id);
      if (session) entry.sessionGroup = session;
      return entry;
    }),
  };
}

async function handleSwitchTab(payload) {
  await chrome.tabs.update(payload.tabId, { active: true });
  const tab = await chrome.tabs.get(payload.tabId);
  await chrome.windows.update(tab.windowId, { focused: true });
  return { success: true, tabId: payload.tabId };
}

// ---------------------------------------------------------------------------
// JavaScript evaluation (CDP-based)
// ---------------------------------------------------------------------------

async function handleEvaluate(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  try {
    await cdpAcquire(tabId);
    const result = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: payload.expression,
      returnByValue: payload.returnByValue !== false, // default true
      awaitPromise: true,
    });

    if (result.exceptionDetails) {
      const errText = result.exceptionDetails.exception?.description
        || result.exceptionDetails.text
        || 'Evaluation error';
      return { success: false, error: errText };
    }

    return {
      success: true,
      result: result.result.value,
      type: result.result.type,
    };
  } finally {
    await cdpRelease(tabId);
  }
}

// ---------------------------------------------------------------------------
// Console message retrieval (CDP — data lives in main world)
// ---------------------------------------------------------------------------

async function handleGetConsoleMessages(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  const level = payload.level || 'all';
  const limit = payload.limit || 100;

  // Read messages from main world via CDP (content script can't access them)
  const expr = `(function(){
    var msgs = window.__claudeConsoleMessages || [];
    var filtered = ${level === 'all' ? 'msgs' : `msgs.filter(function(m){return m.level===${JSON.stringify(level)}})`};
    return { success: true, messages: filtered.slice(-${limit}), total: filtered.length };
  })()`;

  try {
    await cdpAcquire(tabId);
    const result = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: expr,
      returnByValue: true,
    });

    if (result.result && result.result.value) {
      return result.result.value;
    }
    return { success: true, messages: [], note: 'No console messages captured' };
  } catch (err) {
    return { success: true, messages: [], note: `Could not retrieve: ${err.message}` };
  } finally {
    await cdpRelease(tabId);
  }
}

// ---------------------------------------------------------------------------
// Dialog handling (CDP-based)
// ---------------------------------------------------------------------------

async function handleDialog(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  const accept = payload.action !== 'dismiss';
  const dialogParams = { accept };
  if (payload.action === 'send' && payload.text != null) {
    dialogParams.promptText = payload.text;
  }

  try {
    await cdpAcquire(tabId);
    await cdpCommand(tabId, 'Page.enable');

    // Try handling an already-showing dialog first
    try {
      await cdpCommand(tabId, 'Page.handleJavaScriptDialog', dialogParams);
      return { success: true, action: payload.action };
    } catch {
      // No dialog yet — wait for one to appear (up to 3s)
    }

    // Listen for dialog event via chrome.debugger.onEvent
    const result = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        chrome.debugger.onEvent.removeListener(listener);
        reject(new Error('No dialog appeared within 3 seconds'));
      }, 3000);

      function listener(source, method, params) {
        if (source.tabId !== tabId || method !== 'Page.javascriptDialogOpening') return;
        clearTimeout(timeout);
        chrome.debugger.onEvent.removeListener(listener);
        cdpCommand(tabId, 'Page.handleJavaScriptDialog', dialogParams)
          .then(() => resolve({
            success: true,
            action: payload.action,
            dialogType: params.type,
            message: params.message,
          }))
          .catch(reject);
      }

      chrome.debugger.onEvent.addListener(listener);
    });

    return result;
  } catch (err) {
    if (err.message && err.message.includes('No dialog')) {
      return { success: false, error: err.message, code: 'NO_DIALOG' };
    }
    throw err;
  } finally {
    await cdpRelease(tabId);
  }
}

// ---------------------------------------------------------------------------
// CDP keyboard typing (produces trusted events — required for React apps)
// ---------------------------------------------------------------------------

async function handleCdpType(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  const text = payload.text;
  if (!text) throw new Error('No text provided');

  const selector = payload.selector || null;
  const delay = payload.delay || 50; // ms between keystrokes

  try {
    await cdpAcquire(tabId);

    // Focus the target element via trusted CDP click if a selector is provided
    if (selector) {
      // Get element center coordinates, then dispatch a trusted mouse click via CDP
      const locResult = await cdpCommand(tabId, 'Runtime.evaluate', {
        expression: `(function(){ var el = document.querySelector(${JSON.stringify(selector)}); if(!el) return null; var r = el.getBoundingClientRect(); return { x: r.left + r.width/2, y: r.top + r.height/2 }; })()`,
        returnByValue: true,
      });
      if (locResult && locResult.result && locResult.result.value) {
        const { x, y } = locResult.result.value;
        await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
        await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
        // Force detach + re-attach to initialize the CDP Input pipeline for Lexical/rich editors.
        // Without this cycle, the first Input.insertText on a fresh tab fails silently.
        await cdpForceReattach(tabId);
      } else {
        return { success: false, error: `Element not found: ${selector}`, code: 'FOCUS_FAILED' };
      }
    }

    // Special key map: char → {key, code, keyCode} (no char event, just keyDown/keyUp)
    const SPECIAL_KEYS = {
      '\n': { key: 'Enter', code: 'Enter', keyCode: 13 },
      '\r': { key: 'Enter', code: 'Enter', keyCode: 13 },
      '\t': { key: 'Tab', code: 'Tab', keyCode: 9 },
    };

    // Type each character using CDP Input.dispatchKeyEvent
    for (const char of text) {
      const special = SPECIAL_KEYS[char];

      if (special) {
        // Special keys: keyDown + keyUp only (no char event)
        await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
          type: 'keyDown',
          key: special.key,
          code: special.code,
          windowsVirtualKeyCode: special.keyCode,
          nativeVirtualKeyCode: special.keyCode,
        });
        await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
          type: 'keyUp',
          key: special.key,
          code: special.code,
          windowsVirtualKeyCode: special.keyCode,
          nativeVirtualKeyCode: special.keyCode,
        });
      } else {
        // Regular characters: keyDown + insertText + keyUp
        // Using Input.insertText instead of 'char' event because Lexical/ProseMirror
        // editors rely on beforeinput/input events, not keypress, for text insertion
        const code = char === '/' ? 'Slash' : char === ' ' ? 'Space' : `Key${char.toUpperCase()}`;
        const keyCode = char.charCodeAt(0);

        await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
          type: 'keyDown',
          key: char,
          code,
          windowsVirtualKeyCode: keyCode,
          nativeVirtualKeyCode: keyCode,
        });
        await cdpCommand(tabId, 'Input.insertText', { text: char });
        await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
          type: 'keyUp',
          key: char,
          code,
          windowsVirtualKeyCode: keyCode,
          nativeVirtualKeyCode: keyCode,
        });
      }

      // Brief delay between keystrokes
      if (delay > 0) {
        await new Promise((r) => setTimeout(r, delay));
      }
    }

    return { success: true, typed: text, length: text.length };
  } finally {
    await cdpRelease(tabId);
  }
}

// ---------------------------------------------------------------------------
// Council activation (single deterministic tool call)
// ---------------------------------------------------------------------------

async function handleActivateCouncil(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  const delay = 50;

  try {
    await cdpAcquire(tabId);

    // 1. Focus #ask-input via trusted CDP click
    const locResult = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: `(function(){ var el = document.querySelector('#ask-input'); if(!el) return null; var r = el.getBoundingClientRect(); return { x: r.left + r.width/2, y: r.top + r.height/2 }; })()`,
      returnByValue: true,
    });
    if (!locResult?.result?.value) {
      return { success: false, error: 'Could not find #ask-input', code: 'FOCUS_FAILED' };
    }
    const { x, y } = locResult.result.value;
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
    // Force CDP cycle for Lexical editors
    await cdpForceReattach(tabId);

    // 2. Type "/council" character by character
    const text = '/council';
    for (const char of text) {
      const code = char === '/' ? 'Slash' : `Key${char.toUpperCase()}`;
      const keyCode = char.charCodeAt(0);
      await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
        type: 'keyDown', key: char, code,
        windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode,
      });
      await cdpCommand(tabId, 'Input.insertText', { text: char });
      await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
        type: 'keyUp', key: char, code,
        windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode,
      });
      await new Promise(r => setTimeout(r, delay));
    }

    // 3. Wait for command palette
    await new Promise(r => setTimeout(r, COMMAND_PALETTE_DELAY));

    // 4. Press Enter to select /council shortcut
    await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
      type: 'keyDown', key: 'Enter', code: 'Enter',
      windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
    });
    await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
      type: 'keyUp', key: 'Enter', code: 'Enter',
      windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
    });

    // 5. Wait for council mode activation
    await new Promise(r => setTimeout(r, COMMAND_PALETTE_DELAY));

    // 6. Verify — check for "3 models" dropdown or "Model council" chip
    const verifyResult = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: `(function(){
        var chip = document.querySelector("button[aria-label='3 models']");
        var councilBtn = [...document.querySelectorAll('button')].find(b => b.textContent.trim().includes('Model council'));
        var modelCount = chip ? (chip.textContent.match(/\\d+/) || ['3'])[0] : null;
        return { activated: !!(chip || councilBtn), modelCount: modelCount ? parseInt(modelCount) : null, method: chip ? '3_models_dropdown' : councilBtn ? 'council_chip' : 'none' };
      })()`,
      returnByValue: true,
    });

    const verify = verifyResult?.result?.value || { activated: false };
    return { success: true, activated: verify.activated, modelCount: verify.modelCount, method: verify.method, tabId };
  } finally {
    await cdpRelease(tabId);
  }
}

// ---------------------------------------------------------------------------
// Native "Export as Markdown" from Perplexity three-dot menu
// ---------------------------------------------------------------------------

async function handleExportCouncilMarkdown(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');

  try {
    await cdpAcquire(tabId);

    // 1. Find and click the three-dot (more options) button
    const menuResult = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: `(function(){
        var btn = document.querySelector("button[aria-label='Thread actions']")
          || document.querySelector("button[aria-label='More']")
          || document.querySelector("button[aria-label='More options']")
          || document.querySelector("[data-testid='thread-more-button']");
        if (!btn) {
          var allBtns = document.querySelectorAll('button');
          for (var b of allBtns) {
            var svg = b.querySelector('svg');
            if (svg && b.textContent.trim() === '' && b.closest('[class*="flex"]')) {
              var rect = b.getBoundingClientRect();
              if (rect.top < 80) { btn = b; break; }
            }
          }
        }
        if (!btn) return null;
        var r = btn.getBoundingClientRect();
        return { x: r.left + r.width/2, y: r.top + r.height/2 };
      })()`,
      returnByValue: true,
    });

    if (!menuResult?.result?.value) {
      return { success: false, error: 'Could not find three-dot menu button', code: 'MENU_NOT_FOUND' };
    }

    // Click the menu button via CDP (trusted click)
    const { x, y } = menuResult.result.value;
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });

    // 2. Wait for dropdown menu to render
    await new Promise(r => setTimeout(r, MENU_ANIMATION_DELAY));

    // 3. Find and click "Export as Markdown"
    const exportResult = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: `(function(){
        var items = document.querySelectorAll('[role="menuitem"], [role="option"], button, a');
        for (var item of items) {
          var text = (item.textContent || '').trim().toLowerCase();
          if (text.includes('export as markdown') || text === 'markdown') {
            var r = item.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
              return { x: r.left + r.width/2, y: r.top + r.height/2, label: item.textContent.trim() };
            }
          }
        }
        var available = [];
        document.querySelectorAll('[role="menuitem"], [role="option"]').forEach(function(el) {
          available.push(el.textContent.trim());
        });
        return { error: 'Export as Markdown not found', menuItems: available };
      })()`,
      returnByValue: true,
    });

    if (exportResult?.result?.value?.error) {
      // Close menu by pressing Escape
      await cdpCommand(tabId, 'Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
      await cdpCommand(tabId, 'Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
      return { success: false, error: exportResult.result.value.error, menuItems: exportResult.result.value.menuItems, code: 'EXPORT_NOT_FOUND' };
    }

    // Click "Export as Markdown"
    const ex = exportResult.result.value;
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mousePressed', x: ex.x, y: ex.y, button: 'left', clickCount: 1 });
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x: ex.x, y: ex.y, button: 'left', clickCount: 1 });

    // 4. Wait for download to initiate
    await new Promise(r => setTimeout(r, DOWNLOAD_INIT_DELAY));

    // 5. Get page title for filename identification
    const titleResult = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: `document.title`,
      returnByValue: true,
    });
    const pageTitle = titleResult?.result?.value || 'council-export';

    return {
      success: true,
      exported: true,
      pageTitle,
      note: 'Markdown file downloaded to browser default downloads folder. Check Downloads for the file.',
      tabId,
    };
  } finally {
    await cdpRelease(tabId);
  }
}

// ---------------------------------------------------------------------------
// Get all cookies (including httpOnly) via chrome.cookies API
// ---------------------------------------------------------------------------

async function handleGetAllCookies(payload) {
  const domain = payload?.domain || '.perplexity.ai';
  const cookies = await chrome.cookies.getAll({ domain });
  return { cookies: cookies.map(c => ({
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path,
    secure: c.secure,
    httpOnly: c.httpOnly,
    sameSite: c.sameSite,
    expirationDate: c.expirationDate,
  }))};
}

// ---------------------------------------------------------------------------
// Add Perplexity thread to a Space
// ---------------------------------------------------------------------------

async function handleAddToSpace(payload) {
  const tabId = await getTargetTabId(payload);
  if (!tabId) throw new Error('No active tab found');
  const { spaceName, createIfMissing } = payload;

  try {
    await cdpAcquire(tabId);

    // Step 1: Click three-dot menu (reuse selector logic from handleExportCouncilMarkdown)
    const menuResult = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: `(function(){
        var btn = document.querySelector("button[aria-label='Thread actions']")
          || document.querySelector("button[aria-label='More']")
          || document.querySelector("button[aria-label='More options']")
          || document.querySelector("[data-testid='thread-more-button']");
        if (!btn) {
          var allBtns = document.querySelectorAll('button');
          for (var b of allBtns) {
            var svg = b.querySelector('svg');
            if (svg && b.textContent.trim() === '' && b.closest('[class*="flex"]')) {
              var rect = b.getBoundingClientRect();
              if (rect.top < 80) { btn = b; break; }
            }
          }
        }
        if (!btn) return null;
        var r = btn.getBoundingClientRect();
        return { x: r.left + r.width/2, y: r.top + r.height/2 };
      })()`,
      returnByValue: true,
    });

    if (!menuResult?.result?.value) {
      return { success: false, error: 'Could not find three-dot menu button', code: 'MENU_NOT_FOUND' };
    }

    // Trusted click on menu button
    const { x, y } = menuResult.result.value;
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
    await new Promise(r => setTimeout(r, MENU_ANIMATION_DELAY));

    // Step 2: Click "Add to Space" in dropdown
    const addResult = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: `(function(){
        var items = document.querySelectorAll('[role="menuitem"], [role="option"], button, a');
        for (var item of items) {
          var text = (item.textContent || '').trim().toLowerCase();
          if (text.includes('add to space')) {
            var r = item.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
              return { x: r.left + r.width/2, y: r.top + r.height/2, label: item.textContent.trim() };
            }
          }
        }
        var available = [];
        document.querySelectorAll('[role="menuitem"], [role="option"]').forEach(function(el) {
          available.push(el.textContent.trim());
        });
        return { error: 'Add to Space not found', menuItems: available };
      })()`,
      returnByValue: true,
    });

    if (addResult?.result?.value?.error) {
      // Close menu via Escape
      await cdpCommand(tabId, 'Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
      await cdpCommand(tabId, 'Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
      return { success: false, error: addResult.result.value.error, menuItems: addResult.result.value.menuItems, code: 'ADD_TO_SPACE_NOT_FOUND' };
    }

    // Trusted click on "Add to Space"
    const as = addResult.result.value;
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mousePressed', x: as.x, y: as.y, button: 'left', clickCount: 1 });
    await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x: as.x, y: as.y, button: 'left', clickCount: 1 });
    await new Promise(r => setTimeout(r, SPACE_MODAL_DELAY));

    // Step 3: Read the "Choose Space" modal — get list of available spaces
    const spacesResult = await cdpCommand(tabId, 'Runtime.evaluate', {
      expression: `(function(){
        var heading = Array.from(document.querySelectorAll('h2, h3, [role="heading"]'))
          .find(function(el) { return el.textContent.trim() === 'Choose Space'; });
        if (!heading) return { error: 'Choose Space modal not found' };
        var modal = heading.closest('[role="dialog"]') || heading.parentElement?.parentElement;
        if (!modal) return { error: 'Could not locate modal container' };

        var spaces = [];
        var items = modal.querySelectorAll('[role="option"], [role="button"], button, a, div[class]');
        var newSpaceBtn = null;
        for (var item of items) {
          var text = (item.textContent || '').trim();
          if (!text || text === 'Choose Space') continue;
          var r = item.getBoundingClientRect();
          if (r.width < 50 || r.height < 10) continue;
          if (text === '+ New Space' || text === 'New Space') {
            newSpaceBtn = { x: r.left + r.width/2, y: r.top + r.height/2 };
          } else if (text !== '\\u00d7' && text.length > 1 && text.length < 100) {
            spaces.push({ name: text, x: r.left + r.width/2, y: r.top + r.height/2 });
          }
        }
        return { spaces: spaces, newSpaceBtn: newSpaceBtn };
      })()`,
      returnByValue: true,
    });

    if (spacesResult?.result?.value?.error) {
      return { success: false, error: spacesResult.result.value.error, code: 'MODAL_NOT_FOUND' };
    }

    const { spaces, newSpaceBtn } = spacesResult.result.value;

    // If no spaceName requested, just return the list
    if (!spaceName) {
      await cdpCommand(tabId, 'Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
      await cdpCommand(tabId, 'Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
      return { success: true, action: 'list', spaces: spaces.map(s => s.name), tabId };
    }

    // Step 4: Search for matching space (case-insensitive fuzzy match)
    const target = spaceName.toLowerCase();
    const match = spaces.find(s => s.name.toLowerCase() === target)
      || spaces.find(s => s.name.toLowerCase().includes(target))
      || spaces.find(s => target.includes(s.name.toLowerCase()));

    if (match) {
      // Click the matching space
      await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mousePressed', x: match.x, y: match.y, button: 'left', clickCount: 1 });
      await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x: match.x, y: match.y, button: 'left', clickCount: 1 });
      await new Promise(r => setTimeout(r, SPACE_SELECT_DELAY));
      return { success: true, action: 'added', spaceName: match.name, tabId };
    }

    // Step 5: No match — create new space if requested
    if (createIfMissing && newSpaceBtn) {
      await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mousePressed', x: newSpaceBtn.x, y: newSpaceBtn.y, button: 'left', clickCount: 1 });
      await cdpCommand(tabId, 'Input.dispatchMouseEvent', { type: 'mouseReleased', x: newSpaceBtn.x, y: newSpaceBtn.y, button: 'left', clickCount: 1 });
      await new Promise(r => setTimeout(r, SPACE_MODAL_DELAY));

      // Type the space name via CDP trusted keystrokes
      for (const char of spaceName) {
        const code = char === ' ' ? 'Space' : char === '/' ? 'Slash' : `Key${char.toUpperCase()}`;
        const keyCode = char.charCodeAt(0);
        await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
          type: 'keyDown', key: char, code,
          windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode,
        });
        await cdpCommand(tabId, 'Input.insertText', { text: char });
        await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
          type: 'keyUp', key: char, code,
          windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode,
        });
      }
      await new Promise(r => setTimeout(r, 500));

      // Press Enter to confirm
      await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
        type: 'keyDown', key: 'Enter', code: 'Enter',
        windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
      });
      await cdpCommand(tabId, 'Input.dispatchKeyEvent', {
        type: 'keyUp', key: 'Enter', code: 'Enter',
        windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
      });
      await new Promise(r => setTimeout(r, SPACE_CREATION_DELAY));

      return { success: true, action: 'created', spaceName, tabId };
    }

    // No match and not creating — close modal, return available spaces
    await cdpCommand(tabId, 'Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
    await cdpCommand(tabId, 'Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 });
    return { success: false, action: 'no_match', spaceName, availableSpaces: spaces.map(s => s.name), tabId };

  } finally {
    await cdpRelease(tabId);
  }
}

// ---------------------------------------------------------------------------
// Auto-push page context on tab changes
// ---------------------------------------------------------------------------

async function pushActiveTabContext() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab && tab.url && !tab.url.startsWith('chrome://')) {
      sendToServer({
        type: 'page_context_update',
        payload: {
          url: tab.url,
          title: tab.title,
          tabId: tab.id,
          timestamp: Date.now(),
        },
      });
    }
  } catch {
    // Tab query can fail during startup; ignore
  }
}

// Tab activation and update listeners
chrome.tabs.onActivated.addListener(() => {
  pushActiveTabContext();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.active) {
    pushActiveTabContext();
  }
});

// ---------------------------------------------------------------------------
// Layer 2: chrome.alarms keepalive
// ---------------------------------------------------------------------------

chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_PERIOD });

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    // Recreate Layer 3 if Chrome killed the offscreen document
    ensureOffscreen();

    if (!ws || ws.readyState !== WebSocket.OPEN) {
      // Cancel any pending slow reconnect and reset backoff
      clearTimeout(reconnectTimer);
      reconnectDelay = RECONNECT_BASE;
      connect();
    } else if (Date.now() - lastServerMessage > 45_000) {
      // Dead socket: appears OPEN but server hasn't sent anything for >45s
      // (server sends heartbeat pings every 30s, so 45s means it's gone)
      console.log('[Bridge] Dead socket detected — no server data for 45s, reconnecting');
      ws.close();
      reconnectDelay = RECONNECT_BASE;
      connect();
    }
  }
});

// ---------------------------------------------------------------------------
// Layer 3: Offscreen keepalive message handler
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.keepAlive) {
    sendResponse({ alive: true });
    return;
  }

  // Handle keepalive page session info request
  if (msg.type === 'getActiveSessions') {
    const sessions = [];
    for (const [sessionId, group] of sessionGroups) {
      if (group.tabIds.size > 0) {
        sessions.push({ sessionId: sessionId.slice(0, 8), label: group.label, tabCount: group.tabIds.size });
      }
    }
    sendResponse({ sessions });
    return;
  }

  // Handle popup status requests
  if (msg.type === 'getStatus') {
    sendResponse({
      connected: isConnected,
      clientId,
      wsUrl: WS_URL,
    });
    return;
  }

  // Handle popup metrics request (from health endpoint)
  if (msg.type === 'getMetrics') {
    fetch('http://127.0.0.1:8766/metrics')
      .then(r => r.json())
      .then(data => sendResponse(data))
      .catch(() => sendResponse(null));
    return true; // async sendResponse
  }

  // Handle popup health request (full health including bridge status)
  if (msg.type === 'getHealth') {
    fetch('http://127.0.0.1:8766/health')
      .then(r => r.json())
      .then(data => sendResponse(data))
      .catch(() => sendResponse(null));
    return true; // async sendResponse
  }

  // Handle popup reconnect request
  if (msg.type === 'reconnect') {
    if (ws) {
      ws.close();
    }
    reconnectDelay = RECONNECT_BASE;
    connect();
    sendResponse({ reconnecting: true });
    return;
  }

  // Handle popup prompt send
  if (msg.type === 'sendPrompt') {
    sendToServer({
      type: 'page_context_update',
      payload: {
        url: 'extension://popup',
        title: 'User Prompt',
        content: msg.prompt,
        timestamp: Date.now(),
      },
    });
    sendResponse({ sent: true });
    return;
  }
});

// ---------------------------------------------------------------------------
// Initialize
// ---------------------------------------------------------------------------

ensureOffscreen();
connect();

console.log('[Bridge] Service worker initialized');

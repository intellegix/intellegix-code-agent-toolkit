/**
 * WebSocketBridge — manages Chrome extension connections via WebSocket.
 */

import { WebSocketServer } from 'ws';
import { randomUUID } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { CONFIG } from './config.js';
import { log } from './logger.js';

export class WebSocketBridge extends EventEmitter {
  constructor() {
    super();
    this.wss = null;
    this.browserClients = new Map();     // ws -> { id, lastPing, connectedAt }
    this.relayClients = new Map();       // ws -> { id, lastPing, connectedAt, role, sessionId, lastActivity, pid }
    this.pendingRequests = new Map();     // requestId -> { resolve, reject, timer }
    this.heartbeatTimer = null;
    this.cachedPageContext = null;
  }

  start() {
    return new Promise((resolve, reject) => {
      this.wss = new WebSocketServer({
        host: CONFIG.wsHost,
        port: CONFIG.wsPort,
        maxPayload: CONFIG.maxMessageSize,
      });

      this.wss.once('listening', () => {
        console.error(`[WebSocketBridge] Listening on ws://${CONFIG.wsHost}:${CONFIG.wsPort}`);
        // Re-attach persistent error handler after successful bind
        this.wss.on('error', (err) => {
          console.error('[WebSocketBridge] Server error:', err.message);
        });
        resolve();
      });

      this.wss.once('error', (err) => {
        console.error('[WebSocketBridge] Server error:', err.message);
        reject(err);
      });

      this.wss.on('connection', (ws) => this._onConnection(ws));
      this.heartbeatTimer = setInterval(() => this._checkHeartbeats(), CONFIG.heartbeatCheck);
    });
  }

  _onConnection(ws) {
    const clientId = randomUUID();
    this.browserClients.set(ws, { id: clientId, lastPing: Date.now(), lastAppMsg: Date.now(), connectedAt: Date.now(), role: 'default' });

    // Send connection init
    this._send(ws, { type: 'connection_init', clientId, serverVersion: '1.0.0' });

    ws.on('message', (raw) => {
      try {
        const msg = JSON.parse(raw.toString());
        this._onMessage(ws, msg);
      } catch {
        console.error('[WebSocketBridge] Bad JSON from client');
      }
    });

    ws.on('pong', () => {
      const info = this.browserClients.get(ws) || this.relayClients.get(ws);
      if (info) info.lastPing = Date.now();
    });

    ws.on('close', () => {
      const closingInfo = this.browserClients.get(ws) || this.relayClients.get(ws);
      this.browserClients.delete(ws);
      this.relayClients.delete(ws);
      this.emit('clientDisconnected', clientId);
      log.info('ws_close', { clientId, browsers: this.browserClients.size, relays: this.relayClients.size });

      // When a relay disconnects, emit event for recovery handling and tell browser clients to close its session tabs
      if (closingInfo && closingInfo.role === 'stdio-relay' && closingInfo.sessionId) {
        this.emit('relayDisconnected', { sessionId: closingInfo.sessionId, pid: closingInfo.pid });
        const cleanup = { type: 'session_cleanup', payload: { sessionId: closingInfo.sessionId } };
        for (const [clientWs] of this.browserClients) {
          this._send(clientWs, cleanup);
        }
        console.error(`[WebSocketBridge] Sent session_cleanup for relay session ${closingInfo.sessionId.slice(0, 8)}`);
      }
    });

    ws.on('error', (err) => {
      console.error(`[WebSocketBridge] Client ${clientId} error:`, err.message);
    });

    this.emit('clientConnected', clientId);
    log.info('ws_connect', { clientId, browsers: this.browserClients.size, relays: this.relayClients.size });
  }

  async _onMessage(ws, msg) {
    // Update last activity — app-level messages update both timestamps
    const info = this.browserClients.get(ws) || this.relayClients.get(ws);
    if (info) {
      info.lastPing = Date.now();
      info.lastAppMsg = Date.now();
    }

    // Browser client identification — record role so we can pick a single
    // canonical target when more than one Chrome has the extension (e.g. the
    // user's personal profile AND the always-on keeper). Backward-compatible:
    // clients that never send 'hello' keep the default role.
    if (msg.type === 'hello') {
      const bi = this.browserClients.get(ws);
      if (bi) {
        bi.role = msg.role === 'keeper' ? 'keeper' : 'default';
        if (msg.clientId) bi.clientId = msg.clientId;
      }
      log.info('browser_hello', { clientId: bi?.id, role: bi?.role, browsers: this.browserClients.size });
      return;
    }

    // Handle relay client identification — move from browserClients to relayClients
    if (msg.type === 'relay_init') {
      const browserInfo = this.browserClients.get(ws);
      if (browserInfo) {
        this.browserClients.delete(ws);
        browserInfo.role = 'stdio-relay';
        browserInfo.sessionId = msg.payload?.sessionId;
        browserInfo.lastActivity = Date.now();
        browserInfo.pid = msg.payload?.pid;
        this.relayClients.set(ws, browserInfo);
      }
      log.info('relay_connect', { clientId: browserInfo?.id, pid: msg.payload?.pid, sessionId: msg.payload?.sessionId?.slice(0, 8), totalRelays: this.relayClients.size });
      this.emit('relayConnected', { sessionId: msg.payload?.sessionId, pid: msg.payload?.pid, projectPath: msg.payload?.projectPath, projectLabel: msg.payload?.projectLabel });
      return;
    }

    // Handle relay-forwarded tool calls — route to browser clients via broadcast
    if (msg.type === 'relay_forward') {
      // Track relay activity for idle TTL
      const relayInfo = this.relayClients.get(ws);
      if (relayInfo) relayInfo.lastActivity = Date.now();

      const relayRequestId = msg.requestId;
      const timeout = msg.timeout || CONFIG.requestTimeout;

      // Wait for a browser client if none connected (e.g. extension reconnecting)
      if (this.browserClients.size === 0) {
        try {
          await this._waitForBrowserClient();
        } catch {
          this._send(ws, { requestId: relayRequestId, error: 'No browser extension connected' });
          return;
        }
      }

      // Use a fresh requestId for the browser broadcast to avoid collision
      const browserRequestId = randomUUID();
      const envelope = { ...msg.payload, requestId: browserRequestId };

      const timer = setTimeout(() => {
        this.pendingRequests.delete(browserRequestId);
        this._send(ws, { requestId: relayRequestId, error: `Request timed out after ${timeout}ms`, code: 'TIMEOUT' });
      }, timeout);

      this.pendingRequests.set(browserRequestId, {
        resolve: (result) => {
          this._send(ws, { requestId: relayRequestId, result });
        },
        reject: (err) => {
          this._send(ws, { requestId: relayRequestId, error: err.message, ...(err.code && { code: err.code }) });
        },
        timer,
      });

      const relayTarget = this._selectTargetClient();
      if (!relayTarget) {
        clearTimeout(timer);
        this.pendingRequests.delete(browserRequestId);
        this._send(ws, { requestId: relayRequestId, error: 'No browser extension connected' });
        return;
      }
      this._send(relayTarget, envelope);
      return;
    }

    // Handle response to a pending request
    if (msg.requestId && this.pendingRequests.has(msg.requestId)) {
      const pending = this.pendingRequests.get(msg.requestId);
      this.pendingRequests.delete(msg.requestId);
      clearTimeout(pending.timer);
      if (msg.error) {
        const err = new Error(msg.error);
        if (msg.code) err.code = msg.code;
        pending.reject(err);
      } else {
        pending.resolve(msg.result || msg);
      }
      return;
    }

    // Handle browser-initiated events
    if (msg.type === 'page_context_update') {
      this.cachedPageContext = msg.payload;
      this.emit('pageContextUpdate', msg.payload);
      return;
    }

    if (msg.type === 'pong' || msg.type === 'keepalive') {
      return; // heartbeat responses
    }

    // Forward unhandled events
    this.emit('message', msg);
  }

  /**
   * Choose the SINGLE browser client that should execute a command when more
   * than one is connected. Preference: role 'keeper' over 'default'; tiebreak
   * most-recently-connected. Only OPEN sockets are eligible. Returns the chosen
   * ws, or null if none are usable.
   *
   * This replaces the former send-to-ALL broadcast fan-out, which caused every
   * connected extension to EXECUTE mutation commands (navigate/click/switch_tab)
   * — a real bug once a dedicated keeper Chrome runs alongside the user's
   * personal Chrome. With a single client this always returns that client, so
   * behavior is unchanged. Failover is automatic and per-command: a dead
   * primary is skipped (readyState) or already evicted, and the next command
   * re-selects the surviving client.
   */
  _selectTargetClient() {
    let best = null;
    let bestScore = -Infinity;
    for (const [ws, info] of this.browserClients) {
      if (ws.readyState !== 1 /* OPEN */) continue;
      const score = (info.role === 'keeper' ? 1e13 : 0) + (info.connectedAt || 0);
      if (score > bestScore) {
        bestScore = score;
        best = ws;
      }
    }
    return best;
  }

  /**
   * Wait up to `timeoutMs` for at least one non-relay browser client to connect.
   * Resolves immediately if one is already present.
   */
  _waitForBrowserClient(timeoutMs = CONFIG.waitForBrowserTimeout) {
    if (this.browserClients.size > 0) return Promise.resolve();

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.removeListener('clientConnected', onClient);
        reject(new Error('No browser extension connected'));
      }, timeoutMs);

      const onClient = () => {
        if (this.browserClients.size > 0) {
          clearTimeout(timer);
          this.removeListener('clientConnected', onClient);
          resolve();
        }
      };
      this.on('clientConnected', onClient);
    });
  }

  /**
   * Send a request to all connected browser extensions and await the first response.
   */
  async broadcast(message, timeout = CONFIG.requestTimeout) {
    await this._waitForBrowserClient();

    const requestId = randomUUID();
    const envelope = { ...message, requestId };
    log.debug('broadcast', { requestId, type: message.type });

    return new Promise((resolve, reject) => {
      const broadcastStart = Date.now();
      const timer = setTimeout(() => {
        this.pendingRequests.delete(requestId);
        log.warn('broadcast_timeout', { requestId, type: message.type, timeout });
        const err = new Error(`Request timed out after ${timeout}ms`);
        err.code = 'TIMEOUT';
        reject(err);
      }, timeout);

      this.pendingRequests.set(requestId, {
        resolve: (result) => {
          log.debug('broadcast_response', { requestId, duration: Date.now() - broadcastStart });
          resolve(result);
        },
        reject,
        timer,
      });

      // Single-target dispatch: send only to the elected client (keeper-first),
      // never fan out to every connected extension (which double-executed
      // mutations on multiple Chromes).
      const target = this._selectTargetClient();
      if (!target) {
        clearTimeout(timer);
        this.pendingRequests.delete(requestId);
        const err = new Error('No browser extension connected');
        err.code = 'NO_CLIENT';
        reject(err);
        return;
      }
      this._send(target, envelope);
    });
  }

  _send(ws, data) {
    if (ws.readyState === 1 /* OPEN */) {
      ws.send(JSON.stringify(data));
    }
  }

  _checkHeartbeats() {
    const now = Date.now();
    // Browser clients — evict zombies via app-level staleness (45s = 2 missed keepalives)
    // WS-level pong keeps lastPing fresh on half-open sockets, but only real
    // extension service workers send keepalive app messages every 20s.
    const APP_MSG_TIMEOUT = CONFIG.appMsgTimeout;
    for (const [ws, info] of this.browserClients) {
      if (now - (info.lastAppMsg || info.connectedAt) > APP_MSG_TIMEOUT) {
        log.warn('ws_app_stale', { clientId: info.id, staleSec: Math.round((now - (info.lastAppMsg || info.connectedAt)) / 1000) });
        ws.close(1000, 'App-level heartbeat timeout');
        this.browserClients.delete(ws);
      } else if (now - info.lastPing > CONFIG.heartbeatTimeout) {
        log.warn('ws_heartbeat_timeout', { clientId: info.id });
        ws.close(1000, 'Heartbeat timeout');
        this.browserClients.delete(ws);
      } else {
        ws.ping();
      }
    }
    // Relay clients — heartbeat + idle TTL eviction
    for (const [ws, info] of this.relayClients) {
      if (now - info.lastPing > CONFIG.heartbeatTimeout) {
        log.warn('ws_heartbeat_timeout', { clientId: info.id, type: 'relay' });
        ws.close(1000, 'Heartbeat timeout');
        this.relayClients.delete(ws);
      } else if (now - (info.lastActivity || info.connectedAt) > CONFIG.relayIdleTtl) {
        log.warn('relay_idle_evict', { clientId: info.id, sessionId: info.sessionId?.slice(0, 8), idleMin: Math.round((now - (info.lastActivity || info.connectedAt)) / 60000) });
        ws.close(1000, 'Relay idle timeout');
        this.relayClients.delete(ws);
      } else {
        ws.ping();
      }
    }
  }

  getStatus() {
    const now = Date.now();
    return {
      connected: this.browserClients.size > 0,
      clientCount: this.browserClients.size + this.relayClients.size,
      browserCount: this.browserClients.size,
      relayCount: this.relayClients.size,
      browsers: [...this.browserClients.values()].map(c => ({
        id: c.id,
        connectedAt: c.connectedAt,
        lastPing: c.lastPing,
      })),
      relays: [...this.relayClients.values()].map(c => ({
        id: c.id,
        connectedAt: c.connectedAt,
        lastPing: c.lastPing,
        sessionId: c.sessionId?.slice(0, 8),
        pid: c.pid,
        idleSeconds: Math.round((now - (c.lastActivity || c.connectedAt)) / 1000),
      })),
      cachedPageContext: this.cachedPageContext
        ? { url: this.cachedPageContext.url, title: this.cachedPageContext.title }
        : null,
    };
  }

  stop() {
    clearInterval(this.heartbeatTimer);
    for (const [ws] of this.browserClients) ws.close(1000, 'Server shutting down');
    for (const [ws] of this.relayClients) ws.close(1000, 'Server shutting down');
    for (const [, pending] of this.pendingRequests) {
      clearTimeout(pending.timer);
      pending.reject(new Error('Server shutting down'));
    }
    this.pendingRequests.clear();
    this.browserClients.clear();
    this.relayClients.clear();
    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }
  }
}

/**
 * test-target-selection.js — Unit tests for WebSocketBridge._selectTargetClient.
 *
 * The bridge used to fan every command out to ALL connected browser extensions,
 * which meant mutation commands (navigate/click/switch_tab) executed on every
 * connected Chrome. Once a dedicated keeper Chrome runs alongside the user's
 * personal Chrome, that double-executes. _selectTargetClient() picks ONE target
 * (keeper-first, tiebreak newest, OPEN-only). These tests lock that contract.
 *
 * Run with: node --test test-target-selection.js
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { WebSocketBridge } from './lib/websocket-bridge.js';

const OPEN = 1;
const CLOSING = 2;

/** Fake ws with a settable readyState. */
function fakeWs(readyState = OPEN) {
  return { readyState };
}

function bridgeWith(clients) {
  const b = new WebSocketBridge();
  for (const { ws, info } of clients) b.browserClients.set(ws, info);
  return b;
}

describe('_selectTargetClient', () => {
  it('returns the sole client when only one is connected (default)', () => {
    const ws = fakeWs();
    const b = bridgeWith([{ ws, info: { role: 'default', connectedAt: 100 } }]);
    assert.equal(b._selectTargetClient(), ws);
  });

  it('returns the sole client when only one is connected (keeper)', () => {
    const ws = fakeWs();
    const b = bridgeWith([{ ws, info: { role: 'keeper', connectedAt: 100 } }]);
    assert.equal(b._selectTargetClient(), ws);
  });

  it('prefers the keeper over the default when both are connected', () => {
    const personal = fakeWs();
    const keeper = fakeWs();
    // personal connected LATER — keeper must still win on role, not recency.
    const b = bridgeWith([
      { ws: personal, info: { role: 'default', connectedAt: 200 } },
      { ws: keeper, info: { role: 'keeper', connectedAt: 100 } },
    ]);
    assert.equal(b._selectTargetClient(), keeper);
  });

  it('falls over to the default when the keeper socket is not OPEN', () => {
    const personal = fakeWs(OPEN);
    const keeper = fakeWs(CLOSING);
    const b = bridgeWith([
      { ws: personal, info: { role: 'default', connectedAt: 200 } },
      { ws: keeper, info: { role: 'keeper', connectedAt: 100 } },
    ]);
    assert.equal(b._selectTargetClient(), personal);
  });

  it('breaks ties between same-role clients by most-recent connection', () => {
    const older = fakeWs();
    const newer = fakeWs();
    const b = bridgeWith([
      { ws: older, info: { role: 'default', connectedAt: 100 } },
      { ws: newer, info: { role: 'default', connectedAt: 300 } },
    ]);
    assert.equal(b._selectTargetClient(), newer);
  });

  it('treats a missing role as default', () => {
    const noRole = fakeWs();
    const keeper = fakeWs();
    const b = bridgeWith([
      { ws: noRole, info: { connectedAt: 500 } },           // no role field
      { ws: keeper, info: { role: 'keeper', connectedAt: 100 } },
    ]);
    assert.equal(b._selectTargetClient(), keeper);
  });

  it('returns null when there are no clients', () => {
    const b = bridgeWith([]);
    assert.equal(b._selectTargetClient(), null);
  });

  it('returns null when every client socket is non-OPEN', () => {
    const b = bridgeWith([
      { ws: fakeWs(CLOSING), info: { role: 'keeper', connectedAt: 100 } },
      { ws: fakeWs(CLOSING), info: { role: 'default', connectedAt: 200 } },
    ]);
    assert.equal(b._selectTargetClient(), null);
  });
});

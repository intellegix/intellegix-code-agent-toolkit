/**
 * browser-discovery.js — best-effort census of Chromium-family browser instances
 * running on this machine, so the bridge can DECLARE what it cannot see.
 *
 * Why this exists (2026-09-10):
 * `browser_get_tabs` returned five tabs from the single Chrome the extension is
 * installed in, with nothing in the response saying that a second Chrome was
 * running. An agent read that silence as absence, told Austin his fantasy
 * football league "must be on your phone", and started a phone-takeover relay.
 * The tab was open the whole time in a second Chrome on --remote-debugging-port=9223.
 * See STALE-TABS-AND-SINGLE-BROWSER-BLINDNESS-EVIDENCE-2026-09-10.md.
 *
 * Design constraints, in priority order:
 *  1. DETECT AND DECLARE ONLY. This module never attaches to, drives, or reads
 *     pages from another browser. It answers "does another browser exist?" so the
 *     tool response can say so. Controlling another instance is a separate,
 *     explicitly opt-in decision that has NOT been made.
 *  2. Never block the MCP stdio transport. Everything is async with hard
 *     deadlines; every failure degrades to "unknown", never to a throw.
 *  3. Cheap on the hot path. One PowerShell spawn per TTL window, not per call.
 *
 * Windows notes:
 *  - `wmic.exe` is REMOVED in Windows 11 24H2 and later and is deliberately not
 *    used here. `Get-CimInstance Win32_Process` is the supported replacement.
 *  - Chrome is multi-process. A chrome.exe PID is NOT a browser instance; only
 *    the process whose command line has no `--type=` switch is a browser root.
 */

import { spawn } from 'node:child_process';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

/** Chromium-family executables worth counting as "a browser instance". */
const BROWSER_EXES = new Set(['chrome.exe', 'msedge.exe', 'chromium.exe', 'brave.exe']);

/** Small fallback sweep for CDP endpoints whose port we could not read from a command line. */
const FALLBACK_PORT_LO = 9222;
const FALLBACK_PORT_HI = 9235;

export const DISCOVERY_TTL_MS = 15_000;

const POWERSHELL = process.env.SystemRoot
  ? `${process.env.SystemRoot}\\System32\\WindowsPowerShell\\v1.0\\powershell.exe`
  : 'powershell.exe';

const CIM_SCRIPT = `
$ErrorActionPreference = 'Stop'
Get-CimInstance -ClassName Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe' OR Name='chromium.exe' OR Name='brave.exe'" |
  Select-Object ProcessId, Name, CommandLine |
  ConvertTo-Json -Compress -Depth 3
`;

let _cache = { expiresAt: 0, value: null };
let _inFlight = null;

/**
 * Extract a Chromium switch value from a Windows command line.
 * Handles `--k=v`, `--k v`, and quoted values containing spaces. Never split a
 * Windows command line on whitespace — quoted profile paths break that.
 * @param {string} commandLine
 * @param {string} name switch name without leading dashes
 * @returns {string|null}
 */
export function getSwitchValue(commandLine, name) {
  if (!commandLine) return null;
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(`(?:^|\\s)--${escaped}(?:=|\\s+)(?:"((?:\\\\.|[^"])*)"|([^\\s]+))`, 'i');
  const m = commandLine.match(re);
  if (!m) return null;
  const raw = m[1] ?? m[2];
  return raw == null ? null : raw.replace(/\\"/g, '"');
}

/**
 * True when a Chromium process is a BROWSER ROOT rather than a renderer/GPU/utility
 * child. Chromium passes `--type=<kind>` to every child; the root has no `--type`.
 * Heuristic, not a Chromium API contract.
 * @param {{CommandLine?: string}} proc
 */
export function isBrowserRoot(proc) {
  const cmd = proc?.CommandLine || '';
  if (!cmd) return false; // no command line readable → cannot classify, don't count it as a root
  return !/\s--type(?:=|\s)/i.test(cmd);
}

/**
 * Enumerate Chromium-family processes with their command lines.
 * Resolves to [] on any failure — this is best-effort telemetry, never a hard error.
 * @param {{timeoutMs?: number}} [opts]
 * @returns {Promise<Array<{ProcessId:number,Name:string,CommandLine:string}>>}
 */
export function listChromiumProcesses({ timeoutMs = 2000 } = {}) {
  if (process.platform !== 'win32') return Promise.resolve([]);

  return new Promise((resolve) => {
    let stdout = '';
    let settled = false;
    const finish = (v) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(v);
    };

    let child;
    try {
      child = spawn(
        POWERSHELL,
        ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', CIM_SCRIPT],
        { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] },
      );
    } catch {
      resolve([]);
      return;
    }

    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* already gone */ }
      finish([]);
    }, timeoutMs);

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (c) => {
      stdout += c;
      if (stdout.length > 4_000_000) { try { child.kill(); } catch { /* noop */ } }
    });
    child.stderr.on('data', () => { /* discard */ });
    child.on('error', () => finish([]));
    child.on('close', () => {
      if (!stdout.trim()) return finish([]);
      try {
        const parsed = JSON.parse(stdout);
        finish(Array.isArray(parsed) ? parsed : [parsed]);
      } catch {
        finish([]);
      }
    });
  });
}

/**
 * Read a Chromium profile's DevToolsActivePort file. Line 1 is the live port,
 * line 2 a browser-target GUID. The file can be STALE after a crash, so a port
 * read from here is a hint that must still be probed before being believed.
 * @param {string} userDataDir
 * @returns {Promise<number|null>}
 */
export async function readDevToolsActivePort(userDataDir) {
  try {
    const text = await readFile(join(userDataDir, 'DevToolsActivePort'), 'utf8');
    const first = text.replace(/^﻿/, '').split(/\r?\n/)[0];
    const port = Number((first || '').trim());
    return Number.isInteger(port) && port >= 1 && port <= 65535 ? port : null;
  } catch {
    return null;
  }
}

/**
 * Probe one loopback port for a Chromium DevTools HTTP endpoint.
 * Host is pinned to 127.0.0.1 and the Host header set explicitly — Chrome's
 * DNS-rebinding protection rejects unexpected Host headers on /json.
 * @param {number} port
 * @param {{deadlineMs?: number}} [opts]
 * @returns {Promise<{port:number, browser:string}|null>}
 */
export function probeCdpPort(port, { deadlineMs = 300 } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (v) => { if (!settled) { settled = true; clearTimeout(deadline); resolve(v); } };

    const req = http.request(
      {
        host: '127.0.0.1',
        port,
        path: '/json/version',
        method: 'GET',
        headers: { Host: `127.0.0.1:${port}`, Accept: 'application/json' },
        agent: false,
        timeout: deadlineMs,
      },
      (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (c) => {
          body += c;
          if (body.length > 128 * 1024) req.destroy();
        });
        res.on('end', () => {
          if (res.statusCode !== 200) return finish(null);
          try {
            const v = JSON.parse(body);
            const ws = v.webSocketDebuggerUrl;
            const looksLikeCdp = typeof ws === 'string'
              && /^ws:\/\/(?:127\.0\.0\.1|localhost):\d+\/devtools\/browser\//i.test(ws)
              && typeof v.Browser === 'string'
              && /(Chrome|Chromium|Edg|Brave)/i.test(v.Browser);
            finish(looksLikeCdp ? { port, browser: v.Browser } : null);
          } catch {
            finish(null);
          }
        });
      },
    );

    const deadline = setTimeout(() => req.destroy(), deadlineMs);
    req.on('timeout', () => req.destroy());
    req.on('error', () => finish(null));
    req.end();
  });
}

/** Probe many ports with bounded concurrency. */
async function probePorts(ports, concurrency = 5) {
  const list = [...new Set(ports)].filter((p) => Number.isInteger(p) && p >= 1 && p <= 65535);
  const found = [];
  let cursor = 0;
  const worker = async () => {
    while (cursor < list.length) {
      const port = list[cursor++];
      const hit = await probeCdpPort(port);
      if (hit) found.push(hit);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, list.length) }, worker));
  return found.sort((a, b) => a.port - b.port);
}

/**
 * Best-effort census of browser instances on this machine.
 *
 * Returns `{ known, browserRoots, cdpEndpoints, asOf }`. `known:false` means
 * discovery failed or is unsupported here — callers MUST then report coverage as
 * unknown rather than claiming completeness.
 *
 * @param {{force?: boolean, budgetMs?: number}} [opts]
 */
export async function getBrowserCensus({ force = false, budgetMs = 2500 } = {}) {
  const now = Date.now();
  if (!force && _cache.value && now < _cache.expiresAt) return _cache.value;
  if (_inFlight) return _inFlight;

  _inFlight = (async () => {
    const census = { known: false, browserRoots: [], cdpEndpoints: [], asOf: new Date().toISOString(), note: null };
    try {
      const procs = await listChromiumProcesses({ timeoutMs: Math.min(budgetMs, 2000) });
      if (procs.length === 0) {
        census.note = process.platform === 'win32'
          ? 'Process enumeration returned nothing (PowerShell/CIM unavailable, timed out, or blocked).'
          : `Process enumeration is only implemented for win32; this host is ${process.platform}.`;
        return census;
      }

      census.known = true;
      const candidatePorts = [];

      for (const p of procs) {
        if (!BROWSER_EXES.has(String(p.Name || '').toLowerCase())) continue;
        if (!isBrowserRoot(p)) continue;
        const cmd = p.CommandLine || '';
        const userDataDir = getSwitchValue(cmd, 'user-data-dir');
        const rawPort = getSwitchValue(cmd, 'remote-debugging-port');
        const port = rawPort && /^\d+$/.test(rawPort) ? Number(rawPort) : null;
        // --remote-debugging-port=0 means "pick an ephemeral port": the command
        // line does not carry the real one, DevToolsActivePort does.
        if (port) candidatePorts.push(port);
        census.browserRoots.push({
          pid: Number(p.ProcessId),
          exe: p.Name,
          userDataDir: userDataDir || null,
          declaredDebuggingPort: port,
          usesDebuggingPipe: /\s--remote-debugging-pipe(?:\s|$)/i.test(cmd),
        });
      }

      const dirPorts = await Promise.all(
        census.browserRoots.filter((r) => r.userDataDir).map((r) => readDevToolsActivePort(r.userDataDir)),
      );
      for (const p of dirPorts) if (p) candidatePorts.push(p);

      for (let p = FALLBACK_PORT_LO; p <= FALLBACK_PORT_HI; p += 1) candidatePorts.push(p);

      census.cdpEndpoints = await probePorts(candidatePorts);

      // Correlate a live endpoint back to the root that declared it.
      for (const ep of census.cdpEndpoints) {
        const root = census.browserRoots.find((r) => r.declaredDebuggingPort === ep.port);
        if (root) {
          ep.pid = root.pid;
          ep.userDataDir = root.userDataDir;
        }
      }
    } catch (err) {
      census.known = false;
      census.note = `Discovery failed: ${err.message}`;
    }
    return census;
  })();

  try {
    const value = await _inFlight;
    _cache = { expiresAt: Date.now() + DISCOVERY_TTL_MS, value };
    return value;
  } finally {
    _inFlight = null;
  }
}

/**
 * How many browser extension clients are actually connected to the bridge.
 *
 * Almost every server.js process on this machine runs in RELAY mode — the first
 * process to bind ws:8765 owns the browser clients and the rest forward to it. So
 * a relay's own `bridge.browserClients` is EMPTY and using it would report
 * "0 of 2 browsers queried" while happily returning that browser's tabs. Ask the
 * primary's health endpoint instead, and fall back conservatively.
 *
 * @param {{localCount:number, tabsReturned:boolean, healthPort:number, deadlineMs?:number}} input
 * @returns {Promise<number>}
 */
export function getConnectedBrowserCount({ localCount, tabsReturned, healthPort, deadlineMs = 400 }) {
  if (localCount > 0) return Promise.resolve(localCount);

  return new Promise((resolve) => {
    let settled = false;
    // A tab listing came back, so at least one browser client served it.
    const fallback = () => resolve(tabsReturned ? 1 : 0);
    const finish = (v) => { if (!settled) { settled = true; clearTimeout(deadline); resolve(v); } };

    const req = http.request(
      { host: '127.0.0.1', port: healthPort, path: '/health', method: 'GET', agent: false, timeout: deadlineMs },
      (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (c) => { body += c; if (body.length > 256 * 1024) req.destroy(); });
        res.on('end', () => {
          try {
            const n = JSON.parse(body)?.bridge?.browserCount;
            if (Number.isInteger(n) && n >= 0) return finish(n);
          } catch { /* fall through */ }
          if (!settled) { settled = true; clearTimeout(deadline); fallback(); }
        });
      },
    );
    const deadline = setTimeout(() => req.destroy(), deadlineMs);
    req.on('timeout', () => req.destroy());
    req.on('error', () => { if (!settled) { settled = true; clearTimeout(deadline); fallback(); } });
    req.end();
  });
}

/** Reset the TTL cache. Tests only. */
export function _resetCensusCache() {
  _cache = { expiresAt: 0, value: null };
  _inFlight = null;
}

/**
 * Build the coverage + freshness envelope for a tab listing.
 *
 * The shape and the wording here are deliberate, and follow the research pass run
 * on 2026-09-10 (precedents: Elasticsearch `_shards`/`timed_out` partial results,
 * GraphQL `data` + `errors`, the DNS TC bit, HTTP 206 range declaration):
 *  - `status` is an ENUM, not a boolean. COMPLETE / PARTIAL / SCOPED / UNAVAILABLE
 *    are four materially different states that `complete: false` conflates.
 *  - `negativeEvidence` states the INFERENCE the caller is permitted to draw,
 *    because that — not "results may be incomplete" — is what the incident got
 *    wrong. It names the invalid inference explicitly.
 *  - It is ALWAYS present, including in the ordinary single-browser case, so an
 *    agent can distinguish "the tool knows it saw everything" from "the tool
 *    simply did not mention its limits".
 *  - It is a SUCCESS result, never isError: the tabs that were observed are real
 *    and useful. isError is reserved for producing no observation at all.
 *
 * @param {{tabCount:number, connectedBrowserClients:number, census:Awaited<ReturnType<typeof getBrowserCensus>>}} input
 */
export function buildCoverage({ tabCount, connectedBrowserClients, census }) {
  const asOf = new Date().toISOString();
  const observedCount = connectedBrowserClients;

  if (!census || !census.known) {
    return {
      status: 'SCOPED',
      negativeEvidence: 'UNKNOWN',
      scope: 'the single browser client currently connected to this bridge',
      summary:
        'This list covers only the one browser the bridge extension is connected to. '
        + 'This bridge could not determine whether other browsers are running on this machine, '
        + 'so a tab missing from this list is NOT evidence that the tab is not open somewhere else.',
      observedCount,
      detectedCount: null,
      observedTabCount: tabCount,
      unobserved: [],
      discoveryNote: census?.note || 'Browser discovery unavailable.',
      asOf,
    };
  }

  const detectedCount = census.browserRoots.length;
  const unobserved = [];

  // Every detected browser root beyond the ones actually connected is unobserved.
  // We cannot map a specific root to the connected client, so this is expressed as
  // a count plus per-instance detail, not as a claim about which one is which.
  if (detectedCount > observedCount) {
    for (const root of census.browserRoots) {
      const endpoint = census.cdpEndpoints.find((e) => e.pid === root.pid);
      unobserved.push({
        reasonCode: endpoint ? 'REACHABLE_VIA_CDP_NOT_CONNECTED' : 'NO_BRIDGE_EXTENSION_CONNECTION',
        reason: endpoint
          ? 'A separate browser instance with a reachable local DevTools endpoint. This bridge does not drive it.'
          : 'A separate browser instance with no bridge extension connection and no reachable DevTools endpoint found.',
        exe: root.exe,
        pid: root.pid,
        userDataDir: root.userDataDir,
        cdpEndpoint: endpoint ? `http://127.0.0.1:${endpoint.port}` : null,
        tabCount: null,
      });
    }
  }

  const partial = detectedCount > observedCount;

  return {
    status: partial ? 'PARTIAL' : 'COMPLETE',
    negativeEvidence: partial ? 'UNSAFE' : 'SAFE_WITHIN_DECLARED_SCOPE',
    scope: partial
      ? `${observedCount} of ${detectedCount} Chromium-family browser instances detected on this machine`
      : 'all Chromium-family browser instances detected on this machine',
    summary: partial
      ? `Only ${observedCount} of ${detectedCount} browser instances running on this machine were queried. `
        + 'A tab missing from this list is NOT evidence that the tab is not open — it may be open in an '
        + 'instance this bridge cannot see. Check the cdpEndpoint values under coverage.unobserved '
        + '(GET /json/list) before concluding that a page is not open.'
      : 'Every detected browser instance on this machine was queried. A tab missing from this list is '
        + 'genuinely not open in any browser this bridge could detect.',
    observedCount,
    detectedCount,
    observedTabCount: tabCount,
    unobserved,
    ...(partial && {
      unobservedNote:
        'The bridge cannot map its connected extension client back to a specific process, so exactly '
        + `${observedCount} of the ${unobserved.length} instances listed here IS the browser these tabs came from. `
        + 'The observedCount/detectedCount ratio is the reliable figure; treat the per-instance list as candidates.',
    }),
    asOf,
  };
}

/**
 * One-line, model-facing banner emitted ABOVE the JSON when a negative inference
 * would be unsafe. Placement is the point: a caveat below a long array is read
 * after the model has already anchored on the list.
 * @param {ReturnType<typeof buildCoverage>} coverage
 * @returns {string|null}
 */
export function coverageBanner(coverage) {
  if (!coverage || coverage.negativeEvidence === 'SAFE_WITHIN_DECLARED_SCOPE') return null;
  return `RESULT STATUS: ${coverage.status} — NEGATIVE EVIDENCE ${coverage.negativeEvidence}.\n${coverage.summary}`;
}

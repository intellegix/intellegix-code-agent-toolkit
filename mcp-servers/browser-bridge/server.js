#!/usr/bin/env node

/**
 * Claude Browser Bridge MCP Server v1.0
 *
 * Bidirectional bridge between Claude Code CLI and Chrome extension via WebSocket.
 * Replaces native messaging with a clean localhost WebSocket approach.
 *
 * Components (in lib/):
 *   - ContextManager: SQLite-backed conversation/context persistence
 *   - WebSocketBridge: ws server on 127.0.0.1:8765 for Chrome extension connections
 *   - BrowserBridgeServer: MCP protocol handler with tool/resource definitions
 *   - Health check HTTP server on port 8766
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import { WebSocket } from 'ws';
import { writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { basename, dirname, join, resolve as pathResolve } from 'node:path';
import { homedir } from 'node:os';
import { execFileSync, execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';

import { CONFIG, _debugLog } from './lib/config.js';
import { Validator } from './lib/validator.js';
import { log, sanitizeArgs } from './lib/logger.js';
import { RateLimiter } from './lib/rate-limiter.js';
import { ContextManager } from './lib/context-manager.js';
import { WebSocketBridge } from './lib/websocket-bridge.js';
import { startHealthServer } from './lib/health-server.js';
import { MetricsCollector } from './lib/metrics.js';

_debugLog(`imports OK — cwd=${process.cwd()} argv=${process.argv.join(' ')} ppid=${process.ppid}`);

const rateLimiter = new RateLimiter(60, 1);

/**
 * Async wrapper around child_process.execFile — does NOT block the event loop.
 * Used for long-running council/research/labs queries so multiple can run concurrently.
 */
function execFileAsync(command, args, options) {
  return new Promise((resolve, reject) => {
    execFile(command, args, options, (error, stdout, stderr) => {
      if (error) {
        error.stderr = stderr;
        reject(error);
      } else {
        resolve({ stdout, stderr });
      }
    });
  });
}

// ---------------------------------------------------------------------------
// BrowserBridgeServer — MCP protocol handler
// ---------------------------------------------------------------------------

class BrowserBridgeServer {
  constructor() {
    _debugLog('constructor entered');
    this.contextManager = new ContextManager();
    this.bridge = new WebSocketBridge();
    this.metrics = new MetricsCollector();
    this.healthServer = null;
    this.sessionId = randomUUID(); // Unique per CLI instance — used for tab group isolation
    this.projectLabel = basename(process.cwd()); // e.g. "my-project"

    this.server = new Server(
      { name: 'claude-browser-bridge', version: '1.0.0' },
      { capabilities: { tools: {}, resources: {} } },
    );

    this._registerTools();
    this._registerResources();
    this._registerBridgeEvents();
    _debugLog(`constructor OK — sessionId=${this.sessionId} project=${this.projectLabel}`);
  }

  /** Inject sessionId and projectLabel into a payload object for tab group routing */
  _withSession(payload) {
    return { ...payload, sessionId: this.sessionId, projectLabel: this.projectLabel };
  }

  // -----------------------------------------------------------------------
  // Tool definitions
  // -----------------------------------------------------------------------

  _registerTools() {
    // --- List tools ---
    this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: [
        {
          name: 'browser_execute',
          description: 'Execute a DOM action in the browser (click, type, etc.)',
          inputSchema: {
            type: 'object',
            properties: {
              action: {
                type: 'string',
                enum: ['click', 'type', 'hover', 'focus', 'blur', 'select', 'check', 'uncheck'],
                description: 'DOM action to perform',
              },
              selector: { type: 'string', description: 'CSS selector targeting the element' },
              text: { type: 'string', description: 'Text to type (for "type" action)' },
              value: { type: 'string', description: 'Value to set (for "select" action)' },
              tabId: { type: 'number', description: 'Target tab ID (uses active tab if omitted)' },
            },
            required: ['action', 'selector'],
          },
        },
        {
          name: 'browser_navigate',
          description: 'Navigate the browser to a URL',
          inputSchema: {
            type: 'object',
            properties: {
              url: { type: 'string', description: 'URL to navigate to' },
              tabId: { type: 'number', description: 'Tab to navigate (uses active tab if omitted)' },
            },
            required: ['url'],
          },
        },
        {
          name: 'browser_get_context',
          description: 'Get the current page context (URL, title, selected text, meta)',
          inputSchema: {
            type: 'object',
            properties: {
              tabId: { type: 'number', description: 'Tab to query (uses active tab if omitted)' },
            },
          },
        },
        {
          name: 'browser_screenshot',
          description: 'Capture a screenshot of the current visible tab, a specific element, or the full scrollable page',
          inputSchema: {
            type: 'object',
            properties: {
              tabId: { type: 'number', description: 'Tab to capture (uses active tab if omitted)' },
              format: { type: 'string', enum: ['png', 'jpeg'], default: 'png' },
              quality: { type: 'number', description: 'JPEG quality 0-100 (only for jpeg format)' },
              selector: { type: 'string', description: 'CSS selector — capture only this element (cropped)' },
              fullPage: { type: 'boolean', description: 'Capture the entire scrollable page (stitched strips)' },
              savePath: { type: 'string', description: 'Absolute file path to save the screenshot to disk' },
            },
          },
        },
        {
          name: 'browser_sync_context',
          description: 'Sync conversation context between CLI and browser',
          inputSchema: {
            type: 'object',
            properties: {
              conversationId: { type: 'string', description: 'Conversation ID to sync' },
              messages: {
                type: 'array',
                items: {
                  type: 'object',
                  properties: {
                    role: { type: 'string' },
                    content: { type: 'string' },
                  },
                },
                description: 'Messages to sync',
              },
            },
            required: ['conversationId', 'messages'],
          },
        },
        {
          name: 'browser_wait_for_element',
          description: 'Wait for a DOM element to appear (MutationObserver-based)',
          inputSchema: {
            type: 'object',
            properties: {
              selector: { type: 'string', description: 'CSS selector to wait for' },
              timeout: { type: 'number', description: 'Max wait time in ms (default 10000)', default: 10000 },
              tabId: { type: 'number', description: 'Target tab ID' },
            },
            required: ['selector'],
          },
        },
        {
          name: 'browser_fill_form',
          description: 'Fill multiple form fields in one call',
          inputSchema: {
            type: 'object',
            properties: {
              fields: {
                type: 'object',
                description: 'Map of selector to value pairs to fill',
                additionalProperties: { type: 'string' },
              },
              tabId: { type: 'number', description: 'Target tab ID' },
            },
            required: ['fields'],
          },
        },
        {
          name: 'browser_get_tabs',
          description: 'List all open browser tabs',
          inputSchema: { type: 'object', properties: {} },
        },
        {
          name: 'browser_switch_tab',
          description: 'Activate a specific browser tab',
          inputSchema: {
            type: 'object',
            properties: {
              tabId: { type: 'number', description: 'Tab ID to activate' },
            },
            required: ['tabId'],
          },
        },
        {
          name: 'browser_extract_data',
          description: 'Extract structured data from the page using CSS selectors',
          inputSchema: {
            type: 'object',
            properties: {
              selectors: {
                type: 'object',
                description: 'Map of field name to CSS selector for extraction',
                additionalProperties: { type: 'string' },
              },
              tabId: { type: 'number', description: 'Target tab ID' },
            },
            required: ['selectors'],
          },
        },
        {
          name: 'browser_scroll',
          description: 'Scroll the page to an element or by a specific amount',
          inputSchema: {
            type: 'object',
            properties: {
              selector: { type: 'string', description: 'CSS selector to scroll into view' },
              direction: { type: 'string', enum: ['up', 'down', 'left', 'right'] },
              amount: { type: 'number', description: 'Pixels to scroll (default 500)', default: 500 },
              tabId: { type: 'number', description: 'Target tab ID' },
            },
          },
        },
        {
          name: 'browser_close_session',
          description: 'Close all browser tabs opened by this Claude session. IMPORTANT: You MUST call this tool when you are finished with all browser automation work to prevent tab clutter. Always call this as your final browser action.',
          inputSchema: { type: 'object', properties: {} },
        },
        {
          name: 'browser_close_tabs',
          description: 'Close specific browser tabs by their IDs',
          inputSchema: {
            type: 'object',
            properties: {
              tabIds: {
                type: 'array',
                items: { type: 'number' },
                description: 'Array of tab IDs to close',
              },
            },
            required: ['tabIds'],
          },
        },
        {
          name: 'browser_select',
          description: 'Select an option from a dropdown/select element',
          inputSchema: {
            type: 'object',
            properties: {
              selector: { type: 'string', description: 'CSS selector for the select element' },
              value: { type: 'string', description: 'Option value or visible text to select' },
              tabId: { type: 'number', description: 'Target tab ID' },
            },
            required: ['selector', 'value'],
          },
        },
        {
          name: 'browser_evaluate',
          description: 'Execute arbitrary JavaScript in the page context and return the result. Useful for reading cookies, localStorage, DOM state, or running custom logic.',
          inputSchema: {
            type: 'object',
            properties: {
              expression: { type: 'string', description: 'JavaScript expression to evaluate in the page context' },
              tabId: { type: 'number', description: 'Target tab ID (uses active tab if omitted)' },
              returnByValue: { type: 'boolean', description: 'Whether to return the result by value (default true). Set false for large DOM objects.', default: true },
            },
            required: ['expression'],
          },
        },
        {
          name: 'browser_console_messages',
          description: 'Retrieve captured console log/warning/error/info messages from the page. Useful for debugging page errors and application state.',
          inputSchema: {
            type: 'object',
            properties: {
              tabId: { type: 'number', description: 'Target tab ID (uses active tab if omitted)' },
              level: { type: 'string', enum: ['all', 'log', 'warning', 'error', 'info'], description: 'Filter by log level (default: all)', default: 'all' },
              limit: { type: 'number', description: 'Max number of messages to return (default 100)', default: 100 },
              clear: { type: 'boolean', description: 'Clear the message buffer after reading', default: false },
            },
          },
        },
        {
          name: 'browser_press_key',
          description: 'Send a keyboard event to the page or a specific element. Supports named keys (Enter, Escape, Tab, ArrowUp, etc.) and modifier combinations.',
          inputSchema: {
            type: 'object',
            properties: {
              key: { type: 'string', description: 'Key name: Enter, Escape, Tab, ArrowUp, ArrowDown, ArrowLeft, ArrowRight, Backspace, Delete, Space, Home, End, PageUp, PageDown, or a single character' },
              selector: { type: 'string', description: 'CSS selector to focus before pressing key (optional)' },
              tabId: { type: 'number', description: 'Target tab ID (uses active tab if omitted)' },
              modifiers: {
                type: 'object',
                description: 'Modifier keys to hold during keypress',
                properties: {
                  ctrl: { type: 'boolean' },
                  shift: { type: 'boolean' },
                  alt: { type: 'boolean' },
                  meta: { type: 'boolean' },
                },
              },
            },
            required: ['key'],
          },
        },
        {
          name: 'browser_handle_dialog',
          description: 'Dismiss or accept a JavaScript alert/confirm/prompt dialog that is blocking automation. Must be called while the dialog is showing.',
          inputSchema: {
            type: 'object',
            properties: {
              action: { type: 'string', enum: ['accept', 'dismiss', 'send'], description: 'accept: click OK, dismiss: click Cancel, send: type text and click OK (for prompt dialogs)' },
              text: { type: 'string', description: 'Text to enter in a prompt dialog (only used with action: "send")' },
              tabId: { type: 'number', description: 'Target tab ID (uses active tab if omitted)' },
            },
            required: ['action'],
          },
        },
        {
          name: 'browser_insert_text',
          description: 'Insert text into React/framework-controlled inputs using a multi-strategy fallback chain (native setter, execCommand, clipboard paste, direct value). More reliable than browser_execute type for modern web apps.',
          inputSchema: {
            type: 'object',
            properties: {
              selector: { type: 'string', description: 'CSS selector for the target input/textarea/contenteditable element' },
              text: { type: 'string', description: 'The text to insert' },
              append: { type: 'boolean', description: 'If true, append to existing content instead of replacing (default: false)' },
              tabId: { type: 'number', description: 'Target tab ID (uses active tab if omitted)' },
            },
            required: ['selector', 'text'],
          },
        },
        {
          name: 'browser_cdp_type',
          description: 'Type text using CDP Input.dispatchKeyEvent — produces trusted keyboard events that trigger React and framework event handlers. Use this when browser_insert_text or browser_execute type fails to trigger slash commands, autocomplete, or other keyboard-driven UI.',
          inputSchema: {
            type: 'object',
            properties: {
              text: { type: 'string', description: 'The text to type character by character' },
              selector: { type: 'string', description: 'CSS selector to focus before typing (optional)' },
              delay: { type: 'number', description: 'Milliseconds between keystrokes (default: 50)' },
              tabId: { type: 'number', description: 'Target tab ID (uses active tab if omitted)' },
            },
            required: ['text'],
          },
        },
        {
          name: 'browser_wait_for_stable',
          description: 'Wait for streaming/dynamic content to stabilize. Polls an element\'s textContent and resolves when unchanged for the specified duration. Useful for waiting on LLM streaming responses, live feeds, or any content that updates incrementally.',
          inputSchema: {
            type: 'object',
            properties: {
              selector: { type: 'string', description: 'CSS selector for the element whose content to monitor' },
              stableMs: { type: 'number', description: 'Milliseconds of no change required to consider content stable (default: 8000)' },
              timeout: { type: 'number', description: 'Maximum wait time in milliseconds before returning with timedOut: true (default: 180000)' },
              pollInterval: { type: 'number', description: 'How often to check content in milliseconds (default: 2000)' },
              tabId: { type: 'number', description: 'Target tab ID (uses active tab if omitted)' },
            },
            required: ['selector'],
          },
        },
        {
          name: 'browser_activate_council',
          description: 'Activate Perplexity Model Council mode on the current page. Types /council slash command using trusted CDP keyboard events, waits for command palette, presses Enter, and verifies activation. Requires Perplexity Max subscription and /council shortcut configured in Perplexity settings.',
          inputSchema: {
            type: 'object',
            properties: {
              tabId: { type: 'number', description: 'Target tab ID (uses active tab if omitted)' },
            },
          },
        },
        {
          name: 'browser_export_council_md',
          description: "Click Perplexity's native 'Export as Markdown' from the three-dot menu on a council response page. Downloads the .md file to the browser's default downloads folder. Use after a council response has finished streaming.",
          inputSchema: {
            type: 'object',
            properties: {
              tabId: { type: 'number', description: 'Target tab ID (uses active tab if omitted)' },
            },
          },
        },
        {
          name: 'browser_add_to_space',
          description: "Add the current Perplexity thread to a Space. Opens the three-dot menu → 'Add to Space' → 'Choose Space' modal. Can list available spaces, add to an existing space by name (fuzzy match), or create a new space.",
          inputSchema: {
            type: 'object',
            properties: {
              spaceName: { type: 'string', description: 'Name of the space to add to. If omitted, returns list of available spaces.' },
              createIfMissing: { type: 'boolean', description: 'If true and spaceName not found, create a new space with that name. Default false.' },
              tabId: { type: 'number', description: 'Target tab ID (uses active session tab if omitted)' },
            },
          },
        },
        {
          name: 'council_query',
          description: 'Query 3 AI models (GPT-5.2, Claude Sonnet 4.5, Gemini 3 Pro) via Perplexity API or Playwright browser automation, then synthesize. Runs externally as subprocess — zero context tokens during execution. Results cached to ~/.claude/council-cache/. Returns synthesis only (~3-5K tokens). Default mode: browser ($0, uses Perplexity login).',
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'The question or analysis request for the multi-model council' },
              mode: { type: 'string', enum: ['api', 'direct', 'browser', 'auto'], description: 'Query mode. browser: Playwright UI automation (reliable, ~90s, $0). api: Perplexity API + Opus synthesis (fast, ~20s, requires API keys). direct: Provider APIs directly. auto: try api, fallback to browser. Default: browser.' },
              includeContext: { type: 'boolean', description: 'Include project context (git log, CLAUDE.md, MEMORY.md). Default: true.' },
              headful: { type: 'boolean', description: 'Run browser in visible mode (browser/auto modes only). Default: false.' },
              opusSynthesis: { type: 'boolean', description: 'Run Opus 4.6 re-synthesis on browser results (requires ANTHROPIC_API_KEY). Default: false.' },
              autoPlan: { type: 'boolean', description: 'Automatically enter plan mode after receiving council results to study implementation. Default: true.' },
            },
            required: ['query'],
          },
        },
        {
          name: 'research_query',
          description: "Run a deep research query on Perplexity using /research mode via Playwright browser automation. Similar to council_query but uses Perplexity's deep research mode instead of multi-model council. Returns a comprehensive, single-thread research synthesis with citations. Good fallback when council mode defaults to single-model. Cost: $0 (uses Perplexity login session). Time: ~60-120s.",
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'The research question or analysis request' },
              includeContext: { type: 'boolean', description: 'Include project context (git log, CLAUDE.md, MEMORY.md). Default: true.' },
              headful: { type: 'boolean', description: 'Run browser in visible mode. Default: false.' },
              opusSynthesis: { type: 'boolean', description: 'Run Opus 4.6 re-synthesis on results (requires ANTHROPIC_API_KEY). Default: false.' },
            },
            required: ['query'],
          },
        },
        {
          name: 'labs_query',
          description: "Run a query on Perplexity using /labs mode via Playwright browser automation. Similar to research_query but uses Perplexity's experimental labs mode with a longer 15-minute timeout. Cost: $0 (uses Perplexity login session).",
          inputSchema: {
            type: 'object',
            properties: {
              query: { type: 'string', description: 'The research question or analysis request' },
              includeContext: { type: 'boolean', description: 'Include project context (git log, CLAUDE.md, MEMORY.md). Default: true.' },
              headful: { type: 'boolean', description: 'Run browser in visible mode. Default: false.' },
              opusSynthesis: { type: 'boolean', description: 'Run Opus 4.6 re-synthesis on results (requires ANTHROPIC_API_KEY). Default: false.' },
            },
            required: ['query'],
          },
        },
        {
          name: 'council_metrics',
          description: 'Get operational metrics from the council pipeline run log. Shows degradation ratio, avg cost, per-mode breakdown, error rate. Read-only — no network calls. Use to check pipeline health.',
          inputSchema: {
            type: 'object',
            properties: {},
          },
        },
        {
          name: 'council_read',
          description: 'Read cached council results from the most recent council_query. No network calls, no subprocess — pure file read. Use after council_query to retrieve results at different detail levels.',
          inputSchema: {
            type: 'object',
            properties: {
              level: { type: 'string', enum: ['synthesis', 'full', 'gpt-5.2', 'claude-sonnet-4.5', 'gemini-3-pro'], description: 'Detail level. synthesis: Opus analysis only (~3K tokens). full: all model responses + synthesis. Or a model name for one response. Default: synthesis.' },
            },
          },
        },
        {
          name: 'automate_perplexity_task',
          description: 'Send a task to Perplexity AI via browser-bridge automation, wait for response, optionally validate, and return results. Single tool call handles the full flow: navigate → type → submit → wait → extract → validate → return. Uses the authenticated Chrome session — $0/query.',
          inputSchema: {
            type: 'object',
            properties: {
              task: { type: 'string', description: 'The query or task to send to Perplexity' },
              mode: { type: 'string', enum: ['standard', 'research', 'labs'], description: "Perplexity mode. 'standard' for quick answers, 'research' for deep research, 'labs' for experimental. Default: standard." },
              validate: { type: 'boolean', description: 'Run validation barrier on response (checks for destructive commands, syntax errors, etc.). Default: true.' },
              maxWaitMs: { type: 'number', description: 'Maximum time to wait for response in milliseconds. Default: 300000 (5 min).' },
              stableMs: { type: 'number', description: 'Milliseconds of no content change to consider response complete. Default: 8000.' },
              tabId: { type: 'number', description: 'Tab ID to use. If omitted, finds existing perplexity.ai tab or navigates one.' },
            },
            required: ['task'],
          },
        },
      ],
    }));

    // --- Call tool handler ---
    this.server.setRequestHandler(CallToolRequestSchema, async (request) => {
      const { name, arguments: args } = request.params;
      const start = Date.now();
      log.info('tool_call', { tool: name, sessionId: this.sessionId, args: sanitizeArgs(args) });

      // Rate limiting
      if (!rateLimiter.check(this.sessionId)) {
        log.warn('rate_limited', { tool: name, sessionId: this.sessionId });
        this.metrics.record(name, Date.now() - start, false, 'RATE_LIMITED');
        return {
          content: [{ type: 'text', text: JSON.stringify({ error: 'Rate limit exceeded (60 req/min). Please wait.', code: 'RATE_LIMITED' }) }],
          isError: true,
        };
      }

      try {
        const result = await this._handleToolCall(name, args || {});
        const duration = Date.now() - start;
        log.info('tool_result', { tool: name, duration, success: true });
        this.metrics.record(name, duration, true);

        // Screenshot results carry _screenshotData — return as MCP image content
        if (result && result._screenshotData) {
          const { base64, mimeType, meta } = result._screenshotData;
          const content = [
            { type: 'image', data: base64, mimeType },
          ];
          if (meta && Object.keys(meta).length > 0) {
            content.push({ type: 'text', text: JSON.stringify(meta, null, 2) });
          }
          return { content };
        }

        return {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        };
      } catch (err) {
        const duration = Date.now() - start;
        log.error('tool_error', { tool: name, duration, error: err.message, code: err.code });
        this.metrics.record(name, duration, false, err.code);
        return {
          content: [{ type: 'text', text: JSON.stringify({ error: err.message, ...(err.code && { code: err.code }) }) }],
          isError: true,
        };
      }
    });
  }

  async _handleToolCall(name, args) {
    switch (name) {
      case 'browser_execute': {
        const action = Validator.action(args.action, ['click', 'type', 'hover', 'focus', 'blur', 'select', 'check', 'uncheck']);
        const selector = Validator.selector(args.selector);
        const text = args.text !== undefined ? Validator.text(args.text, 50_000) : undefined;
        const value = args.value !== undefined ? Validator.text(args.value, 1000) : undefined;
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast({
          type: 'action_request',
          payload: this._withSession({ action, selector, text, value, tabId }),
        });
      }

      case 'browser_navigate': {
        const url = Validator.url(args.url);
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast({
          type: 'navigate',
          payload: this._withSession({ url, tabId }),
        });
      }

      case 'browser_get_context': {
        const tabId = Validator.tabId(args.tabId);
        try {
          const live = await this.bridge.broadcast(
            { type: 'get_context', payload: this._withSession({ tabId }) },
            CONFIG.timeouts.quick,
          );
          return live;
        } catch {
          const snapshot = this.contextManager.getLatestSnapshot();
          if (snapshot) {
            return { source: 'cache', ...JSON.parse(snapshot.content) };
          }
          throw new Error('No browser context available (extension not connected and no cache)');
        }
      }

      case 'browser_screenshot': {
        const format = Validator.action(args.format || 'png', ['png', 'jpeg']);
        const mimeType = format === 'jpeg' ? 'image/jpeg' : 'image/png';
        const quality = args.quality !== undefined ? Validator.timeout(args.quality, 1, 100, 80) : undefined;
        const fullPage = Validator.boolean(args.fullPage);
        const selector = args.selector ? Validator.selector(args.selector) : undefined;
        const savePath = args.savePath ? Validator.text(args.savePath, 500) : undefined;
        const tabId = Validator.tabId(args.tabId);
        let broadcastType = 'screenshot';
        let timeout = CONFIG.requestTimeout;

        if (fullPage) {
          broadcastType = 'screenshot_full_page';
          timeout = CONFIG.timeouts.fullPage;
        } else if (selector) {
          broadcastType = 'screenshot_element';
          timeout = CONFIG.timeouts.heavy;
        }

        const result = await this.bridge.broadcast(
          {
            type: broadcastType,
            payload: this._withSession({ tabId, format, quality, selector }),
          },
          timeout,
        );

        let base64 = result.screenshot || '';
        if (base64.startsWith('data:')) {
          base64 = base64.split(',')[1];
        }

        const meta = {};
        if (result.width) meta.width = result.width;
        if (result.height) meta.height = result.height;
        if (result.warning) meta.warning = result.warning;

        if (savePath) {
          try {
            const absPath = pathResolve(savePath);
            mkdirSync(dirname(absPath), { recursive: true });
            writeFileSync(absPath, Buffer.from(base64, 'base64'));
            meta.savedTo = absPath;
          } catch (saveErr) {
            meta.saveError = saveErr.message;
          }
        }

        return { _screenshotData: { base64, mimeType, meta } };
      }

      case 'browser_sync_context': {
        Validator.text(args.conversationId, 200);
        Validator.array(args.messages, 'messages');
        let convId = args.conversationId;
        const conv = this.contextManager.getConversation(convId);
        if (!conv) {
          convId = this.contextManager.createConversation(`sync-${convId}`);
        }
        for (const msg of args.messages) {
          this.contextManager.addMessage(convId, msg.role, msg.content);
        }
        try {
          await this.bridge.broadcast({
            type: 'context_sync',
            payload: this._withSession({ conversationId: convId, messages: args.messages }),
          });
        } catch { /* browser not connected, still saved locally */ }
        return { conversationId: convId, messageCount: args.messages.length, synced: true };
      }

      case 'browser_wait_for_element': {
        const selector = Validator.selector(args.selector);
        const timeout = Validator.timeout(args.timeout, 100, 60_000, 10_000);
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast(
          {
            type: 'action_request',
            payload: this._withSession({ action: 'waitForElement', selector, timeout, tabId }),
          },
          timeout + 2000,
        );
      }

      case 'browser_fill_form': {
        const fields = Validator.object(args.fields, 'fields');
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast({
          type: 'action_request',
          payload: this._withSession({ action: 'fillForm', fields, tabId }),
        });
      }

      case 'browser_get_tabs':
        return this.bridge.broadcast({ type: 'get_tabs', payload: this._withSession({}) });

      case 'browser_switch_tab': {
        const tabId = Validator.tabId(args.tabId);
        if (!tabId) throw new Error('tabId is required');
        return this.bridge.broadcast({
          type: 'switch_tab',
          payload: this._withSession({ tabId }),
        });
      }

      case 'browser_extract_data': {
        const selectors = Validator.object(args.selectors, 'selectors');
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast({
          type: 'action_request',
          payload: this._withSession({ action: 'extractData', selectors, tabId }),
        });
      }

      case 'browser_scroll': {
        const selector = args.selector ? Validator.selector(args.selector) : undefined;
        const direction = args.direction ? Validator.action(args.direction, ['up', 'down', 'left', 'right']) : undefined;
        const amount = Validator.timeout(args.amount, 1, 10_000, 500);
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast({
          type: 'action_request',
          payload: this._withSession({
            action: 'scroll',
            selector,
            direction,
            amount,
            tabId,
          }),
        });
      }

      case 'browser_close_session':
        return this.bridge.broadcast(
          { type: 'session_cleanup', payload: this._withSession({}) },
          CONFIG.timeouts.quick,
        );

      case 'browser_close_tabs': {
        const tabIds = Validator.array(args.tabIds, 'tabIds');
        tabIds.forEach((id, i) => { if (typeof id !== 'number' || id <= 0) throw new Error(`tabIds[${i}] must be a positive number`); });
        return this.bridge.broadcast(
          { type: 'close_tabs', payload: this._withSession({ tabIds }) },
          CONFIG.timeouts.quick,
        );
      }

      case 'browser_select': {
        const selector = Validator.selector(args.selector);
        const tabId = Validator.tabId(args.tabId);
        Validator.text(args.value, 1000);
        return this.bridge.broadcast({
          type: 'action_request',
          payload: this._withSession({ action: 'selectOption', selector, value: args.value, tabId }),
        });
      }

      case 'browser_evaluate': {
        const expression = Validator.expression(args.expression);
        const tabId = Validator.tabId(args.tabId);
        const returnByValue = Validator.boolean(args.returnByValue, true);
        return this.bridge.broadcast(
          {
            type: 'evaluate',
            payload: this._withSession({ expression, tabId, returnByValue }),
          },
          CONFIG.timeouts.heavy,
        );
      }

      case 'browser_console_messages': {
        const tabId = Validator.tabId(args.tabId);
        const clear = Validator.boolean(args.clear);
        const level = args.level ? Validator.action(args.level, ['all', 'log', 'warning', 'error', 'info']) : 'all';
        const limit = Validator.timeout(args.limit, 1, 500, 100);
        const result = await this.bridge.broadcast({
          type: 'get_console_messages',
          payload: this._withSession({ level, limit, tabId }),
        });
        if (clear && result && result.success) {
          try {
            await this.bridge.broadcast({
              type: 'evaluate',
              payload: this._withSession({
                expression: 'window.__claudeConsoleMessages = []; true;',
                tabId,
              }),
            }, CONFIG.timeouts.quick);
          } catch { /* non-fatal */ }
        }
        return result;
      }

      case 'browser_press_key': {
        const key = Validator.key(args.key);
        const selector = args.selector ? Validator.selector(args.selector) : undefined;
        const modifiers = args.modifiers ? Validator.object(args.modifiers, 'modifiers') : undefined;
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast({
          type: 'action_request',
          payload: this._withSession({ action: 'pressKey', key, selector, modifiers, tabId }),
        });
      }

      case 'browser_handle_dialog': {
        const action = Validator.action(args.action, ['accept', 'dismiss', 'send']);
        const text = args.text !== undefined ? Validator.text(args.text, 10_000) : undefined;
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast(
          {
            type: 'handle_dialog',
            payload: this._withSession({ action, text, tabId }),
          },
          CONFIG.timeouts.interactive,
        );
      }

      case 'browser_insert_text': {
        const selector = Validator.selector(args.selector);
        const text = Validator.text(args.text);
        const append = Validator.boolean(args.append);
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast(
          {
            type: 'action_request',
            payload: this._withSession({ action: 'insertText', selector, text, append, tabId }),
          },
          CONFIG.timeouts.heavy,
        );
      }

      case 'browser_cdp_type': {
        const text = Validator.text(args.text, 10_000);
        const selector = args.selector ? Validator.selector(args.selector) : undefined;
        const delay = Validator.timeout(args.delay, 0, 1000, 50);
        const tabId = Validator.tabId(args.tabId);
        const timeout = Math.max(CONFIG.timeouts.councilUi, text.length * (delay + 100));
        return this.bridge.broadcast(
          {
            type: 'cdp_type',
            payload: this._withSession({ text, selector, delay, tabId }),
          },
          timeout,
        );
      }

      case 'browser_wait_for_stable': {
        const selector = Validator.selector(args.selector);
        const actionTimeout = Validator.timeout(args.timeout, 1000, 300_000, 180_000);
        const stableMs = Validator.timeout(args.stableMs, 100, 60_000, 8_000);
        const pollInterval = Validator.timeout(args.pollInterval, 100, 30_000, 2_000);
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast(
          {
            type: 'action_request',
            payload: this._withSession({ action: 'waitForStable', selector, stableMs, timeout: actionTimeout, pollInterval, tabId }),
          },
          actionTimeout + 5000,
        );
      }

      case 'browser_activate_council': {
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast(
          { type: 'activate_council', payload: this._withSession({ tabId }) },
          CONFIG.timeouts.councilUi,
        );
      }

      case 'browser_export_council_md': {
        const tabId = Validator.tabId(args.tabId);
        return this.bridge.broadcast(
          { type: 'export_council_md', payload: this._withSession({ tabId }) },
          CONFIG.timeouts.councilUi,
        );
      }

      case 'browser_add_to_space': {
        const tabId = Validator.tabId(args.tabId);
        const spaceName = args.spaceName ? Validator.text(args.spaceName, 200) : undefined;
        const createIfMissing = Validator.boolean(args.createIfMissing);
        return this.bridge.broadcast(
          { type: 'add_to_space', payload: this._withSession({ tabId, spaceName, createIfMissing }) },
          CONFIG.timeouts.space,
        );
      }

      case 'labs_query':
      case 'research_query':
      case 'council_query': {
        const query = Validator.text(args.query, 10_000);
        const isResearch = name === 'research_query';
        const isLabs = name === 'labs_query';
        const validModes = ['api', 'direct', 'browser', 'auto'];
        // Default to browser mode (no API keys needed). research_query/labs_query always use browser.
        const mode = (isResearch || isLabs) ? 'browser' : (validModes.includes(args.mode) ? args.mode : 'browser');
        const includeContext = Validator.boolean(args.includeContext, true);
        const scriptDir = join(homedir(), '.claude', 'council-automation');
        const cacheDir = join(homedir(), '.claude', 'council-cache');
        // Force UTF-8 stdout in Python subprocesses — prevents TextIOWrapper buffering
        // from eating output when Node pipes use non-UTF-8 encoding (Windows cp1252).
        const pythonEnv = { ...process.env, PYTHONIOENCODING: 'utf-8' };

        // Ensure cache dir exists
        mkdirSync(cacheDir, { recursive: true });

        // Generate session context if requested
        if (includeContext) {
          try {
            const ctxOut = execFileSync('python', [join(scriptDir, 'session_context.py'), process.cwd()], {
              timeout: CONFIG.timeouts.councilExec,
              encoding: 'utf-8',
              env: pythonEnv,
            });
            writeFileSync(join(cacheDir, 'session_context.md'), ctxOut, 'utf-8');
          } catch (ctxErr) {
            log.warn('council_context_failed', { error: ctxErr.message });
            // Non-fatal — query will proceed without context
          }
        }

        const scriptArgs = [join(scriptDir, 'council_query.py'), '--mode', mode];
        if (includeContext && existsSync(join(cacheDir, 'session_context.md'))) {
          scriptArgs.push('--context-file', join(cacheDir, 'session_context.md'));
        }
        const headful = Validator.boolean(args.headful);
        const opusSynthesis = Validator.boolean(args.opusSynthesis);
        if (headful) scriptArgs.push('--headful');
        if (opusSynthesis) scriptArgs.push('--opus-synthesis');
        if (isResearch) scriptArgs.push('--perplexity-mode', 'research');
        if (isLabs) scriptArgs.push('--perplexity-mode', 'labs');
        scriptArgs.push(query);

        // Browser/auto modes need longer timeout; research/labs modes need even more
        const timeout = isLabs ? CONFIG.timeouts.councilLabs : isResearch ? CONFIG.timeouts.councilResearch : (mode === 'browser' || mode === 'auto') ? CONFIG.timeouts.councilBrowser : CONFIG.timeouts.councilApi;
        let raw = await execFileAsync('python', scriptArgs, {
          timeout,
          encoding: 'utf-8',
          env: pythonEnv,
          cwd: scriptDir,
        });
        let result = raw.stdout;
        // Check for browser busy error (concurrent session holding the profile lock)
        if (result.includes('BROWSER_BUSY')) {
          return {
            error: 'Another browser council/research session is active. Wait ~2 min or use --mode api.',
            code: 'BROWSER_BUSY',
          };
        }
        // Retry once if stdout is empty (Bug 3: empty synthesis)
        if (!result || !result.trim()) {
          log.warn('research_empty_result', { stderr: (raw.stderr || '').substring(0, 500) });
          await new Promise(r => setTimeout(r, 3000));
          raw = await execFileAsync('python', scriptArgs, {
            timeout,
            encoding: 'utf-8',
            env: pythonEnv,
            cwd: scriptDir,
          });
          result = raw.stdout;
          if (!result || !result.trim()) {
            log.warn('research_empty_result_retry', { stderr: (raw.stderr || '').substring(0, 500) });
          }
        }
        return { synthesis: result };
      }

      case 'council_metrics': {
        const scriptDir = join(homedir(), '.claude', 'council-automation');
        const pyEnv = { ...process.env, PYTHONIOENCODING: 'utf-8' };
        const result = execFileSync('python', [
          join(scriptDir, 'council_metrics.py'), '--json',
        ], {
          timeout: CONFIG.timeouts.councilExec,
          encoding: 'utf-8',
          env: pyEnv,
          cwd: scriptDir,
        });
        return JSON.parse(result);
      }

      case 'council_read': {
        const level = args.level || 'synthesis';
        const scriptDir = join(homedir(), '.claude', 'council-automation');
        const pyEnv = { ...process.env, PYTHONIOENCODING: 'utf-8' };

        const result = execFileSync('python', [
          join(scriptDir, 'council_query.py'),
          level === 'full' ? '--read-full' : level === 'synthesis' ? '--read' : '--read-model',
          ...(level !== 'full' && level !== 'synthesis' ? [level] : []),
        ], {
          timeout: CONFIG.timeouts.councilExec,
          encoding: 'utf-8',
          env: pyEnv,
          cwd: scriptDir,
        });
        return JSON.parse(result);
      }

      case 'automate_perplexity_task': {
        const task = Validator.text(args.task, 10_000);
        const mode = args.mode ? Validator.action(args.mode, ['standard', 'research', 'labs']) : 'standard';
        const shouldValidate = Validator.boolean(args.validate, true);
        const maxWaitMs = Validator.timeout(args.maxWaitMs, 10_000, 900_000, 300_000);
        const stableMs = Validator.timeout(args.stableMs, 2_000, 60_000, 8_000);
        let tabId = Validator.tabId(args.tabId);
        const automationStart = Date.now();

        // Selectors (mapped from DOM discovery)
        const SEL = {
          input: '[role="textbox"][contenteditable="true"]',
          submit: 'button[aria-label="Submit"]',
          prose: '.prose',
        };

        // Step A: Find or navigate to perplexity.ai tab
        if (!tabId) {
          const tabsResult = await this.bridge.broadcast(
            { type: 'get_tabs', payload: this._withSession({}) },
            CONFIG.timeouts.quick,
          );
          const tabs = tabsResult.tabs || tabsResult;
          const pplxTab = (Array.isArray(tabs) ? tabs : []).find(
            (t) => t.url && t.url.includes('perplexity.ai') && !t.url.includes('/search/'),
          );
          if (pplxTab) {
            tabId = pplxTab.id;
            // Switch to the tab
            await this.bridge.broadcast(
              { type: 'switch_tab', payload: this._withSession({ tabId }) },
              CONFIG.timeouts.quick,
            );
          } else {
            // Navigate a new or existing tab to perplexity.ai
            const navResult = await this.bridge.broadcast(
              { type: 'navigate', payload: this._withSession({ url: 'https://www.perplexity.ai/' }) },
              CONFIG.requestTimeout,
            );
            tabId = navResult.tabId || tabId;
            // Wait for input to be ready
            await this.bridge.broadcast(
              {
                type: 'action_request',
                payload: this._withSession({ action: 'waitForElement', selector: SEL.input, timeout: 10_000, tabId }),
              },
              12_000,
            );
          }
        }

        // Step B: If mode is research or labs, type the slash command first
        if (mode === 'research' || mode === 'labs') {
          // Click the input to focus it
          await this.bridge.broadcast(
            { type: 'action_request', payload: this._withSession({ action: 'click', selector: SEL.input, tabId }) },
            CONFIG.timeouts.quick,
          );
          // Type the slash command
          const slashCmd = mode === 'research' ? '/research ' : '/labs ';
          await this.bridge.broadcast(
            { type: 'cdp_type', payload: this._withSession({ text: slashCmd, delay: 50, tabId }) },
            CONFIG.timeouts.councilUi,
          );
          // Wait for the command palette to appear and press Enter to select
          await new Promise((r) => setTimeout(r, 1500));
          await this.bridge.broadcast(
            { type: 'action_request', payload: this._withSession({ action: 'pressKey', key: 'Enter', tabId }) },
            CONFIG.timeouts.quick,
          );
          await new Promise((r) => setTimeout(r, 500));
        }

        // Step C: Click input and type the task
        await this.bridge.broadcast(
          { type: 'action_request', payload: this._withSession({ action: 'click', selector: SEL.input, tabId }) },
          CONFIG.timeouts.quick,
        );
        const typingTimeout = Math.max(CONFIG.timeouts.councilUi, task.length * 150);
        await this.bridge.broadcast(
          { type: 'cdp_type', payload: this._withSession({ text: task, delay: 30, tabId }) },
          typingTimeout,
        );

        // Step D: Click Submit button
        await new Promise((r) => setTimeout(r, 300));
        await this.bridge.broadcast(
          { type: 'action_request', payload: this._withSession({ action: 'click', selector: SEL.submit, tabId }) },
          CONFIG.timeouts.quick,
        );

        // Step E: Wait for response to stabilize
        // First, wait for the page to navigate to a thread URL and prose to appear
        await new Promise((r) => setTimeout(r, 3000));
        // Re-fetch tabs to find the new thread tab (page may have navigated)
        let responseTabId = tabId;
        try {
          const updatedTabs = await this.bridge.broadcast(
            { type: 'get_tabs', payload: this._withSession({}) },
            CONFIG.timeouts.quick,
          );
          const allTabs = updatedTabs.tabs || updatedTabs;
          const threadTab = (Array.isArray(allTabs) ? allTabs : []).find(
            (t) => t.url && t.url.includes('perplexity.ai/search/'),
          );
          if (threadTab) responseTabId = threadTab.id;
        } catch { /* Use original tabId */ }

        // Wait for prose to stabilize
        const waitResult = await this.bridge.broadcast(
          {
            type: 'action_request',
            payload: this._withSession({
              action: 'waitForStable',
              selector: SEL.prose,
              stableMs,
              timeout: maxWaitMs,
              pollInterval: 2000,
              tabId: responseTabId,
            }),
          },
          maxWaitMs + 5000,
        );

        // Step F: Extract response text
        const extractResult = await this.bridge.broadcast(
          {
            type: 'evaluate',
            payload: this._withSession({
              expression: `(() => {
                const prose = document.querySelector('.prose');
                return {
                  text: prose ? prose.textContent : '',
                  html: prose ? prose.innerHTML.substring(0, 50000) : '',
                  url: window.location.href
                };
              })()`,
              tabId: responseTabId,
            }),
          },
          CONFIG.timeouts.heavy,
        );

        const responseText = extractResult?.result?.text || '';
        const responseUrl = extractResult?.result?.url || '';
        const elapsed = Date.now() - automationStart;

        // Step G: Optionally validate
        let validated = false;
        let violations = [];
        let sanitizedResponse = responseText;

        if (shouldValidate && responseText.length > 0) {
          try {
            const scriptDir = join(homedir(), '.claude', 'council-automation');
            const pythonEnv = { ...process.env, PYTHONIOENCODING: 'utf-8' };
            const validatorInput = JSON.stringify({ response: responseText, task });
            const validatorRaw = await execFileAsync('python', [
              join(scriptDir, 'response_validator.py'),
              '--json', validatorInput,
            ], {
              timeout: CONFIG.timeouts.councilExec,
              encoding: 'utf-8',
              env: pythonEnv,
              cwd: scriptDir,
            });
            const validatorResult = JSON.parse(validatorRaw.stdout);
            validated = validatorResult.valid;
            violations = validatorResult.violations || [];
            sanitizedResponse = validatorResult.sanitized_response || responseText;
          } catch (valErr) {
            log.warn('validator_failed', { error: valErr.message });
            // Non-fatal — return unvalidated response
            validated = false;
            violations = [{ rule: 'validator_error', severity: 'warn', message: valErr.message }];
          }
        } else if (responseText.length === 0) {
          validated = false;
          violations = [{ rule: 'empty_response', severity: 'block', message: 'Perplexity returned empty response' }];
        } else {
          validated = true; // Validation skipped
        }

        // Step H: Return results
        return {
          response: sanitizedResponse,
          validated,
          violations,
          mode,
          url: responseUrl,
          elapsed_ms: elapsed,
          timedOut: waitResult?.timedOut || false,
        };
      }

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  }

  // -----------------------------------------------------------------------
  // Resource definitions
  // -----------------------------------------------------------------------

  _registerResources() {
    this.server.setRequestHandler(ListResourcesRequestSchema, async () => ({
      resources: [
        {
          uri: 'browser://current-page',
          name: 'Current Page Context',
          description: 'Latest cached browser page context (URL, title, content)',
          mimeType: 'application/json',
        },
        {
          uri: 'browser://connection-status',
          name: 'Bridge Connection Status',
          description: 'WebSocket bridge and extension connection health',
          mimeType: 'application/json',
        },
      ],
    }));

    this.server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
      const { uri } = request.params;

      if (uri === 'browser://current-page') {
        const ctx = this.bridge.cachedPageContext;
        const snapshot = this.contextManager.getLatestSnapshot();
        const data = ctx || (snapshot ? JSON.parse(snapshot.content) : { message: 'No page context available' });
        return {
          contents: [{ uri, mimeType: 'application/json', text: JSON.stringify(data, null, 2) }],
        };
      }

      if (uri === 'browser://connection-status') {
        return {
          contents: [{
            uri,
            mimeType: 'application/json',
            text: JSON.stringify(this.bridge.getStatus(), null, 2),
          }],
        };
      }

      throw new Error(`Unknown resource: ${uri}`);
    });
  }

  // -----------------------------------------------------------------------
  // Bridge event handlers
  // -----------------------------------------------------------------------

  _registerBridgeEvents() {
    this.bridge.on('pageContextUpdate', (payload) => {
      try {
        this.contextManager.saveSnapshot(payload);
      } catch (err) {
        console.error('[BrowserBridge] Failed to save context snapshot:', err.message);
      }
    });

    this.bridge.on('clientConnected', (clientId) => {
      console.error(`[BrowserBridge] Extension connected: ${clientId}`);
    });

    this.bridge.on('clientDisconnected', (clientId) => {
      console.error(`[BrowserBridge] Extension disconnected: ${clientId}`);
    });

    // Relay lifecycle — persist session state for auto-recovery
    this.bridge.on('relayConnected', ({ sessionId, pid, projectPath, projectLabel }) => {
      if (!sessionId || !projectPath) return;
      try {
        this.contextManager.saveRelaySession(sessionId, projectLabel || '', projectPath, pid);
        _debugLog(`relay session saved: ${sessionId.slice(0, 8)} path=${projectPath}`);
      } catch (err) {
        console.error('[BrowserBridge] Failed to save relay session:', err.message);
      }
    });

    this.bridge.on('relayDisconnected', ({ sessionId }) => {
      if (!sessionId) return;
      try {
        this.contextManager.markRelayOrphaned(sessionId);
        _debugLog(`relay session orphaned: ${sessionId.slice(0, 8)}`);
        console.error(`[BrowserBridge] Relay session ${sessionId.slice(0, 8)} marked orphaned for recovery`);
      } catch (err) {
        console.error('[BrowserBridge] Failed to mark relay orphaned:', err.message);
      }
    });
  }

  // -----------------------------------------------------------------------
  // Relay mode — connect to existing WS server as client
  // -----------------------------------------------------------------------

  _connectAsRelay() {
    _debugLog(`_connectAsRelay() entered — target ws://${CONFIG.wsHost}:${CONFIG.wsPort}`);
    return new Promise((resolve, reject) => {
      const wsUrl = `ws://${CONFIG.wsHost}:${CONFIG.wsPort}`;
      this._relayWs = null;
      this._relayPending = new Map();
      this._relayReconnectTimer = null;
      this._relayConnected = false;

      const connect = (isInitial = false) => {
        _debugLog(`_connectAsRelay() connect() isInitial=${isInitial}`);
        const ws = new WebSocket(wsUrl, { maxPayload: CONFIG.maxMessageSize });

        ws.on('open', () => {
          this._relayWs = ws;
          this._relayConnected = true;
          _debugLog('_connectAsRelay() WS open — sending relay_init');
          console.error('[BrowserBridge] Relay connected to primary WS server');

          // Check for orphaned session to recover (same project directory)
          try {
            const orphaned = this.contextManager.findOrphanedSession(process.cwd());
            if (orphaned) {
              this.sessionId = orphaned.session_id;
              this.contextManager.recoverRelaySession(orphaned.session_id, process.pid);
              _debugLog(`recovered orphaned session: ${orphaned.session_id.slice(0, 8)}`);
              console.error(`[BrowserBridge] Recovered orphaned session ${orphaned.session_id.slice(0, 8)} — tab group preserved`);
            }
          } catch (err) {
            _debugLog(`session recovery check failed: ${err.message}`);
            // Non-fatal — proceed with new session
          }

          // Identify as relay, not browser extension — include sessionId + project info for recovery
          ws.send(JSON.stringify({
            type: 'relay_init',
            payload: { pid: process.pid, role: 'stdio-relay', sessionId: this.sessionId, projectPath: process.cwd(), projectLabel: this.projectLabel },
          }));

          if (isInitial) resolve();
        });

        ws.on('message', (data) => {
          try {
            const msg = JSON.parse(data.toString());

            // Handle responses to our forwarded requests
            if (msg.requestId && this._relayPending.has(msg.requestId)) {
              const pending = this._relayPending.get(msg.requestId);
              this._relayPending.delete(msg.requestId);
              clearTimeout(pending.timer);
              if (msg.error) {
                const err = new Error(msg.error);
                if (msg.code) err.code = msg.code;
                pending.reject(err);
              } else {
                pending.resolve(msg.result);
              }
              return;
            }

            // Ignore connection_init from primary server
            if (msg.type === 'connection_init') return;
          } catch (e) {
            console.error('[BrowserBridge] Relay parse error:', e.message);
          }
        });

        ws.on('close', () => {
          this._relayConnected = false;
          this._relayWs = null;
          console.error('[BrowserBridge] Relay disconnected — reconnecting in 3s');
          // Reject all pending requests
          for (const [id, pending] of this._relayPending) {
            clearTimeout(pending.timer);
            pending.reject(new Error('Relay connection lost'));
          }
          this._relayPending.clear();
          this._relayReconnectTimer = setTimeout(() => connect(false), CONFIG.relayReconnectDelay);
        });

        ws.on('error', (err) => {
          _debugLog(`_connectAsRelay() WS error: ${err.code || err.message} isInitial=${isInitial} connected=${this._relayConnected}`);
          console.error('[BrowserBridge] Relay WS error:', err.message);
          if (isInitial && !this._relayConnected) {
            reject(err);
          }
        });
      };

      connect(true);

      // --- Relay death detection ---
      // Strategy 1: stdin close (primary — OS closes pipe when parent dies)
      // NOTE: Do NOT call process.stdin.resume() here! StdioServerTransport
      // reads MCP messages from stdin. Calling resume() before the transport
      // is connected puts stdin into flowing mode, discarding the MCP
      // initialize handshake. The 'end'/'close' listeners work because
      // StdioServerTransport resumes stdin when it starts reading.
      const stdinCleanup = () => {
        _debugLog('relay stdin closed — parent exited');
        console.error('[BrowserBridge] Relay stdin closed — parent exited, shutting down');
        clearTimeout(this._relayReconnectTimer);
        if (this._ppidCheckTimer) clearInterval(this._ppidCheckTimer);
        if (this._relayWs) this._relayWs.close(1000, 'Parent process exited');
        process.exit(0);
      };
      process.stdin.on('end', stdinCleanup);
      process.stdin.on('close', stdinCleanup);

      // Strategy 2: PPID polling (backup — catches edge cases on Windows/Git Bash)
      const parentPid = process.ppid;
      this._ppidCheckTimer = setInterval(() => {
        try {
          process.kill(parentPid, 0); // signal 0 = existence check, no actual signal sent
        } catch (e) {
          if (e.code === 'ESRCH') {
            console.error(`[BrowserBridge] Relay parent PID ${parentPid} gone — shutting down`);
            clearInterval(this._ppidCheckTimer);
            clearTimeout(this._relayReconnectTimer);
            if (this._relayWs) this._relayWs.close(1000, 'Parent process exited');
            process.exit(0);
          }
        }
      }, CONFIG.ppidPollInterval);

      // Override bridge.broadcast to relay through the primary server
      const originalBroadcast = this.bridge.broadcast.bind(this.bridge);
      this.bridge.broadcast = (message, timeout = CONFIG.requestTimeout) => {
        if (!this._relayWs || this._relayWs.readyState !== WebSocket.OPEN) {
          return Promise.reject(new Error('Relay not connected to primary server'));
        }

        const requestId = randomUUID();

        return new Promise((resolve, reject) => {
          const timer = setTimeout(() => {
            this._relayPending.delete(requestId);
            const err = new Error(`Relay request timed out after ${timeout}ms`);
            err.code = 'TIMEOUT';
            reject(err);
          }, timeout);

          this._relayPending.set(requestId, { resolve, reject, timer });

          this._relayWs.send(JSON.stringify({
            type: 'relay_forward',
            requestId,
            payload: message,
            timeout,
          }));
        });
      };

      // Override bridge.getStatus for relay mode
      this.bridge.getStatus = () => ({
        connected: this._relayConnected,
        mode: 'relay',
        relayTarget: wsUrl,
        clientCount: this._relayConnected ? 1 : 0,
        clients: [],
        cachedPageContext: this.bridge.cachedPageContext,
      });
    });
  }

  // -----------------------------------------------------------------------
  // Lifecycle
  // -----------------------------------------------------------------------

  async start() {
    const isStandalone = process.argv.includes('--standalone');
    _debugLog(`start() entered — standalone=${isStandalone}`);

    try {
      await this.bridge.start();
      this.healthServer = await startHealthServer(this.bridge, rateLimiter, this.metrics);
      _debugLog('start() primary mode — WS+Health bound OK');
      console.error('[BrowserBridge] Primary mode — owns WebSocket + Health servers');
    } catch (err) {
      if (err.code === 'EADDRINUSE' && !isStandalone) {
        _debugLog(`start() EADDRINUSE — switching to relay mode`);
        console.error('[BrowserBridge] Ports in use — connecting as relay client');
        await this._connectAsRelay();
        _debugLog('start() relay connected OK');
      } else {
        _debugLog(`start() FATAL bind error: ${err.code || err.message}`);
        throw err;
      }
    }

    if (!isStandalone) {
      _debugLog('start() connecting MCP stdio transport...');
      const transport = new StdioServerTransport();
      await this.server.connect(transport);
      _debugLog('start() MCP stdio connected — server ready');
      console.error('[BrowserBridge] MCP server started (stdio transport)');
    } else {
      _debugLog('start() standalone mode — no stdio');
      console.error('[BrowserBridge] Running in standalone mode (WebSocket + Health only)');
    }
  }

  async shutdown() {
    console.error('[BrowserBridge] Shutting down...');

    // Notify extension to close this session's tabs
    try {
      await this.bridge.broadcast(
        { type: 'session_cleanup', payload: this._withSession({}) },
        CONFIG.timeouts.quick,
      );
    } catch { /* extension may not be connected */ }

    // Clean up relay if in relay mode
    if (this._ppidCheckTimer) clearInterval(this._ppidCheckTimer);
    if (this._relayReconnectTimer) clearTimeout(this._relayReconnectTimer);
    if (this._relayWs) {
      this._relayWs.close(1000, 'Relay shutting down');
      this._relayWs = null;
    }
    if (this._relayPending) {
      for (const [, pending] of this._relayPending) {
        clearTimeout(pending.timer);
        pending.reject(new Error('Server shutting down'));
      }
      this._relayPending.clear();
    }

    this.bridge.stop();
    this.contextManager.destroy();
    this.metrics.destroy();

    if (this.healthServer) {
      await new Promise((resolve) => this.healthServer.close(resolve));
    }

    await this.server.close();
    console.error('[BrowserBridge] Shutdown complete');
  }
}

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

_debugLog('creating BrowserBridgeServer instance...');
const serverInstance = new BrowserBridgeServer();

async function main() {
  await serverInstance.start();
}

// Graceful shutdown
function handleShutdown(signal) {
  _debugLog(`shutdown signal: ${signal}`);
  try { console.error(`\n[BrowserBridge] Received ${signal}, shutting down...`); } catch (_) { /* stderr broken */ }
  serverInstance.shutdown().then(() => process.exit(0)).catch(() => process.exit(1));
}

process.on('SIGINT', () => handleShutdown('SIGINT'));
process.on('SIGTERM', () => handleShutdown('SIGTERM'));
process.on('uncaughtException', (err) => {
  _debugLog(`UNCAUGHT: ${err.stack || err.message}`);
  // EPIPE means parent disconnected — don't write to broken stderr (causes infinite loop)
  if (err.code === 'EPIPE' || err.code === 'ERR_STREAM_DESTROYED') {
    _debugLog('EPIPE detected — parent disconnected, exiting cleanly');
    process.exit(0);
  }
  try { console.error('[BrowserBridge] Uncaught exception:', err); } catch (_) { /* stderr broken */ }
});
process.on('unhandledRejection', (err) => {
  _debugLog(`UNHANDLED_REJECTION: ${err?.stack || err?.message || err}`);
  if (err?.code === 'EPIPE' || err?.code === 'ERR_STREAM_DESTROYED') {
    _debugLog('EPIPE rejection — parent disconnected, exiting cleanly');
    process.exit(0);
  }
  try { console.error('[BrowserBridge] Unhandled rejection:', err); } catch (_) { /* stderr broken */ }
});

main().catch((err) => {
  _debugLog(`main() FATAL: ${err.stack || err.message}`);
  console.error('[BrowserBridge] Fatal error:', err);
  process.exit(1);
});

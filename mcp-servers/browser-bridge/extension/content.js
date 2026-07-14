/**
 * Claude Browser Bridge — Content Script
 *
 * Injected into all pages. Provides DOM interaction helpers and responds to
 * messages from the background service worker.
 */

// ---------------------------------------------------------------------------
// DOM helpers exposed on window for debugging
// ---------------------------------------------------------------------------

window.claudeHelpers = {
  /**
   * Find an element by CSS selector, with a short wait for dynamic content.
   */
  async findElement(selector, timeout = 2000) {
    const existing = document.querySelector(selector);
    if (existing) return existing;

    return new Promise((resolve) => {
      const observer = new MutationObserver(() => {
        const el = document.querySelector(selector);
        if (el) {
          observer.disconnect();
          resolve(el);
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });
      setTimeout(() => {
        observer.disconnect();
        resolve(null);
      }, timeout);
    });
  },

  /**
   * Check if an element is visible and interactable.
   */
  isInteractable(el) {
    if (!el) return false;
    const style = getComputedStyle(el);
    return (
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      style.pointerEvents !== 'none' &&
      el.offsetParent !== null
    );
  },
};

// ---------------------------------------------------------------------------
// Message handler
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.source !== 'claude-bridge') return;

  handleAction(msg)
    .then(sendResponse)
    .catch((err) => sendResponse({ success: false, error: err.message }));

  return true; // async response
});

async function handleAction(msg) {
  const { action } = msg;

  switch (action) {
    case 'click':
      return actionClick(msg.selector);
    case 'type':
      return actionType(msg.selector, msg.text);
    case 'hover':
      return actionHover(msg.selector);
    case 'focus':
      return actionFocus(msg.selector);
    case 'blur':
      return actionBlur(msg.selector);
    case 'check':
    case 'uncheck':
      return actionCheck(msg.selector, action === 'check');
    case 'scroll':
      return actionScroll(msg);
    case 'selectOption':
      return actionSelectOption(msg.selector, msg.value);
    case 'waitForElement':
      return actionWaitForElement(msg.selector, msg.timeout || 10000);
    case 'fillForm':
      return actionFillForm(msg.fields);
    case 'extractData':
      return actionExtractData(msg.selectors);
    case 'getPageContent':
      return actionGetPageContent();
    case 'getElementRect':
      return actionGetElementRect(msg.selector);
    case 'getPageDimensions':
      return actionGetPageDimensions();
    case 'scrollToPosition':
      return actionScrollToPosition(msg.x, msg.y);
    case 'pressKey':
      return actionPressKey(msg.key, msg.selector, msg.modifiers);
    case 'insertText':
      return actionInsertText(msg.selector, msg.text, msg.append);
    case 'waitForStable':
      return actionWaitForStable(msg.selector, msg.stableMs, msg.timeout, msg.pollInterval);
    case 'autoScrollLoad':
      return actionAutoScrollLoad(msg);
    default:
      throw new Error(`Unknown action: ${action}`);
  }
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function actionClick(selector) {
  const el = await window.claudeHelpers.findElement(selector);
  if (!el) throw new Error(`Element not found: ${selector}`);
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  await sleep(100);
  el.click();
  return { success: true, tagName: el.tagName, text: (el.textContent || '').slice(0, 100) };
}

async function actionType(selector, text) {
  const el = await window.claudeHelpers.findElement(selector);
  if (!el) throw new Error(`Element not found: ${selector}`);
  el.focus();
  el.value = '';
  el.dispatchEvent(new Event('input', { bubbles: true }));
  for (const char of text) {
    el.value += char;
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: char }));
  }
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { success: true, typed: text.length };
}

async function actionHover(selector) {
  const el = await window.claudeHelpers.findElement(selector);
  if (!el) throw new Error(`Element not found: ${selector}`);
  el.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
  el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
  return { success: true };
}

async function actionFocus(selector) {
  const el = await window.claudeHelpers.findElement(selector);
  if (!el) throw new Error(`Element not found: ${selector}`);
  el.focus();
  return { success: true };
}

async function actionBlur(selector) {
  const el = await window.claudeHelpers.findElement(selector);
  if (!el) throw new Error(`Element not found: ${selector}`);
  el.blur();
  return { success: true };
}

async function actionCheck(selector, checked) {
  const el = await window.claudeHelpers.findElement(selector);
  if (!el) throw new Error(`Element not found: ${selector}`);
  el.checked = checked;
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { success: true, checked };
}

async function actionScroll(msg) {
  if (msg.selector) {
    const el = await window.claudeHelpers.findElement(msg.selector);
    if (!el) throw new Error(`Element not found: ${msg.selector}`);
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return { success: true, scrolledTo: msg.selector };
  }

  const amount = msg.amount || 500;
  const map = { up: [0, -amount], down: [0, amount], left: [-amount, 0], right: [amount, 0] };
  const [x, y] = map[msg.direction] || [0, amount];
  window.scrollBy({ left: x, top: y, behavior: 'smooth' });
  return { success: true, direction: msg.direction, amount };
}

async function actionSelectOption(selector, value) {
  const el = await window.claudeHelpers.findElement(selector);
  if (!el || el.tagName !== 'SELECT') throw new Error(`Select element not found: ${selector}`);

  let option = el.querySelector(`option[value="${CSS.escape(value)}"]`);
  if (!option) {
    option = [...el.options].find((o) => o.textContent.trim().toLowerCase() === value.toLowerCase());
  }
  if (!option) throw new Error(`Option not found: ${value}`);

  el.value = option.value;
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { success: true, selected: option.value, text: option.textContent.trim() };
}

async function actionWaitForElement(selector, timeout) {
  const el = await window.claudeHelpers.findElement(selector, timeout);
  return { success: !!el, found: !!el, tagName: el ? el.tagName : null };
}

async function actionFillForm(fields) {
  const results = {};
  for (const [selector, value] of Object.entries(fields)) {
    try {
      const el = await window.claudeHelpers.findElement(selector);
      if (!el) throw new Error('Not found');
      el.focus();
      el.value = value;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      results[selector] = { success: true };
    } catch (err) {
      results[selector] = { success: false, error: err.message };
    }
  }
  return { success: true, fields: results };
}

async function actionExtractData(selectors) {
  const data = {};
  for (const [name, selector] of Object.entries(selectors)) {
    const el = await window.claudeHelpers.findElement(selector);
    data[name] = el
      ? { text: (el.textContent || '').trim().slice(0, 5000), html: el.innerHTML.slice(0, 5000), tagName: el.tagName }
      : null;
  }
  return { success: true, data };
}

async function actionGetPageContent() {
  const clone = document.cloneNode(true);
  clone.querySelectorAll('script, style, noscript').forEach((el) => el.remove());
  const text = (clone.body?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 50000);
  return {
    success: true,
    url: location.href,
    title: document.title,
    text,
    metaDescription: document.querySelector('meta[name="description"]')?.content || '',
  };
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ---------------------------------------------------------------------------
// Auto-scroll loader — drives viewport-gated infinite-scroll / lazy content
// ---------------------------------------------------------------------------
//
// Scrolls a container (or the page) to the bottom in jittered increments until
// the amount of loaded content stops growing, then reports how much loaded.
//
// IMPORTANT: IntersectionObserver / lazy hydration only fire when the tab is
// actually foregrounded (Chrome throttles background tabs and pauses paint).
// The background service worker foregrounds the target tab before invoking this
// action and restores the prior tab afterwards (see handleLoadDynamic).

async function actionAutoScrollLoad(msg) {
  const containerSelector = msg.containerSelector || null;
  const itemSelector = msg.itemSelector || null;
  const maxScrolls = Math.max(1, Math.min(500, msg.maxScrolls || 60));
  const stableMs = Math.max(200, Math.min(20000, msg.stableMs || 1200));
  const minDelay = Math.max(100, Math.min(10000, msg.minDelay || 600));
  const maxDelay = Math.max(minDelay, Math.min(20000, msg.maxDelay || 1400));
  const timeout = Math.max(2000, Math.min(600000, msg.timeout || 90000));
  const stagnantLimit = 3;

  // Resolve the scroll target. When a container selector is given but not yet
  // present, wait briefly for it (lazy layouts mount it after hydration).
  let scrollTarget = null;
  if (containerSelector) {
    scrollTarget = await window.claudeHelpers.findElement(containerSelector, 5000);
    if (!scrollTarget) {
      return { success: false, error: `Scroll container not found: ${containerSelector}`, code: 'CONTAINER_NOT_FOUND' };
    }
  }

  const measure = () => {
    if (itemSelector) return document.querySelectorAll(itemSelector).length;
    return scrollTarget
      ? scrollTarget.scrollHeight
      : (document.scrollingElement || document.documentElement).scrollHeight;
  };

  const scrollToBottom = () => {
    if (scrollTarget) {
      scrollTarget.scrollTo({ top: scrollTarget.scrollHeight, behavior: 'instant' });
    } else {
      const el = document.scrollingElement || document.documentElement;
      window.scrollTo({ top: el.scrollHeight, behavior: 'instant' });
    }
  };

  const startTime = Date.now();
  let prev = -1;
  let stagnant = 0;
  let scrolls = 0;
  let timedOut = false;

  for (let i = 0; i < maxScrolls; i++) {
    if (Date.now() - startTime >= timeout) { timedOut = true; break; }

    scrollToBottom();
    scrolls++;

    // Jittered, human-ish pause so lazy content can load between steps.
    const delay = Math.round(minDelay + Math.random() * (maxDelay - minDelay));
    await sleep(delay);

    const current = measure();
    if (current <= prev) stagnant++;
    else stagnant = 0;
    prev = current;

    // Content stopped growing for several consecutive steps — fully loaded.
    if (stagnant >= stagnantLimit) break;
  }

  // Best-effort settle wait so the final batch finishes rendering before extraction.
  if (itemSelector || containerSelector) {
    await sleep(Math.min(stableMs, 2000));
  }

  return {
    success: true,
    loaded: itemSelector ? document.querySelectorAll(itemSelector).length : prev,
    metric: itemSelector ? 'itemCount' : 'scrollHeight',
    scrolls,
    stagnant,
    timedOut,
    elapsed: Date.now() - startTime,
  };
}

// ---------------------------------------------------------------------------
// Keyboard + Console actions
// ---------------------------------------------------------------------------

const KEY_MAP = {
  Enter: { key: 'Enter', code: 'Enter', keyCode: 13 },
  Escape: { key: 'Escape', code: 'Escape', keyCode: 27 },
  Tab: { key: 'Tab', code: 'Tab', keyCode: 9 },
  Backspace: { key: 'Backspace', code: 'Backspace', keyCode: 8 },
  Delete: { key: 'Delete', code: 'Delete', keyCode: 46 },
  Space: { key: ' ', code: 'Space', keyCode: 32 },
  ArrowUp: { key: 'ArrowUp', code: 'ArrowUp', keyCode: 38 },
  ArrowDown: { key: 'ArrowDown', code: 'ArrowDown', keyCode: 40 },
  ArrowLeft: { key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37 },
  ArrowRight: { key: 'ArrowRight', code: 'ArrowRight', keyCode: 39 },
  Home: { key: 'Home', code: 'Home', keyCode: 36 },
  End: { key: 'End', code: 'End', keyCode: 35 },
  PageUp: { key: 'PageUp', code: 'PageUp', keyCode: 33 },
  PageDown: { key: 'PageDown', code: 'PageDown', keyCode: 34 },
};

async function actionPressKey(key, selector, modifiers) {
  let target = document.activeElement || document.body;

  if (selector) {
    const el = await window.claudeHelpers.findElement(selector);
    if (!el) throw new Error(`Element not found: ${selector}`);
    el.focus();
    target = el;
  }

  const mapped = KEY_MAP[key] || { key, code: `Key${key.toUpperCase()}`, keyCode: key.charCodeAt(0) };
  const mods = modifiers || {};

  const eventInit = {
    key: mapped.key,
    code: mapped.code,
    keyCode: mapped.keyCode,
    which: mapped.keyCode,
    bubbles: true,
    cancelable: true,
    ctrlKey: !!mods.ctrl,
    shiftKey: !!mods.shift,
    altKey: !!mods.alt,
    metaKey: !!mods.meta,
  };

  target.dispatchEvent(new KeyboardEvent('keydown', eventInit));
  target.dispatchEvent(new KeyboardEvent('keypress', eventInit));
  target.dispatchEvent(new KeyboardEvent('keyup', eventInit));

  return { success: true, key: mapped.key, target: target.tagName };
}

// ---------------------------------------------------------------------------
// Text insertion (React-compatible) + content stability detection
// ---------------------------------------------------------------------------

async function actionInsertText(selector, text, append) {
  if (!selector || typeof selector !== 'string') throw new Error('Selector must be a non-empty string');
  if (typeof text !== 'string') throw new Error('Text must be a string');
  if (text.length > 100_000) throw new Error('Text too long (max 100KB)');
  const el = await window.claudeHelpers.findElement(selector);
  if (!el) throw new Error(`Element not found: ${selector}`);
  el.focus();
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  await sleep(100);

  const isTextarea = el instanceof HTMLTextAreaElement;
  const isInput = el instanceof HTMLInputElement;

  // If not appending, clear current content first
  if (!append) {
    if (isTextarea || isInput) {
      const proto = isTextarea ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (nativeSetter) {
        nativeSetter.call(el, '');
        el.dispatchEvent(new Event('input', { bubbles: true }));
      } else {
        el.value = '';
        el.dispatchEvent(new Event('input', { bubbles: true }));
      }
    } else if (el.isContentEditable) {
      el.textContent = '';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }

  // Strategy 1: Native value setter (works on React-controlled inputs/textareas)
  if (isTextarea || isInput) {
    const proto = isTextarea ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (nativeSetter) {
      const newVal = append ? el.value + text : text;
      nativeSetter.call(el, newVal);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      // Verify it took
      if (el.value.includes(text)) {
        return { success: true, method: 'nativeSetter', length: text.length };
      }
    }
  }

  // Strategy 2: execCommand insertText (works on contenteditable, some textareas)
  el.focus();
  if (!append && el.isContentEditable) {
    document.execCommand('selectAll', false, null);
  }
  const execResult = document.execCommand('insertText', false, text);
  if (execResult) {
    const content = el.value ?? el.textContent ?? '';
    if (content.includes(text)) {
      return { success: true, method: 'execCommand', length: text.length };
    }
  }

  // Strategy 3: Clipboard paste simulation
  try {
    el.focus();
    const dt = new DataTransfer();
    dt.setData('text/plain', text);
    const pasteEvent = new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt });
    el.dispatchEvent(pasteEvent);
    await sleep(100);
    const content = el.value ?? el.textContent ?? '';
    if (content.includes(text)) {
      return { success: true, method: 'clipboardPaste', length: text.length };
    }
  } catch { /* fall through */ }

  // Strategy 4: Direct value manipulation (least reliable for React)
  if ('value' in el) {
    el.value = append ? el.value + text : text;
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return { success: true, method: 'directValue', length: text.length };
  } else if (el.isContentEditable) {
    if (append) {
      el.textContent += text;
    } else {
      el.textContent = text;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return { success: true, method: 'contentEditable', length: text.length };
  }

  throw new Error('All text insertion strategies failed');
}

async function actionWaitForStable(selector, stableMs, timeout, pollInterval) {
  if (!selector || typeof selector !== 'string') throw new Error('Selector must be a non-empty string');
  stableMs = Math.max(100, Math.min(60000, stableMs || 8000));
  timeout = Math.max(1000, Math.min(300000, timeout || 180000));
  pollInterval = Math.max(100, Math.min(30000, pollInterval || 2000));

  const startTime = Date.now();
  let lastContent = '';
  let lastChangeTime = startTime;
  let samples = 0;

  while (true) {
    const elapsed = Date.now() - startTime;
    if (elapsed >= timeout) {
      return {
        success: true,
        stable: false,
        timedOut: true,
        contentLength: lastContent.length,
        elapsed,
        samples,
      };
    }

    const el = await window.claudeHelpers.findElement(selector, Math.min(pollInterval, 2000));
    if (!el) {
      if (samples > 0 && lastContent.length > 0) {
        return { success: true, stable: false, error: 'Element removed from DOM', contentLength: lastContent.length, elapsed: Date.now() - startTime, samples };
      }
      // Element hasn't appeared yet — keep waiting
      samples++;
      await sleep(pollInterval);
      continue;
    }
    const currentContent = (el.textContent || '').trim();
    samples++;

    if (currentContent !== lastContent) {
      lastContent = currentContent;
      lastChangeTime = Date.now();
    }

    const stableDuration = Date.now() - lastChangeTime;
    if (stableDuration >= stableMs && currentContent.length > 0) {
      return {
        success: true,
        stable: true,
        timedOut: false,
        contentLength: currentContent.length,
        elapsed: Date.now() - startTime,
        samples,
      };
    }

    await sleep(pollInterval);
  }
}

// ---------------------------------------------------------------------------
// Screenshot helper actions
// ---------------------------------------------------------------------------

async function actionGetElementRect(selector) {
  const el = await window.claudeHelpers.findElement(selector);
  if (!el) throw new Error(`Element not found: ${selector}`);
  // Return page-absolute coordinates (viewport rect + scroll offset)
  // CDP's captureBeyondViewport uses page coordinates, no need to scroll into view
  const rect = el.getBoundingClientRect();
  return {
    success: true,
    rect: {
      x: rect.x + window.scrollX,
      y: rect.y + window.scrollY,
      width: rect.width,
      height: rect.height,
    },
    dpr: window.devicePixelRatio || 1,
  };
}

function actionGetPageDimensions() {
  return {
    success: true,
    scrollHeight: Math.max(
      document.body.scrollHeight,
      document.documentElement.scrollHeight,
    ),
    viewportHeight: window.innerHeight,
    viewportWidth: window.innerWidth,
    dpr: window.devicePixelRatio || 1,
    scrollX: window.scrollX,
    scrollY: window.scrollY,
  };
}

function actionScrollToPosition(x, y) {
  window.scrollTo({ left: x, top: y, behavior: 'instant' });
  return { success: true, scrollX: window.scrollX, scrollY: window.scrollY };
}

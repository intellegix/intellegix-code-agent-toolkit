"""Extended Research Runner — exhaustive multi-pass artifact verification.

Standalone async orchestrator invoked by the /extended-research slash command.
Spawned with `&` so Claude Code returns immediately; loops 5-40 Perplexity
research_query passes, writes ledger.json + passes.jsonl + report.md to a
per-run workdir, terminates on AND-gate convergence or hard cap.

Design rationale: 5 design passes (2026-05-18) — see
~/.claude/commands/extended-research.md for full architecture.

Key behaviors:
- Forced-JSON output from Perplexity; jsonschema-validated; PARSE-FAILED on miss
- AND-gate convergence: 3 zero-finding + 3 zero-contradiction + all-LOW × 2 + adversarial_count >= ceil(N/2)
- Forced-ADVERSARIAL injection when convergence would fire but adversarial deficit blocks
- POSTMORTEM event-triggered (after all HIGH/MED have >= 1 TARGETED_PROBE), fires once
- FRESH_OBSERVER at pass 8 / 14 / 20, sees frozen Claude-generated 2k-token summary only
- ANALOGOUS findings: effective_severity = min(raw_severity, MEDIUM) for convergence gate
- TARGETED_PROBE upsert: dedup ledger findings on ID; preserve history in findings_history[]
- last_heartbeat_ts written before every pass; stale detection by status command
- Windows-safe atomic ledger writes (same-directory tempfile + os.replace)
- Trailing-prose-tolerant JSON parser (regex extract first, then json.loads)
- submission_lock with 180s timeout (re-uses ~/.claude/council-automation/submission_lock.py)
- SIGINT → INTERRUPTED marker + ledger snapshot; --resume continues from interrupted_at_pass + 1
- SHA-256 artifact hash on Pass 1; --resume compares and warns on drift
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Optional deps loaded lazily so the runner can fail with a clear message
# if requirements.txt wasn't installed.
try:
    import jsonschema  # type: ignore
except ImportError:
    print("[ERROR] jsonschema not installed. Run: pip install -r requirements.txt", file=sys.stderr, flush=True)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COUNCIL_AUTOMATION_DIR = Path(__file__).parent
COUNCIL_QUERY_SCRIPT = COUNCIL_AUTOMATION_DIR / "council_query.py"

# Phase 1 instrumentation (2026-05-29 empirical-reassessment plan).
# Per-pass JSONL emitted into {WORKDIR}/instrumentation.jsonl AND into the
# global rolling log at ~/.claude/council-cache/instrumentation.jsonl. Global
# log is capped at 10 MB with tail-truncation to 8 MB at write time; workdir
# log is per-run and never explicitly capped.
INSTRUMENTATION_GLOBAL_LOG = (
    Path.home() / ".claude" / "council-cache" / "instrumentation.jsonl"
)
INSTRUMENTATION_CAP_BYTES = 10 * 1024 * 1024


def _append_instrumentation_jsonl(
    path: Path, record: dict, cap_bytes: int = INSTRUMENTATION_CAP_BYTES
) -> None:
    """Append a JSONL record with size-cap enforcement at write time.

    Tail-truncate to 8 MB if file > cap_bytes. Recent data is more valuable
    than oldest. Drops any partial first line after truncation. Never raises:
    instrumentation must not block runner execution.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > cap_bytes:
            data = path.read_bytes()[-(8 * 1024 * 1024):]
            first_nl = data.find(b"\n")
            if first_nl > 0:
                data = data[first_nl + 1:]
            path.write_bytes(data)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def _emit_pass_instrumentation(workdir: Path, record: dict) -> None:
    """Emit one Phase 1 per-pass instrumentation record to both sinks."""
    try:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
    except Exception:
        pass
    _append_instrumentation_jsonl(workdir / "instrumentation.jsonl", record, cap_bytes=10**12)
    _append_instrumentation_jsonl(INSTRUMENTATION_GLOBAL_LOG, record)


# 2026-05-29 14:30 PT data-driven revert 5000 -> 3500. Same-day instrumentation
# (commits d90e4cf + 49fdaab) captured 26 queries / 21 passes in ~5 hours after
# the 3500 -> 5000 relaxation landed. Empirical cliff observed at prompt > 18 KB:
# successful extractions at qchars 14-17 KB returned 5-8 KB synthesis; failures
# at qchars 18-24 KB returned 200-480 char partial-mount shells. The 5000-token
# cap was pushing artifact text to ~20 KB which crossed the cliff once the
# ~5 KB prompt scaffold was added.
#
# 3500 was the working pre-d90e4cf value; today's data confirms it's empirically
# safe (caps artifact text at ~14 KB, prompts at ~19 KB max). See issue #44
# comment 4579954481 for the cliff localization data:
#   https://github.com/intellegix/intellegix-code-agent-toolkit/issues/44#issuecomment-4579954481
#
# Phase 4 may later support raising to 4000 once 20+ runs accumulate, but the
# 5000 value is empirically wrong. The Phase 4 trigger threshold (20-run minimum
# before adjustment) assumed slow calibration; the day-1 signal was loud enough
# to enact the data-driven adjustment immediately.
ARTIFACT_TEXT_TOKEN_CAP = 3500          # max tokens for {{ARTIFACT_TEXT}} injection
FRESH_OBSERVER_SUMMARY_CAP = 2000       # Claude-generated summary cap
FRESH_OBSERVER_TOTAL_CAP = 4000         # summary + finding titles list combined
FRESH_OBSERVER_SCHEDULE = (8, 14, 20)   # pass numbers (and every 6 after)
RESEARCH_QUERY_TIMEOUT_S = 360          # per-pass wall timeout
SUBMISSION_LOCK_TIMEOUT_S = 180         # raised from 30s per Pass 4 review
HEARTBEAT_STALE_THRESHOLD_S = 300       # 5 min; status command flags stale
STRUCTURAL_UNRESOLVABLE_THRESHOLD = 3   # HIGH findings persisting >= 3 passes get this tag

# Convergence AND-gate thresholds
CONVERGENCE_ZERO_FINDING_PASSES = 3
# Circuit breaker — if K consecutive passes return status=PARSE-FAILED, abort early
# instead of burning the full pass budget. Catches expired Perplexity sessions,
# bot-detection lockout, runner-side parser bugs, and other "garbage in for the whole
# run" failure modes. Caller can salvage via salvaged-responses.md.
PARSE_FAIL_STREAK_THRESHOLD = 3

# PATCH 2026-05-20 (Perplexity audit Pattern A mitigation): a heavy 115s+
# research call from one IP+cookie session degrades the NEXT call within
# ~60s — Perplexity's backend throttles synthesis, page loads but
# div.prose.max-w-none never populates → 0-byte raw response. Inserting a
# cooldown between sequential passes mitigates this until the deferred
# single-Playwright-context refactor lands. 0 disables the cooldown.
# Env var: PERPLEXITY_INTER_PASS_SLEEP_S (default 30s).
try:
    INTER_PASS_SLEEP_S = max(0.0, float(os.environ.get("PERPLEXITY_INTER_PASS_SLEEP_S", "30")))
except ValueError:
    INTER_PASS_SLEEP_S = 30.0
# Circuit breaker — if K consecutive passes return status=SKIPPED-NETWORK, abort.
# Catches subprocess.run failures (TimeoutExpired, OSError, non-zero exit from
# council_query.py — Cloudflare lockout, expired session, persistent network drop,
# or the cmdline-length blowup that the stdin-piping fix above prevents).
# Distinct from PARSE_FAIL because the response never arrived (vs arrived-but-unparseable).
SKIPPED_NETWORK_STREAK_THRESHOLD = 3
CONVERGENCE_ZERO_CONTRADICTION_PASSES = 3
CONVERGENCE_ALL_LOW_PASSES = 2


# ---------------------------------------------------------------------------
# JSON Schema for forced-output Perplexity responses
# ---------------------------------------------------------------------------
RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "pass_type",
        "findings",
        "contradictions",
        "options",
        "verdict_hint",
        "raw_evidence_summary",
    ],
    "properties": {
        "pass_type": {
            "type": "string",
            "enum": [
                "DECOMPOSE", "CRITIQUE", "ADVERSARIAL", "OPTIONS_SWEEP",
                "TARGETED_PROBE", "FRESH_OBSERVER", "POSTMORTEM",
                "INTEGRATION", "FINAL_VERDICT",
                # Agentic pass types — recommended by Perplexity via recommended_next_pass
                "BLUEPRINT", "GUIDANCE", "EXPLORATORY_BRANCH",
            ],
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "severity", "claim", "phase", "source"],
                "properties": {
                    "id": {"type": "string", "pattern": "^F[0-9]{3,4}$"},
                    "severity": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW", "INFO"]},
                    "claim": {"type": "string", "minLength": 5, "maxLength": 1000},
                    "phase": {"type": "string"},
                    "source": {"type": "string"},
                    "source_flag": {
                        "type": "string",
                        "enum": ["PRIMARY", "SECONDARY", "ANALOGOUS", "INFERRED"],
                    },
                    "prior_finding_id": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        "contradictions": {"type": "array", "items": {"type": "string"}},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["option_id", "label", "pros", "cons"],
                "properties": {
                    "option_id": {"type": "string"},
                    "label": {"type": "string"},
                    "pros": {"type": "array", "items": {"type": "string"}},
                    "cons": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                "additionalProperties": True,
            },
        },
        "verdict_hint": {"type": "string"},
        "raw_evidence_summary": {"type": "string", "minLength": 10},
        "domain_postmortem_note": {"type": "string"},
        "fresh_observer_re_raises": {"type": "array", "items": {"type": "string"}},
        "phases": {  # DECOMPOSE only — required for phase-scoped artifact injection
            "type": "array",
            "items": {
                "type": "object",
                "required": ["label"],
                "properties": {
                    "label": {"type": "string"},
                    "line_start": {"type": "integer", "minimum": 1},
                    "line_end": {"type": "integer", "minimum": 1},
                },
                "additionalProperties": True,
            },
        },
        # Agentic next-pass recommendation — populated by every pass except
        # DECOMPOSE (Pass 1, no prior context) and FINAL_VERDICT (terminal).
        # The orchestrator reads this and uses it to choose the next pass type.
        "recommended_next_pass": {
            "type": "object",
            "required": ["pass_type", "question", "rationale"],
            "properties": {
                "pass_type": {
                    "type": "string",
                    "enum": [
                        "TARGETED_PROBE", "EXPLORATORY_BRANCH", "BLUEPRINT",
                        "GUIDANCE", "ADVERSARIAL", "INTEGRATION", "CRITIQUE",
                    ],
                },
                "target_finding_id": {"type": ["string", "null"]},
                "question": {"type": "string", "minLength": 10, "maxLength": 1000},
                "rationale": {"type": "string", "minLength": 10, "maxLength": 500},
            },
            "additionalProperties": True,
        },
    },
    "additionalProperties": True,
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg: str, level: str = "INFO") -> None:
    """Append timestamped line to stdout (which the launcher redirects to runner.log).

    Windows-safe encoding: when stdout uses cp1252 (default Windows console),
    Perplexity rationales containing Unicode (e.g. U+2011 non-breaking hyphen,
    smart quotes, em-dashes) crash `print()`. Encode to stdout's codec with
    replacement instead of letting the codec raise.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{level}] {msg}"
    enc = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Fallback: encode to stdout's codec replacing un-encodable chars.
        safe = line.encode(enc, errors="replace").decode(enc, errors="replace")
        print(safe, flush=True)


# ---------------------------------------------------------------------------
# Ledger (atomic write, Windows-safe)
# ---------------------------------------------------------------------------
def write_ledger_atomic(ledger: dict, workdir: Path) -> None:
    """Write ledger.json atomically using a same-directory tempfile.

    os.replace requires same-filesystem move on Windows; using
    tempfile.NamedTemporaryFile with dir=workdir keeps src/dst on the
    same filesystem (the workdir's drive). Falls back to shutil.move if
    os.replace fails for any reason (e.g., antivirus locking the tmp).
    """
    final_path = workdir / "ledger.json"
    tmp = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(workdir),
        prefix=".ledger_",
        suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(ledger, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        try:
            os.replace(tmp.name, final_path)
        except OSError:
            shutil.move(tmp.name, final_path)
    except OSError as e:
        # Disk full or permission — emergency fallback to /tmp
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except OSError:
            pass
        emergency = Path(tempfile.gettempdir()) / f"extended-research-emergency-{ledger.get('slug', 'unknown')}-ledger.json"
        with open(emergency, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2)
        log(f"[CRITICAL] Disk write to workdir failed: {e}. Ledger emergency-written to {emergency}", "ERROR")
        sys.exit(13)


def load_ledger(workdir: Path) -> dict:
    return json.loads((workdir / "ledger.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# JSON parsing — prompt-echo and trailing-footer tolerant
# ---------------------------------------------------------------------------
# Required top-level keys for a valid response. Used by extract_json to choose
# between multiple {...} candidates when an LLM response echoes the prompt
# (which contains literal {...} schema fragments) before delivering the real
# JSON payload at the tail.
_RESPONSE_REQUIRED_KEYS: frozenset[str] = frozenset({
    "pass_type", "findings", "verdict_hint", "raw_evidence_summary",
})


def _balanced_object_spans(text: str) -> list[tuple[int, int]]:
    """Return (start_inclusive, end_exclusive) pairs for every TOP-LEVEL
    balanced `{...}` object in `text`. String-literal-aware: braces inside
    JSON-style double-quoted string literals don't count toward depth.
    Nested objects are NOT returned separately — only the outermost spans.
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    in_str = False
    escape = False
    start = -1
    for i, c in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append((start, i + 1))
                    start = -1
    return spans


def _walk_balanced_object(text: str, start: int) -> int | None:
    """Walk forward from `start` (which must point to `{`) counting braces
    string-literal-aware, and return the index immediately past the matching
    closing `}`. Returns None if no balanced match is found.

    Robust within actual JSON because real responses are valid JSON — the
    string tracker only has to be correct inside JSON, not arbitrary prose.
    """
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
            if depth < 0:
                return None
    return None


def _extract_longest_valid_json_prefix(text: str) -> dict | None:
    """Return the longest balanced JSON object prefix that parses cleanly.

    Used when Perplexity truncates a JSON response mid-stream (max_tokens or
    render-stall): we can often salvage the first N keys before the cut. Walks
    the string tracking brace depth + in-string state, finds the first balanced
    `{...}` slice, attempts json.loads. Returns None if no balanced prefix
    parses. 2026-05-27 Tier 3 free-recovery pass.
    """
    if not text:
        return None
    s = text.strip()
    depth = 0
    in_string = False
    escape_next = False
    last_balanced = -1
    for i, ch in enumerate(s):
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if ch == "\\":
                escape_next = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_balanced = i + 1
                break
    if last_balanced < 0:
        return None
    try:
        return json.loads(s[:last_balanced])
    except Exception:
        return None


# Phase 2 parser provisions (2026-05-29). Address wrapper-style responses where
# the same JSON appears twice — once truncated in `## Summary`, once complete in
# `## Detailed Analysis` — by anchoring search after the last Detailed Analysis
# header and selecting the largest brace-balanced span.
KNOWN_PASS_TYPES = frozenset([
    "DECOMPOSE", "CRITIQUE", "ADVERSARIAL", "OPTIONS_SWEEP", "TARGETED_PROBE",
    "BLUEPRINT", "GUIDANCE", "EXPLORATORY_BRANCH", "POSTMORTEM",
    "FRESH_OBSERVER", "INTEGRATION", "FINAL_VERDICT",
])
REQUIRED_TOP_LEVEL_KEYS = frozenset([
    "pass_type", "findings", "contradictions", "options", "verdict_hint",
    "raw_evidence_summary",
])


def _extract_after_detailed_analysis(text: str) -> str:
    """Return text after the last `## Detailed Analysis` header if present.

    Perplexity often wraps replies as `## Summary` (truncated preview) +
    `## Detailed Analysis` (complete). Anchoring search after the LAST
    Detailed Analysis header drops the truncated Summary copy from
    consideration. Falls back to returning text unchanged if header absent.
    """
    if not text:
        return text
    idx = text.rfind("## Detailed Analysis")
    if idx < 0:
        return text
    return text[idx:]


def _is_complete_finding(f: Any) -> bool:
    """Reject partially-parsed final-array-element fragments.

    A finding is complete iff it's a dict with `id` key AND >=1 other key.
    Mid-array truncation can leave the last finding as `{"id": "F009"` which
    parses to a dict-without-id-key in some json-repair scenarios.
    """
    return isinstance(f, dict) and "id" in f and len(f) >= 2


def _filter_complete_findings(parsed: dict) -> dict:
    """Drop incomplete findings from parsed['findings'] in place."""
    findings = parsed.get("findings")
    if isinstance(findings, list):
        parsed["findings"] = [f for f in findings if _is_complete_finding(f)]
    return parsed


def _is_valid_shape(d: Any) -> bool:
    """Stricter shape gate (2026-05-29). Rejects narrow sub-objects.

    Requires:
      - pass_type is a string AND its value is in KNOWN_PASS_TYPES
      - findings is a list (empty list OK)
      - >=3 of REQUIRED_TOP_LEVEL_KEYS present in dict
    """
    if not isinstance(d, dict):
        return False
    if not isinstance(d.get("pass_type"), str):
        return False
    if d["pass_type"] not in KNOWN_PASS_TYPES:
        return False
    if not isinstance(d.get("findings"), list):
        return False
    if len(d.keys() & REQUIRED_TOP_LEVEL_KEYS) < 3:
        return False
    return True


def _largest_balanced_json_dict(text: str, anchor_pattern: str) -> dict | None:
    """Walk ALL anchor matches; return the LONGEST brace-balanced dict that
    parses cleanly AND passes the shape gate.

    Truncated copies close their brace early, producing shorter spans;
    complete copies span further. The shape gate (_is_valid_shape) rejects
    narrow sub-objects that happen to be the longest balanced sub-tree.
    """
    matches = list(re.finditer(anchor_pattern, text))
    best_dict: dict | None = None
    best_len = 0
    for m in matches:
        start = m.start()
        end = _walk_balanced_object(text, start)
        if end is None:
            continue
        if end - start <= best_len:
            continue
        candidate = text[start:end]
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not _is_valid_shape(obj):
            continue
        best_dict = obj
        best_len = end - start
    return best_dict


def extract_json(text: str) -> dict | None:
    """Extract the response JSON from an LLM/Perplexity reply.

    Tolerates: a markdown preamble that echoes the prompt (with literal `{...}`
    schema fragments that would mislead a naive "first `{`" extractor),
    code-fence wrappers, prose analysis between echo and payload, and a
    trailing footer like `\\n---\\nCache: ...`. Prompt-echo brace imbalance from
    arbitrary markdown prose can't be tracked reliably as a left-to-right walk,
    so the extractor anchors on the response's known top-level key instead.

    Strategy (each stage falls through on failure):
      1. Happy path — `json.loads(text)` for pure-JSON replies.
      2. Strip code-fence wrappers (```` ```json ... ``` ````); retry direct parse.
      3. Pre-strip the council_browser preamble (drop everything up to and
         including the LAST `**Query:**` marker) and the trailing `Cache:` footer.
      4. **Anchored extraction** — find the LAST occurrence of `{\\s*"pass_type"`
         (the response's signature opening) and walk forward, brace-balanced
         string-aware, to find the matching `}`. Try `json.loads` on that slice.
      5. **Anchored fallback** — find the LAST `{\\s*"<any_key>"\\s*:` start of an
         object, walk forward, parse. Prefer dicts containing the response's
         required top-level keys (pass_type, findings, verdict_hint,
         raw_evidence_summary). Fall back to the last dict that simply parses.
      6. **json-repair last-ditch** (only if installed) — if a candidate slice
         exists but `json.loads` rejected it (e.g., LLM emitted unescaped `"`
         inside a string value), try `json_repair.loads`. Soft dep — silently
         skipped if the library is missing.
    Returns None only if no candidate parses at all.
    """
    if not text or not text.strip():
        return None

    # 1. Happy path
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        pass

    # 2. Strip code fences and retry direct parse
    stripped = re.sub(r"```(?:json)?\s*", "", text)
    stripped = re.sub(r"```\s*", "", stripped)
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 3. Pre-strip council_browser preamble + Cache footer
    body = text
    qmatches = list(re.finditer(r"\*\*Query:\*\*", body))
    if qmatches:
        body = body[qmatches[-1].end():]
    body = re.sub(r"\n-{3,}\s*\nCache:.*\Z", "", body, flags=re.DOTALL)

    # 3.5. Phase 2 (2026-05-29 reassessment) — wrapper-aware extraction.
    # Perplexity often emits the same JSON twice: a truncated copy in `## Summary`
    # and a complete copy in `## Detailed Analysis`. Anchor search after the LAST
    # Detailed Analysis header to drop the Summary copy, then pick the LARGEST
    # brace-balanced span (truncated copies close their brace early), with the
    # shape gate rejecting narrow sub-objects that happen to be the longest
    # balanced sub-tree. Filter incomplete findings (final-element truncation)
    # before returning. Free attempt — falls through to existing stages on miss.
    body_scoped = _extract_after_detailed_analysis(body)
    wrapper_result = _largest_balanced_json_dict(body_scoped, r'\{\s*"pass_type"')
    if wrapper_result is not None:
        return _filter_complete_findings(wrapper_result)

    # 4. Anchored extraction — locate the response's signature opener.
    # Try in order of specificity. The response ALWAYS starts with `{"pass_type"`
    # (possibly with whitespace). The echoed prompt's schema description contains
    # things like `"pass_type": {` (key first, brace second) which won't match.
    anchor_patterns = (
        r'\{\s*"pass_type"',                    # exact response signature
        r'\{\s*"[a-zA-Z_][a-zA-Z0-9_]*"\s*:',   # any object starting with a string-key
    )
    fallback: dict | None = None
    repair_candidates: list[str] = []  # slices that walked balanced but json.loads rejected
    for pattern in anchor_patterns:
        matches = list(re.finditer(pattern, body))
        # Walk right-to-left — the real response is at the tail
        for m in reversed(matches):
            start = m.start()
            end = _walk_balanced_object(body, start)
            if end is None:
                continue
            candidate = body[start:end]
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError:
                # Track for json-repair fallback (only the most-specific anchor's
                # misses — the broader anchor's misses are usually noise like
                # inline option objects from option_sweep results).
                if pattern == anchor_patterns[0]:
                    repair_candidates.append(candidate)
                continue
            if not isinstance(obj, dict):
                continue
            if _RESPONSE_REQUIRED_KEYS.issubset(obj.keys()):
                return obj  # got the response — done
            if fallback is None:
                fallback = obj  # remember last-parseable-dict for very-last-ditch

    # 6. json-repair last-ditch — only if the soft dep is installed AND we have
    # at least one specific-anchor candidate that walked balanced but failed
    # strict json.loads (typically LLM-emitted unescaped `"` inside a string).
    if repair_candidates:
        try:
            import json_repair  # type: ignore[import-not-found]
        except ImportError:
            json_repair = None  # type: ignore[assignment]
        if json_repair is not None:
            for cand in repair_candidates:
                try:
                    repaired = json_repair.loads(cand)
                except Exception:
                    continue
                if isinstance(repaired, dict) and _RESPONSE_REQUIRED_KEYS.issubset(repaired.keys()):
                    log(f"extract_json: recovered via json-repair (cand_len={len(cand)})", "WARN")
                    return repaired
                if isinstance(repaired, dict) and fallback is None:
                    fallback = repaired

    return fallback


def validate_response(obj: dict | None) -> tuple[bool, str | None]:
    """Validate parsed Perplexity response against schema. Returns (ok, error_str)."""
    if obj is None:
        return False, "no JSON object extracted from response"
    try:
        jsonschema.validate(obj, RESPONSE_SCHEMA)
        return True, None
    except jsonschema.ValidationError as e:
        return False, f"schema validation failed: {e.message} at {list(e.absolute_path)}"


def _defensive_inject_required(parsed: dict | None, pass_type: str) -> dict | None:
    """Inject sane defaults for top-level required fields the model may have dropped.

    Per 2026-05-22 audit: Perplexity sometimes omits `pass_type` and other
    required top-level fields (or returns malformed sub-items in `findings` /
    `options` arrays). Schema validation rejects, the runner retries once,
    same drop happens, and the PARSE_FAIL streak triggers ABORTED termination
    even though the response actually contained useful content.

    The defensive fix:
      - `pass_type` is always known from context (passed to run_pass); inject it.
      - Missing arrays default to empty (`findings`, `contradictions`, `options`).
      - Missing strings get short sentinel values (`verdict_hint`,
        `raw_evidence_summary` — the latter needs minLength=10).
      - Filter out malformed sub-items in `findings` / `options` (missing
        required sub-fields) so a single bad item doesn't kill the whole pass.

    Only applies to dict responses. None pass-through unchanged (real failure).
    """
    if not isinstance(parsed, dict):
        return parsed
    DEFAULTS: dict[str, Any] = {
        "pass_type": pass_type,
        "findings": [],
        "contradictions": [],
        "options": [],
        "verdict_hint": "INCOMPLETE_RESPONSE",
        "raw_evidence_summary": "(no raw_evidence_summary in response; defensively injected)",
    }
    injected: list[str] = []
    for k, v in DEFAULTS.items():
        if k not in parsed or parsed[k] is None:
            parsed[k] = v
            injected.append(k)
        elif k == "pass_type" and parsed[k] != pass_type:
            # Perplexity sometimes echoes the WRONG pass_type (e.g. claims DECOMPOSE
            # in response to a CRITIQUE prompt). Always trust the caller's intent.
            log(f"Overriding pass_type {parsed[k]!r} → {pass_type!r} (caller authority)", "WARN")
            parsed[k] = pass_type
            injected.append(f"pass_type_overridden({k})")
    # Filter findings missing required sub-fields rather than failing whole response.
    REQUIRED_FINDING_KEYS = {"id", "severity", "claim", "phase", "source"}
    REQUIRED_OPTION_KEYS = {"option_id", "label", "pros", "cons"}
    if isinstance(parsed.get("findings"), list):
        before = len(parsed["findings"])
        cleaned_f = [f for f in parsed["findings"]
                     if isinstance(f, dict) and REQUIRED_FINDING_KEYS.issubset(f.keys())]
        if len(cleaned_f) != before:
            injected.append(f"findings_filtered({before}→{len(cleaned_f)})")
            parsed["findings"] = cleaned_f
    if isinstance(parsed.get("options"), list):
        before = len(parsed["options"])
        cleaned_o = [o for o in parsed["options"]
                     if isinstance(o, dict) and REQUIRED_OPTION_KEYS.issubset(o.keys())]
        if len(cleaned_o) != before:
            injected.append(f"options_filtered({before}→{len(cleaned_o)})")
            parsed["options"] = cleaned_o
    # raw_evidence_summary minLength=10 enforcement
    res = parsed.get("raw_evidence_summary", "")
    if isinstance(res, str) and len(res) < 10:
        parsed["raw_evidence_summary"] = res + " (padded by defensive injection)"
        injected.append("raw_evidence_summary_padded")
    if injected:
        log(f"Defensive injection on {pass_type} response: {', '.join(injected)}", "INFO")
    return parsed


# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------
def read_artifact(workdir: Path) -> tuple[str, str]:
    """Read artifact.txt (with header), return (full_text_body, sha256_from_header)."""
    raw = (workdir / "artifact.txt").read_text(encoding="utf-8")
    # Header format: HASH:sha256:...\nVERSION:1\n---\n<body>
    m = re.match(r"HASH:sha256:([0-9a-f]{64})\s*\nVERSION:\d+\s*\n---\s*\n(.*)$", raw, re.DOTALL)
    if not m:
        # No header — treat whole file as body, recompute hash
        body = raw
        return body, hashlib.sha256(body.encode("utf-8")).hexdigest()
    return m.group(2), m.group(1)


def truncate_to_token_budget(text: str, token_cap: int) -> str:
    """Approximate token-cap truncation using ~4 chars/token heuristic.

    Not perfect tokenization but adequate for prompt-budget safety. Cuts
    at paragraph boundaries when possible, otherwise at the cap.
    """
    char_cap = token_cap * 4
    if len(text) <= char_cap:
        return text
    cut = text[:char_cap]
    # Prefer cutting at last paragraph break
    last_para = cut.rfind("\n\n")
    if last_para > char_cap * 0.7:
        cut = cut[:last_para]
    return cut + f"\n\n[... TRUNCATED at ~{token_cap} tokens for prompt budget ...]"


def slice_artifact_by_phase(body: str, line_start: int | None, line_end: int | None) -> str:
    """Return the artifact slice for a TARGETED_PROBE on a specific phase.

    If phase line bounds are missing or invalid, falls back to the
    truncated full artifact (won't blow the token budget).
    """
    lines = body.splitlines()
    if line_start and line_end and 1 <= line_start <= line_end <= len(lines):
        slice_lines = lines[line_start - 1:line_end]
        slice_text = "\n".join(slice_lines)
        # Header for clarity in the prompt
        return f"[Phase slice: lines {line_start}-{line_end} of {len(lines)}]\n{slice_text}"
    return truncate_to_token_budget(body, ARTIFACT_TEXT_TOKEN_CAP)


# ---------------------------------------------------------------------------
# Effective severity (ANALOGOUS cap)
# ---------------------------------------------------------------------------
SEVERITY_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}


def effective_severity(finding: dict) -> str:
    """ANALOGOUS-sourced findings cap at MEDIUM for convergence gate purposes.

    Raw severity is preserved on the finding object for human review;
    this function computes the convergence-relevant value.
    """
    raw = finding.get("severity", "INFO")
    source_flag = finding.get("source_flag", "PRIMARY")
    if source_flag == "ANALOGOUS" and SEVERITY_ORDER.get(raw, 0) > SEVERITY_ORDER["MEDIUM"]:
        return "MEDIUM"
    return raw


# ---------------------------------------------------------------------------
# Ledger merge — upsert findings by ID, preserve history
# ---------------------------------------------------------------------------
def upsert_findings(ledger: dict, new_findings: list[dict], pass_num: int) -> int:
    """Merge new findings into ledger.findings, deduplicating by id.

    On collision: the prior finding moves to findings_history[] with a
    `superseded_at_pass` annotation; the new finding takes its slot.
    Returns count of net-new findings (excluding updates of existing IDs).
    """
    existing_by_id = {f["id"]: f for f in ledger.setdefault("findings", [])}
    history = ledger.setdefault("findings_history", [])
    net_new = 0
    for nf in new_findings:
        fid = nf["id"]
        nf.setdefault("first_seen_pass", pass_num)
        nf.setdefault("last_updated_pass", pass_num)
        nf.setdefault("source_flag", "PRIMARY")
        nf.setdefault("status", "OPEN")
        if fid in existing_by_id:
            # Update — move old to history
            prior = existing_by_id[fid]
            prior_snapshot = dict(prior)
            prior_snapshot["superseded_at_pass"] = pass_num
            history.append(prior_snapshot)
            # Preserve first_seen_pass from the original
            nf["first_seen_pass"] = prior.get("first_seen_pass", pass_num)
            existing_by_id[fid] = nf
        else:
            existing_by_id[fid] = nf
            net_new += 1
    # STRUCTURAL-UNRESOLVABLE auto-tag: HIGH findings present >= 3 consecutive passes unresolved
    for f in existing_by_id.values():
        if f.get("severity") == "HIGH" and f.get("status") == "OPEN":
            age = pass_num - f.get("first_seen_pass", pass_num)
            if age >= STRUCTURAL_UNRESOLVABLE_THRESHOLD - 1 and not f.get("structural_unresolvable"):
                f["structural_unresolvable"] = True
    ledger["findings"] = list(existing_by_id.values())
    return net_new


def next_finding_id(ledger: dict) -> str:
    """Return the next F### identifier (sequential, padded)."""
    existing = ledger.get("findings", []) + ledger.get("findings_history", [])
    nums = []
    for f in existing:
        m = re.match(r"^F(\d{3,4})$", f.get("id", ""))
        if m:
            nums.append(int(m.group(1)))
    nxt = max(nums) + 1 if nums else 1
    return f"F{nxt:03d}"


# ---------------------------------------------------------------------------
# research_query invocation — subprocess to council_query.py
# ---------------------------------------------------------------------------
def call_research_query(prompt: str, invocation_id: str) -> tuple[str | None, str | None]:
    """Invoke council_query.py with research mode. Returns (stdout, error_or_None).

    Uses subprocess so the runner is independent of any Perplexity SDK
    state; council_query.py owns the Playwright session.
    """
    if not COUNCIL_QUERY_SCRIPT.exists():
        return None, f"council_query.py not found at {COUNCIL_QUERY_SCRIPT}"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # Pipe prompt via stdin (not argv) — Windows CreateProcess has a 32,767-char
    # lpCommandLine limit (UNICODE_STRING 16-bit length field). Large prompts hit
    # OSError WinError 206. council_query.py reads from stdin when --prompt-stdin
    # is set. See plans/jiggly-mapping-pretzel.md.
    cmd = [
        sys.executable,
        str(COUNCIL_QUERY_SCRIPT),
        "--mode", "browser",
        "--perplexity-mode", "research",
        "--invocation-id", invocation_id,
        "--prompt-stdin",
    ]
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=RESEARCH_QUERY_TIMEOUT_S,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            stderr_tail = (result.stderr or "")[-500:]
            return None, f"council_query.py exited {result.returncode}: {stderr_tail}"
        return result.stdout, None
    except subprocess.TimeoutExpired:
        return None, f"council_query.py timed out after {RESEARCH_QUERY_TIMEOUT_S}s"
    except OSError as e:
        return None, f"subprocess error: {e}"


# ---------------------------------------------------------------------------
# Prompt templates — JSON-output constrained
# ---------------------------------------------------------------------------
JSON_SUFFIX_TEMPLATE = """\

---
OUTPUT CONSTRAINTS — STRICT:
You MUST respond with a single valid JSON object conforming to this schema.
No markdown. No prose before or after. The very first character of your response must be `{{` and the last must be `}}`.

Required top-level fields: pass_type, findings, contradictions, options, verdict_hint, raw_evidence_summary.

Schema essentials:
- pass_type: must be exactly "{pass_type}"
- findings: array of objects, each with:
    id (string matching ^F[0-9]{{3,4}}$, sequential starting at "{next_id}"),
    severity ("HIGH" | "MEDIUM" | "LOW" | "INFO"),
    claim (5-1000 chars),
    phase (string),
    source (string),
    source_flag ("PRIMARY" | "SECONDARY" | "ANALOGOUS" | "INFERRED")
- contradictions: array of strings (one per detected contradiction)
- options: array of objects with option_id, label, pros[], cons[], optional confidence (0.0-1.0)
- verdict_hint: short string summarizing the pass result
- raw_evidence_summary: 50-2000 char narrative of what you found

If no direct domain literature exists, set source_flag="ANALOGOUS" and explain in raw_evidence_summary.
"""


def jsuf(pass_type: str, next_id: str) -> str:
    return JSON_SUFFIX_TEMPLATE.format(pass_type=pass_type, next_id=next_id)


# ---------------------------------------------------------------------------
# ADVISORY MODE — Phases 1-3 of the 2026-05-20 rewrite
# Activated by --perplexity-advisory-runner CLI flag (default: legacy JSON mode).
# Goal: match /research-perplexity's natural-language advisory output rather
# than forcing rigid JSON. Research mode is engaged at the subprocess level
# (--perplexity-mode research); the JSON_SUFFIX_TEMPLATE was suppressing its
# natural multi-section synthesis. Advisory mode unblocks it.
# ---------------------------------------------------------------------------

# Module-level flag set by main() from --perplexity-advisory-runner
ADVISORY_MODE: bool = False

ADVISORY_RESPONSE_TEMPLATE = """\

---
RESPONSE FORMAT — REQUIRED:
Respond as a development strategy advisor using these eight section headers in this exact order.
Each header must appear on its own line, prefixed with `## ` (markdown H2). Use natural prose,
tables where useful, numbered lists for the actionable sections. Do NOT use JSON.

## CURRENT STATE
What has been accomplished based on the artifact + prior passes (1 paragraph max).

## PROGRESS VS PLAN
How the artifact aligns with its stated plan/goals (1 paragraph; use a small table if helpful).

## SCRUTINY
This pass's focus question, answered head-on. Multiple numbered sub-answers if the focus has
multiple parts. This is where the meat of the analysis goes for this pass type — {focus_label}

## IMMEDIATE NEXT STEPS
3-5 numbered, concrete actions in priority order. Each MUST include specific file paths and
code/command changes (not abstract advice). Format each as:
**N. Title (estimated time)** -- what to do + which files/commands.

## BLOCKERS
Numbered list of any issues blocking the immediate next steps. Each entry: one sentence
description + which step(s) it blocks + concrete unblock action.

## TECHNICAL DEBT
Items worth fixing soon but not blocking. Bulleted list.

## STRATEGIC RECOMMENDATIONS
Longer-term suggestions for direction. 1-2 paragraphs.

## RISKS & MITIGATIONS
Markdown table: | Risk | Likelihood | Impact | Mitigation |

## CODEBASE FIT
How the recommendations integrate with the existing code structure described in the artifact.

---

After the eight sections, end with TWO MANDATORY closing lines:

NEXT PASS RECOMMENDATION: <one short sentence -- what should the next pass investigate, and which finding/blocker it should target. If no further investigation is warranted, write "TERMINATE: convergence reached."> {pass_type_hint}

VERDICT: <one of: CONVERGED | NEEDS_MORE_PASSES | NEEDS_ADVERSARIAL | TERMINATE_ON_STRUCTURAL_LIMIT> -- your honest assessment of whether further passes will add new substantive findings or whether the analysis has reached saturation given this artifact's complexity.
"""


# Pass-type-specific focus labels and hint text.
ADVISORY_PASS_FOCUS: dict[str, dict[str, str]] = {
    "DECOMPOSE": {
        "label": (
            "this is the first pass -- read the artifact and identify its phases, the central "
            "question of each phase, and any obvious gaps that emerge from a first read. Treat "
            "SCRUTINY as the phase-by-phase breakdown and emit one numbered point per phase "
            "with line ranges."
        ),
        "pass_hint": "TARGETED_PROBE on the highest-risk phase identified.",
    },
    "CRITIQUE": {
        "label": (
            "search current literature/best-practice for gaps in the artifact's claims. SCRUTINY "
            "lists numbered claims you found weak with their evidence. Treat NEXT STEPS as 'what "
            "to investigate further', BLOCKERS as 'what cannot proceed until resolved'."
        ),
        "pass_hint": "ADVERSARIAL -- adopt a hostile-expert lens to attack the weakest claims.",
    },
    "ADVERSARIAL": {
        "label": (
            "you are a hostile expert reviewer. Mount the strongest possible attacks against the "
            "artifact's claims. SCRUTINY enumerates attacks; STRATEGIC RECOMMENDATIONS suggests "
            "what would need to be true to defeat each attack."
        ),
        "pass_hint": "OPTIONS_SWEEP or TARGETED_PROBE on the most damaging attack.",
    },
    "OPTIONS_SWEEP": {
        "label": (
            "enumerate 3-6 distinct viable solution paths for the central problem. Each option "
            "as a numbered SCRUTINY entry with pros, cons, when-to-pick, and confidence."
        ),
        "pass_hint": "TARGETED_PROBE on the highest-scoring option to stress-test it.",
    },
    "TARGETED_PROBE": {
        "label": (
            "your job is to either confirm or disprove the specific target finding given to you. "
            "SCRUTINY answers the targeted question with evidence; NEXT STEPS proposes the next "
            "concrete action; BLOCKERS lists what remains unresolved on this finding."
        ),
        "pass_hint": "TARGETED_PROBE on next highest-severity open finding, or BLUEPRINT if probe resolves to a design need.",
    },
    "BLUEPRINT": {
        "label": (
            "produce a complete architectural blueprint for the named problem. SCRUTINY contains "
            "component layout, data flow, failure modes, 2-3 alternative architectures; "
            "IMMEDIATE NEXT STEPS contains implementation step ordering."
        ),
        "pass_hint": "INTEGRATION if other findings depend on this blueprint, else TARGETED_PROBE.",
    },
    "GUIDANCE": {
        "label": (
            "you are a strategic routing advisor. SCRUTINY contains 2-4 ranked research routes "
            "for the stated blocker, ordered by expected information gain per cost. No deep "
            "evidence -- your job is to point at the highest-ROI next investigation."
        ),
        "pass_hint": "Whichever route ranked #1 in SCRUTINY (specify the pass type).",
    },
    "EXPLORATORY_BRANCH": {
        "label": (
            "branch outward from an interesting/anomalous prior finding into adjacent territory "
            "the prior passes did not investigate. SCRUTINY contains net-new findings with their "
            "rationale and evidence."
        ),
        "pass_hint": "TARGETED_PROBE on the strongest net-new finding, or ADVERSARIAL if the branch revealed a weakness.",
    },
    "POSTMORTEM": {
        "label": (
            "compare the artifact against real-world failure cases / postmortems in the relevant "
            "domain. If no direct postmortem literature exists, switch to ANALOGOUS domain and "
            "explicitly tag it in SCRUTINY. Findings here are warnings; NEXT STEPS are "
            "defensive improvements."
        ),
        "pass_hint": "INTEGRATION -- synthesize postmortem warnings with prior findings.",
    },
    "FRESH_OBSERVER": {
        "label": (
            "you are a fresh observer with NO knowledge of prior passes. Read the artifact "
            "summary + finding titles only. SCRUTINY identifies only what is missing from the "
            "analysis so far. If nothing new emerges, mark VERDICT: CONVERGED and explain why."
        ),
        "pass_hint": "TARGETED_PROBE on any novel finding found, or TERMINATE if nothing new.",
    },
    "INTEGRATION": {
        "label": (
            "cross-phase synthesis. SCRUTINY identifies seams that break when phases combine; "
            "STRATEGIC RECOMMENDATIONS describes the cross-phase resolutions; verdict prepares "
            "the final pass."
        ),
        "pass_hint": "FINAL_VERDICT.",
    },
    "FINAL_VERDICT": {
        "label": (
            "this is the terminal pass. SCRUTINY contains per-phase verdict "
            "(CONFIRMED/REFUTED/INCONCLUSIVE/STRUCTURAL-UNRESOLVABLE) and ranked options for any "
            "HIGH findings. The full Risks table consolidates every unresolved risk."
        ),
        "pass_hint": "TERMINATE: this is the final pass.",
    },
}


def advsuf(pass_type: str) -> str:
    """Return the advisory-mode response template, parameterized by pass type."""
    meta = ADVISORY_PASS_FOCUS.get(pass_type, {
        "label": f"address the central concern raised by this {pass_type} pass.",
        "pass_hint": "Whichever investigation would close the most uncertainty next.",
    })
    return ADVISORY_RESPONSE_TEMPLATE.format(
        focus_label=meta["label"],
        pass_type_hint=f"(suggested: {meta['pass_hint']})",
    )


def _suf(pass_type: str, next_id: str) -> str:
    """Dispatcher: returns advisory template under ADVISORY_MODE, else JSON suffix."""
    if ADVISORY_MODE:
        return advsuf(pass_type)
    return jsuf(pass_type, next_id)


def _parse_response(raw: str, pass_type: str, *, next_id_seed: str = "F001") -> dict | None:
    """Mode dispatcher: parse advisory prose under ADVISORY_MODE, else legacy JSON.

    Both paths return the same legacy-shaped dict (pass_type, findings, contradictions,
    options, verdict_hint, raw_evidence_summary, optionally recommended_next_pass) so
    every downstream consumer (validate_response, upsert_findings, check_convergence,
    write_report, running brief) works without per-mode branches.
    """
    if not raw:
        return None
    if ADVISORY_MODE:
        parsed = parse_advisory_response(raw)
        if parsed is None:
            return None
        return _advisory_to_legacy_shape(parsed, pass_type, next_id_seed)
    return extract_json(raw)


# Markdown section parser for advisory-mode responses
ADVISORY_SECTION_HEADERS = [
    "CURRENT STATE",
    "PROGRESS VS PLAN",
    "SCRUTINY",
    "IMMEDIATE NEXT STEPS",
    "BLOCKERS",
    "TECHNICAL DEBT",
    "STRATEGIC RECOMMENDATIONS",
    "RISKS & MITIGATIONS",
    "CODEBASE FIT",
]


def parse_advisory_response(text: str) -> dict | None:
    """Extract the 8 sections + NEXT PASS RECOMMENDATION + VERDICT from advisory prose.

    Returns a dict shaped like the JSON-mode parsed response (so the orchestration logic
    doesn't need to know which mode produced it). Sections are stored verbatim as strings.
    Pseudo-findings are synthesized from numbered items in IMMEDIATE NEXT STEPS + BLOCKERS
    so existing finding-targeting logic still works.

    Returns None if the response is empty / lacks the expected section structure.
    """
    if not text or not text.strip():
        return None

    # Strip council_browser wrapper if present (echoes prompt under **Query:**)
    body = text
    query_marker = "**Query:**"
    if query_marker in body:
        body = body.split(query_marker, 1)[-1]
        next_double = body.find("\n## ")
        body = body[next_double:] if next_double >= 0 else body
    cache_marker = "\n---\nCache:"
    if cache_marker in body:
        body = body.split(cache_marker, 1)[0]

    # PATCH 2026-05-20 (Perplexity audit smoke #2 finding):
    # Perplexity often outputs the response sections using BARE header names
    # (no `## ` prefix) even when the prompt template uses `## HEADER`. Result:
    # the prompt echo contains `## CURRENT STATE` etc. (template descriptions),
    # and the model's actual response has plain `CURRENT STATE` on its own line.
    # We must match BOTH forms and prefer the LAST occurrence of each known
    # header so we extract the response prose, not the prompt template echo.
    sections: dict[str, str] = {}
    # Build a per-line list of (line_index, char_start, char_end, matched_header).
    # A "header line" is either `## HEADER...` or a bare known-header line with
    # nothing else on it (case-insensitive, allowing trailing colon/whitespace).
    header_set = {h.upper(): h for h in ADVISORY_SECTION_HEADERS}
    lines = body.splitlines(keepends=True)
    line_offsets = [0]
    for ln in lines[:-1]:
        line_offsets.append(line_offsets[-1] + len(ln))
    header_hits: list[tuple[int, int, str]] = []  # (line_index, char_offset, header)
    for li, ln in enumerate(lines):
        stripped = ln.strip()
        # Tolerate trailing punctuation: "CODEBASE FIT", "CODEBASE FIT:", "**CODEBASE FIT**"
        candidate = stripped.lstrip("#").strip().rstrip(":").strip()
        candidate = candidate.strip("*").strip()
        if not candidate or len(candidate) > 60:
            continue
        up = candidate.upper()
        if up in header_set:
            header_hits.append((li, line_offsets[li], header_set[up]))
    # Group by header, take LAST hit (response, not template).
    last_by_header: dict[str, tuple[int, int]] = {}  # header -> (line_index, char_offset)
    for li, off, h in header_hits:
        last_by_header[h] = (li, off)
    # For each chosen hit, slice body from end-of-header-line to the next
    # header-line (regardless of which header) so sections terminate properly.
    sorted_hits = sorted(header_hits, key=lambda x: x[1])  # by char offset
    for h, (li, off) in last_by_header.items():
        end_of_header_line = off + len(lines[li])
        # Find next header hit AFTER this one (any header).
        next_off = len(body)
        for ohli, ooff, _ohh in sorted_hits:
            if ooff > off:
                next_off = ooff
                break
        section_body = body[end_of_header_line:next_off].strip()
        sections[h] = section_body

    if not sections:
        # No advisory-style headers found -- not advisory format
        return None

    # Extract closing NEXT PASS RECOMMENDATION + VERDICT lines (search whole body)
    next_pass_rec = ""
    verdict_signal = ""
    for line in body.splitlines():
        s = line.strip()
        if s.upper().startswith("NEXT PASS RECOMMENDATION:"):
            next_pass_rec = s.split(":", 1)[1].strip() if ":" in s else s
        elif s.upper().startswith("VERDICT:"):
            v = s.split(":", 1)[1].strip() if ":" in s else ""
            # Take just the first token (CONVERGED / NEEDS_MORE_PASSES / etc.)
            verdict_signal = v.split()[0] if v else ""

    return {
        "advisory_mode": True,
        "sections": sections,
        "next_pass_recommendation_text": next_pass_rec,
        "verdict_signal": verdict_signal.upper(),
        "raw_response": text,
    }


def _synthesize_findings_from_advisory(parsed: dict, pass_type: str, next_id_seed: str) -> list[dict]:
    """Synthesize pseudo-findings from advisory sections so orchestration logic works.

    Maps numbered IMMEDIATE NEXT STEPS + BLOCKERS items into findings with severity HIGH/MEDIUM.
    Risk-table rows become MEDIUM findings. This keeps select_next_pass_type/findings_history
    semantics intact under advisory mode without rewriting the entire orchestrator.
    """
    sections = parsed.get("sections", {})
    findings: list[dict] = []

    # Parse next_id_seed (e.g., "F003") into prefix + counter
    m = re.match(r"^F(\d+)$", next_id_seed)
    counter = int(m.group(1)) if m else 1
    width = max(3, len(m.group(1)) if m else 3)

    def next_id() -> str:
        nonlocal counter
        i = counter
        counter += 1
        return f"F{i:0{width}d}"

    _PLACEHOLDER_RE = re.compile(
        r"^\s*(none(\s+(at\s+this\s+time|identified|found|known|reported))?|"
        r"n/?a|no(ne)?\s+(blockers?|issues?|risks?|debt|recommendations?|items?|steps?)|"
        r"nothing\s+to\s+report|no\s+items?|tbd|—|-)\s*\.?\s*$",
        re.IGNORECASE,
    )

    def _is_placeholder(text: str) -> bool:
        first = text.splitlines()[0].strip() if text else ""
        return bool(_PLACEHOLDER_RE.match(first))

    def split_numbered(text: str) -> list[str]:
        """Split a section body into numbered items (1. ... 2. ... etc.)."""
        items: list[str] = []
        # Match top-level numbered items at the start of a line
        pattern = re.compile(r"^\s*(?:\*\*)?(\d+)[\.\)]\s+(.+?)(?=^\s*(?:\*\*)?\d+[\.\)]\s+|\Z)",
                             re.MULTILINE | re.DOTALL)
        for m in pattern.finditer(text):
            chunk = m.group(2).strip()
            if chunk and not _is_placeholder(chunk):
                items.append(chunk[:600])
        return items

    # BLOCKERS -> HIGH severity findings
    for body in split_numbered(sections.get("BLOCKERS", "")):
        title = body.splitlines()[0][:140] if body else "Unspecified blocker"
        findings.append({
            "id": next_id(),
            "severity": "HIGH",
            "claim": body[:800],
            "phase": pass_type,
            "source": "advisory-mode-BLOCKERS",
            "source_flag": "INFERRED",
            "status": "OPEN",
            "title": title,
        })

    # IMMEDIATE NEXT STEPS -> MEDIUM severity findings (actionable but not blocking)
    for body in split_numbered(sections.get("IMMEDIATE NEXT STEPS", "")):
        title = body.splitlines()[0][:140] if body else "Unspecified next step"
        findings.append({
            "id": next_id(),
            "severity": "MEDIUM",
            "claim": body[:800],
            "phase": pass_type,
            "source": "advisory-mode-IMMEDIATE_NEXT_STEPS",
            "source_flag": "INFERRED",
            "status": "OPEN",
            "title": title,
        })

    return findings


def _advisory_to_legacy_shape(parsed: dict, pass_type: str, next_id_seed: str) -> dict:
    """Convert advisory parse output into the legacy JSON-shape dict the runner expects.

    This is the compatibility shim that lets existing orchestration code (validate_response,
    upsert_findings, check_convergence, write_report) work unchanged under advisory mode.
    """
    sections = parsed.get("sections", {})
    findings = _synthesize_findings_from_advisory(parsed, pass_type, next_id_seed)
    # Risk table rows -> contradictions (loose mapping; consumers just want a list of strings)
    contradictions: list[str] = []
    risks_text = sections.get("RISKS & MITIGATIONS", "")
    for line in risks_text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        # Skip header row, separator row, and rows where the first cell is just dashes
        if "Risk" in s or "----" in s or "---" in s:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        # Need at least 2 cells, first must not be empty/dashes-only
        if len(cells) >= 2 and cells[0] and not re.match(r"^[-:\s]+$", cells[0]):
            contradictions.append(cells[0][:280])

    # Synthesize a verdict_hint string from the VERDICT signal
    verdict_signal = parsed.get("verdict_signal", "")
    verdict_hint_map = {
        "CONVERGED": "CONVERGENCE_LIKELY",
        "NEEDS_MORE_PASSES": "NEEDS_TARGETED_PROBE",
        "NEEDS_ADVERSARIAL": "NEEDS_ADVERSARIAL",
        "TERMINATE_ON_STRUCTURAL_LIMIT": "STRUCTURAL_LIMIT",
    }
    verdict_hint = verdict_hint_map.get(verdict_signal, "ADVISORY_PASS_COMPLETE")

    # Build a raw_evidence_summary from the prose sections — first ~1500 chars of SCRUTINY
    raw_evidence = (sections.get("SCRUTINY") or sections.get("CURRENT STATE") or "")[:1500]

    # Parse the NEXT PASS RECOMMENDATION text into a structured-ish dict
    rec_text = parsed.get("next_pass_recommendation_text", "")
    recommended = None
    if rec_text and not rec_text.upper().startswith("TERMINATE"):
        # Try to detect pass type token
        rec_pass_type = None
        for pt in ADVISORY_PASS_FOCUS.keys():
            if pt in rec_text.upper():
                rec_pass_type = pt
                break
        if rec_pass_type:
            recommended = {
                "pass_type": rec_pass_type,
                "target_finding_id": None,
                "question": rec_text[:1000],
                "rationale": "advisory-mode parsed from NEXT PASS RECOMMENDATION line",
            }

    # PATCH 2026-05-20 (Perplexity audit re-smoke #2 finding):
    # Schema requires `recommended_next_pass` to be an object when present, but
    # FINAL_VERDICT (terminal pass) legitimately has no next pass to recommend,
    # and any pass whose model response writes "TERMINATE: convergence reached"
    # produces `recommended = None`. The schema field is OPTIONAL (not in
    # `required`), so omit the key entirely when there is no real recommendation.
    out = {
        "pass_type": pass_type,
        "findings": findings,
        "contradictions": contradictions,
        "options": [],  # advisory mode does not emit structured options; see SCRUTINY prose instead
        "verdict_hint": verdict_hint,
        "raw_evidence_summary": raw_evidence,
        "_advisory": parsed,  # preserve original for report generation + debugging
    }
    if recommended is not None:
        out["recommended_next_pass"] = recommended
    return out


def _render_brief_block(running_brief: str) -> str:
    """Render the RUNNING BRIEF section that prefixes every non-bootstrap prompt."""
    body = (running_brief or "").strip() or "(no prior passes — this is the first pass.)"
    return (
        "RUNNING BRIEF (synthesis of prior passes — read carefully before answering;\n"
        "this is the conversation so far, and your job is to extend it, not restart it):\n"
        f"{body}\n"
    )


def _render_next_pass_instruction() -> str:
    """Closing instruction asking Perplexity to recommend the highest-value next pass.

    Appended to every builder except DECOMPOSE (first pass — no recommendation needed) and
    FINAL_VERDICT (terminal pass — nothing comes after). The runner reads this back from
    the response and uses it to steer the next iteration.
    """
    return (
        "\n\nALSO populate the optional top-level field `recommended_next_pass` with the\n"
        "single highest-value next investigation given what you just found and the running\n"
        "brief. This is how you steer the next iteration of research.\n"
        "Schema:\n"
        "{\n"
        '  "pass_type": one of "TARGETED_PROBE" | "EXPLORATORY_BRANCH" | "BLUEPRINT" |\n'
        '               "GUIDANCE" | "ADVERSARIAL" | "INTEGRATION" | "CRITIQUE",\n'
        '  "target_finding_id": "<finding id (e.g. F003) or null if no specific finding>",\n'
        '  "question": "<the specific research question to ask next — 10-1000 chars>",\n'
        '  "rationale": "<10-500 chars on why this is the highest-value next step>"\n'
        "}\n"
        "Choose based on the evidence:\n"
        "- TARGETED_PROBE: deepen evidence on one specific open finding\n"
        "- EXPLORATORY_BRANCH: a finding suggests adjacent areas we haven't investigated\n"
        "- BLUEPRINT: we know the problem; we need a complete architectural design / blueprint\n"
        "- GUIDANCE: we're stuck or have contradictions; ask for guidance on which route to pursue\n"
        "- ADVERSARIAL: claims need hostile stress-testing\n"
        "- CRITIQUE: artifact has new sections worth peer-reviewing\n"
        "- INTEGRATION: convergence is near; cross-phase synthesis is the next valuable step\n"
        "Be specific. A good `question` reads like a real research prompt, not a generic next-step."
    )


def build_prompt_decompose(artifact_body: str, next_id: str) -> str:
    # 2026-05-27: dropped DECOMPOSE multiplier from 2x to 1x. With the cap at
    # 3500 tokens (~14 KB), 2x = 7000 tokens (~28 KB) puts DECOMPOSE back over
    # the Perplexity browser-UI synthesis-render cliff (~20 KB), nullifying the
    # cap entirely. If DECOMPOSE genuinely needs more context, the operator
    # must precompress the artifact before running.
    full = truncate_to_token_budget(artifact_body, ARTIFACT_TEXT_TOKEN_CAP)
    return f"""You are a research decomposition engine. Break the following artifact into 2-6 distinct, independently-researchable phases.

This is Pass 1 — the bootstrap pass. Subsequent passes will build on what you decompose here.

ARTIFACT:
{full}

TASK:
1. Identify 2-6 core phases (research domains, sub-systems, or logical sections). Each phase must be specific enough that a targeted research query could verify it independently.
2. For each phase, also output line_start and line_end (1-indexed line numbers in the artifact body above) so the orchestrator can extract just that slice for TARGETED_PROBE passes.
3. For each phase, produce one HIGH-severity finding naming the central unresolved question or empirical gap.
4. Note any immediately-visible internal contradictions in the artifact.

Output the schema's phases[] array with {{label, line_start, line_end}} entries (in addition to findings[]).
Set verdict_hint to "DECOMPOSE_OK" if 2+ phases found.
{_suf("DECOMPOSE", next_id)}"""


def build_prompt_critique(artifact_body: str, prior_findings: list[dict], next_id: str, *, running_brief: str = "") -> str:
    full = truncate_to_token_budget(artifact_body, ARTIFACT_TEXT_TOKEN_CAP)
    pf_json = json.dumps([{"id": f["id"], "claim": f["claim"], "phase": f["phase"]} for f in prior_findings], indent=2)
    return f"""You are a rigorous peer reviewer with web-search access.

{_render_brief_block(running_brief)}
ARTIFACT:
{full}

PRIOR FINDINGS (from DECOMPOSE):
{pf_json}

TASK:
1. For each prior finding, search literature for supporting AND contradicting evidence.
2. Identify logical gaps, unsupported assumptions, missing citations in the artifact.
3. Severity: HIGH if a gap invalidates a core claim, MEDIUM if it weakens, LOW if tangential.
4. Populate contradictions[] with specific clashes between artifact claims and external evidence.
5. verdict_hint: "NEEDS_ADVERSARIAL"

Do not speculate. Use source_flag="ANALOGOUS" when only loosely-related literature exists.
{_render_next_pass_instruction()}
{_suf("CRITIQUE", next_id)}"""


def build_prompt_adversarial(artifact_body: str, prior_findings: list[dict], next_id: str, *, running_brief: str = "") -> str:
    full = truncate_to_token_budget(artifact_body, ARTIFACT_TEXT_TOKEN_CAP)
    pf_json = json.dumps([{"id": f["id"], "claim": f["claim"], "severity": f["severity"]} for f in prior_findings], indent=2)
    return f"""You are a hostile expert trying to invalidate the artifact's conclusions. Build the STRONGEST possible case against it. Not balanced — adversarial.

{_render_brief_block(running_brief)}
ARTIFACT:
{full}

ALL PRIOR FINDINGS:
{pf_json}

TASK:
1. Mount the 3 strongest possible attacks against the artifact's core claims. Each attack = one HIGH-severity finding.
2. Provide the specific evidence or logical principle behind each attack.
3. For prior findings unresolved over multiple passes, note structural_unresolvable=true in the finding object.
4. Populate contradictions[] with claims your adversarial evidence directly undermines.
5. Be maximally hostile but evidence-grounded. Do NOT fabricate sources.
6. verdict_hint: "ADVERSARIAL_COMPLETE"

This pass is required for AND-gate convergence (adversarial_count >= ceil(N/2)).
{_render_next_pass_instruction()}
{_suf("ADVERSARIAL", next_id)}"""


def build_prompt_options_sweep(artifact_body: str, findings: list[dict], next_id: str, *, running_brief: str = "") -> str:
    full = truncate_to_token_budget(artifact_body, ARTIFACT_TEXT_TOKEN_CAP)
    unresolved = [f for f in findings if f.get("status") == "OPEN" and f.get("severity") in ("HIGH", "MEDIUM")]
    f_json = json.dumps([{"id": f["id"], "claim": f["claim"]} for f in unresolved], indent=2)
    return f"""You are an options analyst. Enumerate distinct solution paths for the artifact's central decisions.

{_render_brief_block(running_brief)}
ARTIFACT:
{full}

UNRESOLVED FINDINGS:
{f_json}

TASK:
1. Enumerate 3-6 distinct, meaningfully-different options. No overlap. "Do nothing" is valid if genuinely viable.
2. For each option: option_id, label, pros[] (2-4), cons[] (2-4), confidence (0.0-1.0).
3. ANALOGOUS-sourced options cap at confidence=0.6.
4. Note option-specific HIGH-severity risks in findings[].
5. verdict_hint: "OPTIONS_READY"
{_render_next_pass_instruction()}
{_suf("OPTIONS_SWEEP", next_id)}"""


def build_prompt_targeted_probe(target_finding: dict, phase_slice: str, next_id: str, *, running_brief: str = "") -> str:
    tf_json = json.dumps(target_finding, indent=2)
    return f"""You are a precision investigator. ONE finding needs deep verification.

{_render_brief_block(running_brief)}
TARGET FINDING:
{tf_json}

ARTIFACT SLICE (the relevant phase only):
{phase_slice}

TASK:
1. Focus entirely on the target finding. Search for 3-5 independent sources confirming or refuting its claim.
2. Produce ONE finding with id="{target_finding['id']}" (same as target — this is an UPDATE).
3. Update severity based on evidence: confirmed by 2+ PRIMARY sources -> stays or lowers; refuted -> severity=LOW with refutation in claim text; evidence genuinely absent -> source_flag="INFERRED", severity=MEDIUM.
4. verdict_hint: "PROBE_RESOLVED" if claim addressed, "NEEDS_ADVERSARIAL" if opens new questions.

One finding. One target. No scope creep.
{_render_next_pass_instruction()}
{_suf("TARGETED_PROBE", next_id)}"""


def build_prompt_fresh_observer(fresh_summary: str, finding_titles: list[str], next_id: str) -> str:
    # FRESH_OBSERVER intentionally does NOT receive the running brief — its purpose
    # is to provide an unanchored read. It also does not emit recommended_next_pass
    # for the same reason: the orchestrator chooses what comes after based on whether
    # the fresh observer found anything net-new.
    titles_str = "\n".join(f"- {t}" for t in finding_titles) if finding_titles else "(none yet)"
    return f"""You are a FRESH reviewer who has NOT seen the prior research conversation. You receive only this summary + the titles of issues already raised.

ARTIFACT SUMMARY (Claude-generated, frozen):
{fresh_summary}

FINDING TITLES ALREADY RAISED (do NOT re-raise these):
{titles_str}

TASK:
1. Read only the summary and titles. Do not reconstruct the prior thread.
2. Identify NET-NEW findings — issues NOT in the titles list.
3. CRITICAL: if you find yourself about to re-raise a listed finding, OMIT it and add its title to fresh_observer_re_raises[].
4. If you find nothing genuinely new, return findings=[] and explain in raw_evidence_summary.
5. verdict_hint: "CONVERGENCE_LIKELY" if nothing new; "NEEDS_TARGETED_PROBE" if novel HIGH found.

Trust your independent read — your value is exactly that you are NOT anchored.
{_suf("FRESH_OBSERVER", next_id)}"""


def build_prompt_postmortem(domain: str, all_findings: list[dict], next_id: str, *, running_brief: str = "") -> str:
    unresolved = [f for f in all_findings if f.get("status") == "OPEN" and f.get("severity") in ("HIGH", "MEDIUM")]
    uf_json = json.dumps([{"id": f["id"], "claim": f["claim"]} for f in unresolved], indent=2)
    return f"""You are a domain postmortem analyst.

{_render_brief_block(running_brief)}
ARTIFACT DOMAIN: {domain}

UNRESOLVED FINDINGS:
{uf_json}

TASK:
1. Search for documented public postmortems in this exact domain.
2. If found: cite specific papers/reports/case studies. Produce findings tagged with source_flag="PRIMARY" or "SECONDARY".
3. If no direct postmortem literature exists: set verdict_hint="DOMAIN-POSTMORTEM-UNAVAILABLE" and explain in domain_postmortem_note: (a) why direct postmortem is impossible, (b) which analogous domain you substitute, (c) confidence penalty applied. All findings in this case must have source_flag="ANALOGOUS".
4. Identify <=3 systematic research gaps that left HIGH findings unresolved.
5. Do not re-litigate resolved findings.

Postmortem findings are informational. They do not re-open the convergence gate.
{_render_next_pass_instruction()}
{_suf("POSTMORTEM", next_id)}"""


def build_prompt_integration(artifact_body: str, all_findings: list[dict], all_contradictions: list[str], next_id: str, *, running_brief: str = "") -> str:
    full = truncate_to_token_budget(artifact_body, ARTIFACT_TEXT_TOKEN_CAP)
    f_json = json.dumps([{"id": f["id"], "claim": f["claim"], "phase": f["phase"], "severity": f["severity"], "status": f.get("status", "OPEN")} for f in all_findings], indent=2)
    c_json = json.dumps(all_contradictions, indent=2)
    return f"""You are a synthesis engine. Integrate all findings into a coherent unified picture.

{_render_brief_block(running_brief)}
ALL FINDINGS:
{f_json}

CONTRADICTION LOG:
{c_json}

ARTIFACT:
{full}

TASK:
1. Group all HIGH/MED findings by phase. Per-phase synthesis (2-4 sentences each) in raw_evidence_summary.
2. Produce findings tagged structural_unresolvable=true for any HIGH unresolved across multiple passes.
3. For each contradiction pair, determine which side is better-supported; mark the weaker side LOW.
4. ANALOGOUS findings get lower-confidence weighting; do not treat as equivalent to PRIMARY.
5. Populate options[] if alternatives remain for unresolved items.
6. verdict_hint: "FINAL_VERDICT_READY" if <= 2 HIGH unresolved; else "NEEDS_ADVERSARIAL".

Be conservative — do not over-resolve.
{_render_next_pass_instruction()}
{_suf("INTEGRATION", next_id)}"""


def build_prompt_final_verdict(all_findings: list[dict], all_options: list[dict], adversarial_count: int, next_id: str, *, running_brief: str = "") -> str:
    # FINAL_VERDICT is the terminal pass — no recommended_next_pass needed.
    f_json = json.dumps([{"id": f["id"], "claim": f["claim"], "severity": f["severity"], "status": f.get("status", "OPEN"), "structural_unresolvable": f.get("structural_unresolvable", False)} for f in all_findings], indent=2)
    o_json = json.dumps(all_options, indent=2)
    return f"""You are the final adjudicator. You have all evidence. Produce a definitive verdict.

{_render_brief_block(running_brief)}
ALL FINDINGS:
{f_json}

OPTIONS ENUMERATED ACROSS RUN:
{o_json}

ADVERSARIAL PASSES COMPLETED: {adversarial_count}

TASK:
1. Per research phase, output a phase-verdict finding: CONFIRMED / REFUTED / INCONCLUSIVE / STRUCTURAL-UNRESOLVABLE.
2. For HIGH findings tagged structural_unresolvable=true, include with claim prefixed "STRUCTURAL-UNRESOLVABLE: ".
3. Populate options[] with the ranked final options (best to worst), including confidence scores. ANALOGOUS-sourced cap at 0.6.
4. raw_evidence_summary must include: total findings reviewed, adversarial_pass_count, convergence basis (which gate condition closed it).
5. verdict_hint: "FINAL_VERDICT_READY".

Terminal pass. No hedging. Name structural unresolvables clearly.
{_suf("FINAL_VERDICT", next_id)}"""


# ---------------------------------------------------------------------------
# Agentic pass types — Perplexity-recommended next moves
# ---------------------------------------------------------------------------
def build_prompt_blueprint(target_finding: dict | None, artifact_body: str, next_id: str, *, running_brief: str = "") -> str:
    """Ask Perplexity for a complete architectural blueprint addressing an open finding.

    Used when the running brief + findings make the *problem* clear and the next
    valuable step is a *design*: component layout, data flow, failure modes,
    alternative architectures, concrete implementation steps.
    """
    full = truncate_to_token_budget(artifact_body, ARTIFACT_TEXT_TOKEN_CAP)
    if target_finding:
        tf_json = json.dumps({"id": target_finding.get("id"), "claim": target_finding.get("claim"), "phase": target_finding.get("phase")}, indent=2)
        target_block = f"TARGET FINDING TO RESOLVE WITH A BLUEPRINT:\n{tf_json}\n"
    else:
        target_block = "TARGET: the artifact's central design question (no specific finding tagged).\n"
    return f"""You are a senior systems architect. You have been given enough context to design.

{_render_brief_block(running_brief)}
{target_block}
ARTIFACT (for grounding only — your blueprint should resolve the target, not re-describe the artifact):
{full}

TASK — produce a complete architectural blueprint:
1. **Component layout**: 4-8 named components, their responsibilities, the boundaries between them. Use a textual diagram (ASCII or labeled adjacency list).
2. **Data flow**: how data/control moves between components for the 2-3 most important paths.
3. **Failure modes**: at least 4 concrete failure modes and how the blueprint handles each. Tag any unhandled failure as a finding (severity HIGH).
4. **Alternatives**: enumerate 2-3 *meaningfully different* architectures (e.g., monolithic vs event-driven; pull vs push; sync vs async). For each, populate options[] with option_id, label, pros[], cons[], confidence.
5. **Implementation steps**: 5-10 ordered concrete steps a developer would follow to realize the recommended architecture.
6. Emit ONE INFO-severity finding with claim prefixed "BLUEPRINT: " summarizing the recommendation. Add field `blueprint=true` if the schema allows; otherwise note in claim text.
7. raw_evidence_summary: 200-1500 chars synthesizing the blueprint and why it resolves the target.
8. verdict_hint: "BLUEPRINT_READY".

Cite real prior art (papers, RFCs, postmortems) where it informs the design. Use source_flag="ANALOGOUS" if no domain-direct prior art exists.
{_render_next_pass_instruction()}
{_suf("BLUEPRINT", next_id)}"""


def build_prompt_guidance(blocker_summary: str, artifact_body: str, next_id: str, *, running_brief: str = "") -> str:
    """Ask Perplexity for *routing guidance*: which research direction to pursue next.

    Used when the brief contains contradictions, blockers, or genuinely-open strategic
    questions and the orchestrator wants Perplexity to advise on the best next move
    rather than commit to a specific probe.
    """
    full = truncate_to_token_budget(artifact_body, ARTIFACT_TEXT_TOKEN_CAP // 2)  # smaller — focus on direction not depth
    return f"""You are a research strategist. The team is uncertain which direction to pursue next.

{_render_brief_block(running_brief)}
CURRENT BLOCKER OR STRATEGIC QUESTION:
{blocker_summary}

ARTIFACT (compact reference):
{full}

TASK — give routing guidance, not direct evidence:
1. Identify 2-4 candidate research routes the team could pursue from here. Each route should be meaningfully different (different question, different domain, different methodology).
2. For each route, populate options[] with: option_id, label, pros[] (what we'd learn), cons[] (cost, time, risk of dead-end), confidence (0.0-1.0 — your confidence this route would resolve the blocker).
3. Rank the routes by *expected information gain per unit cost*.
4. Emit at most 1 finding — only if guidance reveals a HIGH-severity gap in the current investigation strategy itself (e.g., "team is anchored on the wrong axis").
5. raw_evidence_summary: 150-1000 chars explaining the strategic landscape and why route #1 is best.
6. verdict_hint: "GUIDANCE_ROUTES_READY".

Be specific. "Investigate X further" is not guidance — "Ask whether mechanism M can produce signal S under condition C, because if not, the whole finding F003 collapses" is guidance.
{_render_next_pass_instruction()}
{_suf("GUIDANCE", next_id)}"""


def build_prompt_exploratory_branch(anchor_finding: dict | None, artifact_body: str, next_id: str, *, running_brief: str = "") -> str:
    """Branch outward from an interesting/anomalous finding into adjacent territory.

    Used when a finding looks consequential and likely connects to research domains
    we haven't touched yet — generates net-new HIGH/MEDIUM findings in new phases.
    """
    full = truncate_to_token_budget(artifact_body, ARTIFACT_TEXT_TOKEN_CAP)
    if anchor_finding:
        af_json = json.dumps({"id": anchor_finding.get("id"), "claim": anchor_finding.get("claim"), "phase": anchor_finding.get("phase")}, indent=2)
        anchor_block = f"ANCHOR FINDING (branch outward from this):\n{af_json}\n"
    else:
        anchor_block = "ANCHOR: explore adjacent territory implied by the running brief as a whole.\n"
    return f"""You are an exploratory researcher. A finding has implications we haven't traced yet.

{_render_brief_block(running_brief)}
{anchor_block}
ARTIFACT:
{full}

TASK — branch outward, do NOT re-investigate the anchor itself:
1. Identify 2-4 *adjacent research areas* this finding implies but the artifact does not cover. Adjacent = the finding makes new claims plausible or new risks visible.
2. Produce 2-4 net-new HIGH or MEDIUM findings in those adjacent areas. Each must have a distinct phase label — feel free to introduce phase labels that don't appear in ledger.phases (the orchestrator will extend its phase set).
3. For each new finding, cite the linkage: "F00X anchor implies … therefore investigate …".
4. Do NOT re-raise issues already in the running brief.
5. raw_evidence_summary: 150-1000 chars naming the adjacent territory and why these new findings matter.
6. verdict_hint: "EXPLORATION_OPENED" if 2+ net-new findings; "EXPLORATION_DRY" if none net-new.

Quality over coverage — 2 sharp new findings beat 4 dull ones.
{_render_next_pass_instruction()}
{_suf("EXPLORATORY_BRANCH", next_id)}"""


# ---------------------------------------------------------------------------
# Convergence gate
# ---------------------------------------------------------------------------
def check_convergence(ledger: dict) -> tuple[bool, str, bool]:
    """Returns (converged, reason, adversarial_deficit_blocking).

    adversarial_deficit_blocking is True iff conditions 1-3 are satisfied
    but adversarial_count is short — triggers forced-ADVERSARIAL injection.

    Advisory-mode override: if the most recent COMPLETED pass emitted
    VERDICT: CONVERGED (or TERMINATE_ON_STRUCTURAL_LIMIT) AND the safety
    rails are satisfied (min_passes floor, adversarial floor), honor it.
    """
    pass_log = ledger.get("pass_log", [])
    if len(pass_log) < CONVERGENCE_ZERO_FINDING_PASSES:
        return False, "too few passes", False

    n_phases = max(1, len(ledger.get("phases", [])))
    required_adversarial = max(1, math.ceil(n_phases / 2))
    adv_count = sum(1 for p in pass_log if p.get("pass_type") == "ADVERSARIAL" and p.get("status") == "COMPLETED")

    # ---- ADVISORY-MODE VERDICT-LINE OVERRIDE (Phase 3) ----
    # Trust the model when it says it has reached saturation, but only if the safety
    # rails are satisfied. Min-passes floor: at least 3 COMPLETED passes. Adversarial
    # floor: required_adversarial. If both met AND most recent VERDICT == CONVERGED
    # (or TERMINATE_ON_STRUCTURAL_LIMIT), converge.
    if ADVISORY_MODE:
        completed = [p for p in pass_log if p.get("status") == "COMPLETED"]
        if completed:
            last_verdict = (completed[-1].get("advisory_verdict") or "").upper()
            min_passes_floor = max(3, ledger.get("min_passes", 0))
            adv_ok = adv_count >= required_adversarial
            passes_ok = len(completed) >= min_passes_floor
            if last_verdict in ("CONVERGED", "TERMINATE_ON_STRUCTURAL_LIMIT"):
                if adv_ok and passes_ok:
                    return True, f"advisory VERDICT={last_verdict} (passes={len(completed)}, adv={adv_count}/{required_adversarial})", False
                if not adv_ok:
                    return False, f"advisory VERDICT={last_verdict} but adversarial deficit ({adv_count}/{required_adversarial})", True
                return False, f"advisory VERDICT={last_verdict} but min_passes floor not met ({len(completed)}/{min_passes_floor})", False
            # NEEDS_ADVERSARIAL hint -> let select_next_pass_type pick ADVERSARIAL via the forced path
            if last_verdict == "NEEDS_ADVERSARIAL" and not adv_ok:
                return False, f"advisory VERDICT=NEEDS_ADVERSARIAL ({adv_count}/{required_adversarial})", True
        # If advisory has no verdict yet, fall through to legacy AND-gate logic.


    # Last K passes' findings_count (net-new)
    recent_finding_counts = [p.get("net_new_findings", 0) for p in pass_log[-CONVERGENCE_ZERO_FINDING_PASSES:]]
    recent_contradiction_counts = [p.get("net_new_contradictions", 0) for p in pass_log[-CONVERGENCE_ZERO_CONTRADICTION_PASSES:]]

    cond_findings = len(recent_finding_counts) >= CONVERGENCE_ZERO_FINDING_PASSES and all(c == 0 for c in recent_finding_counts)
    cond_contradictions = len(recent_contradiction_counts) >= CONVERGENCE_ZERO_CONTRADICTION_PASSES and all(c == 0 for c in recent_contradiction_counts)

    # All open findings have effective_severity == LOW for last 2 passes
    open_findings = [f for f in ledger.get("findings", []) if f.get("status") == "OPEN"]
    all_low = all(effective_severity(f) == "LOW" for f in open_findings)
    # We approximate "for 2 consecutive passes" by requiring all_low NOW (last pass already wrote findings)
    cond_all_low = all_low

    cond_adversarial = adv_count >= required_adversarial

    if cond_findings and cond_contradictions and cond_all_low:
        if cond_adversarial:
            return True, f"AND-gate satisfied (adv={adv_count}/{required_adversarial})", False
        else:
            return False, f"adversarial deficit (have {adv_count}, need {required_adversarial})", True

    return False, f"zero-find streak={sum(1 for c in recent_finding_counts if c==0)}/{CONVERGENCE_ZERO_FINDING_PASSES}, all_low={all_low}, adv={adv_count}/{required_adversarial}", False


# ---------------------------------------------------------------------------
# Pass-type selector
# ---------------------------------------------------------------------------
def select_next_pass_type(ledger: dict, pass_num: int) -> tuple[str, dict | None]:
    """Pick the next pass type. Returns (pass_type, target_finding_or_None).

    Schedule rules:
    - Passes 1-4 fixed: DECOMPOSE / CRITIQUE / ADVERSARIAL / OPTIONS_SWEEP
    - FRESH_OBSERVER at scheduled positions (8, 14, 20, ...)
    - POSTMORTEM event-trigger: once, when all HIGH/MED have >= 1 TARGETED_PROBE
    - INTEGRATION reserved for second-to-last
    - FINAL_VERDICT reserved for last
    - Adversarial deficit at convergence -> forced ADVERSARIAL
    - Default: TARGETED_PROBE on highest-severity open finding never-probed-or-least-probed
    """
    if pass_num == 1:
        return "DECOMPOSE", None
    if pass_num == 2:
        return "CRITIQUE", None
    if pass_num == 3:
        return "ADVERSARIAL", None
    if pass_num == 4:
        return "OPTIONS_SWEEP", None

    # TODO(F-runner / 2026-05-27): FRESH_OBSERVER schedule rail fires here BEFORE the
    # reserved-final-pass checks below at lines ~1720-1725. When max_passes == 8 these
    # collide on pass_num == 8: FRESH_OBSERVER wins and FINAL_VERDICT never runs, so
    # report.md ships without per-finding scored options. Same risk at any max_passes
    # value that lands on the FRESH_OBSERVER schedule (8, 14, 20, ...). Fix: reorder so
    # the `max_passes and pass_num == max_passes` check returns FINAL_VERDICT BEFORE
    # this fresh_due block, OR add `and pass_num != max_passes` to the fresh_due guard.
    # Mitigation in /extended-research skill (commands/extended-research.md Risk Register
    # row "Runner-FRESH_OBSERVER-collision"): recommend --max-passes >= 9 for runs that
    # need FINAL_VERDICT. Surfaced by E2E run plan-add-plan-synthesis-tail-9c9ce0b2.
    # Fresh observer at scheduled positions
    fresh_due = pass_num == 8 or (pass_num > 8 and (pass_num - 8) % 6 == 0)
    if fresh_due and not _has_recent_pass(ledger, "FRESH_OBSERVER", lookback=2):
        return "FRESH_OBSERVER", None

    # Postmortem event-trigger
    if not _ledger_has_pass(ledger, "POSTMORTEM"):
        if _all_high_med_have_targeted_probe(ledger):
            return "POSTMORTEM", None

    # Check convergence — if blocked only by adversarial deficit, force ADVERSARIAL
    converged, reason, adv_deficit = check_convergence(ledger)
    if adv_deficit:
        return "ADVERSARIAL", None

    # Reserved final passes — only assign when we know max_passes
    max_passes = ledger.get("max_passes")
    if max_passes and pass_num == max_passes - 1:
        return "INTEGRATION", None
    if max_passes and pass_num == max_passes:
        return "FINAL_VERDICT", None

    # NEW (agentic): honor Perplexity's recommended_next_pass from the last completed pass
    # if it passes validation and a target finding (if specified) is still open.
    rec_type, rec_target = _resolve_recommendation(ledger)
    if rec_type:
        rationale = ""
        for p in reversed(ledger.get("pass_log", [])):
            rnp = p.get("recommended_next_pass")
            if rnp:
                rationale = (rnp.get("rationale") or "")[:80]
                break
        log(f"[AGENTIC] honoring Perplexity recommendation: {rec_type} — {rationale}")
        return rec_type, rec_target

    # Default fallback: TARGETED_PROBE on highest-priority open finding (deterministic).
    target = _pick_probe_target(ledger)
    if target is None:
        # Nothing left to probe — go straight to integration
        return "INTEGRATION", None
    return "TARGETED_PROBE", target


def _resolve_recommendation(ledger: dict) -> tuple[str | None, dict | None]:
    """Read recommended_next_pass from the most recent COMPLETED pass record.

    Returns (pass_type, target_finding_or_None) if a usable recommendation exists,
    else (None, None). Validates that:
      - The recommendation's pass_type is one we know how to dispatch.
      - If a target_finding_id is given, the finding still exists and is OPEN.
      - For TARGETED_PROBE with no/invalid target, falls back to _pick_probe_target.
    """
    allowed = {"TARGETED_PROBE", "EXPLORATORY_BRANCH", "BLUEPRINT", "GUIDANCE", "ADVERSARIAL", "INTEGRATION", "CRITIQUE"}
    rec = None
    for p in reversed(ledger.get("pass_log", [])):
        if p.get("status") != "COMPLETED":
            continue
        rnp = p.get("recommended_next_pass")
        if rnp:
            rec = rnp
            break
    if not rec:
        return None, None
    pt = rec.get("pass_type")
    if pt not in allowed:
        log(f"[AGENTIC] ignoring recommendation: invalid pass_type={pt!r}", "WARN")
        return None, None

    target = None
    tid = rec.get("target_finding_id")
    if tid:
        target = next((f for f in ledger.get("findings", []) if f.get("id") == tid and f.get("status") == "OPEN"), None)

    # TARGETED_PROBE always needs a target; fall back to deterministic picker if missing
    if pt == "TARGETED_PROBE" and target is None:
        target = _pick_probe_target(ledger)
        if target is None:
            # No open findings to probe — let caller fall through to INTEGRATION fallback
            return None, None

    return pt, target


def _has_recent_pass(ledger: dict, pass_type: str, lookback: int = 2) -> bool:
    pl = ledger.get("pass_log", [])[-lookback:]
    return any(p.get("pass_type") == pass_type for p in pl)


def _ledger_has_pass(ledger: dict, pass_type: str) -> bool:
    return any(p.get("pass_type") == pass_type and p.get("status") == "COMPLETED" for p in ledger.get("pass_log", []))


def _all_high_med_have_targeted_probe(ledger: dict) -> bool:
    open_hm = [f for f in ledger.get("findings", []) if f.get("status") == "OPEN" and f.get("severity") in ("HIGH", "MEDIUM")]
    if not open_hm:
        return True
    probed_ids = set()
    for p in ledger.get("pass_log", []):
        if p.get("pass_type") == "TARGETED_PROBE":
            probed_ids.add(p.get("target_id"))
    return all(f["id"] in probed_ids for f in open_hm)


def _pick_probe_target(ledger: dict) -> dict | None:
    """Pick the highest-severity OPEN finding with the fewest prior probes."""
    candidates = [f for f in ledger.get("findings", []) if f.get("status") == "OPEN"]
    if not candidates:
        return None
    probe_counts: dict[str, int] = {}
    for p in ledger.get("pass_log", []):
        if p.get("pass_type") == "TARGETED_PROBE":
            tid = p.get("target_id")
            if tid:
                probe_counts[tid] = probe_counts.get(tid, 0) + 1
    severity_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
    candidates.sort(key=lambda f: (severity_rank.get(f.get("severity"), 9), probe_counts.get(f["id"], 0)))
    return candidates[0]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
class InterruptedException(Exception):
    pass


_interrupted = False


def sigint_handler(signum, frame):
    global _interrupted
    _interrupted = True
    log("SIGINT received — finishing current pass then writing INTERRUPTED marker", "WARN")


# ---------------------------------------------------------------------------
# Running brief — narrative state that survives across passes and feeds every prompt
# ---------------------------------------------------------------------------
RUNNING_BRIEF_CHAR_CAP = 8000  # ~2000 tokens — leaves room for artifact body + JSON excerpts
RUNNING_BRIEF_MAX_ENTRIES = 12  # Last N pass entries retained; older entries collapsed into header


def _summarize_entry(pass_num: int, pass_type: str, parsed: dict, target: dict | None) -> str:
    """Build a 3-6 line markdown summary of one pass's outcome for the running brief."""
    headline = (parsed.get("raw_evidence_summary") or "").strip().split("\n")[0][:240]
    new_findings = parsed.get("findings") or []
    new_contradictions = parsed.get("contradictions") or []
    verdict = parsed.get("verdict_hint", "")
    rec = parsed.get("recommended_next_pass") or {}
    target_label = f" (target {target['id']})" if target and target.get("id") else ""
    finding_ids = [f.get("id", "?") for f in new_findings][:6]
    fids = ", ".join(finding_ids) if finding_ids else "none"
    cs = f"{len(new_contradictions)} new contradiction(s)" if new_contradictions else "no new contradictions"
    rec_line = ""
    if rec.get("pass_type") and rec.get("question"):
        rec_line = f"\n  - Recommended next: {rec['pass_type']} — {rec['question'][:160]}"
    return (
        f"### Pass {pass_num} — {pass_type}{target_label}\n"
        f"  - Verdict: {verdict or '(none)'}\n"
        f"  - Headline: {headline or '(no summary)'}\n"
        f"  - Findings emitted: {fids}; {cs}"
        f"{rec_line}\n"
    )


def update_running_brief(ledger: dict, workdir: Path, pass_num: int, pass_type: str, parsed: dict, target: dict | None) -> None:
    """Append a new pass entry to the running brief; cap size; mirror to disk.

    The brief is mirrored to {workdir}/running_brief.md so the user can tail it,
    and also stored in ledger['running_brief'] so prompt builders can pull it
    from a single in-memory source.
    """
    current = ledger.get("running_brief", "") or ""
    new_entry = _summarize_entry(pass_num, pass_type, parsed, target)

    # Split existing brief into entries (each starts with "### Pass ")
    if current:
        entries = [seg for seg in current.split("### Pass ") if seg.strip()]
        entries = ["### Pass " + seg if not seg.startswith("Pass ") else "### " + seg for seg in entries]
    else:
        entries = []
    entries.append(new_entry)

    # Keep only last N entries
    if len(entries) > RUNNING_BRIEF_MAX_ENTRIES:
        dropped = len(entries) - RUNNING_BRIEF_MAX_ENTRIES
        entries = entries[-RUNNING_BRIEF_MAX_ENTRIES:]
        # Prepend a synthesis note so context isn't silently lost
        header = f"_(Earlier {dropped} pass entries elided — see passes.jsonl for full history.)_\n\n"
        brief = header + "".join(entries)
    else:
        brief = "".join(entries)

    # Hard char cap as a final safety net
    if len(brief) > RUNNING_BRIEF_CHAR_CAP:
        brief = "_(Brief truncated to char cap.)_\n\n" + brief[-RUNNING_BRIEF_CHAR_CAP:]

    ledger["running_brief"] = brief

    # Mirror to disk
    try:
        (workdir / "running_brief.md").write_text(brief, encoding="utf-8")
    except OSError as e:
        log(f"running_brief.md write failed: {e}", "WARN")


def run_pass(ledger: dict, workdir: Path, pass_num: int, pass_type: str, target: dict | None, artifact_body: str, fresh_observer_summary: str) -> dict:
    """Execute one pass: build prompt, call research_query, parse, validate, merge."""
    # Heartbeat first (so status command sees activity)
    ledger["last_heartbeat_ts"] = datetime.now(timezone.utc).isoformat()
    ledger["current_pass_num"] = pass_num
    ledger["current_pass_type"] = pass_type
    write_ledger_atomic(ledger, workdir)

    next_id = next_finding_id(ledger)
    invocation_id = f"er-{ledger['slug'][:8]}-p{pass_num:02d}-{uuid.uuid4().hex[:6]}"

    running_brief = ledger.get("running_brief", "")

    if pass_type == "DECOMPOSE":
        prompt = build_prompt_decompose(artifact_body, next_id)
    elif pass_type == "CRITIQUE":
        prompt = build_prompt_critique(artifact_body, ledger.get("findings", []), next_id, running_brief=running_brief)
    elif pass_type == "ADVERSARIAL":
        prompt = build_prompt_adversarial(artifact_body, ledger.get("findings", []), next_id, running_brief=running_brief)
    elif pass_type == "OPTIONS_SWEEP":
        prompt = build_prompt_options_sweep(artifact_body, ledger.get("findings", []), next_id, running_brief=running_brief)
    elif pass_type == "TARGETED_PROBE":
        assert target is not None
        # Look up phase line range for the target's phase
        phase_label = target.get("phase", "")
        phase_meta = next((p for p in ledger.get("phases", []) if p.get("label") == phase_label), None)
        line_start = phase_meta.get("line_start") if phase_meta else None
        line_end = phase_meta.get("line_end") if phase_meta else None
        slice_text = slice_artifact_by_phase(artifact_body, line_start, line_end)
        prompt = build_prompt_targeted_probe(target, slice_text, next_id, running_brief=running_brief)
    elif pass_type == "FRESH_OBSERVER":
        titles = [f["claim"][:80] for f in ledger.get("findings", [])]
        prompt = build_prompt_fresh_observer(fresh_observer_summary, titles, next_id)
    elif pass_type == "POSTMORTEM":
        domain = ledger.get("domain", "general software engineering")
        prompt = build_prompt_postmortem(domain, ledger.get("findings", []), next_id, running_brief=running_brief)
    elif pass_type == "INTEGRATION":
        prompt = build_prompt_integration(artifact_body, ledger.get("findings", []), ledger.get("contradictions", []), next_id, running_brief=running_brief)
    elif pass_type == "FINAL_VERDICT":
        adv = sum(1 for p in ledger.get("pass_log", []) if p.get("pass_type") == "ADVERSARIAL" and p.get("status") == "COMPLETED")
        all_opts = []
        for p in ledger.get("pass_log", []):
            for o in p.get("options_emitted", []):
                all_opts.append(o)
        prompt = build_prompt_final_verdict(ledger.get("findings", []), all_opts, adv, next_id, running_brief=running_brief)
    elif pass_type == "BLUEPRINT":
        # target may be None — blueprint can address the artifact's central question directly
        prompt = build_prompt_blueprint(target, artifact_body, next_id, running_brief=running_brief)
    elif pass_type == "GUIDANCE":
        # Build a compact blocker summary from open HIGH findings + unresolved contradictions
        open_high = [f for f in ledger.get("findings", []) if f.get("status") == "OPEN" and f.get("severity") == "HIGH"]
        contras = ledger.get("contradictions", [])
        if target and target.get("claim"):
            blocker = f"Blocker centered on {target.get('id')}: {target['claim'][:300]}"
        elif open_high:
            blocker = "Open HIGH findings: " + "; ".join(f"{f['id']}: {f['claim'][:120]}" for f in open_high[:5])
        elif contras:
            blocker = "Unresolved contradictions: " + " | ".join(c[:160] for c in contras[:4])
        else:
            blocker = "No specific blocker — strategist asked to advise on highest-value next research direction overall."
        prompt = build_prompt_guidance(blocker, artifact_body, next_id, running_brief=running_brief)
    elif pass_type == "EXPLORATORY_BRANCH":
        # anchor_finding defaults to the highest-severity open finding if not specified
        anchor = target
        if anchor is None:
            opens = [f for f in ledger.get("findings", []) if f.get("status") == "OPEN"]
            sev_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
            opens.sort(key=lambda f: sev_rank.get(f.get("severity"), 9))
            anchor = opens[0] if opens else None
        prompt = build_prompt_exploratory_branch(anchor, artifact_body, next_id, running_brief=running_brief)
    else:
        return {"pass_num": pass_num, "pass_type": pass_type, "status": "SKIPPED-UNKNOWN-TYPE", "timestamp": datetime.now(timezone.utc).isoformat()}

    log(f"PASS {pass_num} {pass_type} starting (target={target['id'] if target else 'n/a'})")
    raw, err = call_research_query(prompt, invocation_id)

    pass_record: dict[str, Any] = {
        "pass_num": pass_num,
        "pass_type": pass_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "invocation_id": invocation_id,
        "target_id": target["id"] if target else None,
        # Phase 1 instrumentation (2026-05-29).
        "prompt_len_chars": len(prompt) if prompt else 0,
        "raw_response_len_chars": len(raw) if raw else 0,
        "partial_prefix_attempted": False,
        "partial_prefix_succeeded": False,
        "retry_fired": False,
        "backoff_s": 0.0,
    }

    if err:
        pass_record["status"] = "SKIPPED-NETWORK"
        pass_record["error"] = err
        log(f"PASS {pass_num} {pass_type} SKIPPED-NETWORK: {err}", "WARN")
        _append_passes_jsonl(workdir, pass_record, raw_response=None)
        _emit_pass_instrumentation(workdir, pass_record)
        return pass_record

    parsed = _parse_response(raw or "", pass_type, next_id_seed=next_finding_id(ledger))
    # Defensive injection BEFORE validation: fills in `pass_type` and other
    # required fields the model may have dropped, filters malformed sub-items.
    # This catches the most common "missing-field" PARSE-FAILED pattern that
    # was triggering the 3-strike streak abort even when the response had
    # useful content (per 2026-05-22 multi-run audit).
    parsed = _defensive_inject_required(parsed, pass_type)
    ok, vmsg = validate_response(parsed)
    if not ok:
        # 2026-05-27 Tier 3 partial-JSON recovery: if `raw` looks like a JSON
        # fragment that was truncated mid-stream (Perplexity's render stalled
        # before the closing brace), try to salvage the longest balanced prefix
        # before triggering a full retry. Free attempt — no new network call.
        if raw and raw.strip().startswith("{"):
            pass_record["partial_prefix_attempted"] = True
            partial = _extract_longest_valid_json_prefix(raw)
            if partial is not None:
                log(f"PASS {pass_num} {pass_type} salvaged longest valid JSON prefix from truncated fragment", "WARN")
                parsed_partial = _defensive_inject_required(partial, pass_type)
                ok_p, _ = validate_response(parsed_partial)
                if ok_p:
                    pass_record["partial_prefix_succeeded"] = True
                    parsed = parsed_partial
                    ok = True

    if not ok:
        # Retry once with a stricter reminder + 20-25s jittered backoff to dodge
        # Perplexity rate-limit cooldowns that follow a failed research query.
        import random as _random
        backoff_s = 20 + _random.uniform(0, 5)
        pass_record["retry_fired"] = True
        pass_record["backoff_s"] = backoff_s
        log(f"PASS {pass_num} {pass_type} schema-fail ({vmsg}); sleeping {backoff_s:.1f}s before retry", "WARN")
        time.sleep(backoff_s)
        if ADVISORY_MODE:
            retry_prompt = prompt + (
                "\n\nREMINDER: Your previous response failed parsing. Use the eight required "
                "## section headers exactly. End with NEXT PASS RECOMMENDATION: and VERDICT: lines."
            )
        else:
            retry_prompt = prompt + "\n\nREMINDER: Your previous response failed validation. Return ONLY valid JSON, nothing else."
        raw2, err2 = call_research_query(retry_prompt, invocation_id + "-r")
        if not err2:
            parsed = _parse_response(raw2 or "", pass_type, next_id_seed=next_finding_id(ledger))
            parsed = _defensive_inject_required(parsed, pass_type)
            ok, vmsg = validate_response(parsed)
            raw = raw2 if raw2 else raw
            # Also try partial-prefix salvage on the retry response.
            if not ok and raw2 and raw2.strip().startswith("{"):
                pass_record["partial_prefix_attempted"] = True
                partial = _extract_longest_valid_json_prefix(raw2)
                if partial is not None:
                    log(f"PASS {pass_num} {pass_type} salvaged longest valid JSON prefix from retry response", "WARN")
                    parsed_partial = _defensive_inject_required(partial, pass_type)
                    ok_p, _ = validate_response(parsed_partial)
                    if ok_p:
                        pass_record["partial_prefix_succeeded"] = True
                        parsed = parsed_partial
                        ok = True

    if not ok:
        pass_record["status"] = "PARSE-FAILED"
        pass_record["error"] = vmsg
        # Re-capture raw_response_len in case retry produced a longer response.
        pass_record["raw_response_len_chars"] = len(raw) if raw else 0
        log(f"PASS {pass_num} {pass_type} PARSE-FAILED after retry: {vmsg}", "ERROR")
        _append_passes_jsonl(workdir, pass_record, raw_response=raw)
        _emit_pass_instrumentation(workdir, pass_record)
        return pass_record

    # Merge findings, contradictions, options into ledger
    new_findings = parsed.get("findings", [])
    net_new = upsert_findings(ledger, new_findings, pass_num)
    pass_record["net_new_findings"] = net_new

    new_contradictions = parsed.get("contradictions", [])
    existing_contras = set(ledger.get("contradictions", []))
    new_contras = [c for c in new_contradictions if c not in existing_contras]
    ledger.setdefault("contradictions", []).extend(new_contras)
    pass_record["net_new_contradictions"] = len(new_contras)

    opts = parsed.get("options", [])
    if opts:
        ledger.setdefault("options", []).extend(opts)
        pass_record["options_emitted"] = opts

    # Pass-type-specific extras
    if pass_type == "DECOMPOSE":
        phases = parsed.get("phases", [])
        ledger["phases"] = phases
        # Recompute max_passes now that N is known
        n = max(1, len(phases))
        formula = 4 + n + math.ceil(n / 2) + 2
        ledger["pass_formula"] = f"{formula} = 4(bootstrap) + {n}(targets) + ceil({n}/2)(followup) + 2(reserved)"
        # If user passed --max-passes explicitly, keep it. Otherwise, set to formula value.
        if ledger.get("max_passes_user_override") is not True:
            ledger["max_passes"] = max(ledger.get("min_passes", 5), formula)
        log(f"DECOMPOSE found N={n} phases. max_passes = {ledger['max_passes']} ({ledger['pass_formula']})")
        # Domain inference for POSTMORTEM
        ledger["domain"] = parsed.get("raw_evidence_summary", "")[:100]

    pass_record["status"] = "COMPLETED"
    pass_record["verdict_hint"] = parsed.get("verdict_hint", "")
    # Advisory mode: surface the model-emitted VERDICT signal for convergence logic
    if ADVISORY_MODE and isinstance(parsed.get("_advisory"), dict):
        pass_record["advisory_verdict"] = parsed["_advisory"].get("verdict_signal", "")

    # Persist Perplexity's next-pass recommendation onto the pass_record so the
    # selector can read it next iteration. Stored shallowly — schema validates shape.
    rec = parsed.get("recommended_next_pass")
    if rec:
        pass_record["recommended_next_pass"] = rec

    # Append to the running narrative brief (and mirror to running_brief.md)
    update_running_brief(ledger, workdir, pass_num, pass_type, parsed, target)

    _append_passes_jsonl(workdir, pass_record, raw_response=raw)
    pass_record["raw_response_len_chars"] = len(raw) if raw else 0
    _emit_pass_instrumentation(workdir, pass_record)
    log(f"PASS {pass_num} {pass_type} COMPLETED net_new_findings={net_new} net_new_contras={len(new_contras)}")
    return pass_record


def _append_passes_jsonl(workdir: Path, pass_record: dict, raw_response: str | None) -> None:
    entry = dict(pass_record)
    entry["raw_response"] = raw_response
    with open(workdir / "passes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def generate_fresh_observer_summary(artifact_body: str, phases: list[dict]) -> str:
    """Generate a frozen ~2k-token summary from the artifact + phase decomposition.

    This is a static heuristic summary (not a Claude call from inside the
    runner — Claude isn't running here). It includes: artifact head/tail,
    list of phases, total length.
    """
    lines = artifact_body.splitlines()
    head = "\n".join(lines[:30])
    tail = "\n".join(lines[-15:]) if len(lines) > 45 else ""
    phase_list = "\n".join(f"- {p.get('label', '?')} (lines {p.get('line_start', '?')}-{p.get('line_end', '?')})" for p in phases)
    summary = f"""ARTIFACT SUMMARY (frozen post-DECOMPOSE):

Total length: {len(lines)} lines, ~{len(artifact_body)//4} tokens estimated.

Decomposed phases:
{phase_list or '(none extracted by DECOMPOSE)'}

OPENING (first 30 lines):
{head}

{f'CLOSING (last 15 lines):{chr(10)}{tail}' if tail else ''}
"""
    return truncate_to_token_budget(summary, FRESH_OBSERVER_SUMMARY_CAP)


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------
def write_salvaged_responses(workdir: Path) -> None:
    """Dump every pass's raw_response from passes.jsonl into a single salvaged-responses.md.

    Runs on every completion (success, CAP-HIT, INTERRUPTED, or PARSE-FAILED-STREAK).
    Lets the caller pull text out of failed runs without parsing JSONL by hand —
    addresses the "/extended-research failed but the prose was valuable" pattern.
    Skipped silently if passes.jsonl is missing or empty.
    """
    src = workdir / "passes.jsonl"
    if not src.exists() or src.stat().st_size == 0:
        return
    try:
        records: list[dict] = []
        for line in src.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        out: list[str] = []
        out.append(f"# Salvaged Perplexity Responses — {workdir.name}\n")
        out.append(f"_Captured {len(records)} passes. Generated automatically when the runner exits._\n\n")
        for r in records:
            pn = r.get("pass_num", "?")
            pt = r.get("pass_type", "?")
            st = r.get("status", "?")
            err = r.get("error", "")
            raw = r.get("raw_response") or ""
            out.append(f"---\n\n## Pass {pn} — {pt} (status={st})\n")
            if err:
                out.append(f"\n**Error:** {err}\n")
            if raw:
                out.append(f"\n```\n{raw}\n```\n")
            else:
                out.append("\n_(no raw response captured)_\n")
        (workdir / "salvaged-responses.md").write_text("".join(out), encoding="utf-8")
    except OSError as e:
        log(f"salvaged-responses.md write failed: {e}", "WARN")


def write_report(ledger: dict, workdir: Path, termination_reason: str) -> None:
    findings = ledger.get("findings", [])
    open_high = [f for f in findings if f.get("status") == "OPEN" and f.get("severity") == "HIGH"]
    structural = [f for f in findings if f.get("structural_unresolvable")]

    if not open_high and not ledger.get("contradictions"):
        verdict = "APPROVED"
    elif open_high and not structural:
        verdict = f"REVISE ({len(open_high)} HIGH findings)"
    elif structural:
        verdict = f"STRUCTURAL-UNRESOLVABLE ({len(structural)} unresolvable)"
    else:
        verdict = "INCONCLUSIVE"

    md = []
    md.append(f"# Extended Research Report: {ledger['slug']}")
    md.append(f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}  ")
    md.append(f"**Passes:** {len(ledger.get('pass_log', []))}/{ledger.get('max_passes', '?')}  ")
    md.append(f"**Verdict:** {verdict}  ")
    md.append(f"**Termination Reason:** {termination_reason}\n")

    md.append("## Executive Summary")
    md.append(f"Artifact verified across {len(ledger.get('pass_log', []))} Perplexity research passes. ")
    md.append(f"{len(findings)} findings ({len(open_high)} HIGH open, {len(structural)} STRUCTURAL-UNRESOLVABLE). ")
    md.append(f"adversarial_pass_count = {sum(1 for p in ledger.get('pass_log', []) if p.get('pass_type') == 'ADVERSARIAL' and p.get('status') == 'COMPLETED')}.\n")

    md.append("## Findings by Phase")
    by_phase: dict[str, list[dict]] = {}
    for f in findings:
        by_phase.setdefault(f.get("phase", "(unspecified)"), []).append(f)
    for phase, items in by_phase.items():
        md.append(f"### Phase: {phase}")
        for f in sorted(items, key=lambda x: SEVERITY_ORDER.get(x.get("severity"), 0), reverse=True):
            tag = " [STRUCTURAL-UNRESOLVABLE]" if f.get("structural_unresolvable") else ""
            src = f" [{f.get('source_flag', 'PRIMARY')}]"
            md.append(f"- **{f['id']} [{f['severity']}]{src}{tag}** — {f['claim']}")
            md.append(f"  - Source: {f.get('source', 'n/a')}")
            md.append(f"  - Status: {f.get('status', 'OPEN')}")
        md.append("")

    if ledger.get("contradictions"):
        md.append("## Contradiction Log")
        for c in ledger["contradictions"]:
            md.append(f"- {c}")
        md.append("")

    if ledger.get("options"):
        md.append("## Options Enumerated")
        for o in ledger["options"]:
            md.append(f"### {o.get('label', o.get('option_id', '?'))}")
            md.append(f"**Confidence:** {o.get('confidence', 'n/a')}")
            md.append(f"**Pros:** {', '.join(o.get('pros', []))}")
            md.append(f"**Cons:** {', '.join(o.get('cons', []))}\n")

    md.append("## Recommended Next Actions")
    if open_high:
        for f in open_high:
            md.append(f"- Address {f['id']} ({f['severity']}): {f['claim'][:120]}")
    else:
        md.append("- No open HIGH findings. Artifact may proceed.")
    md.append("")

    md.append("## Metadata")
    md.append(f"- adversarial_pass_count: {sum(1 for p in ledger.get('pass_log', []) if p.get('pass_type') == 'ADVERSARIAL' and p.get('status') == 'COMPLETED')}")
    md.append(f"- fresh_observer_passes: {[p['pass_num'] for p in ledger.get('pass_log', []) if p.get('pass_type') == 'FRESH_OBSERVER' and p.get('status') == 'COMPLETED']}")
    md.append(f"- last_heartbeat_ts: {ledger.get('last_heartbeat_ts', 'n/a')}")
    md.append(f"- workdir: {workdir}")
    md.append(f"- artifact_hash: {ledger.get('artifact_hash', 'n/a')}")
    md.append(f"- pass_formula: {ledger.get('pass_formula', 'n/a')}")

    (workdir / "report.md").write_text("\n".join(md), encoding="utf-8")
    log(f"Report written to {workdir / 'report.md'}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Extended Research Runner")
    parser.add_argument("--workdir", required=True, help="Per-invocation working directory")
    parser.add_argument("--mode", choices=["whole", "per-phase"], default="per-phase")
    parser.add_argument("--max-passes", type=int, default=None, help="Override dynamic formula")
    parser.add_argument("--min-passes", type=int, default=5, help="Floor for short artifacts")
    parser.add_argument("--resume", action="store_true", help="Resume from existing ledger.json")
    parser.add_argument(
        "--perplexity-advisory-runner",
        action="store_true",
        help=(
            "Advisory mode: prompts request /research-perplexity-style 8-section prose "
            "instead of rigid JSON. Convergence honors model-emitted VERDICT line. Default off "
            "(legacy JSON mode); turn on to match the natural research-mode output style."
        ),
    )
    args = parser.parse_args()

    # Activate advisory-runner mode globally for this run (read by _suf, _parse_response, etc.)
    global ADVISORY_MODE
    ADVISORY_MODE = bool(args.perplexity_advisory_runner)
    if ADVISORY_MODE:
        log("Advisory mode ACTIVE: prompts use 8-section /research-perplexity template", "INFO")

    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        print(f"[ERROR] workdir {workdir} does not exist", file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, sigint_handler)
    try:
        signal.signal(signal.SIGTERM, sigint_handler)
    except (AttributeError, OSError):
        pass  # Windows may not support SIGTERM

    # Load or initialize ledger
    if args.resume and (workdir / "ledger.json").exists():
        ledger = load_ledger(workdir)
        # Resume: honor the mode the original run used (CLI flag wins if explicitly set this time)
        if ledger.get("advisory_mode") and not args.perplexity_advisory_runner:
            ADVISORY_MODE = True
            log("Resume: restored advisory_mode=True from ledger", "INFO")
        log(f"Resuming from pass {ledger.get('interrupted_at_pass', ledger.get('current_pass_num', 0))} (slug={ledger['slug']})")
        # Verify artifact hash hasn't drifted
        _, current_hash = read_artifact(workdir)
        if current_hash != ledger.get("artifact_hash"):
            log(f"WARNING: artifact_hash drift detected. Ledger says {ledger.get('artifact_hash')[:16]}..., current is {current_hash[:16]}...", "WARN")
            ledger["drift_warning"] = True
        start_pass = (ledger.get("interrupted_at_pass") or ledger.get("current_pass_num") or 0) + 1
    else:
        body, ahash = read_artifact(workdir)
        slug = workdir.name
        ledger = {
            "slug": slug,
            "artifact_hash": ahash,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": args.mode,
            "min_passes": args.min_passes,
            "max_passes": args.max_passes,
            "max_passes_user_override": args.max_passes is not None,
            "advisory_mode": ADVISORY_MODE,
            "passes_completed": 0,
            "status": "RUNNING",
            "findings": [],
            "findings_history": [],
            "contradictions": [],
            "options": [],
            "phases": [],
            "pass_log": [],
            "running_brief": "",  # Narrative state — grows across passes; injected into every prompt
            "last_heartbeat_ts": datetime.now(timezone.utc).isoformat(),
        }
        write_ledger_atomic(ledger, workdir)
        start_pass = 1

    artifact_body, _ = read_artifact(workdir)

    # Pre-flight oversize warning. INTERIM PENDING PHASE 4 CALIBRATION — threshold
    # kept at 16 KB after the 7ee00ea revert (ARTIFACT_TEXT_TOKEN_CAP 5000 -> 3500,
    # i.e. artifact text now caps at ~14 KB / prompts at ~19 KB max). 16 KB is
    # intentionally conservative: it warns just above the ~14 KB truncation point
    # so users see the notice *before* pass-level truncation silently drops
    # coverage, while staying below the empirical ~18 KB browser-UI render cliff.
    # (Prior comment claimed this paired with the 5000-token cap; that cap no
    # longer exists. Whether to relax to 18 KB defers to the issue #44 20-run gate.)
    _artifact_size_kb = len(artifact_body) / 1024
    if _artifact_size_kb > 16:
        log(
            f"WARNING: artifact is {_artifact_size_kb:.1f} KB — approaching "
            "Perplexity's ~20 KB browser-UI synthesis-render cliff. Pass-level "
            "truncation will keep each prompt under ~20 KB, but multi-phase "
            "coverage may suffer. Recommend either: (a) precompress the "
            "artifact, or (b) re-run with --perplexity-advisory-runner.",
            "WARN",
        )

    # Fresh-observer summary — generated after DECOMPOSE (Pass 1) and frozen.
    fresh_summary_path = workdir / "fresh_observer_summary.txt"
    if fresh_summary_path.exists():
        fresh_summary = fresh_summary_path.read_text(encoding="utf-8")
    else:
        fresh_summary = ""  # populated post-Pass-1

    # Main pass loop
    pass_num = start_pass
    termination_reason = None
    while True:
        if _interrupted:
            log("Interrupt acknowledged — writing INTERRUPTED state", "WARN")
            ledger["status"] = "INTERRUPTED"
            ledger["interrupted_at_pass"] = pass_num - 1
            write_ledger_atomic(ledger, workdir)
            (workdir / "INTERRUPTED").write_text(datetime.now(timezone.utc).isoformat())
            termination_reason = f"INTERRUPTED at pass {pass_num - 1}; resume with --resume {ledger['slug']}"
            break

        # Hard cap check (max_passes may be None until DECOMPOSE)
        max_p = ledger.get("max_passes")
        if max_p and pass_num > max_p:
            converged, reason, _ = check_convergence(ledger)
            if converged:
                termination_reason = f"CONVERGED at hard cap; {reason}"
            else:
                open_high = sum(1 for f in ledger.get("findings", []) if f.get("status") == "OPEN" and f.get("severity") == "HIGH")
                contras = len(ledger.get("contradictions", []))
                termination_reason = f"CAP-HIT ({open_high} open HIGH, {contras} contradictions; {reason})"
            break

        # Pre-pass convergence check (don't re-do passes that already passed)
        if pass_num > 5:  # only after bootstrap
            converged, reason, _adv_deficit = check_convergence(ledger)
            if converged and pass_num > ledger.get("min_passes", 5):
                termination_reason = f"CONVERGED — {reason}"
                # Run INTEGRATION + FINAL_VERDICT before terminating
                if not _ledger_has_pass(ledger, "INTEGRATION"):
                    integ = run_pass(ledger, workdir, pass_num, "INTEGRATION", None, artifact_body, fresh_summary)
                    ledger["pass_log"].append(integ)
                    ledger["passes_completed"] = pass_num
                    write_ledger_atomic(ledger, workdir)
                    pass_num += 1
                final = run_pass(ledger, workdir, pass_num, "FINAL_VERDICT", None, artifact_body, fresh_summary)
                ledger["pass_log"].append(final)
                ledger["passes_completed"] = pass_num
                write_ledger_atomic(ledger, workdir)
                break

        # Select and run the next pass
        pass_type, target = select_next_pass_type(ledger, pass_num)
        record = run_pass(ledger, workdir, pass_num, pass_type, target, artifact_body, fresh_summary)
        ledger["pass_log"].append(record)
        ledger["passes_completed"] = pass_num
        write_ledger_atomic(ledger, workdir)

        # Circuit breaker: detect persistent parse-fail streaks.
        # If the last K consecutive COMPLETED OR FAILED records are all PARSE-FAILED
        # (i.e. something is structurally wrong — expired Perplexity session, bot
        # detection, parser bug, malformed responses) abort the run instead of
        # burning the full pass budget. The caller can salvage raw responses from
        # salvaged-responses.md.
        recent_statuses = [p.get("status") for p in ledger["pass_log"][-PARSE_FAIL_STREAK_THRESHOLD:]]
        if (
            len(recent_statuses) >= PARSE_FAIL_STREAK_THRESHOLD
            and all(s == "PARSE-FAILED" for s in recent_statuses)
        ):
            termination_reason = (
                f"PARSE-FAILED-STREAK ({PARSE_FAIL_STREAK_THRESHOLD} consecutive passes returned "
                f"unparseable responses). Likely causes: expired Perplexity session — run "
                f"`/cache-perplexity-session`; bot detection / Cloudflare lockout; or "
                f"runner-side parser regression. Raw responses preserved in salvaged-responses.md "
                f"for manual review."
            )
            log(termination_reason, "ERROR")
            break

        # Sibling circuit breaker: SKIPPED-NETWORK streak. Distinct from PARSE-FAIL
        # because the response never arrived (subprocess error, timeout, council_query
        # non-zero exit). Catches Cloudflare lockout, expired session, transient outage,
        # and the WinError 206 cmdline-length blowup that the stdin-piping fix prevents.
        recent_statuses_net = [p.get("status") for p in ledger["pass_log"][-SKIPPED_NETWORK_STREAK_THRESHOLD:]]
        if (
            len(recent_statuses_net) >= SKIPPED_NETWORK_STREAK_THRESHOLD
            and all(s == "SKIPPED-NETWORK" for s in recent_statuses_net)
        ):
            termination_reason = (
                f"SKIPPED-NETWORK-STREAK ({SKIPPED_NETWORK_STREAK_THRESHOLD} consecutive passes "
                f"failed to reach the research backend). Likely causes: expired Perplexity session — "
                f"run `/cache-perplexity-session`; Cloudflare bot lockout; council_query.py crash; "
                f"or transient network outage. Raw responses (if any) preserved in "
                f"salvaged-responses.md."
            )
            log(termination_reason, "ERROR")
            break

        # Post-DECOMPOSE: generate the frozen fresh-observer summary
        if pass_num == 1 and pass_type == "DECOMPOSE" and not fresh_summary_path.exists():
            fresh_summary = generate_fresh_observer_summary(artifact_body, ledger.get("phases", []))
            fresh_summary_path.write_text(fresh_summary, encoding="utf-8")
            log(f"Fresh-observer summary generated ({len(fresh_summary)} chars) — frozen for run")

        pass_num += 1
        # PATCH 2026-05-20: inter-pass cooldown to avoid Perplexity backend
        # throttling that produces 0-byte synthesis responses. Skip cooldown
        # after the LAST pass (no next pass coming).
        # PATCH 2026-05-26: dict.get(k, default) returns the actual value when
        # the key is present-but-None — not the default. Coerce explicitly so
        # `pass_num <= _cap` doesn't crash when max_passes is None (observed
        # after a PARSE-FAILED early in the run left the ledger field unset).
        _cap = ledger.get("max_passes") or 0
        if INTER_PASS_SLEEP_S > 0 and pass_num <= _cap:
            log(f"Inter-pass cooldown {INTER_PASS_SLEEP_S:.0f}s (PERPLEXITY_INTER_PASS_SLEEP_S)")
            time.sleep(INTER_PASS_SLEEP_S)

    # Cleanup
    if termination_reason is None:
        termination_reason = "ended without explicit convergence (unexpected)"
    # Distinguish INTERRUPTED (SIGINT, resumable) from PARSE-FAILED-STREAK (abort)
    if "INTERRUPTED" in termination_reason:
        ledger["status"] = "INTERRUPTED"
    elif "PARSE-FAILED-STREAK" in termination_reason:
        ledger["status"] = "ABORTED"
    elif "SKIPPED-NETWORK-STREAK" in termination_reason:
        ledger["status"] = "ABORTED-NETWORK"
    else:
        ledger["status"] = "COMPLETED"
    ledger["termination_reason"] = termination_reason
    write_ledger_atomic(ledger, workdir)
    write_report(ledger, workdir, termination_reason)
    # Always dump raw responses alongside report.md so callers can salvage prose
    # from runs that failed strict JSON parsing.
    write_salvaged_responses(workdir)

    # Phase 2 (2026-05-29 follow-ups): consume per-pass instrumentation into
    # the global calibration summary. Fire-and-forget — never blocks runner.
    # Step 7 critique Q2: WARN when aggregator returns zero rows on a non-empty
    # source so silent failure is visible in runner.log instead of only via CLI.
    try:
        from calibration_log import aggregate_run, append_to_global_summary
        jsonl_path = workdir / "instrumentation.jsonl"
        run_summary = aggregate_run(workdir)
        if run_summary.get("entry_count", 0) == 0:
            if jsonl_path.exists() and jsonl_path.stat().st_size > 0:
                log(
                    f"calibration_log aggregation produced zero rows from non-empty "
                    f"source {jsonl_path} ({jsonl_path.stat().st_size} bytes) — "
                    "inspect manually with `python calibration_log.py`",
                    "WARN",
                )
        append_to_global_summary(run_summary)
    except Exception as _calib_e:
        log(f"calibration_log aggregation skipped: {type(_calib_e).__name__}: {_calib_e}", "WARN")

    # Sentinel for Claude to detect completion
    (workdir / "runner.log.done").write_text(datetime.now(timezone.utc).isoformat() + "\n")
    log(f"DONE. Termination: {termination_reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

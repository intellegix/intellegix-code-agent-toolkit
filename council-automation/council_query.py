"""Hybrid council query engine — multi-tier multi-model queries with Opus 4.6 synthesis.

Queries 3 frontier models via Perplexity API (fast) or browser automation (reliable),
then optionally synthesizes with Claude Opus 4.6 extended thinking. Caches results.

Usage:
    python council_query.py --mode api "What architecture for X?"
    python council_query.py --mode browser "Reliable browser query"
    python council_query.py --mode auto "Try API first, fallback to browser"
    python council_query.py --mode browser --headful "Debug with visible browser"
    python council_query.py --mode browser --opus-synthesis "Add Opus re-synthesis"
    python council_query.py --auto-context --mode browser "Auto-inject project context"
    python council_query.py --context-file ctx.md "Analyze this"
    python council_query.py --read                    # synthesis only (from cache)
    python council_query.py --read-full               # all 3 + synthesis
    python council_query.py --read-model gpt-5.2      # one model's response
"""

import argparse
import asyncio
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout on Windows (cp1252 can't encode Unicode)
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import anthropic

from council_config import (
    ANALYSIS_MODELS,
    BROWSER_TIMEOUT,
    CACHE_DIR,
    DIRECT_PROVIDERS_ENABLED,
    DIRECT_TIMEOUT,
    FALLBACK_ENABLED,
    FALLBACK_MODEL,
    HISTORY_DIR,
    MAX_OUTPUT_TOKENS,
    MODEL_INSTRUCTIONS,
    PERPLEXITY_CONNECT_TIMEOUT,
    PERPLEXITY_RETRIES,
    PERPLEXITY_TIMEOUT,
    SYNTHESIS_MODEL,
    SYNTHESIS_PROMPT_PATH,
    SYNTHESIS_RETRIES,
    SYNTHESIS_TIMEOUT,
    THINKING_BUDGET,
    WEB_SEARCH_ENABLED,
    print_validation,
)


def load_synthesis_prompt() -> str:
    if SYNTHESIS_PROMPT_PATH.exists():
        return SYNTHESIS_PROMPT_PATH.read_text(encoding="utf-8")
    return "Synthesize the following multi-model responses into a structured JSON analysis."


async def query_perplexity_model(
    client, model_cfg: dict, full_input: str,
) -> dict:
    """Query a single Perplexity model with capability-aware tool selection.

    Tool inclusion uses the handshake: model.web_search_capable AND WEB_SEARCH_ENABLED.
    """
    label = model_cfg["label"]
    use_web_search = model_cfg.get("web_search_capable", False) and WEB_SEARCH_ENABLED
    for attempt in range(1 + PERPLEXITY_RETRIES):
        try:
            kwargs: dict = {
                "model": model_cfg["id"],
                "input": full_input,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "instructions": MODEL_INSTRUCTIONS,
            }
            if use_web_search:
                kwargs["tools"] = [{"type": "web_search"}]

            response = await asyncio.wait_for(
                client.responses.create(**kwargs),
                timeout=PERPLEXITY_TIMEOUT,
            )

            out_text = response.output_text
            if not out_text:
                print(f"  [{label}] WARNING: empty output_text", file=sys.stderr)
                # Try recovering text from output items
                if hasattr(response, "output") and response.output:
                    for item in response.output:
                        if hasattr(item, "content"):
                            for c in item.content:
                                if hasattr(c, "text") and c.text:
                                    out_text += c.text
                    if out_text:
                        print(f"  [{label}] Recovered {len(out_text)} chars", file=sys.stderr)
            else:
                print(f"  [{label}] OK: {len(out_text)} chars", file=sys.stderr)

            # Extract usage and cost
            usage = {}
            if hasattr(response, "usage") and response.usage:
                u = response.usage
                usage = {
                    "input_tokens": getattr(u, "input_tokens", 0),
                    "output_tokens": getattr(u, "output_tokens", 0),
                }
                if hasattr(u, "cost") and u.cost:
                    usage["cost"] = getattr(u.cost, "total_cost", 0)

            # Extract citations
            citations = []
            if hasattr(response, "citations"):
                citations = response.citations or []

            # Extract search results for injection into analysis models
            search_results = []
            if hasattr(response, "output") and response.output:
                for item in response.output:
                    if getattr(item, "type", "") == "search_results":
                        for r in getattr(item, "results", []):
                            search_results.append({
                                "title": getattr(r, "title", ""),
                                "url": getattr(r, "url", ""),
                                "snippet": getattr(r, "snippet", ""),
                            })

            return {
                "model": model_cfg["id"],
                "label": label,
                "response": out_text,
                "tokens_in": usage.get("input_tokens", 0),
                "tokens_out": usage.get("output_tokens", 0),
                "cost": usage.get("cost", 0),
                "citations": citations,
                "search_results": search_results,
                "web_search_used": use_web_search,
                "error": None,
            }
        except Exception as e:
            if attempt < PERPLEXITY_RETRIES:
                await asyncio.sleep(2)
                continue
            return {
                "model": model_cfg["id"],
                "label": label,
                "response": None,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost": 0,
                "citations": [],
                "search_results": [],
                "web_search_used": use_web_search,
                "error": str(e),
            }


def query_sonar_fallback(query: str) -> dict:
    """Fallback: query Sonar Pro via chat/completions when Responses API is down.

    Uses requests (sync) since this is only called when async Responses API fails.
    Sonar Pro has built-in web search — no tool needed.
    """
    import requests as req

    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        return {"model": FALLBACK_MODEL, "label": "Sonar Pro (fallback)",
                "response": None, "error": "PERPLEXITY_API_KEY not set",
                "tokens_in": 0, "tokens_out": 0, "cost": 0, "citations": [],
                "search_results": [], "web_search_used": True}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": FALLBACK_MODEL,
        "messages": [{"role": "user", "content": query}],
    }

    try:
        r = req.post(
            "https://api.perplexity.ai/chat/completions",
            headers=headers, json=payload,
            timeout=PERPLEXITY_TIMEOUT,
        )
        if r.status_code != 200:
            return {"model": FALLBACK_MODEL, "label": "Sonar Pro (fallback)",
                    "response": None, "error": f"HTTP {r.status_code}: {r.text[:200]}",
                    "tokens_in": 0, "tokens_out": 0, "cost": 0, "citations": [],
                    "search_results": [], "web_search_used": True}

        data = r.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        citations = data.get("citations", [])

        return {
            "model": FALLBACK_MODEL,
            "label": "Sonar Pro (fallback)",
            "response": text,
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "cost": 0,  # Included in subscription
            "citations": citations if isinstance(citations, list) else [],
            "search_results": [],
            "web_search_used": True,
            "error": None,
        }
    except Exception as e:
        return {"model": FALLBACK_MODEL, "label": "Sonar Pro (fallback)",
                "response": None, "error": str(e),
                "tokens_in": 0, "tokens_out": 0, "cost": 0, "citations": [],
                "search_results": [], "web_search_used": True}


async def _test_responses_api_health(client) -> bool:
    """Quick health check: can we reach /v1/responses at all?"""
    try:
        r = await asyncio.wait_for(
            client.responses.create(
                model="openai/gpt-5.2",
                input="ping",
                max_output_tokens=10,
            ),
            timeout=PERPLEXITY_CONNECT_TIMEOUT,
        )
        return True  # Got any response = API is alive
    except (asyncio.TimeoutError, Exception):
        return False


async def _query_direct_providers(full_input: str) -> list[dict]:
    """Tier 3: Query models directly via their native APIs."""
    from council_providers import query_direct_providers

    return await query_direct_providers(ANALYSIS_MODELS, full_input, DIRECT_TIMEOUT)


async def query_all_models(query: str, context: str) -> list[dict]:
    """Query models via Responses API, fallback to Sonar via chat/completions.

    Capability handshake: each model gets web_search tool only if
    model.web_search_capable AND WEB_SEARCH_ENABLED (system toggle).

    Primary: 3 third-party models via /v1/responses (parallel)
    Fallback: Sonar Pro via /chat/completions (when Responses API is down)
    """
    from perplexity import AsyncPerplexity, DefaultAioHttpClient

    full_input = f"{context}\n\n{query}" if context else query

    async with AsyncPerplexity(
        http_client=DefaultAioHttpClient()
    ) as client:
        # Health check: is Responses API responsive?
        if FALLBACK_ENABLED:
            print("  Checking Responses API health...", file=sys.stderr)
            api_healthy = await _test_responses_api_health(client)
            if not api_healthy:
                print("  Responses API unreachable — falling back to Sonar Pro via chat/completions", file=sys.stderr)
                result = query_sonar_fallback(full_input)
                chars = len(result.get("response") or "")
                if chars:
                    print(f"  [Sonar Pro fallback] OK: {chars} chars", file=sys.stderr)
                    return [result]
                else:
                    print(f"  [Sonar Pro fallback] FAILED: {result.get('error')}", file=sys.stderr)
                    if DIRECT_PROVIDERS_ENABLED:
                        print("  Trying Tier 3 (direct providers)...", file=sys.stderr)
                        return await _query_direct_providers(full_input)
                    return [result]

        # Query all analysis models in parallel (capability-aware tool selection)
        labels = ", ".join(m["label"] for m in ANALYSIS_MODELS)
        web_note = " (web_search enabled)" if WEB_SEARCH_ENABLED else ""
        print(f"  Querying {labels}{web_note}...", file=sys.stderr)
        tasks = [
            query_perplexity_model(client, model, full_input)
            for model in ANALYSIS_MODELS
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[dict] = []
        for r in raw_results:
            if isinstance(r, Exception):
                results.append({
                    "model": "unknown", "label": "unknown",
                    "response": None, "error": str(r),
                })
            else:
                results.append(r)

        # If all models returned empty/error, try Sonar fallback then direct
        all_empty = all(not r.get("response") for r in results)
        if all_empty and FALLBACK_ENABLED:
            print("  All models returned empty — trying Sonar fallback...", file=sys.stderr)
            fallback = query_sonar_fallback(full_input)
            chars = len(fallback.get("response") or "")
            if chars:
                print(f"  [Sonar Pro fallback] OK: {chars} chars", file=sys.stderr)
                results.append(fallback)
            elif DIRECT_PROVIDERS_ENABLED:
                # Tier 3: Direct provider APIs
                print("  Sonar fallback failed — trying Tier 3 (direct providers)...", file=sys.stderr)
                direct_results = await _query_direct_providers(full_input)
                return direct_results
            else:
                results.append(fallback)

        return results


def run_opus_synthesis(
    query: str, model_results: list[dict], context: str
) -> dict:
    """Run Opus 4.6 synthesis with extended thinking."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set", "response": None}

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = load_synthesis_prompt()

    # Build the user message with all model responses
    model_sections = []
    for r in model_results:
        if isinstance(r, Exception):
            model_sections.append(f"## {r}\n\n(Error: model query failed)")
            continue
        if r.get("error"):
            model_sections.append(
                f"## {r['label']}\n\n(Error: {r['error']})"
            )
            continue
        citations_str = ""
        if r.get("citations"):
            citations_str = "\n\nCitations:\n" + "\n".join(
                f"- {c}" for c in r["citations"][:10]
            )
        model_sections.append(
            f"## {r['label']} ({r['model']})\n\n{r['response']}{citations_str}"
        )

    user_message = f"""ORIGINAL QUERY:
{query}

PROJECT CONTEXT:
{context if context else "(none provided)"}

MODEL RESPONSES:

{"".join(chr(10) + s + chr(10) for s in model_sections)}

Produce your structured JSON synthesis now."""

    for attempt in range(1 + SYNTHESIS_RETRIES):
        try:
            response = client.messages.create(
                model=SYNTHESIS_MODEL,
                max_tokens=16_000,
                thinking={
                    "type": "enabled",
                    "budget_tokens": THINKING_BUDGET,
                },
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                timeout=SYNTHESIS_TIMEOUT,
            )

            # Extract text and thinking from response
            text_content = ""
            thinking_text = ""
            for block in response.content:
                if block.type == "text":
                    text_content += block.text
                elif block.type == "thinking":
                    thinking_text += block.thinking

            # Estimate thinking tokens from thinking text (~0.75 words per token)
            thinking_tokens = int(len(thinking_text.split()) / 0.75) if thinking_text else 0

            # Extract usage from API response
            input_tokens = 0
            output_tokens = 0
            if hasattr(response, "usage"):
                u = response.usage
                input_tokens = getattr(u, "input_tokens", 0)
                output_tokens = getattr(u, "output_tokens", 0)

            # Try to parse the JSON from the response
            synthesis_data = {}
            try:
                # Find JSON in the response (may be wrapped in markdown code block)
                json_text = text_content
                if "```json" in json_text:
                    json_text = json_text.split("```json")[1].split("```")[0]
                elif "```" in json_text:
                    json_text = json_text.split("```")[1].split("```")[0]
                synthesis_data = json.loads(json_text.strip())
            except (json.JSONDecodeError, IndexError):
                # If JSON parse fails, use the raw text as narrative
                synthesis_data = {
                    "summary": text_content[:500],
                    "narrative": text_content,
                    "agreements": [],
                    "disagreements": [],
                    "unique_insights": [],
                    "recommended_actions": [],
                    "confidence": "medium",
                    "risks": [],
                }

            # Opus 4.6 pricing: $15/1M input, $75/1M output
            cost = (input_tokens * 15 + output_tokens * 75) / 1_000_000

            return {
                "model": SYNTHESIS_MODEL,
                "thinking_tokens": int(thinking_tokens),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "response": text_content,
                "parsed": synthesis_data,
                "cost": cost,
                "error": None,
            }
        except Exception as e:
            if attempt < SYNTHESIS_RETRIES:
                time.sleep(2)
                continue
            return {
                "model": SYNTHESIS_MODEL,
                "thinking_tokens": 0,
                "response": None,
                "parsed": {},
                "cost": 0,
                "error": str(e),
            }


def save_results(results: dict) -> Path:
    """Save results to cache and history."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    # Save as latest
    latest_path = CACHE_DIR / "council_latest.json"
    latest_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    # Save timestamped copy
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    slug = re.sub(r'[^a-zA-Z0-9_-]', '-', results.get("query", "query")[:40]).strip("-")
    history_path = HISTORY_DIR / f"{ts}-{slug}.json"
    history_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    return latest_path


def append_run_log(results: dict) -> None:
    """Append a single JSON-line summary to the operational run log."""
    log_entry = {
        "timestamp": results.get("timestamp"),
        "mode": results.get("mode"),
        "query_len": len(results.get("query", "")),
        "models_extracted": len(results.get("models", {})),
        "synthesis_model": results.get("synthesis", {}).get("model"),
        "thinking_tokens": results.get("synthesis", {}).get("thinking_tokens", 0),
        "cost": results.get("total_cost", 0),
        "execution_time_ms": results.get("execution_time_ms", 0),
        "degraded": results.get("degraded", False),
        "fallback_count": len(results.get("fallback_log", [])),
        "error": results.get("synthesis", {}).get("error"),
    }
    try:
        log_path = Path("~/.claude/council-logs/runs.jsonl").expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, default=str) + "\n")
    except Exception as e:
        print(f"WARNING: Failed to write run log: {e}", file=sys.stderr)


def read_cached(level: str = "synthesis") -> str:
    """Read cached results at the specified detail level."""
    latest = CACHE_DIR / "council_latest.json"
    if not latest.exists():
        return json.dumps({"error": "No cached council results. Run a query first."})

    data = json.loads(latest.read_text(encoding="utf-8"))

    if level == "synthesis":
        synthesis = data.get("synthesis", {})
        parsed = synthesis.get("parsed", synthesis)
        return json.dumps(
            {
                "query": data.get("query"),
                "timestamp": data.get("timestamp"),
                "mode": data.get("mode"),
                "synthesis": {
                    "summary": parsed.get("summary", ""),
                    "agreements": parsed.get("agreements", []),
                    "disagreements": parsed.get("disagreements", []),
                    "unique_insights": parsed.get("unique_insights", []),
                    "recommended_actions": parsed.get("recommended_actions", []),
                    "confidence": parsed.get("confidence", ""),
                    "risks": parsed.get("risks", []),
                    "narrative": parsed.get("narrative", ""),
                },
                "total_cost": data.get("total_cost"),
                "execution_time_ms": data.get("execution_time_ms"),
            },
            indent=2,
        )

    if level == "full":
        return json.dumps(data, indent=2, default=str)

    # Specific model
    models = data.get("models", {})
    for key, val in models.items():
        if level.lower() in key.lower():
            return json.dumps(
                {
                    "model": key,
                    "response": val.get("response"),
                    "citations": val.get("citations", []),
                    "cost": val.get("cost"),
                },
                indent=2,
            )

    return json.dumps({"error": f"Model '{level}' not found in cache. Available: {list(models.keys())}"})


async def run_api_query(query: str, context: str) -> dict:
    """Execute the full API query pipeline."""
    start = time.time()
    fallback_log: list[dict] = []

    # Step 1: Query models (parallel, capability-aware)
    print("Querying models...", file=sys.stderr)
    raw_results = await query_all_models(query, context)

    # Process results
    model_results = []
    models_dict = {}
    for r in raw_results:
        if isinstance(r, Exception):
            model_results.append({
                "model": "unknown",
                "label": "unknown",
                "response": None,
                "error": str(r),
            })
        else:
            model_results.append(r)
            label = r.get("label", r.get("model", "unknown"))
            models_dict[label] = {
                "response": r.get("response"),
                "tokens_in": r.get("tokens_in", 0),
                "tokens_out": r.get("tokens_out", 0),
                "cost": r.get("cost", 0),
                "citations": r.get("citations", []),
                "error": r.get("error"),
            }
            # Track fallback models
            if r.get("label") == "Sonar Pro (fallback)":
                fallback_log.append({
                    "decision": "sonar_fallback",
                    "reason": "Responses API unreachable or all models empty",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

    # Step 2: Opus synthesis
    print("Running Opus 4.6 synthesis with extended thinking...", file=sys.stderr)
    synthesis = run_opus_synthesis(query, model_results, context)

    if synthesis.get("error"):
        fallback_log.append({
            "decision": "synthesis_error",
            "reason": synthesis["error"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    elapsed = int((time.time() - start) * 1000)

    # Calculate total cost
    model_cost = sum(m.get("cost", 0) for m in models_dict.values())
    synthesis_cost = synthesis.get("cost", 0)

    results = {
        "query": query,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "api",
        "models": models_dict,
        "synthesis": {
            "model": synthesis.get("model"),
            "thinking_tokens": synthesis.get("thinking_tokens", 0),
            "response": synthesis.get("response"),
            **synthesis.get("parsed", {}),
            "cost": synthesis_cost,
            "error": synthesis.get("error"),
        },
        "total_cost": round(model_cost + synthesis_cost, 6),
        "execution_time_ms": elapsed,
        "fallback_log": fallback_log,
        "degraded": any(f.get("severity") != "info" for f in fallback_log),
    }

    save_results(results)
    append_run_log(results)
    return results


async def run_browser_query(
    query: str,
    context: str,
    headful: bool | None = None,
    opus_synthesis: bool = False,
    perplexity_mode: str = "council",
) -> dict:
    """Execute query via Playwright browser automation against Perplexity UI.

    Args:
        perplexity_mode: "council" for multi-model council, "research" for deep research, "labs" for experimental labs.
    """
    try:
        from council_browser import PerplexityCouncil
    except ImportError:
        return {
            "error": "playwright not installed. Run: pip install playwright && playwright install chromium",
            "step": "import",
        }

    start = time.time()
    fallback_log: list[dict] = []
    full_query = f"{context}\n\n{query}" if context else query

    # Use config default (BROWSER_HEADLESS=False, i.e. headful) unless explicitly overridden
    kwargs = {"save_artifacts": opus_synthesis, "perplexity_mode": perplexity_mode}
    if headful is not None:
        kwargs["headless"] = not headful
    council = PerplexityCouncil(**kwargs)
    try:
        browser_result = await council.run(full_query)
    finally:
        await council.stop()

    # Diagnostic: warn if browser returned no synthesis (Bug 3 investigation)
    if not browser_result.get("error") and not browser_result.get("synthesis"):
        print(
            f"WARNING: browser_result has no synthesis. "
            f"Keys: {list(browser_result.keys())}, "
            f"models: {len(browser_result.get('models', {}))}, "
            f"step: {browser_result.get('step', 'N/A')}",
            file=sys.stderr,
        )

    if browser_result.get("error"):
        error_results = {
            "query": query,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "browser",
            "models": {},
            "synthesis": {"response": None, "error": browser_result["error"]},
            "total_cost": 0,
            "execution_time_ms": int((time.time() - start) * 1000),
            "error": browser_result["error"],
            "code": browser_result.get("code"),
            "step": browser_result.get("step"),
            "fallback_log": [],
            "degraded": True,
        }
        save_results(error_results)
        append_run_log(error_results)
        return error_results

    # Convert browser result to standard council result format
    models_dict = {}
    for label, model_data in browser_result.get("models", {}).items():
        models_dict[label] = {
            "response": model_data.get("response"),
            "tokens_in": 0,
            "tokens_out": 0,
            "cost": 0,
            "citations": [],
            "error": None,
        }

    elapsed = int((time.time() - start) * 1000)

    # Use Perplexity's native synthesis by default
    synthesis_data = {
        "model": f"perplexity-{perplexity_mode}",
        "thinking_tokens": 0,
        "response": browser_result.get("synthesis", ""),
        "summary": browser_result.get("synthesis", "")[:500],
        "narrative": browser_result.get("synthesis", ""),
        "agreements": [],
        "disagreements": [],
        "unique_insights": [],
        "recommended_actions": [],
        "confidence": "medium",
        "risks": [],
        "cost": 0,
        "error": None,
    }

    # Optionally run Opus re-synthesis for structured output
    if opus_synthesis:
        print("Running optional Opus 4.6 re-synthesis...", file=sys.stderr)
        model_results = [
            {"label": k, "model": k, "response": v.get("response"), "citations": [], "error": None}
            for k, v in models_dict.items()
            if v.get("response")
        ]
        # Fallback: if no individual models extracted, use the synthesis text.
        # This is normal — Perplexity may use single-model mode for simpler queries.
        if not model_results and browser_result.get("synthesis"):
            print("  No individual model responses — using Perplexity synthesis as input", file=sys.stderr)
            fallback_log.append({
                "decision": "opus_input_fallback",
                "reason": f"0/{len(browser_result.get('models', {}))} models extracted, using synthesis text",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "severity": "info",  # Not an error — council may use fewer models
            })
            model_results = [{
                "label": "Perplexity Council Synthesis",
                "model": "perplexity-council",
                "response": browser_result["synthesis"],
                "citations": browser_result.get("citations", []),
                "error": None,
            }]
        if model_results:
            opus_result = run_opus_synthesis(query, model_results, context)
            if not opus_result.get("error"):
                synthesis_data = {
                    "model": opus_result.get("model"),
                    "thinking_tokens": opus_result.get("thinking_tokens", 0),
                    "response": opus_result.get("response"),
                    **opus_result.get("parsed", {}),
                    "cost": opus_result.get("cost", 0),
                    "error": None,
                }
            else:
                print(f"  Opus synthesis failed: {opus_result.get('error')}", file=sys.stderr)
                fallback_log.append({
                    "decision": "opus_synthesis_error",
                    "reason": opus_result["error"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
        else:
            print("  No model results or synthesis text available for Opus", file=sys.stderr)
            fallback_log.append({
                "decision": "opus_skipped",
                "reason": "No model results or synthesis text available",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    # Only mark as degraded for genuine errors, not informational fallbacks
    error_fallbacks = [f for f in fallback_log if f.get("severity") != "info"]
    results = {
        "query": query,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "browser",
        "models": models_dict,
        "synthesis": synthesis_data,
        "citations": browser_result.get("citations", []),
        "total_cost": synthesis_data.get("cost", 0),
        "execution_time_ms": elapsed,
        "fallback_log": fallback_log,
        "degraded": len(error_fallbacks) > 0,
    }

    save_results(results)
    append_run_log(results)
    return results


async def run_auto_query(query: str, context: str) -> dict:
    """Try API first, fall back to browser on failure."""
    print("Auto mode: trying API first...", file=sys.stderr)
    try:
        api_result = await run_api_query(query, context)
        # Check if we got any useful responses
        models = api_result.get("models", {})
        has_responses = any(v.get("response") for v in models.values())
        if has_responses:
            print("Auto mode: API succeeded", file=sys.stderr)
            return api_result
        print("Auto mode: API returned empty results, falling back to browser...", file=sys.stderr)
    except Exception as e:
        print(f"Auto mode: API failed ({e}), falling back to browser...", file=sys.stderr)

    result = await run_browser_query(query, context)
    if result.get("code") == "BROWSER_BUSY":
        print("Auto mode: browser busy (another session holds the lock)", file=sys.stderr)
    return result


def format_synthesis_output(results: dict) -> str:
    """Format synthesis for stdout (what Claude reads)."""
    # Check for error results (BROWSER_BUSY, session expired, etc.)
    if results.get("error"):
        error_msg = results["error"]
        code = results.get("code", "UNKNOWN")
        step = results.get("step", "unknown")
        return (
            f"# Research/Council Query FAILED\n\n"
            f"**Error:** {error_msg}\n"
            f"**Code:** {code}\n"
            f"**Step:** {step}\n\n"
            f"If BROWSER_BUSY: another session is using Playwright. Wait ~2 min.\n"
            f"If session expired: run `python council_browser.py --save-session`\n"
        )

    synthesis = results.get("synthesis", {})
    summary = synthesis.get("summary", "")
    narrative = synthesis.get("narrative", "")
    response = synthesis.get("response") or ""

    # Guard against empty synthesis (browser completed but extracted 0 chars)
    if not response and not summary and not narrative:
        if not results.get("models"):
            return (
                f"# Research/Council Query — No Results\n\n"
                f"**Mode:** {results.get('mode', 'unknown')}\n"
                f"**Time:** {results.get('execution_time_ms', 0)/1000:.1f}s\n\n"
                f"The query completed but returned no synthesis text.\n"
                f"Check `~/.claude/council-cache/council_latest.json` for raw data.\n"
            )

    confidence = synthesis.get("confidence", "unknown")
    total_cost = results.get("total_cost", 0)
    elapsed = results.get("execution_time_ms", 0)

    agreements = synthesis.get("agreements", [])
    disagreements = synthesis.get("disagreements", [])
    actions = synthesis.get("recommended_actions", [])
    risks = synthesis.get("risks", [])
    insights = synthesis.get("unique_insights", [])

    sections = [
        f"# Council Synthesis (Opus 4.6 + Extended Thinking)",
        f"",
        f"**Query:** {results.get('query', '')}",
        f"**Confidence:** {confidence} | **Cost:** ${total_cost:.4f} | **Time:** {elapsed/1000:.1f}s",
        f"**Thinking tokens:** {synthesis.get('thinking_tokens', 0)}",
        f"",
        f"## Summary",
        summary,
        f"",
    ]

    if agreements:
        sections.append("## Agreements (all 3 models)")
        for a in agreements:
            sections.append(f"- {a}")
        sections.append("")

    if disagreements:
        sections.append("## Disagreements")
        for d in disagreements:
            if isinstance(d, dict):
                sections.append(f"- **{d.get('topic', '')}**: {d.get('assessment', '')}")
            else:
                sections.append(f"- {d}")
        sections.append("")

    if insights:
        sections.append("## Unique Insights")
        for i in insights:
            if isinstance(i, dict):
                sections.append(f"- [{i.get('model', '')}] {i.get('insight', '')} (value: {i.get('value', '')})")
            else:
                sections.append(f"- {i}")
        sections.append("")

    if actions:
        sections.append("## Recommended Actions")
        for a in actions:
            if isinstance(a, dict):
                pri = a.get("priority", "")
                act = a.get("action", "")
                rat = a.get("rationale", "")
                fp = a.get("file_path", "")
                line = f"{pri}. {act}"
                if rat:
                    line += f" — {rat}"
                if fp:
                    line += f" (`{fp}`)"
                sections.append(line)
            else:
                sections.append(f"- {a}")
        sections.append("")

    if risks:
        sections.append("## Risks")
        for r in risks:
            sections.append(f"- {r}")
        sections.append("")

    if narrative:
        sections.append("## Detailed Analysis")
        sections.append(narrative)
        sections.append("")

    # Degradation notes
    if results.get("degraded"):
        sections.append("## Degradation Notes")
        for entry in results.get("fallback_log", []):
            sections.append(f"- **{entry.get('decision', 'unknown')}**: {entry.get('reason', '')}")
        sections.append("")

    # Model error summary
    models = results.get("models", {})
    errors = [(k, v.get("error")) for k, v in models.items() if v.get("error")]
    if errors:
        sections.append("## Model Errors")
        for label, err in errors:
            sections.append(f"- **{label}**: {err}")
        sections.append("")

    sections.append(f"---")
    sections.append(f"Cache: ~/.claude/council-cache/council_latest.json")

    return "\n".join(sections)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hybrid council query engine")
    parser.add_argument("query", nargs="?", help="The question to ask the council")
    parser.add_argument("--mode", choices=["api", "direct", "browser", "auto"], default="browser")
    parser.add_argument("--context-file", help="Path to session context file")
    parser.add_argument("--read", action="store_true", help="Read cached synthesis")
    parser.add_argument("--read-full", action="store_true", help="Read full cached results")
    parser.add_argument("--read-model", help="Read specific model's response from cache")
    parser.add_argument("--headful", action="store_true", help="Run browser in visible mode")
    parser.add_argument("--opus-synthesis", action="store_true", help="Run Opus re-synthesis on browser results")
    parser.add_argument("--perplexity-mode", choices=["council", "research", "labs"], default="council",
        help="Perplexity slash command to use: /council (multi-model), /research (deep research), or /labs (experimental labs)")
    parser.add_argument("--auto-context", action="store_true",
        help="Auto-generate project context from git/CLAUDE.md/MEMORY.md")

    args = parser.parse_args()

    # Read mode — no network calls
    if args.read:
        print(read_cached("synthesis"))
        return
    if args.read_full:
        print(read_cached("full"))
        return
    if args.read_model:
        print(read_cached(args.read_model))
        return

    # Query mode — requires a query
    if not args.query:
        parser.error("Query is required unless using --read/--read-full/--read-model")

    # Startup validation
    if not print_validation(args.mode):
        sys.exit(1)

    # Load context if provided
    context = ""
    if args.context_file:
        ctx_path = Path(args.context_file)
        if ctx_path.exists():
            context = ctx_path.read_text(encoding="utf-8", errors="replace")

    # Auto-generate context from session_context.py if --auto-context and no explicit context
    AUTOMATION_DIR = Path(__file__).parent
    if args.auto_context and not context:
        try:
            import subprocess
            ctx_result = subprocess.run(
                [sys.executable, str(AUTOMATION_DIR / "session_context.py"), os.getcwd()],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            )
            if ctx_result.returncode == 0 and ctx_result.stdout.strip():
                context = ctx_result.stdout
                print(f"Auto-context: {len(context)} chars from session_context.py", file=sys.stderr)
            else:
                print(f"WARNING: auto-context returned no output (exit {ctx_result.returncode})", file=sys.stderr)
        except Exception as e:
            print(f"WARNING: auto-context failed: {e}", file=sys.stderr)

    if args.mode == "browser":
        results = asyncio.run(run_browser_query(
            args.query, context,
            headful=True if args.headful else None,
            opus_synthesis=args.opus_synthesis,
            perplexity_mode=args.perplexity_mode,
        ))
    elif args.mode == "auto":
        results = asyncio.run(run_auto_query(args.query, context))
    elif args.mode == "direct":
        from council_providers import query_direct_providers
        model_results = asyncio.run(query_direct_providers(ANALYSIS_MODELS, f"{context}\n\n{args.query}" if context else args.query, DIRECT_TIMEOUT))
        synthesis = run_opus_synthesis(args.query, model_results, context)
        fallback_log = []
        if synthesis.get("error"):
            fallback_log.append({"decision": "synthesis_error", "reason": synthesis["error"], "timestamp": datetime.now(timezone.utc).isoformat()})
        results = {
            "query": args.query,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "direct",
            "models": {r.get("label", "unknown"): r for r in model_results},
            "synthesis": {"model": synthesis.get("model"), **synthesis.get("parsed", {}), "cost": synthesis.get("cost", 0), "error": synthesis.get("error")},
            "total_cost": sum(r.get("cost", 0) for r in model_results) + synthesis.get("cost", 0),
            "execution_time_ms": 0,
            "fallback_log": fallback_log,
            "degraded": len(fallback_log) > 0,
        }
        save_results(results)
        append_run_log(results)
    else:
        results = asyncio.run(run_api_query(args.query, context))

    output = format_synthesis_output(results)
    if not output or not output.strip():
        print(
            f"WARNING: format_synthesis_output returned empty. "
            f"Mode: {results.get('mode')}, "
            f"error: {results.get('error')}, "
            f"synthesis keys: {list(results.get('synthesis', {}).keys())}",
            file=sys.stderr,
        )
        output = (
            "# Research/Council Query — Empty Output\n\n"
            "The query completed but produced no formatted output.\n"
            f"Check `~/.claude/council-cache/council_latest.json` for raw data.\n"
        )
    print(output)


if __name__ == "__main__":
    main()

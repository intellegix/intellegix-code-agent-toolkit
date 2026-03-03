"""Cross-platform Python loop driver for automated Claude Code + Perplexity research.

Primary entry point — replaces PowerShell as the main loop driver.
Uses subprocess.Popen() to stream NDJSON from claude CLI line-by-line,
handling the Windows hang bug (#25629) where stdout doesn't close after result.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import Result, RetryConfig, WorkflowConfig, load_config
from log_redactor import RedactingFilter
from ndjson_parser import ParsedStream, parse_ndjson_line, process_events
from research_bridge import ResearchBridge
from state_tracker import StateTracker

logger = logging.getLogger(__name__)

# Exit codes
EXIT_COMPLETE = 0
EXIT_MAX_ITERATIONS = 1
EXIT_BUDGET_EXCEEDED = 2
EXIT_STAGNATION = 3


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter for machine-readable output."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
        })


class LoopDriver:
    """Orchestrates the Claude Code -> Perplexity research loop."""

    def __init__(
        self,
        project_path: str | Path,
        config: WorkflowConfig,
        initial_prompt: str = "",
        dry_run: bool = False,
        smoke_test: bool = False,
        skip_preflight: bool = False,
        agent_id: Optional[str] = None,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.config = config
        self.dry_run = dry_run
        self.smoke_test = smoke_test
        self.skip_preflight = skip_preflight
        self.agent_id = agent_id

        if self.smoke_test:
            self.config.limits.max_iterations = 1
            self.config.limits.timeout_seconds = 120
            self.config.limits.max_per_iteration_budget_usd = 2.0
            self.config.limits.max_turns_per_iteration = 10

        self.initial_prompt = initial_prompt or self._default_prompt()
        self._stagnation_reset_done = False  # Track if session was already reset for stagnation
        self._consecutive_timeouts = 0
        self._consecutive_test_failures = 0
        self._gate_rejection_count: int = 0
        self._gate_existed_at_start: bool = self._check_gate_exists_at_start()
        self._using_fallback = False
        self._original_model: Optional[str] = None

        # Agent-aware state routing
        workflow_dir: Optional[Path] = None
        if self.agent_id:
            agent_state = self.config.multi_agent.agent_state_dir
            workflow_dir = self.project_path / agent_state / self.agent_id / ".workflow"
        self.tracker = StateTracker(self.project_path, workflow_dir=workflow_dir)
        self.bridge = ResearchBridge(
            self.project_path,
            retry_config=config.retry,
            research_timeout=config.perplexity.research_timeout_seconds,
            headful=config.perplexity.headful,
            perplexity_mode=config.perplexity.perplexity_mode,
            exploration_config=config.exploration,
            verification_config=config.verification,
        )

    def _check_gate_exists_at_start(self) -> bool:
        """Check if CLAUDE.md has a Completion Gate section at startup."""
        claude_md = self.project_path / "CLAUDE.md"
        checked, unchecked = self._parse_completion_gate(claude_md)
        return bool(checked or unchecked)

    def _default_prompt(self) -> str:
        """Generate a default prompt based on project state."""
        if self.smoke_test:
            return (
                "Review the current project. List the main files and their purpose briefly. "
                "Then output PROJECT_COMPLETE."
            )
        # Check for CLAUDE.md in project dir or parent (Claude CLI searches up)
        claude_md = self.project_path / "CLAUDE.md"
        if not claude_md.exists():
            claude_md = self.project_path.parent / "CLAUDE.md"
        if claude_md.exists():
            return (
                "Read CLAUDE.md first — it contains the current roadmap with phases and their status. "
                "Implement the first phase marked TODO. Do NOT output PROJECT_COMPLETE unless "
                "every phase in CLAUDE.md is marked COMPLETE."
            )
        return "Review the project and continue implementation from where we left off."

    def _write_trace_event(self, event_type: str, **data) -> None:
        """Append a structured event to .workflow/trace.jsonl for observability."""
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "iteration": self.tracker.state.iteration,
            **data,
        }
        if self.agent_id:
            event["agent_id"] = self.agent_id
        try:
            if self.agent_id:
                agent_state = self.config.multi_agent.agent_state_dir
                trace_path = (
                    self.project_path / agent_state / self.agent_id
                    / ".workflow" / "trace.jsonl"
                )
            else:
                trace_path = self.project_path / ".workflow" / "trace.jsonl"
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            # Rotate if over limit
            max_size = self.config.limits.trace_max_size_bytes
            if max_size > 0 and trace_path.exists() and trace_path.stat().st_size > max_size:
                rotated = trace_path.with_suffix(".jsonl.1")
                trace_path.replace(rotated)
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            logger.warning("Failed to write trace event: %s", e)

    def _compute_cooldown(self, timeout_count: int) -> int:
        """Compute exponential backoff cooldown after a timeout."""
        base = self.config.limits.timeout_cooldown_base_seconds
        cap = self.config.limits.timeout_cooldown_max_seconds
        if base == 0:
            return 0
        return min(base * (2 ** (timeout_count - 1)), cap)

    def _preflight_check(self) -> bool:
        """Verify Claude CLI is accessible before starting iterations."""
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.error(
                    "Claude CLI preflight failed: %s. "
                    "Check: 1) claude --version works 2) No other claude process blocking "
                    "3) Try: taskkill /F /IM claude.exe /T (Windows)",
                    result.stderr[:200],
                )
                return False
            logger.info("Claude CLI preflight OK: %s", result.stdout.strip()[:100])
        except FileNotFoundError:
            logger.error(
                "Claude CLI not found on PATH. "
                "Check: 1) claude --version works 2) No other claude process blocking "
                "3) Try: taskkill /F /IM claude.exe /T (Windows)"
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error(
                "Claude CLI preflight timed out (30s). "
                "Check: 1) claude --version works 2) No other claude process blocking "
                "3) Try: taskkill /F /IM claude.exe /T (Windows)"
            )
            return False

        # Run non-fatal project validation checks
        warnings = self._preflight_project_checks()
        for w in warnings:
            logger.warning("Preflight: %s", w)

        # Ensure .workflow/ is writable (fatal)
        try:
            workflow_dir = self.project_path / ".workflow"
            workflow_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error("Cannot create .workflow/ directory: %s", e)
            return False

        return True

    def _preflight_project_checks(self) -> list[str]:
        """Run non-fatal project validation checks. Returns list of warnings."""
        warnings: list[str] = []
        if not (self.project_path / "CLAUDE.md").exists():
            warnings.append("No CLAUDE.md found — Claude may lack project context")
        if not (self.project_path / ".git").exists():
            warnings.append("Not a git repo — session continuity features limited")
        # Perplexity session freshness
        session = Path.home() / ".claude" / "config" / "playwright-session.json"
        if not session.exists():
            warnings.append("No Perplexity session — research queries will fail")
        elif (time.time() - session.stat().st_mtime) > 86400:
            warnings.append("Perplexity session >24h old — may need refresh")
        return warnings

    def run(self) -> int:
        """Execute the main loop. Returns exit code."""
        logger.info("=" * 60)
        if self.smoke_test:
            logger.info("*** SMOKE TEST MODE ***")
        logger.info("Automated Claude Loop Driver (Python)")
        logger.info("Project: %s", self.project_path)
        logger.info("Max iterations: %d", self.config.limits.max_iterations)
        timeout_multiplier = self.config.limits.model_timeout_multipliers.get(
            self.config.claude.model, 1.0
        )
        effective_timeout = int(self.config.limits.timeout_seconds * timeout_multiplier)
        logger.info(
            "Timeout: %ds per iteration (base: %ds, multiplier: %.1fx for %s)",
            effective_timeout, self.config.limits.timeout_seconds,
            timeout_multiplier, self.config.claude.model,
        )
        logger.info("Model: %s", self.config.claude.model)
        logger.info("Dry run: %s", self.dry_run)
        logger.info("=" * 60)

        self.tracker.load()
        self.tracker.start_session()

        if not self.dry_run and not self.skip_preflight:
            if not self._preflight_check():
                self._write_trace_event("preflight_failed")
                self._write_metrics_summary(EXIT_STAGNATION)
                return EXIT_STAGNATION

        self._write_trace_event(
            "loop_start",
            max_iterations=self.config.limits.max_iterations,
            model=self.config.claude.model,
            dry_run=self.dry_run,
            smoke_test=self.smoke_test,
        )

        current_prompt = self.initial_prompt
        session_id: Optional[str] = self.tracker.validate_session_id(
            self.tracker.state.last_session_id
        )

        for i in range(1, self.config.limits.max_iterations + 1):
            logger.info("")
            logger.info("=" * 60)
            logger.info("ITERATION %d / %d", i, self.config.limits.max_iterations)
            logger.info("=" * 60)
            logger.info("Prompt: %s...", current_prompt[:200])

            self._write_trace_event(
                "claude_invoke",
                prompt_preview=current_prompt[:200],
                session_id=session_id,
            )

            start_time = time.monotonic()
            parsed = self._invoke_claude(current_prompt, session_id)
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Track session ID for --resume
            if parsed.session_id:
                session_id = parsed.session_id

            cost_usd = parsed.result.cost_usd if parsed.result else 0.0
            if parsed.result:
                num_turns = parsed.result.num_turns
            else:
                # Estimate turns from user events when no result (e.g., timeout)
                num_turns = sum(1 for e in parsed.events if e.type == "user")
                if num_turns > 0:
                    logger.info(
                        "No result event; estimated %d turns from streamed events",
                        num_turns,
                    )
            is_error = parsed.result.is_error if parsed.result else bool(parsed.errors)

            # Capture tool usage and file modification data
            tools_used = sorted(parsed.tools_used)
            files_modified = list(parsed.files_modified)
            git_diff_stats = self._capture_git_diff_stats()

            self._write_trace_event(
                "claude_complete",
                session_id=parsed.session_id,
                cost_usd=cost_usd,
                num_turns=num_turns,
                is_error=is_error,
                duration_ms=duration_ms,
                tools_used=tools_used,
                files_modified=files_modified,
                git_diff_stats=git_diff_stats,
            )

            self.tracker.increment_iteration()
            self.tracker.add_cycle(
                prompt=current_prompt,
                session_id=session_id,
                model=self.config.claude.model,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                num_turns=num_turns,
                is_error=is_error,
                tools_used=tools_used,
                files_modified=files_modified,
                git_diff_stats=git_diff_stats,
            )
            self.tracker.save()

            # Session rotation check (proactive context refresh)
            should_rotate, rotate_reason = self._should_rotate_session(session_id)
            if should_rotate:
                logger.info("Session rotation: %s", rotate_reason)
                self._write_trace_event(
                    "session_rotation",
                    reason=rotate_reason,
                    session_turns=self.tracker.get_session_turns(session_id),
                    session_cost=self.tracker.get_session_cost(session_id),
                )
                self.tracker.clear_session()
                session_id = None

            # Budget check
            budget_check = self.tracker.check_budget(
                per_iteration_limit=self.config.limits.max_per_iteration_budget_usd,
                total_limit=self.config.limits.max_total_budget_usd,
            )
            if not budget_check.success:
                logger.error(
                    "Budget exceeded: %s. Completed %d iterations. "
                    "Review .workflow/metrics_summary.json for cost breakdown.",
                    budget_check.error, i,
                )
                self._write_trace_event("budget_exceeded", error=budget_check.error)
                self.tracker.fail(budget_check.error or "Budget exceeded")
                self.tracker.save()
                self._write_trace_event("loop_end", exit_code=EXIT_BUDGET_EXCEEDED, status="budget_exceeded")
                self._write_metrics_summary(EXIT_BUDGET_EXCEEDED)
                return EXIT_BUDGET_EXCEEDED

            # Detect timeouts (no result event parsed, returncode was -1)
            timed_out = parsed.result is None and not parsed.errors
            if timed_out:
                self._consecutive_timeouts += 1
                logger.warning(
                    "Timeout detected (%d consecutive). Clearing session for fresh context.",
                    self._consecutive_timeouts,
                )

                # Phase 3: Diagnostic capture — distinguish "CLI never started" from "model slow"
                events_received = len(parsed.events)
                self._write_trace_event(
                    "timeout_detected",
                    consecutive_count=self._consecutive_timeouts,
                    ndjson_events_received=events_received,
                    had_session_id=parsed.session_id is not None,
                )
                if events_received == 0:
                    logger.warning(
                        "Timeout with ZERO events — CLI likely failed to start "
                        "(rate limit? PATH issue?)"
                    )

                self.tracker.clear_session()
                session_id = None

                # Phase 4: Model fallback — try fallback model before stagnation exit
                fallback_threshold = self.config.limits.model_fallback_after_timeouts
                fallback_model = self.config.limits.model_fallback.get(
                    self.config.claude.model
                )
                if (
                    self._consecutive_timeouts >= fallback_threshold
                    and fallback_model
                    and not self._using_fallback
                ):
                    logger.warning(
                        "Falling back from %s to %s after %d timeouts",
                        self.config.claude.model, fallback_model,
                        self._consecutive_timeouts,
                    )
                    self._write_trace_event(
                        "model_fallback",
                        from_model=self.config.claude.model,
                        to_model=fallback_model,
                    )
                    self._original_model = self.config.claude.model
                    self.config.claude.model = fallback_model
                    self._using_fallback = True
                    self._consecutive_timeouts = 0  # Reset counter for fallback model
                    current_prompt = self.initial_prompt
                    # Phase 1: Cooldown before retry
                    cooldown = self._compute_cooldown(1)
                    if cooldown > 0:
                        logger.info("Cooling down %ds before fallback retry", cooldown)
                        self._write_trace_event("timeout_cooldown", cooldown_seconds=cooldown)
                        time.sleep(cooldown)
                    continue

                # Apply model-aware consecutive timeout limit
                max_timeouts = self.config.stagnation.max_consecutive_timeouts
                model_timeout_override = self.config.stagnation.model_timeout_overrides.get(
                    self.config.claude.model
                )
                if model_timeout_override is not None:
                    max_timeouts = model_timeout_override

                if self._consecutive_timeouts >= max_timeouts:
                    reason = (
                        f"Stagnation: {self._consecutive_timeouts} consecutive timeouts "
                        f"(limit: {max_timeouts}). "
                        f"Recovery: 1) Review CLAUDE.md for unclear instructions "
                        f"2) Try --model opus for complex tasks "
                        f"3) Increase --timeout for large codebases"
                    )
                    logger.error(reason)
                    self._write_trace_event("stagnation_exit", reason=reason)
                    self.tracker.fail(reason)
                    self.tracker.save()
                    self._log_summary(i)
                    self._write_trace_event("loop_end", exit_code=EXIT_STAGNATION, status="stagnation")
                    self._write_metrics_summary(EXIT_STAGNATION)
                    return EXIT_STAGNATION

                # Phase 1: Cooldown before retry (exponential backoff)
                cooldown = self._compute_cooldown(self._consecutive_timeouts)
                if cooldown > 0:
                    logger.info(
                        "Cooling down %ds before retry (timeout #%d)",
                        cooldown, self._consecutive_timeouts,
                    )
                    self._write_trace_event("timeout_cooldown", cooldown_seconds=cooldown)
                    time.sleep(cooldown)

                current_prompt = self.initial_prompt
                continue
            else:
                # Reset timeout counter on success
                self._consecutive_timeouts = 0
                # Revert fallback model after productive iteration
                if (
                    self._using_fallback
                    and num_turns > self.config.stagnation.low_turn_threshold
                ):
                    logger.info(
                        "Reverting from fallback %s to primary %s",
                        self.config.claude.model, self._original_model,
                    )
                    self._write_trace_event(
                        "model_fallback_revert",
                        from_model=self.config.claude.model,
                        to_model=self._original_model,
                    )
                    self.config.claude.model = self._original_model
                    self._using_fallback = False

            # Check for errors
            if is_error:
                logger.warning("Claude returned an error. Clearing session for fresh start.")
                self.tracker.clear_session()
                session_id = None
                current_prompt = (
                    "The previous iteration encountered an error. "
                    "Please review the current state and continue from where we left off."
                )
                continue

            # Check for stagnation (diminishing returns)
            stagnation_check = self._check_stagnation()
            if not stagnation_check.success:
                if not self._stagnation_reset_done:
                    # First stagnation: try a session reset before giving up
                    logger.warning(
                        "Diminishing returns detected: %s. Resetting session for one more try.",
                        stagnation_check.error,
                    )
                    self._write_trace_event(
                        "stagnation_reset",
                        reason=stagnation_check.error,
                    )
                    self._stagnation_reset_done = True
                    self.tracker.clear_session()
                    session_id = None
                    current_prompt = self.initial_prompt
                    continue
                else:
                    # Already reset once — exit gracefully
                    logger.error(
                        "Stagnation persists after session reset: %s. "
                        "Recovery: 1) Review CLAUDE.md for unclear instructions "
                        "2) Try --model opus for complex tasks "
                        "3) Increase --timeout for large codebases",
                        stagnation_check.error,
                    )
                    self._write_trace_event(
                        "stagnation_exit",
                        reason=stagnation_check.error,
                    )
                    self.tracker.fail(stagnation_check.error or "Stagnation detected")
                    self.tracker.save()
                    self._log_summary(i)
                    self._write_trace_event("loop_end", exit_code=EXIT_STAGNATION, status="stagnation")
                    self._write_metrics_summary(EXIT_STAGNATION)
                    return EXIT_STAGNATION

            # Reset stagnation flag on productive iteration
            if num_turns > self.config.stagnation.low_turn_threshold:
                self._stagnation_reset_done = False

            # Check for completion markers
            output_text = ""
            if parsed.result:
                output_text = parsed.result.result_text
            output_text += " " + parsed.assistant_text
            if self._check_completion(output_text):
                # Validate against completion gate
                gate_valid, rejection_reason = self._validate_completion_gate(parsed)
                if not gate_valid:
                    self._gate_rejection_count += 1
                    max_rej = self.config.completion_gate.max_rejections
                    logger.warning(
                        "COMPLETION REJECTED (%d/%d): %s",
                        self._gate_rejection_count, max_rej, rejection_reason,
                    )
                    self._write_trace_event(
                        "completion_gate_rejected",
                        reason=rejection_reason,
                        rejection_count=self._gate_rejection_count,
                    )
                    if self._gate_rejection_count >= max_rej:
                        logger.error(
                            "Max completion gate rejections (%d) reached -- exiting as stagnation",
                            max_rej,
                        )
                        self.tracker.fail(f"Completion gate rejected {max_rej} times")
                        self.tracker.save()
                        self._log_summary(i)
                        self._write_trace_event("loop_end", exit_code=EXIT_STAGNATION, status="stagnation")
                        self._write_metrics_summary(EXIT_STAGNATION)
                        return EXIT_STAGNATION
                    current_prompt = (
                        f"COMPLETION REJECTED (attempt {self._gate_rejection_count}/{max_rej}): "
                        "You signaled PROJECT_COMPLETE but the completion gate in CLAUDE.md "
                        "has unchecked items.\n\n"
                        f"{rejection_reason}\n\n"
                        "Complete the remaining items, then check them off in "
                        "CLAUDE.md before signaling completion again."
                    )
                    continue  # Skip research, go to next iteration with rejection prompt

                # Gate passed (or no gate) -- reset counter
                self._gate_rejection_count = 0
                logger.info("Completion marker detected! Project is complete.")
                self._write_trace_event("completion_detected")
                self.tracker.complete()
                self.tracker.save()
                self._run_post_review()
                self._log_summary(i)
                self._write_trace_event("loop_end", exit_code=EXIT_COMPLETE, status="completed")
                self._write_metrics_summary(EXIT_COMPLETE)
                return EXIT_COMPLETE

            # Post-execution validation (only on productive, non-error iterations)
            if (not timed_out and not is_error
                    and num_turns > self.config.stagnation.low_turn_threshold):
                validation = self._run_post_validation()
                if (validation.success and validation.data
                        and not validation.data.get("skipped")
                        and not validation.data.get("passed")):
                    self._consecutive_test_failures += 1
                    if (self.config.validation.fail_action == "inject"
                            and self._consecutive_test_failures < self.config.validation.max_consecutive_failures):
                        test_output = validation.data.get("stdout_tail", "")
                        current_prompt = (
                            "CRITICAL: Tests are failing after your changes. Fix them before continuing.\n\n"
                            f"Test output:\n{test_output}\n\n"
                            "Re-run the test suite to verify the fix, then continue."
                        )
                        continue
                    elif self._consecutive_test_failures >= self.config.validation.max_consecutive_failures:
                        logger.warning(
                            "Max consecutive test failures (%d) — falling back to warn mode",
                            self.config.validation.max_consecutive_failures,
                        )
                else:
                    self._consecutive_test_failures = 0

            # Query Perplexity for next steps
            logger.info("Querying Perplexity for next steps...")
            self._write_trace_event("research_start")
            focus_area = self._derive_focus_area(parsed)
            if focus_area:
                logger.info("Research focus area: %s", focus_area[:100])
            research_result = self.bridge.query(focus_area=focus_area)
            self._write_trace_event(
                "research_complete",
                success=research_result.success,
                error_code=research_result.error_code,
            )
            if research_result.success and research_result.data:
                research_text = research_result.data.response
            else:
                logger.warning(
                    "Research failed [%s]: %s — using fallback prompt",
                    research_result.error_code, research_result.error,
                )
                research_text = "Continue implementing the current plan."

            # Plan verification: send research through Perplexity for critique
            if (
                self.config.verification.enabled
                and research_result.success
                and research_result.data
            ):
                logger.info("Running plan verification...")
                self._write_trace_event("verification_start")
                verification = self.bridge.verify_plan(
                    plan_text=research_text,
                    original_research=research_text,
                    codebase_context=self.bridge.last_codebase_context,
                )
                self._write_trace_event(
                    "verification_complete",
                    success=verification.success,
                )
                if verification.success and verification.data:
                    research_text = self._merge_research_and_verification(
                        research_text, verification.data.response
                    )
                    logger.info("Verification critique merged into prompt")
                else:
                    logger.warning(
                        "Verification failed [%s]: %s — using unverified research",
                        verification.error_code, verification.error,
                    )

            current_prompt = self._build_next_prompt(research_text)
            logger.info("Next prompt built (%d chars)", len(current_prompt))

        # Hit max iterations
        logger.warning("Reached max iterations (%d)", self.config.limits.max_iterations)
        self.tracker.fail(f"Max iterations reached ({self.config.limits.max_iterations})")
        self.tracker.save()
        self._log_summary(self.config.limits.max_iterations)
        self._write_trace_event("loop_end", exit_code=EXIT_MAX_ITERATIONS, status="max_iterations")
        self._write_metrics_summary(EXIT_MAX_ITERATIONS)
        return EXIT_MAX_ITERATIONS

    def _invoke_claude(
        self, prompt: str, resume_session_id: Optional[str] = None
    ) -> ParsedStream:
        """Spawn claude CLI and stream NDJSON output line-by-line.

        Uses subprocess.Popen instead of subprocess.run to handle the Windows
        hang bug (#25629) where Claude CLI doesn't close stdout after the result
        event. We read line-by-line, stop when we see the result event, and kill
        the process ourselves.
        """
        # Apply model-aware max-turns cap
        max_turns = self.config.limits.max_turns_per_iteration
        model_turns_override = self.config.limits.model_max_turns_override.get(
            self.config.claude.model
        )
        if model_turns_override is not None:
            max_turns = min(max_turns, model_turns_override)
            logger.info(
                "Model %s: capping turns to %d (config: %d)",
                self.config.claude.model, max_turns,
                self.config.limits.max_turns_per_iteration,
            )

        args = [
            "claude", "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--model", self.config.claude.model,
            "--max-turns", str(max_turns),
        ]

        if self.config.claude.dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")

        if resume_session_id:
            args.extend(["--resume", resume_session_id])

        logger.info("Spawning: %s", " ".join(args[:6]) + "...")

        if self.dry_run:
            logger.info("[DRY RUN] Would execute: %s", " ".join(args))
            return self._dry_run_result()

        # Apply model-aware timeout multiplier
        timeout_multiplier = self.config.limits.model_timeout_multipliers.get(
            self.config.claude.model, 1.0
        )
        effective_timeout = int(self.config.limits.timeout_seconds * timeout_multiplier)

        # Build env with agent ID if running as part of multi-agent
        proc_env = None
        if self.agent_id:
            import os
            proc_env = os.environ.copy()
            proc_env["CLAUDE_AGENT_ID"] = self.agent_id

        try:
            proc = subprocess.Popen(
                args,
                cwd=str(self.project_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=proc_env,
            )
        except FileNotFoundError:
            logger.error("claude CLI not found. Ensure 'claude' is on PATH.")
            return ParsedStream()

        # Timer kills process on timeout (handles Windows hang bug)
        timed_out = False

        def _on_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            logger.warning(
                "Claude timed out after %ds (base: %ds, multiplier: %.1fx for %s)",
                effective_timeout,
                self.config.limits.timeout_seconds,
                timeout_multiplier,
                self.config.claude.model,
            )
            self._kill_process_tree(proc.pid)
            # Fallback: Python-native kill in case taskkill failed
            try:
                proc.kill()
            except OSError:
                pass

        timer = threading.Timer(effective_timeout, _on_timeout)
        timer.start()

        # Drain stderr in background to prevent pipe buffer deadlock
        stderr_lines: list[str] = []
        stderr_thread = threading.Thread(
            target=self._drain_pipe, args=(proc.stderr, stderr_lines), daemon=True
        )
        stderr_thread.start()

        # Read stdout line-by-line, parsing NDJSON events
        # Use explicit readline() for more responsive pipe reading
        logger.debug("Claude PID: %d, reading NDJSON events...", proc.pid)
        events: list = []
        line_count = 0
        deadline = time.monotonic() + effective_timeout + 30  # 30s grace beyond timer
        try:
            while True:
                # Secondary timeout: break if well past deadline (timer kill failed)
                if time.monotonic() > deadline:
                    logger.error(
                        "Readline deadline exceeded (timer kill likely failed). "
                        "Force-killing PID %d.", proc.pid,
                    )
                    timed_out = True
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    self._kill_process_tree(proc.pid)
                    break
                line = proc.stdout.readline()
                if not line:
                    break  # EOF — pipe closed
                line_count += 1
                event = parse_ndjson_line(line)
                if event:
                    events.append(event)
                    logger.debug(
                        "NDJSON event #%d: type=%s", len(events), event.type
                    )
                    if event.type == "result":
                        break
        except (OSError, ValueError):
            pass  # Pipe closed by timeout kill
        finally:
            timer.cancel()
            self._kill_process_tree(proc.pid)
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        logger.debug(
            "Read %d lines, %d events from Claude stdout", line_count, len(events)
        )
        parsed = process_events(events)

        # Log stderr
        stderr_thread.join(timeout=2)
        stderr_text = "".join(stderr_lines)
        if stderr_text:
            logger.debug("Claude stderr (first 500 chars): %s", stderr_text[:500])

        returncode = proc.returncode or 0
        if timed_out and not parsed.result:
            returncode = -1

        if returncode != 0 and resume_session_id:
            logger.warning(
                "Claude exited non-zero (%d) with --resume %s — session may have expired",
                returncode, resume_session_id,
            )

        if parsed.result:
            logger.info(
                "Claude finished: session=%s, cost=$%.4f, turns=%d",
                parsed.session_id, parsed.result.cost_usd, parsed.result.num_turns,
            )

        return parsed

    @staticmethod
    def _kill_process_tree(pid: int) -> None:
        """Kill a process and its children by PID."""
        if sys.platform == "win32":
            try:
                result = subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid), "/T"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    logger.debug("taskkill PID %d succeeded", pid)
                else:
                    logger.warning(
                        "taskkill PID %d failed (rc=%d): %s",
                        pid, result.returncode, result.stderr[:200],
                    )
            except Exception as e:
                logger.warning("taskkill PID %d exception: %s", pid, e)
        else:
            import signal
            try:
                import os
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    @staticmethod
    def _drain_pipe(pipe, lines: list[str]) -> None:
        """Drain a pipe into a list of lines (for background thread)."""
        try:
            for line in pipe:
                lines.append(line)
        except (OSError, ValueError):
            pass

    def _capture_git_diff_stats(self) -> Optional[dict]:
        """Capture git diff --stat summary for the current HEAD.

        Returns {"files_changed": N, "insertions": N, "deletions": N} or None.
        """
        try:
            result = subprocess.run(
                ["git", "diff", "--stat", "HEAD"],
                cwd=str(self.project_path),
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return None
            # Parse summary line: " N files changed, N insertions(+), N deletions(-)"
            for line in reversed(result.stdout.strip().splitlines()):
                if "changed" in line:
                    stats: dict[str, int] = {"files_changed": 0, "insertions": 0, "deletions": 0}
                    parts = line.strip().split(",")
                    for part in parts:
                        part = part.strip()
                        if "file" in part and "changed" in part:
                            stats["files_changed"] = int(part.split()[0])
                        elif "insertion" in part:
                            stats["insertions"] = int(part.split()[0])
                        elif "deletion" in part:
                            stats["deletions"] = int(part.split()[0])
                    return stats
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None

    def _run_post_validation(self) -> Result:
        """Run post-execution test validation if enabled.

        Returns Result with data dict: {"skipped": True} if disabled/not applicable,
        {"passed": bool, "stdout_tail": str, "returncode": int} otherwise.
        """
        if not self.config.validation.enabled:
            return Result.ok({"skipped": True})

        self._write_trace_event("validation_start")

        try:
            cmd = shlex.split(self.config.validation.test_command)
            result = subprocess.run(
                cmd,
                cwd=str(self.project_path),
                capture_output=True, text=True,
                timeout=self.config.validation.test_timeout_seconds,
            )
            passed = result.returncode == 0
            stdout_tail = result.stdout[-500:] if result.stdout else ""
            stderr_tail = result.stderr[-200:] if result.stderr else ""

            self._write_trace_event(
                "validation_complete",
                passed=passed,
                returncode=result.returncode,
            )

            if passed:
                logger.info("Post-execution validation passed")
            else:
                logger.warning(
                    "Post-execution validation FAILED (rc=%d): %s",
                    result.returncode, stderr_tail[:200],
                )

            return Result.ok({
                "passed": passed,
                "stdout_tail": stdout_tail,
                "returncode": result.returncode,
            })

        except subprocess.TimeoutExpired:
            logger.warning(
                "Post-execution validation timed out after %ds",
                self.config.validation.test_timeout_seconds,
            )
            self._write_trace_event(
                "validation_timeout",
                timeout_seconds=self.config.validation.test_timeout_seconds,
            )
            return Result.ok({"skipped": True, "timeout": True})

        except FileNotFoundError as e:
            logger.warning("Validation command not found: %s", e)
            return Result.fail(f"Validation command not found: {e}", "FILE_NOT_FOUND")

    def _run_post_review(self) -> None:
        """Run post-completion Perplexity quality review.

        Failures are logged as warnings and never block completion.
        """
        if not self.config.post_review.enabled:
            return

        logger.info("Running post-completion Perplexity quality review...")
        self._write_trace_event("post_review_start")

        try:
            result = self.bridge.post_review(
                focus_area=self.config.post_review.focus_area,
                timeout=self.config.post_review.timeout_seconds,
                save_result=self.config.post_review.save_result,
            )

            if result.success:
                logger.info("Post-completion review complete")
                self._write_trace_event(
                    "post_review_complete",
                    verdict_preview=result.data.response[:200] if result.data else "",
                )
            else:
                logger.warning(
                    "Post-completion review failed [%s]: %s",
                    result.error_code, result.error,
                )
                self._write_trace_event(
                    "post_review_failed",
                    error_code=result.error_code,
                    error=result.error,
                )
        except Exception as e:
            logger.warning("Post-completion review unexpected error: %s", e)
            self._write_trace_event(
                "post_review_failed",
                error_code="UNEXPECTED",
                error=str(e),
            )

    def _dry_run_result(self) -> ParsedStream:
        """Return a simulated ParsedStream for dry runs."""
        from ndjson_parser import ClaudeEvent, ClaudeResult

        parsed = ParsedStream()
        parsed.session_id = f"dry-run-{int(time.time())}"
        parsed.assistant_text = "[DRY RUN] Simulated output"
        parsed.result = ClaudeResult(
            session_id=parsed.session_id,
            cost_usd=0.0,
            duration_ms=0.0,
            num_turns=0,
            result_text="[DRY RUN] No actual execution",
            is_error=False,
        )
        return parsed

    def _should_rotate_session(self, session_id: Optional[str]) -> tuple[bool, str]:
        """Check if the current session should be rotated for fresh context.

        Returns (should_rotate, reason). Checks:
        1. Hard turn limit per session
        2. Hard cost limit per session
        3. Behavioral: recent low-productivity iterations suggest context exhaustion
        """
        cfg = self.config.stagnation
        if not cfg.enabled or not session_id:
            return False, ""

        # Check 1: Hard turn limit
        session_turns = self.tracker.get_session_turns(session_id)
        if session_turns >= cfg.session_max_turns:
            return True, (
                f"Session turn limit reached: {session_turns} >= {cfg.session_max_turns}"
            )

        # Check 2: Hard cost limit
        session_cost = self.tracker.get_session_cost(session_id)
        if session_cost >= cfg.session_max_cost_usd:
            return True, (
                f"Session cost limit reached: ${session_cost:.2f} >= ${cfg.session_max_cost_usd:.2f}"
            )

        # Check 3: Behavioral — context exhaustion detection
        cycles = self.tracker.state.cycles
        if len(cycles) >= cfg.context_exhaustion_window:
            window = cycles[-cfg.context_exhaustion_window:]
            low_count = sum(
                1 for c in window
                if c.num_turns < cfg.context_exhaustion_turn_threshold
                and c.session_id == session_id
            )
            # Require majority of window to be low-productivity
            threshold = cfg.context_exhaustion_window - 1  # e.g., 2 of 3
            if low_count >= threshold:
                return True, (
                    f"Context exhaustion: {low_count}/{cfg.context_exhaustion_window} "
                    f"recent iterations below {cfg.context_exhaustion_turn_threshold} turns"
                )

        return False, ""

    def _check_stagnation(self) -> Result:
        """Detect diminishing returns from recent cycle history.

        Examines a sliding window of recent cycles for signals that the loop
        is spinning without progress: all low-turn iterations, consecutive
        timeouts, or all zero-cost iterations (context exhaustion).
        """
        cfg = self.config.stagnation
        if not cfg.enabled:
            return Result.ok(None)

        cycles = self.tracker.state.cycles
        if len(cycles) < cfg.window_size:
            return Result.ok(None)

        window = cycles[-cfg.window_size:]

        # All iterations in window had very few turns (Claude barely worked)
        all_low_turns = all(c.num_turns <= cfg.low_turn_threshold for c in window)
        if all_low_turns:
            return Result.fail(
                f"Stagnation: last {cfg.window_size} iterations all had "
                f"<= {cfg.low_turn_threshold} turns",
                "STAGNATION_LOW_TURNS",
            )

        # All iterations in window cost $0 (context exhaustion / no work done)
        all_zero_cost = all(c.cost_usd == 0.0 for c in window)
        if all_zero_cost:
            return Result.fail(
                f"Stagnation: last {cfg.window_size} iterations all cost $0.00",
                "STAGNATION_ZERO_COST",
            )

        return Result.ok(None)

    def _check_completion(self, text: str) -> bool:
        """Check if output text contains any completion markers (case-insensitive)."""
        for marker in self.config.patterns.completion_markers:
            if re.search(re.escape(marker), text, re.IGNORECASE):
                return True
        return False

    def _parse_completion_gate(self, claude_md_path: Path) -> tuple[list[str], list[str]]:
        """Parse CLAUDE.md for Completion Gate checklist. Returns (checked, unchecked)."""
        if not claude_md_path.exists():
            return ([], [])
        try:
            content = claude_md_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        except OSError as e:
            logger.warning("Failed to read CLAUDE.md for gate: %s", e)
            return ([], [])

        marker = self.config.completion_gate.section_marker
        lines = content.split("\n")
        in_gate = False
        checked: list[str] = []
        unchecked: list[str] = []
        for line in lines:
            if line.strip() == marker:
                in_gate = True
                continue
            if in_gate and line.startswith("#"):
                break
            if in_gate:
                s = line.strip()
                if s.startswith("- [x]") or s.startswith("- [X]"):
                    checked.append(s[5:].strip())
                elif s.startswith("- [ ]"):
                    unchecked.append(s[5:].strip())
        return (checked, unchecked)

    def _validate_completion_gate(self, parsed: ParsedStream) -> tuple[bool, Optional[str]]:
        """Validate completion gate. Returns (is_valid, rejection_reason)."""
        if not self.config.completion_gate.enabled:
            return (True, None)
        claude_md = self.project_path / "CLAUDE.md"
        checked, unchecked = self._parse_completion_gate(claude_md)
        if not checked and not unchecked:
            if self._gate_existed_at_start:
                # Gate was present at startup but is now missing — evasion detected
                return (False, "Completion gate section was present at startup but is now missing from CLAUDE.md. Restore the '## Completion Gate' section with checklist items.")
            return (True, None)  # No gate at startup = backward compat
        if unchecked:
            reason = (
                f"Completion gate rejected: {len(unchecked)} unchecked item(s) in CLAUDE.md:\n"
                + "\n".join(f"  - [ ] {item}" for item in unchecked)
            )
            return (False, reason)
        logger.info("Completion gate passed: %d/%d items checked", len(checked), len(checked))
        return (True, None)

    def _merge_research_and_verification(
        self, research: str, verification: str
    ) -> str:
        """Merge research results with verification critique."""
        return (
            f"{research}\n\n---\n## Plan Verification Critique\n\n"
            f"{verification}\n\nIMPORTANT: Address issues above before implementing."
        )

    def _derive_focus_area(self, parsed: ParsedStream) -> Optional[str]:
        """Derive a research focus area from the last iteration's work.

        Priority: BLUEPRINT.md current phase > last commit messages > files modified > initial prompt.
        """
        # 1. Check BLUEPRINT.md for current phase
        blueprint = self.project_path / "BLUEPRINT.md"
        if blueprint.exists():
            try:
                text = blueprint.read_text(encoding="utf-8")[:2000]
                # Look for TODO/IN_PROGRESS phases
                for line in text.splitlines():
                    if any(marker in line.upper() for marker in ("TODO", "IN_PROGRESS", "IN PROGRESS")):
                        return line.strip().lstrip("#- ").strip()
            except OSError:
                pass

        # 2. Last commit messages (from git log)
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-3"],
                cwd=str(self.project_path),
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                commits = result.stdout.strip().splitlines()
                return f"Recent work: {'; '.join(c.split(' ', 1)[1] if ' ' in c else c for c in commits[:3])}"
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

        # 3. Files modified in last iteration
        if parsed.files_modified:
            files = list(parsed.files_modified)[:5]
            return f"Files modified in last iteration: {', '.join(files)}"

        # 4. Fall back to initial prompt summary
        if self.initial_prompt:
            return self.initial_prompt[:200]

        return None

    def _build_next_prompt(self, research_response: str) -> str:
        """Format structured research results into the next Claude prompt."""
        return (
            "Continue the implementation. Below is a structured strategic analysis from research.\n"
            "Pay attention to IMMEDIATE NEXT STEPS for your priority actions and BLOCKERS for issues to resolve.\n\n"
            f"{research_response}\n\n"
            "Focus on the highest priority item from IMMEDIATE NEXT STEPS. "
            "If all tasks are complete, output PROJECT_COMPLETE."
        )

    def _log_summary(self, iterations: int) -> None:
        """Log final loop summary."""
        metrics = self.tracker.get_metrics()
        logger.info("")
        logger.info("=" * 60)
        logger.info("LOOP %s", "COMPLETE" if self.tracker.state.status == "completed" else "ENDED")
        logger.info("Total iterations: %d", iterations)
        logger.info("Total cost: $%.4f", metrics.total_cost_usd)
        logger.info("Total turns: %d", metrics.total_turns)
        logger.info("Errors: %d", metrics.error_count)
        logger.info("=" * 60)

    def _write_metrics_summary(self, exit_code: int) -> None:
        """Write a metrics summary JSON file on loop completion."""
        metrics = self.tracker.get_metrics()
        analytics = self.tracker.compute_model_analytics()

        # Aggregate tool usage counts across all cycles
        tool_counts: dict[str, int] = {}
        all_files: list[str] = []
        for cycle in self.tracker.state.cycles:
            for tool in cycle.tools_used:
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
            all_files.extend(cycle.files_modified)
        # Deduplicate files
        total_files_modified = list(dict.fromkeys(all_files))

        summary = {
            "exit_code": exit_code,
            "status": self.tracker.state.status,
            "iterations": self.tracker.state.iteration,
            "total_cost_usd": metrics.total_cost_usd,
            "total_turns": metrics.total_turns,
            "error_count": metrics.error_count,
            "total_duration_ms": metrics.total_duration_ms,
            "model_analytics": {k: v.model_dump() for k, v in analytics.items()},
            "tool_usage_counts": tool_counts,
            "total_files_modified": total_files_modified,
        }
        if self.agent_id:
            agent_state = self.config.multi_agent.agent_state_dir
            path = (
                self.project_path / agent_state / self.agent_id
                / ".workflow" / "metrics_summary.json"
            )
        else:
            path = self.project_path / ".workflow" / "metrics_summary.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            logger.info("Metrics summary written to %s", path)
        except Exception as e:
            logger.warning("Failed to write metrics summary: %s", e)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Automated Claude Code + Perplexity Research Loop"
    )
    parser.add_argument("--project", default=".", help="Project directory path")
    parser.add_argument("--max-iterations", type=int, default=None, help="Max loop iterations")
    parser.add_argument("--model", default=None, help="Claude model (sonnet, opus, haiku)")
    parser.add_argument("--prompt", default="", help="Initial prompt for first iteration")
    parser.add_argument("--timeout", type=int, default=None, help="Per-iteration timeout in seconds")
    parser.add_argument("--max-budget", type=float, default=None, help="Max total budget in USD")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without spawning Claude")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--json-log", action="store_true", help="Output structured JSON logs")
    parser.add_argument("--smoke-test", action="store_true", help="Safe single-iteration production validation")
    parser.add_argument("--no-stagnation-check", action="store_true", help="Disable diminishing returns detection")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip Claude CLI preflight check")
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument("--agent-id", default=None, help="Agent ID for multi-agent mode (e.g., agent-1)")
    args = parser.parse_args()

    # Setup logging with redaction
    log_level = logging.DEBUG if args.verbose else logging.INFO
    if args.json_log:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter(datefmt="%Y-%m-%d %H:%M:%S"))
        logging.root.addHandler(handler)
        logging.root.setLevel(log_level)
    else:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    from config import SecurityConfig
    sec = SecurityConfig()
    for handler in logging.root.handlers:
        handler.addFilter(RedactingFilter(sec.log_redact_patterns))

    # Load config
    project_path = Path(args.project).resolve()
    config_path = args.config or (project_path / ".workflow" / "config.json")
    config_result = load_config(config_path)
    if not config_result.success:
        logger.error("Config error: %s", config_result.error)
        sys.exit(1)
    config = config_result.data

    # Apply CLI overrides
    if args.max_iterations is not None:
        config.limits.max_iterations = args.max_iterations
    if args.model is not None:
        config.claude.model = args.model
    if args.timeout is not None:
        config.limits.timeout_seconds = args.timeout
    if args.max_budget is not None:
        config.limits.max_total_budget_usd = args.max_budget
    if args.no_stagnation_check:
        config.stagnation.enabled = False

    driver = LoopDriver(
        project_path=project_path,
        config=config,
        initial_prompt=args.prompt,
        dry_run=args.dry_run,
        smoke_test=args.smoke_test,
        skip_preflight=args.skip_preflight,
        agent_id=args.agent_id,
    )
    exit_code = driver.run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

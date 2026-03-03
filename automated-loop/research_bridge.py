"""Research bridge for automated Claude loop — queries Perplexity via Playwright browser automation.

Gathers project context (CLAUDE.md, MEMORY.md, git log, workflow state)
and builds a structured research query for Perplexity to determine next steps.
Uses council_browser.py subprocess for Playwright-based Perplexity research.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from config import ExplorationConfig, PostReviewConfig, Result, RetryConfig, SecurityConfig, VerificationConfig

logger = logging.getLogger(__name__)

COUNCIL_BROWSER_SCRIPT = Path.home() / ".claude" / "council-automation" / "council_browser.py"


class ResearchResult(BaseModel):
    """Result from a Perplexity research query."""

    query: str
    response: str
    model: str = "perplexity-research"
    cost_estimate: float = 0.0
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class SessionContext:
    """Gathers project context for research queries."""

    def __init__(self, project_path: str | Path) -> None:
        self.project_path = Path(project_path)

    def gather(self) -> dict[str, str]:
        """Collect all available context from the project."""
        ctx: dict[str, str] = {}

        # CLAUDE.md
        claude_md = self.project_path / "CLAUDE.md"
        if claude_md.exists():
            ctx["claude_md"] = claude_md.read_text(encoding="utf-8")[:3000]

        # MEMORY.md (check project and auto-memory dir)
        memory_md = self.project_path / "MEMORY.md"
        if memory_md.exists():
            ctx["memory_md"] = memory_md.read_text(encoding="utf-8")[:2000]

        # Workflow state
        state_file = self.project_path / ".workflow" / "state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                ctx["workflow_state"] = json.dumps(
                    {
                        "iteration": state.get("iteration", 0),
                        "status": state.get("status", "unknown"),
                        "metrics": state.get("metrics", {}),
                        "last_session_id": state.get("last_session_id"),
                    },
                    indent=2,
                )
            except (json.JSONDecodeError, KeyError):
                pass

        # Git log (last 10 commits)
        git_log = self._get_git_log()
        if git_log:
            ctx["git_log"] = git_log

        # Recent research result
        research_file = self.project_path / ".workflow" / "research_result.md"
        if research_file.exists():
            ctx["last_research"] = research_file.read_text(encoding="utf-8")[:2000]

        return ctx

    def explore_codebase(
        self, max_files: int = 10, max_chars: int = 3000
    ) -> dict[str, str]:
        """Read key project files for research context.

        Strategy: git diff --name-only HEAD~5 HEAD for recently changed files,
        plus structural files (README.md, pyproject.toml, setup.py, etc.)
        Falls back to globbing *.py / *.ts in project root if not a git repo.
        Skips files >50KB and binary files.
        """
        SKIP_PATTERNS = {
            ".git", "__pycache__", "node_modules", ".venv", "venv",
            ".mypy_cache", ".pytest_cache", "dist", "build", ".egg-info",
        }
        STRUCTURAL_FILES = [
            "README.md", "pyproject.toml", "setup.py", "setup.cfg",
            "package.json", "tsconfig.json", "Makefile", "requirements.txt",
        ]
        MAX_FILE_SIZE = 50_000  # Skip files >50KB

        files_to_read: list[Path] = []

        # 1. Recently changed files via git
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~5", "HEAD"],
                cwd=str(self.project_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                for name in result.stdout.strip().splitlines():
                    p = self.project_path / name
                    if p.exists() and p.is_file():
                        files_to_read.append(p)
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

        # 2. Structural files
        for name in STRUCTURAL_FILES:
            p = self.project_path / name
            if p.exists() and p not in files_to_read:
                files_to_read.append(p)

        # 3. Fallback: glob for source files if no git results
        if not files_to_read:
            for pattern in ("*.py", "*.ts", "*.js"):
                for p in self.project_path.glob(pattern):
                    if p.is_file() and p not in files_to_read:
                        files_to_read.append(p)

        # Filter and read
        result_files: dict[str, str] = {}
        for p in files_to_read[:max_files]:
            # Skip files in ignored directories
            if any(skip in p.parts for skip in SKIP_PATTERNS):
                continue
            try:
                if p.stat().st_size > MAX_FILE_SIZE:
                    continue
                content = p.read_text(encoding="utf-8", errors="replace")
                relative = str(p.relative_to(self.project_path))
                result_files[relative] = content[:max_chars]
            except (OSError, ValueError):
                continue

        return result_files

    def _get_git_log(self) -> Optional[str]:
        """Get recent git log, returns None if not a git repo."""
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-10"],
                cwd=str(self.project_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        return None


VERIFICATION_PROMPT = """You are a senior code reviewer validating an implementation plan.

ORIGINAL RESEARCH RECOMMENDATIONS:
{original_research}

PROPOSED PLAN:
{plan_text}

CODEBASE CONTEXT:
{codebase_summary}

Critically evaluate this plan for:
1. LOGICAL ERRORS: Contradictions with research or codebase
2. MISSING EDGE CASES: What could go wrong that isn't addressed
3. FILE PATH ACCURACY: Do referenced files match the codebase
4. DEPENDENCY ORDERING: Are phase prerequisites correct
5. SCOPE CREEP: Work not supported by the research
6. FEASIBILITY: Is the plan realistic given the codebase state

Respond with:
- VERDICT: APPROVED | NEEDS_REVISION | MAJOR_ISSUES
- ISSUES: Numbered list of problems
- SUGGESTIONS: Concrete fixes
- RISK_ASSESSMENT: Highest-risk element
"""

POST_REVIEW_PROMPT = """You are a senior QA engineer performing a post-implementation quality audit.

The automated development loop has completed all planned work. Review the implementation
for production readiness.

PROJECT CONTEXT:
{project_context}

CODEBASE (key files):
{codebase_summary}

FOCUS AREA: {focus_area}

Evaluate the implementation across these dimensions:

1. COMPLETENESS: Are all planned features fully implemented? Any stub code or TODOs left?
2. EDGE CASES: What edge cases might be unhandled? Input validation gaps?
3. TEST COVERAGE: Are critical paths tested? Any obvious missing test scenarios?
4. CODE QUALITY: Consistent patterns? Proper error handling? Clean separation of concerns?
5. REGRESSIONS: Could any changes break existing functionality?
6. DOCUMENTATION: Are public interfaces documented? CLAUDE.md / README up to date?

Respond with:
- VERDICT: PASS | CONCERNS | ISSUES_FOUND
- For each dimension above: brief assessment (1-3 sentences)
- PRIORITY_FIXES: Numbered list of issues to address (empty if PASS)
- OVERALL_ASSESSMENT: 2-3 sentence summary
"""

# Error codes that are transient and worth retrying
RETRYABLE_ERRORS = {"TIMEOUT", "PLAYWRIGHT_ERROR", "PARSE_ERROR"}


class ResearchBridge:
    """Queries Perplexity via Playwright browser automation with project context."""

    def __init__(
        self,
        project_path: str | Path,
        retry_config: Optional[RetryConfig] = None,
        research_timeout: int = 600,
        headful: bool = True,
        perplexity_mode: str = "research",
        exploration_config: Optional[ExplorationConfig] = None,
        verification_config: Optional[VerificationConfig] = None,
    ) -> None:
        self.project_path = Path(project_path)
        self.context = SessionContext(project_path)
        self.retry_config = retry_config or RetryConfig()
        self.research_timeout = research_timeout
        self.headful = headful
        self.perplexity_mode = perplexity_mode
        self.exploration_config = exploration_config or ExplorationConfig()
        self.verification_config = verification_config or VerificationConfig()

        # Circuit breaker state
        self._consecutive_failures: int = 0
        self._last_failure_time: float = 0.0

        # Last codebase context for reuse in verification
        self.last_codebase_context: Optional[dict[str, str]] = None

    def build_query(
        self,
        extra_context: Optional[str] = None,
        codebase_context: Optional[dict[str, str]] = None,
        focus_area: Optional[str] = None,
    ) -> str:
        """Build a structured research query from project context.

        Matches the /research-perplexity skill prompt structure for consistent,
        actionable 8-section output.
        """
        ctx = self.context.gather()

        parts = [
            "You are a development strategy advisor analyzing a coding session.",
            "Given the project context below, provide strategic analysis and concrete next steps.",
            "",
            f"## Project Path",
            f"`{self.project_path.resolve()}`",
            "",
        ]

        if focus_area:
            parts.append(f"FOCUS AREA: {focus_area}")
            parts.append("")

        if ctx.get("claude_md"):
            parts.append("## Project Definition (CLAUDE.md)")
            parts.append(ctx["claude_md"])
            parts.append("")

        if ctx.get("workflow_state"):
            parts.append("## Current Workflow State")
            parts.append(ctx["workflow_state"])
            parts.append("")

        if ctx.get("git_log"):
            parts.append("## Recent Commits")
            parts.append(ctx["git_log"])
            parts.append("")

        if ctx.get("last_research"):
            parts.append("## Previous Research Result")
            parts.append(ctx["last_research"])
            parts.append("")

        if codebase_context:
            parts.append("## Key Codebase Files")
            for filepath, content in codebase_context.items():
                parts.append(f"### {filepath}")
                parts.append(f"```\n{content}\n```")
                parts.append("")

        if extra_context:
            parts.append("## Additional Context")
            parts.append(extra_context)
            parts.append("")

        parts.append("## Response Format")
        parts.append(
            "Please analyze and respond with these sections:\n"
            "1. CURRENT STATE: What has been accomplished\n"
            "2. PROGRESS VS PLAN: How does the work align with the implementation plan?\n"
            "3. IMMEDIATE NEXT STEPS: 3-5 concrete actions with file paths\n"
            "4. BLOCKERS: Issues that need resolution\n"
            "5. TECHNICAL DEBT: Items to address soon\n"
            "6. STRATEGIC RECOMMENDATIONS: Longer-term direction\n"
            "7. RISKS: What could go wrong, and mitigations\n"
            "8. CODEBASE FIT: How recommendations integrate with existing code\n"
            "\n"
            "If the project appears complete (all planned work done), "
            "respond with PROJECT_COMPLETE as the first line of CURRENT STATE."
        )

        return "\n".join(parts)

    def _is_circuit_open(self) -> bool:
        """Check if circuit breaker is tripped (too many consecutive failures)."""
        if self._consecutive_failures < self.retry_config.circuit_breaker_threshold:
            return False
        elapsed = time.monotonic() - self._last_failure_time
        if elapsed >= self.retry_config.circuit_breaker_reset_seconds:
            # Reset circuit breaker after cooldown
            logger.info("Circuit breaker reset after %.1fs cooldown", elapsed)
            self._consecutive_failures = 0
            return False
        return True

    def _record_failure(self) -> None:
        """Record a failure for circuit breaker tracking."""
        self._consecutive_failures += 1
        self._last_failure_time = time.monotonic()

    def _record_success(self) -> None:
        """Reset circuit breaker on success."""
        self._consecutive_failures = 0

    def _is_retryable(self, result: Result[ResearchResult]) -> bool:
        """Check if a failed result is worth retrying."""
        if result.success:
            return False
        if result.error_code not in RETRYABLE_ERRORS:
            return False
        return True

    def _calculate_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter."""
        delay = self.retry_config.base_delay_seconds * (2 ** attempt)
        delay = min(delay, self.retry_config.max_delay_seconds)
        # Add jitter: 0.5x to 1.5x
        jitter = 0.5 + random.random()
        return delay * jitter

    def query(
        self,
        extra_context: Optional[str] = None,
        focus_area: Optional[str] = None,
    ) -> Result[ResearchResult]:
        """Execute a research query with retry and circuit breaker."""
        if self._is_circuit_open():
            return Result.fail(
                f"Circuit breaker open — Perplexity research failed "
                f"{self._consecutive_failures} times. Will retry in "
                f"{self.retry_config.circuit_breaker_reset_seconds:.0f}s. "
                f"Check: 1) Perplexity session: ~/.claude/config/playwright-session.json "
                f"2) Run: python council_browser.py --save-session",
                "CIRCUIT_OPEN",
            )

        # Explore codebase for context before building query
        codebase_context: Optional[dict[str, str]] = None
        if self.exploration_config.enabled:
            codebase_context = self.context.explore_codebase(
                max_files=self.exploration_config.max_files_to_read,
                max_chars=self.exploration_config.max_chars_per_file,
            )
            self.last_codebase_context = codebase_context
            if codebase_context:
                logger.info(
                    "Explored %d codebase files for research context",
                    len(codebase_context),
                )

        last_result: Result[ResearchResult] = Result.fail("No attempts made", "UNKNOWN")

        for attempt in range(self.retry_config.max_retries + 1):
            last_result = self._single_query(
                extra_context, codebase_context=codebase_context,
                focus_area=focus_area,
            )

            if last_result.success:
                self._record_success()
                return last_result

            self._record_failure()

            if not self._is_retryable(last_result):
                logger.warning(
                    "Non-retryable error: [%s] %s",
                    last_result.error_code, last_result.error,
                )
                return last_result

            if attempt < self.retry_config.max_retries:
                delay = self._calculate_delay(attempt)
                logger.info(
                    "Retry %d/%d after %.1fs (error: %s)",
                    attempt + 1, self.retry_config.max_retries,
                    delay, last_result.error_code,
                )
                time.sleep(delay)

        return last_result

    def _single_query(
        self,
        extra_context: Optional[str] = None,
        codebase_context: Optional[dict[str, str]] = None,
        focus_area: Optional[str] = None,
    ) -> Result[ResearchResult]:
        """Execute a single research query via Playwright browser automation (no retry)."""
        query_text = self.build_query(
            extra_context, codebase_context=codebase_context, focus_area=focus_area,
        )

        try:
            cmd = [
                sys.executable, str(COUNCIL_BROWSER_SCRIPT),
                "--perplexity-mode", self.perplexity_mode,
                query_text,
            ]
            if self.headful:
                cmd.insert(2, "--headful")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.research_timeout,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip() if result.stderr else "Unknown error"
                return Result.fail(
                    f"Playwright subprocess failed (exit {result.returncode}): {stderr}",
                    "PLAYWRIGHT_ERROR",
                )

            data = json.loads(result.stdout)

            if data.get("error"):
                return Result.fail(data["error"], "PLAYWRIGHT_ERROR")

            content = data.get("synthesis", "")
            if not content:
                return Result.fail("Empty response from Perplexity research", "PARSE_ERROR")

            research_result = ResearchResult(
                query=query_text[:500],
                response=content,
                model=f"perplexity-{self.perplexity_mode}",
            )

            # Save to .workflow/research_result.md
            self._save_result(research_result)

            return Result.ok(research_result)

        except subprocess.TimeoutExpired:
            return Result.fail(
                f"Playwright research timed out ({self.research_timeout}s)", "TIMEOUT"
            )
        except json.JSONDecodeError as e:
            return Result.fail(f"Invalid JSON from Playwright subprocess: {e}", "PARSE_ERROR")
        except FileNotFoundError:
            return Result.fail(
                f"council_browser.py not found at {COUNCIL_BROWSER_SCRIPT}", "SCRIPT_NOT_FOUND"
            )
        except Exception as e:
            return Result.fail(f"Research query failed: {e}", "QUERY_ERROR")

    def verify_plan(
        self,
        plan_text: str,
        original_research: str,
        codebase_context: Optional[dict[str, str]] = None,
    ) -> Result[ResearchResult]:
        """Send a plan through Perplexity for verification critique.

        Reuses _single_query() with the verification prompt template.
        """
        codebase_summary = "(no codebase context available)"
        if codebase_context:
            parts = []
            for filepath, content in codebase_context.items():
                parts.append(f"### {filepath}\n```\n{content[:1000]}\n```")
            codebase_summary = "\n".join(parts)

        verification_query = VERIFICATION_PROMPT.format(
            original_research=original_research[:3000],
            plan_text=plan_text[:5000],
            codebase_summary=codebase_summary[:5000],
        )

        # Use _single_query with the verification prompt as extra_context
        # but override the query text entirely via a direct subprocess call
        timeout = self.verification_config.verification_timeout_seconds

        try:
            cmd = [
                sys.executable, str(COUNCIL_BROWSER_SCRIPT),
                "--perplexity-mode", self.perplexity_mode,
                verification_query,
            ]
            if self.headful:
                cmd.insert(2, "--headful")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip() if result.stderr else "Unknown error"
                return Result.fail(
                    f"Verification subprocess failed (exit {result.returncode}): {stderr}",
                    "PLAYWRIGHT_ERROR",
                )

            data = json.loads(result.stdout)

            if data.get("error"):
                return Result.fail(data["error"], "PLAYWRIGHT_ERROR")

            content = data.get("synthesis", "")
            if not content:
                return Result.fail("Empty verification response", "PARSE_ERROR")

            return Result.ok(ResearchResult(
                query=verification_query[:500],
                response=content,
                model=f"perplexity-{self.perplexity_mode}-verification",
            ))

        except subprocess.TimeoutExpired:
            return Result.fail(
                f"Verification timed out ({timeout}s)", "TIMEOUT"
            )
        except json.JSONDecodeError as e:
            return Result.fail(f"Invalid JSON from verification: {e}", "PARSE_ERROR")
        except FileNotFoundError:
            return Result.fail(
                f"council_browser.py not found at {COUNCIL_BROWSER_SCRIPT}",
                "SCRIPT_NOT_FOUND",
            )
        except Exception as e:
            return Result.fail(f"Verification failed: {e}", "QUERY_ERROR")

    def post_review(
        self,
        focus_area: str = "Review all implementations for completeness, edge cases, and quality",
        timeout: int = 600,
        save_result: bool = True,
    ) -> Result[ResearchResult]:
        """Run a post-completion quality review via Perplexity.

        Gathers project context and codebase files, then sends a quality audit
        prompt through Playwright. Saves result to .workflow/post_review.md.
        """
        # Gather context
        ctx = self.context.gather()
        project_context = ""
        if ctx.get("claude_md"):
            project_context += f"## CLAUDE.md\n{ctx['claude_md']}\n\n"
        if ctx.get("git_log"):
            project_context += f"## Recent Commits\n{ctx['git_log']}\n\n"
        if ctx.get("workflow_state"):
            project_context += f"## Workflow State\n{ctx['workflow_state']}\n\n"

        # Explore codebase
        codebase_summary = "(no codebase context available)"
        if self.exploration_config.enabled:
            codebase_files = self.context.explore_codebase(
                max_files=self.exploration_config.max_files_to_read,
                max_chars=self.exploration_config.max_chars_per_file,
            )
            if codebase_files:
                parts = []
                for filepath, content in codebase_files.items():
                    parts.append(f"### {filepath}\n```\n{content[:1000]}\n```")
                codebase_summary = "\n".join(parts)

        review_query = POST_REVIEW_PROMPT.format(
            project_context=project_context[:5000],
            codebase_summary=codebase_summary[:5000],
            focus_area=focus_area,
        )

        try:
            cmd = [
                sys.executable, str(COUNCIL_BROWSER_SCRIPT),
                "--perplexity-mode", self.perplexity_mode,
                review_query,
            ]
            if self.headful:
                cmd.insert(2, "--headful")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode != 0:
                stderr = result.stderr.strip() if result.stderr else "Unknown error"
                return Result.fail(
                    f"Post-review subprocess failed (exit {result.returncode}): {stderr}",
                    "PLAYWRIGHT_ERROR",
                )

            data = json.loads(result.stdout)

            if data.get("error"):
                return Result.fail(data["error"], "PLAYWRIGHT_ERROR")

            content = data.get("synthesis", "")
            if not content:
                return Result.fail("Empty post-review response", "PARSE_ERROR")

            review_result = ResearchResult(
                query=review_query[:500],
                response=content,
                model=f"perplexity-{self.perplexity_mode}-post-review",
            )

            # Save to .workflow/post_review.md
            if save_result:
                self._save_post_review(review_result)

            return Result.ok(review_result)

        except subprocess.TimeoutExpired:
            return Result.fail(
                f"Post-review timed out ({timeout}s)", "TIMEOUT"
            )
        except json.JSONDecodeError as e:
            return Result.fail(f"Invalid JSON from post-review: {e}", "PARSE_ERROR")
        except FileNotFoundError:
            return Result.fail(
                f"council_browser.py not found at {COUNCIL_BROWSER_SCRIPT}",
                "SCRIPT_NOT_FOUND",
            )
        except Exception as e:
            return Result.fail(f"Post-review failed: {e}", "QUERY_ERROR")

    def _save_post_review(self, result: ResearchResult) -> None:
        """Save post-review result to .workflow/post_review.md."""
        output_path = self.project_path / ".workflow" / "post_review.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        content = (
            f"# Post-Completion Quality Review\n\n"
            f"**Timestamp:** {result.timestamp}\n"
            f"**Model:** {result.model}\n\n"
            f"---\n\n"
            f"{result.response}\n"
        )
        output_path.write_text(content, encoding="utf-8")
        logger.info("Post-review result saved to %s", output_path)

    def _save_result(self, result: ResearchResult) -> None:
        """Save research result to .workflow/research_result.md."""
        output_path = self.project_path / ".workflow" / "research_result.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        content = (
            f"# Research Result\n\n"
            f"**Timestamp:** {result.timestamp}\n"
            f"**Model:** {result.model}\n\n"
            f"---\n\n"
            f"{result.response}\n"
        )
        output_path.write_text(content, encoding="utf-8")
        logger.info("Research result saved to %s", output_path)


def main() -> None:
    """CLI entry point for standalone research bridge usage."""
    parser = argparse.ArgumentParser(description="Query Perplexity for project next steps")
    parser.add_argument("--project", default=".", help="Project directory path")
    parser.add_argument(
        "--mode", default="playwright", choices=["playwright"],
        help="Query mode (playwright only)",
    )
    parser.add_argument("--context", default=None, help="Extra context to include")
    parser.add_argument("--headful", action="store_true", help="Run browser in visible mode")
    parser.add_argument(
        "--perplexity-mode", default="research",
        choices=["research", "council", "labs"],
        help="Perplexity query mode",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Install log redaction filter
    from log_redactor import RedactingFilter

    sec = SecurityConfig()
    for handler in logging.root.handlers:
        handler.addFilter(RedactingFilter(sec.log_redact_patterns))

    bridge = ResearchBridge(
        args.project,
        headful=args.headful,
        perplexity_mode=args.perplexity_mode,
    )
    result = bridge.query(extra_context=args.context)

    if result.success and result.data:
        print(f"\n{'='*60}")
        print("Research Result:")
        print(f"{'='*60}")
        print(result.data.response)
        print(f"\n{'='*60}")
        print(f"Saved to: {Path(args.project) / '.workflow' / 'research_result.md'}")
    else:
        print(f"ERROR [{result.error_code}]: {result.error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

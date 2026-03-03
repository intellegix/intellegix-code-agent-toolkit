"""Configuration validation for the automated Claude loop."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class Result(Generic[T]):
    """Type-safe result wrapper for operations that can fail."""

    success: bool
    data: Optional[T] = None
    error: Optional[str] = None
    error_code: Optional[str] = None

    @classmethod
    def ok(cls, data: T) -> Result[T]:
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, error: str, code: str = "UNKNOWN") -> Result[T]:
        return cls(success=False, error=error, error_code=code)


class LimitsConfig(BaseModel):
    """Iteration and timeout limits."""

    max_iterations: int = Field(default=50, ge=1, le=500)
    timeout_seconds: int = Field(default=300, ge=30, le=1800)
    max_per_iteration_budget_usd: float = Field(default=5.0, gt=0)
    max_total_budget_usd: float = Field(default=50.0, gt=0)
    max_turns_per_iteration: int = Field(default=50, ge=1, le=200)
    model_timeout_multipliers: dict[str, float] = Field(
        default={"opus": 2.0, "sonnet": 1.0, "haiku": 0.5},
        description="Timeout multiplier per model name",
    )
    model_max_turns_override: dict[str, int] = Field(
        default={"opus": 25},
        description="Per-model max turns override (caps max_turns_per_iteration)",
    )
    timeout_cooldown_base_seconds: int = Field(
        default=60, ge=0, le=600,
        description="Base cooldown delay after a timeout before retrying",
    )
    timeout_cooldown_max_seconds: int = Field(
        default=300, ge=0, le=600,
        description="Maximum cooldown delay between timeout retries",
    )
    model_fallback: dict[str, str] = Field(
        default={"opus": "sonnet"},
        description="Fallback model when primary model has sustained timeouts",
    )
    model_fallback_after_timeouts: int = Field(
        default=2, ge=1, le=10,
        description="Fall back after this many consecutive timeouts (before stagnation limit)",
    )
    trace_max_size_bytes: int = Field(
        default=10_000_000, ge=0,
        description="Max trace.jsonl size before rotation (0=unlimited)",
    )


class PerplexityConfig(BaseModel):
    """Perplexity research settings (Playwright browser automation)."""

    research_timeout_seconds: int = Field(default=600, ge=60)
    headful: bool = Field(default=True)
    perplexity_mode: str = Field(default="research")


class ClaudeConfig(BaseModel):
    """Claude CLI settings."""

    model: str = Field(default="sonnet")
    dangerously_skip_permissions: bool = Field(default=True)
    verbose: bool = Field(default=True)


class PatternsConfig(BaseModel):
    """Pattern matching for completion detection."""

    completion_markers: list[str] = Field(
        default_factory=lambda: [
            "PROJECT_COMPLETE",
            "ALL_TASKS_DONE",
            "IMPLEMENTATION_COMPLETE",
        ]
    )


class CompletionGateConfig(BaseModel):
    """Completion gate: validate PROJECT_COMPLETE against CLAUDE.md checklist."""

    enabled: bool = Field(default=True)
    section_marker: str = Field(default="## Completion Gate")
    max_rejections: int = Field(
        default=3, ge=1,
        description="Max consecutive gate rejections before exiting with stagnation",
    )


class SecurityConfig(BaseModel):
    """Security and redaction settings."""

    max_execution_time_minutes: int = Field(default=120, ge=1)
    log_redact_patterns: list[str] = Field(
        default_factory=lambda: [
            r"sk-ant-[\w-]+",
            r"pplx-[\w]+",
            r"sk-proj-[\w-]+",
        ]
    )


class RetryConfig(BaseModel):
    """Retry and circuit breaker settings for API calls."""

    max_retries: int = Field(default=3, ge=0, le=10)
    base_delay_seconds: float = Field(default=1.0, gt=0)
    max_delay_seconds: float = Field(default=30.0, gt=0)
    circuit_breaker_threshold: int = Field(default=5, ge=1)
    circuit_breaker_reset_seconds: float = Field(default=120.0, gt=0)


class ExplorationConfig(BaseModel):
    """Codebase exploration settings before research queries."""

    enabled: bool = Field(default=True)
    max_files_to_read: int = Field(default=10, ge=1, le=30)
    max_chars_per_file: int = Field(default=3000, ge=500, le=10000)


class VerificationConfig(BaseModel):
    """Plan verification settings."""

    enabled: bool = Field(default=True)
    verification_timeout_seconds: int = Field(default=600, ge=60)


class ValidationConfig(BaseModel):
    """Post-execution validation settings."""

    enabled: bool = Field(default=False)
    test_command: str = Field(default="pytest tests/ -v --tb=short")
    test_timeout_seconds: int = Field(default=120, ge=10, le=600)
    fail_action: str = Field(
        default="warn",
        description="Action on test failure: 'warn' logs and continues; 'inject' feeds failure to next prompt",
    )
    max_consecutive_failures: int = Field(
        default=3, ge=1, le=10,
        description="Max consecutive test failures before falling back to 'warn' mode",
    )


class PostReviewConfig(BaseModel):
    """Post-completion Perplexity review settings."""

    enabled: bool = Field(default=True)
    focus_area: str = Field(
        default="Review all implementations for completeness, edge cases, and quality",
    )
    timeout_seconds: int = Field(default=600, ge=60, le=1800)
    save_result: bool = Field(default=True)


class StagnationConfig(BaseModel):
    """Diminishing returns detection to prevent runaway loops.

    Monitors a sliding window of recent iterations and exits gracefully
    when the loop stops making progress (low turns, timeouts, zero cost).
    On first detection, resets the session for a fresh start. On second
    detection, exits with EXIT_STAGNATION (code 3).
    """

    enabled: bool = Field(default=True)
    window_size: int = Field(default=3, ge=2, le=10)
    low_turn_threshold: int = Field(default=2, ge=0)
    max_consecutive_timeouts: int = Field(default=2, ge=1, le=10)
    model_timeout_overrides: dict[str, int] = Field(
        default={"opus": 3},
        description="Per-model override for max_consecutive_timeouts",
    )
    session_max_turns: int = Field(
        default=200, ge=10,
        description="Hard turn limit per session before rotation",
    )
    session_max_cost_usd: float = Field(
        default=20.0, gt=0,
        description="Hard cost limit per session before rotation",
    )
    context_exhaustion_turn_threshold: int = Field(
        default=5, ge=1,
        description="Turns below this count as low-productivity for rotation detection",
    )
    context_exhaustion_window: int = Field(
        default=3, ge=2, le=10,
        description="Window size for behavioral context exhaustion detection",
    )


class FileLockEntry(BaseModel):
    """A single file lock held by an agent."""

    owner: str  # "agent-1", "agent-2", etc.
    acquired_at: str  # ISO-8601 timestamp
    ttl_seconds: int = Field(default=1800, ge=60, le=7200)


class MultiAgentConfig(BaseModel):
    """Configuration for multi-agent parallel orchestration."""

    enabled: bool = False
    max_agents: int = Field(default=4, ge=1, le=8)
    dropbox_sync_delay_seconds: float = Field(default=5.0, ge=0.0, le=15.0)
    lock_retry_attempts: int = Field(default=5, ge=1, le=20)
    lock_retry_delay_seconds: float = Field(default=10.0, ge=0.0, le=60.0)
    lock_ttl_seconds: int = Field(default=1800, ge=60, le=7200)
    dashboard_refresh_seconds: int = Field(default=30, ge=10, le=300)
    merge_timeout_seconds: int = Field(default=600, ge=60, le=3600)
    agent_state_dir: str = ".agents"


class WorkflowConfig(BaseModel):
    """Root configuration model for .workflow/config.json."""

    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    perplexity: PerplexityConfig = Field(default_factory=PerplexityConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    patterns: PatternsConfig = Field(default_factory=PatternsConfig)
    completion_gate: CompletionGateConfig = Field(default_factory=CompletionGateConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    stagnation: StagnationConfig = Field(default_factory=StagnationConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    exploration: ExplorationConfig = Field(default_factory=ExplorationConfig)
    post_review: PostReviewConfig = Field(default_factory=PostReviewConfig)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)


def load_config(config_path: str | Path) -> Result[WorkflowConfig]:
    """Load and validate workflow config from JSON file."""
    path = Path(config_path)
    if not path.exists():
        logger.info("Config not found at %s, using defaults", path)
        return Result.ok(WorkflowConfig())

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        config = WorkflowConfig.model_validate(raw)
        return Result.ok(config)
    except json.JSONDecodeError as e:
        return Result.fail(f"Invalid JSON in {path}: {e}", "JSON_ERROR")
    except Exception as e:
        return Result.fail(f"Config validation failed: {e}", "VALIDATION_ERROR")

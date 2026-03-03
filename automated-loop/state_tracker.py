"""Persistent workflow state tracking for the automated Claude loop."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from config import Result

logger = logging.getLogger(__name__)


class CycleRecord(BaseModel):
    """Record of a single loop iteration."""

    iteration: int
    prompt_preview: str = Field(default="", max_length=200)
    session_id: Optional[str] = None
    model: Optional[str] = None
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    research_query: Optional[str] = None
    completed_at: Optional[str] = None
    is_error: bool = False
    error_message: Optional[str] = None
    tools_used: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    git_diff_stats: Optional[dict] = None


class ModelAnalytics(BaseModel):
    """Per-model performance metrics."""

    model: str
    iterations: int = 0
    avg_turns: float = 0.0
    avg_cost_usd: float = 0.0
    avg_duration_ms: float = 0.0
    timeout_count: int = 0
    timeout_rate: float = 0.0
    error_count: int = 0
    error_rate: float = 0.0


class WorkflowMetrics(BaseModel):
    """Aggregated metrics across all cycles."""

    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    total_turns: int = 0
    error_count: int = 0
    files_modified: list[str] = Field(default_factory=list)


CURRENT_STATE_VERSION = 1


class WorkflowState(BaseModel):
    """Root state model persisted to .workflow/state.json."""

    version: int = Field(default=CURRENT_STATE_VERSION)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    iteration: int = 0
    status: str = Field(default="idle")  # idle, running, paused, completed, failed
    cycles: list[CycleRecord] = Field(default_factory=list)
    metrics: WorkflowMetrics = Field(default_factory=WorkflowMetrics)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    last_session_id: Optional[str] = None  # Claude CLI session ID for --resume


class StateTracker:
    """Manages persistent workflow state in .workflow/state.json."""

    def __init__(
        self, project_path: str | Path, workflow_dir: Optional[Path] = None
    ) -> None:
        self.project_path = Path(project_path)
        if workflow_dir is not None:
            self.state_path = workflow_dir / "state.json"
        else:
            self.state_path = self.project_path / ".workflow" / "state.json"
        self.state = WorkflowState()

    @staticmethod
    def _migrate_state(raw: dict) -> dict:
        """Migrate older state formats to current version."""
        if "version" not in raw:
            raw["version"] = 1
            logger.info("Migrated state file: added version=1")
        return raw

    def load(self) -> Result[WorkflowState]:
        """Load state from disk. Returns defaults if file doesn't exist."""
        if not self.state_path.exists():
            logger.info("No existing state at %s, starting fresh", self.state_path)
            return Result.ok(self.state)

        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            raw = self._migrate_state(raw)
            self.state = WorkflowState.model_validate(raw)
            return Result.ok(self.state)
        except json.JSONDecodeError as e:
            return Result.fail(f"Corrupt state file: {e}", "JSON_ERROR")
        except Exception as e:
            return Result.fail(f"State load failed: {e}", "LOAD_ERROR")

    def save(self) -> Result[None]:
        """Persist current state to disk."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                self.state.model_dump_json(indent=2), encoding="utf-8"
            )
            return Result.ok(None)
        except Exception as e:
            return Result.fail(f"State save failed: {e}", "SAVE_ERROR")

    def start_session(self) -> None:
        """Mark session as running with a fresh start time."""
        self.state.status = "running"
        self.state.start_time = datetime.now(timezone.utc).isoformat()

    def increment_iteration(self) -> int:
        """Advance iteration counter and return the new value."""
        self.state.iteration += 1
        return self.state.iteration

    def add_cycle(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        num_turns: int = 0,
        is_error: bool = False,
        error_message: Optional[str] = None,
        tools_used: Optional[list[str]] = None,
        files_modified: Optional[list[str]] = None,
        git_diff_stats: Optional[dict] = None,
    ) -> None:
        """Record a completed loop cycle."""
        cycle = CycleRecord(
            iteration=self.state.iteration,
            prompt_preview=prompt[:200],
            session_id=session_id,
            model=model,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            num_turns=num_turns,
            completed_at=datetime.now(timezone.utc).isoformat(),
            is_error=is_error,
            error_message=error_message,
            tools_used=tools_used or [],
            files_modified=files_modified or [],
            git_diff_stats=git_diff_stats,
        )
        self.state.cycles.append(cycle)

        # Update aggregated metrics
        self.state.metrics.total_cost_usd += cost_usd
        self.state.metrics.total_duration_ms += duration_ms
        self.state.metrics.total_turns += num_turns
        if is_error:
            self.state.metrics.error_count += 1

        # Track last Claude session ID for --resume
        if session_id:
            self.state.last_session_id = session_id

    def complete(self) -> None:
        """Mark the workflow as completed."""
        self.state.status = "completed"
        self.state.end_time = datetime.now(timezone.utc).isoformat()

    def fail(self, reason: str) -> None:
        """Mark the workflow as failed."""
        self.state.status = "failed"
        self.state.end_time = datetime.now(timezone.utc).isoformat()
        logger.error("Workflow failed: %s", reason)

    def check_budget(
        self, per_iteration_limit: float, total_limit: float
    ) -> Result[None]:
        """Check if the last cycle or total cost exceeds budget limits."""
        if self.state.cycles:
            last_cost = self.state.cycles[-1].cost_usd
            if last_cost > per_iteration_limit:
                return Result.fail(
                    f"Per-iteration budget exceeded: ${last_cost:.4f} > ${per_iteration_limit:.4f}",
                    "BUDGET_EXCEEDED_ITERATION",
                )

        total_cost = self.state.metrics.total_cost_usd
        if total_cost > total_limit:
            return Result.fail(
                f"Total budget exceeded: ${total_cost:.4f} > ${total_limit:.4f}",
                "BUDGET_EXCEEDED_TOTAL",
            )

        return Result.ok(None)

    def validate_session_id(self, session_id: Optional[str]) -> Optional[str]:
        """Validate session ID format. Returns None if invalid."""
        if not session_id or not isinstance(session_id, str):
            return None
        session_id = session_id.strip()
        if not session_id or len(session_id) > 200:
            return None
        return session_id

    def clear_session(self) -> None:
        """Clear the last session ID (e.g., after resume failure)."""
        self.state.last_session_id = None

    def get_metrics(self) -> WorkflowMetrics:
        """Return current aggregated metrics."""
        return self.state.metrics

    def get_session_turns(self, session_id: Optional[str] = None) -> int:
        """Sum num_turns for all cycles matching the given session_id.

        Defaults to last_session_id if no session_id is provided.
        """
        target = session_id or self.state.last_session_id
        if not target:
            return 0
        return sum(c.num_turns for c in self.state.cycles if c.session_id == target)

    def compute_model_analytics(self) -> dict[str, ModelAnalytics]:
        """Compute per-model metrics from cycle history."""
        by_model: dict[str, list[CycleRecord]] = {}
        for cycle in self.state.cycles:
            model = cycle.model or "unknown"
            by_model.setdefault(model, []).append(cycle)

        result: dict[str, ModelAnalytics] = {}
        for model, cycles in by_model.items():
            n = len(cycles)
            timeouts = sum(1 for c in cycles if c.num_turns == 0 and c.cost_usd == 0)
            errors = sum(1 for c in cycles if c.is_error)
            result[model] = ModelAnalytics(
                model=model,
                iterations=n,
                avg_turns=sum(c.num_turns for c in cycles) / n if n else 0,
                avg_cost_usd=sum(c.cost_usd for c in cycles) / n if n else 0,
                avg_duration_ms=sum(c.duration_ms for c in cycles) / n if n else 0,
                timeout_count=timeouts,
                timeout_rate=timeouts / n if n else 0,
                error_count=errors,
                error_rate=errors / n if n else 0,
            )
        return result

    def get_session_cost(self, session_id: Optional[str] = None) -> float:
        """Sum cost_usd for all cycles matching the given session_id.

        Defaults to last_session_id if no session_id is provided.
        """
        target = session_id or self.state.last_session_id
        if not target:
            return 0.0
        return sum(c.cost_usd for c in self.state.cycles if c.session_id == target)

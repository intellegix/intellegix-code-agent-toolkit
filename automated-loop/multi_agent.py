"""Multi-agent orchestration for parallel Claude Code loops.

Manages N parallel loop_driver.py instances, each with isolated workspaces,
file manifests, and Dropbox-safe lock coordination. Provides dashboard
generation, merge-phase execution, and cleanup.

Key classes:
- AgentWorkspace: creates/manages a single agent's isolated directory
- WorkSplitter: divides files into non-overlapping groups for agents
- MultiAgentOrchestrator: top-level coordinator (setup, launch, monitor, merge)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from config import MultiAgentConfig, WorkflowConfig
from file_locking import FileManifest, LockRegistry
from state_tracker import StateTracker, WorkflowState

logger = logging.getLogger(__name__)


class WorkAssignment(BaseModel):
    """Files and instructions assigned to a single agent."""

    agent_id: str
    files: list[str] = Field(default_factory=list)
    instructions: str = ""
    phase_label: str = ""


class AgentStatus(BaseModel):
    """Status snapshot for a single agent."""

    agent_id: str
    status: str = "pending"  # pending, running, completed, failed
    iteration: int = 0
    total_cost_usd: float = 0.0
    total_turns: int = 0
    error_count: int = 0
    files_assigned: int = 0
    last_updated: Optional[str] = None


class AgentWorkspace:
    """Manages an isolated workspace for a single agent within .agents/."""

    def __init__(
        self,
        project_path: str | Path,
        agent_id: str,
        config: MultiAgentConfig,
    ) -> None:
        self.project_path = Path(project_path)
        self.agent_id = agent_id
        self.config = config
        self.workspace_dir = (
            self.project_path / config.agent_state_dir / agent_id
        )
        self.workflow_dir = self.workspace_dir / ".workflow"
        self.manifest_path = self.workspace_dir / "assigned_files.txt"
        self.claude_md_path = self.workspace_dir / "CLAUDE.md"

    def setup(self, assignment: WorkAssignment) -> None:
        """Create the agent workspace directory structure."""
        self.workflow_dir.mkdir(parents=True, exist_ok=True)

        # Write assigned files manifest
        manifest = FileManifest(self.manifest_path)
        manifest.save(assignment.files)

        # Write agent-specific CLAUDE.md
        self._write_agent_claude_md(assignment)

        logger.info(
            "Workspace created for %s: %d files assigned",
            self.agent_id, len(assignment.files),
        )

    def _write_agent_claude_md(self, assignment: WorkAssignment) -> None:
        """Generate agent-specific CLAUDE.md with scoped instructions."""
        files_list = "\n".join(f"- `{f}`" for f in sorted(assignment.files))
        content = (
            f"# Agent: {self.agent_id}\n\n"
            f"## Phase\n{assignment.phase_label}\n\n"
            f"## Instructions\n{assignment.instructions}\n\n"
            f"## Assigned Files\n"
            f"You are responsible ONLY for the following files. "
            f"Do NOT modify files outside this list.\n\n"
            f"{files_list}\n\n"
            f"## Coordination\n"
            f"- Your agent ID is `{self.agent_id}`\n"
            f"- File locks are enforced via PreToolUse hook\n"
            f"- Signal PROJECT_COMPLETE when your assigned work is done\n"
        )
        self.claude_md_path.write_text(content, encoding="utf-8")

    def get_state(self) -> Optional[WorkflowState]:
        """Load the agent's workflow state, or None if not yet created."""
        tracker = StateTracker(self.project_path, workflow_dir=self.workflow_dir)
        result = tracker.load()
        if result.success:
            return result.data
        return None

    def get_status(self) -> AgentStatus:
        """Build an AgentStatus snapshot from the agent's state file."""
        state = self.get_state()
        manifest = FileManifest(self.manifest_path)
        files = manifest.load()

        if state is None:
            return AgentStatus(
                agent_id=self.agent_id,
                status="pending",
                files_assigned=len(files),
            )

        return AgentStatus(
            agent_id=self.agent_id,
            status=state.status,
            iteration=state.iteration,
            total_cost_usd=state.metrics.total_cost_usd,
            total_turns=state.metrics.total_turns,
            error_count=state.metrics.error_count,
            files_assigned=len(files),
            last_updated=state.end_time or state.start_time,
        )

    def cleanup(self) -> None:
        """Remove the workspace directory (optional, for post-merge cleanup)."""
        import shutil
        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir, ignore_errors=True)
            logger.info("Cleaned up workspace for %s", self.agent_id)


class WorkSplitter:
    """Splits files into non-overlapping groups for parallel agents."""

    @staticmethod
    def split_for_agents(
        files: list[str],
        num_agents: int,
    ) -> list[list[str]]:
        """Group files by parent directory and distribute across agents.

        Files in the same directory stay together to minimize cross-agent
        conflicts. Returns a list of file lists, one per agent.
        """
        if num_agents < 1:
            return []
        if not files:
            return [[] for _ in range(num_agents)]

        # Group by parent directory
        groups: dict[str, list[str]] = {}
        for f in files:
            parent = str(Path(f).parent)
            groups.setdefault(parent, []).append(f)

        # Sort groups by size (largest first) for better distribution
        sorted_groups = sorted(groups.values(), key=len, reverse=True)

        # Distribute groups across agents using greedy bin-packing
        buckets: list[list[str]] = [[] for _ in range(num_agents)]
        for group in sorted_groups:
            # Put in the smallest bucket
            smallest = min(range(num_agents), key=lambda i: len(buckets[i]))
            buckets[smallest].extend(group)

        return buckets

    @staticmethod
    def identify_sequential_phases(
        assignments: list[WorkAssignment],
    ) -> list[str]:
        """Identify files that appear in multiple assignments (shouldn't happen).

        Returns list of conflicting file paths.
        """
        seen: dict[str, str] = {}
        conflicts: list[str] = []
        for assignment in assignments:
            for f in assignment.files:
                if f in seen:
                    conflicts.append(f)
                else:
                    seen[f] = assignment.agent_id
        return conflicts


class MultiAgentOrchestrator:
    """Coordinates N parallel loop_driver.py processes.

    Lifecycle:
    1. setup_workspaces() — create agent directories and manifests
    2. launch_all() — spawn loop_driver.py --agent-id for each agent
    3. monitor_all() — poll until all complete, generate dashboard
    4. run_merge_phase() — release locks, run build/test
    5. cleanup() — release locks, kill orphans
    """

    def __init__(
        self,
        project_path: str | Path,
        config: WorkflowConfig,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.config = config
        self.multi_config = config.multi_agent
        self.workspaces: dict[str, AgentWorkspace] = {}
        self.processes: dict[str, subprocess.Popen] = {}
        self.lock_registry = LockRegistry(self.project_path, self.multi_config)

    def setup_workspaces(
        self, assignments: list[WorkAssignment]
    ) -> None:
        """Create workspace directories for all agents."""
        if len(assignments) > self.multi_config.max_agents:
            raise ValueError(
                f"Too many agents: {len(assignments)} > max {self.multi_config.max_agents}"
            )

        # Verify no file conflicts
        conflicts = WorkSplitter.identify_sequential_phases(assignments)
        if conflicts:
            logger.warning(
                "File conflicts detected across agents: %s", conflicts
            )

        for assignment in assignments:
            workspace = AgentWorkspace(
                self.project_path, assignment.agent_id, self.multi_config,
            )
            workspace.setup(assignment)
            self.workspaces[assignment.agent_id] = workspace

        # Create shared locks directory
        shared_dir = (
            self.project_path / self.multi_config.agent_state_dir / "shared"
        )
        shared_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Set up %d agent workspaces in %s/",
            len(assignments), self.multi_config.agent_state_dir,
        )

    def launch_all(
        self,
        prompt: str = "",
        dry_run: bool = False,
        model: Optional[str] = None,
    ) -> dict[str, int]:
        """Spawn loop_driver.py for each agent. Returns {agent_id: pid}."""
        pids: dict[str, int] = {}

        for agent_id, workspace in self.workspaces.items():
            args = [
                sys.executable, "-u",
                str(Path(__file__).parent / "loop_driver.py"),
                "--project", str(self.project_path),
                "--agent-id", agent_id,
                "--skip-preflight",
            ]

            if prompt:
                args.extend(["--prompt", prompt])
            if dry_run:
                args.append("--dry-run")
            if model:
                args.extend(["--model", model])

            logger.info("Launching %s: %s", agent_id, " ".join(args[:8]) + "...")

            env = os.environ.copy()
            env["CLAUDE_AGENT_ID"] = agent_id
            env["PYTHONIOENCODING"] = "utf-8"

            proc = subprocess.Popen(
                args,
                cwd=str(self.project_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            self.processes[agent_id] = proc
            pids[agent_id] = proc.pid
            logger.info("Launched %s with PID %d", agent_id, proc.pid)

        return pids

    def monitor_all(self, timeout: Optional[int] = None) -> dict[str, AgentStatus]:
        """Block until all agents complete or timeout. Returns final statuses."""
        refresh = self.multi_config.dashboard_refresh_seconds
        deadline = time.monotonic() + timeout if timeout else None

        while True:
            all_done = True
            for agent_id, proc in self.processes.items():
                if proc.poll() is None:
                    all_done = False

            if all_done:
                break

            if deadline and time.monotonic() > deadline:
                logger.warning("Monitor timeout reached, stopping remaining agents")
                self._kill_all()
                break

            time.sleep(refresh)

        return self.get_all_statuses()

    def get_all_statuses(self) -> dict[str, AgentStatus]:
        """Get status for all agents."""
        result: dict[str, AgentStatus] = {}
        for agent_id, workspace in self.workspaces.items():
            result[agent_id] = workspace.get_status()
        return result

    def generate_dashboard(self) -> str:
        """Generate a markdown dashboard table of all agents."""
        statuses = self.get_all_statuses()
        locks = self.lock_registry.get_locks()

        # Count locks per agent
        lock_counts: dict[str, int] = {}
        for _, entry in locks.items():
            lock_counts[entry.owner] = lock_counts.get(entry.owner, 0) + 1

        lines = [
            "# Multi-Agent Dashboard",
            "",
            f"**Updated**: {datetime.now(timezone.utc).isoformat()}",
            f"**Agents**: {len(statuses)}",
            "",
            "| Agent | Status | Iteration | Cost | Turns | Errors | Files | Locks |",
            "|-------|--------|-----------|------|-------|--------|-------|-------|",
        ]

        total_cost = 0.0
        total_turns = 0
        total_errors = 0

        for agent_id in sorted(statuses.keys()):
            s = statuses[agent_id]
            agent_locks = lock_counts.get(agent_id, 0)
            lines.append(
                f"| {s.agent_id} | {s.status} | {s.iteration} | "
                f"${s.total_cost_usd:.4f} | {s.total_turns} | "
                f"{s.error_count} | {s.files_assigned} | {agent_locks} |"
            )
            total_cost += s.total_cost_usd
            total_turns += s.total_turns
            total_errors += s.error_count

        lines.extend([
            "",
            f"**Totals**: Cost=${total_cost:.4f}, Turns={total_turns}, Errors={total_errors}",
            f"**Active locks**: {len(locks)}",
        ])

        return "\n".join(lines)

    def run_merge_phase(self) -> dict[str, bool]:
        """Run post-completion merge: release locks, run build/test.

        Returns {"locks_released": bool, "tests_passed": bool}.
        """
        result = {"locks_released": False, "tests_passed": False}

        # Release all locks
        total_released = 0
        for agent_id in self.workspaces:
            released = self.lock_registry.release_all(agent_id)
            total_released += released
        result["locks_released"] = True
        logger.info("Merge phase: released %d locks", total_released)

        # Run build/test if validation is configured
        if self.config.validation.enabled:
            try:
                import shlex
                cmd = shlex.split(self.config.validation.test_command)
                test_proc = subprocess.run(
                    cmd,
                    cwd=str(self.project_path),
                    capture_output=True, text=True,
                    timeout=self.multi_config.merge_timeout_seconds,
                )
                result["tests_passed"] = test_proc.returncode == 0
                if not result["tests_passed"]:
                    logger.warning(
                        "Merge tests failed (rc=%d): %s",
                        test_proc.returncode,
                        test_proc.stderr[:200],
                    )
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                logger.warning("Merge test execution failed: %s", e)
        else:
            result["tests_passed"] = True  # No tests configured = pass

        return result

    def cleanup(self) -> None:
        """Release all locks and kill any orphan processes."""
        for agent_id in list(self.workspaces.keys()):
            self.lock_registry.release_all(agent_id)

        self._kill_all()
        logger.info("Multi-agent cleanup complete")

    def _kill_all(self) -> None:
        """Kill all running agent processes."""
        for agent_id, proc in self.processes.items():
            if proc.poll() is None:
                logger.info("Killing %s (PID %d)", agent_id, proc.pid)
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

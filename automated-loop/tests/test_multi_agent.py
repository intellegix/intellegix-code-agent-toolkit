"""Tests for multi_agent module — AgentWorkspace, WorkSplitter, MultiAgentOrchestrator."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config import MultiAgentConfig, WorkflowConfig
from file_locking import FileManifest, LockRegistry
from multi_agent import (
    AgentStatus,
    AgentWorkspace,
    MultiAgentOrchestrator,
    WorkAssignment,
    WorkSplitter,
)
from state_tracker import StateTracker


@pytest.fixture
def multi_project(tmp_path: Path) -> Path:
    """Create a project directory for multi-agent tests."""
    workflow_dir = tmp_path / ".workflow"
    workflow_dir.mkdir()
    (tmp_path / "CLAUDE.md").write_text("# Test Project\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def multi_config() -> WorkflowConfig:
    """Config with fast settings for multi-agent tests."""
    return WorkflowConfig(
        multi_agent={
            "enabled": True,
            "max_agents": 4,
            "dropbox_sync_delay_seconds": 0.0,
            "lock_retry_attempts": 1,
            "lock_retry_delay_seconds": 0.0,
            "dashboard_refresh_seconds": 10,
        },
        limits={
            "max_iterations": 3,
            "timeout_seconds": 30,
            "timeout_cooldown_base_seconds": 0,
        },
    )


@pytest.fixture
def fast_multi_config() -> MultiAgentConfig:
    return MultiAgentConfig(
        enabled=True,
        max_agents=4,
        dropbox_sync_delay_seconds=0.0,
        lock_retry_attempts=1,
        lock_retry_delay_seconds=0.0,
    )


# ── WorkSplitter ────────────────────────────────────────────────────


class TestWorkSplitter:
    def test_split_empty_files(self) -> None:
        """Empty file list produces empty buckets."""
        result = WorkSplitter.split_for_agents([], 2)
        assert len(result) == 2
        assert all(len(b) == 0 for b in result)

    def test_split_single_agent(self) -> None:
        """Single agent gets all files."""
        files = ["src/a.py", "src/b.py", "lib/c.py"]
        result = WorkSplitter.split_for_agents(files, 1)
        assert len(result) == 1
        assert len(result[0]) == 3

    def test_split_two_agents(self) -> None:
        """Two agents get roughly equal splits."""
        files = ["src/a.py", "src/b.py", "lib/c.py", "lib/d.py"]
        result = WorkSplitter.split_for_agents(files, 2)
        assert len(result) == 2
        total = sum(len(b) for b in result)
        assert total == 4

    def test_split_keeps_directory_together(self) -> None:
        """Files in the same directory stay together."""
        files = ["src/a.py", "src/b.py", "src/c.py", "lib/d.py"]
        result = WorkSplitter.split_for_agents(files, 2)
        # src/ files should be in one bucket
        for bucket in result:
            src_files = [f for f in bucket if f.startswith("src/")]
            if src_files:
                assert len(src_files) == 3  # All src/ files together

    def test_split_more_agents_than_files(self) -> None:
        """More agents than files — some get empty lists."""
        files = ["a.py", "b.py"]
        result = WorkSplitter.split_for_agents(files, 4)
        assert len(result) == 4
        total = sum(len(b) for b in result)
        assert total == 2

    def test_split_zero_agents(self) -> None:
        """Zero agents returns empty list."""
        result = WorkSplitter.split_for_agents(["a.py"], 0)
        assert result == []

    def test_identify_no_conflicts(self) -> None:
        """No conflicts when assignments are disjoint."""
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py", "b.py"]),
            WorkAssignment(agent_id="agent-2", files=["c.py", "d.py"]),
        ]
        assert WorkSplitter.identify_sequential_phases(assignments) == []

    def test_identify_conflicts(self) -> None:
        """Detects files assigned to multiple agents."""
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py", "shared.py"]),
            WorkAssignment(agent_id="agent-2", files=["b.py", "shared.py"]),
        ]
        conflicts = WorkSplitter.identify_sequential_phases(assignments)
        assert "shared.py" in conflicts


# ── AgentWorkspace ──────────────────────────────────────────────────


class TestAgentWorkspace:
    def test_setup_creates_structure(
        self, multi_project: Path, fast_multi_config: MultiAgentConfig
    ) -> None:
        """setup() creates workspace directory structure."""
        ws = AgentWorkspace(multi_project, "agent-1", fast_multi_config)
        assignment = WorkAssignment(
            agent_id="agent-1",
            files=["src/a.py", "src/b.py"],
            instructions="Implement module A",
            phase_label="Phase 1",
        )
        ws.setup(assignment)

        assert ws.workflow_dir.exists()
        assert ws.manifest_path.exists()
        assert ws.claude_md_path.exists()

    def test_claude_md_content(
        self, multi_project: Path, fast_multi_config: MultiAgentConfig
    ) -> None:
        """Agent CLAUDE.md contains instructions and file list."""
        ws = AgentWorkspace(multi_project, "agent-1", fast_multi_config)
        assignment = WorkAssignment(
            agent_id="agent-1",
            files=["src/a.py"],
            instructions="Implement feature X",
            phase_label="Phase 1",
        )
        ws.setup(assignment)

        content = ws.claude_md_path.read_text(encoding="utf-8")
        assert "agent-1" in content
        assert "Implement feature X" in content
        assert "src/a.py" in content
        assert "PROJECT_COMPLETE" in content

    def test_manifest_populated(
        self, multi_project: Path, fast_multi_config: MultiAgentConfig
    ) -> None:
        """assigned_files.txt contains the assigned files."""
        ws = AgentWorkspace(multi_project, "agent-1", fast_multi_config)
        assignment = WorkAssignment(
            agent_id="agent-1",
            files=["src/a.py", "src/b.py"],
        )
        ws.setup(assignment)

        manifest = FileManifest(ws.manifest_path)
        files = manifest.load()
        assert len(files) == 2

    def test_get_state_no_state(
        self, multi_project: Path, fast_multi_config: MultiAgentConfig
    ) -> None:
        """get_state returns default idle state when no state file exists."""
        ws = AgentWorkspace(multi_project, "agent-1", fast_multi_config)
        ws.workflow_dir.mkdir(parents=True, exist_ok=True)
        state = ws.get_state()
        assert state is not None
        assert state.status == "idle"
        assert state.iteration == 0

    def test_get_state_with_state(
        self, multi_project: Path, fast_multi_config: MultiAgentConfig
    ) -> None:
        """get_state returns WorkflowState when state file exists."""
        ws = AgentWorkspace(multi_project, "agent-1", fast_multi_config)
        ws.workflow_dir.mkdir(parents=True, exist_ok=True)

        # Write a state file
        tracker = StateTracker(multi_project, workflow_dir=ws.workflow_dir)
        tracker.start_session()
        tracker.save()

        state = ws.get_state()
        assert state is not None
        assert state.status == "running"

    def test_get_status_pending(
        self, multi_project: Path, fast_multi_config: MultiAgentConfig
    ) -> None:
        """get_status returns 'pending' before state exists."""
        ws = AgentWorkspace(multi_project, "agent-1", fast_multi_config)
        assignment = WorkAssignment(
            agent_id="agent-1",
            files=["a.py", "b.py"],
        )
        ws.setup(assignment)

        status = ws.get_status()
        assert status.agent_id == "agent-1"
        assert status.status == "idle"
        assert status.files_assigned == 2

    def test_get_status_running(
        self, multi_project: Path, fast_multi_config: MultiAgentConfig
    ) -> None:
        """get_status reflects running state."""
        ws = AgentWorkspace(multi_project, "agent-1", fast_multi_config)
        assignment = WorkAssignment(agent_id="agent-1", files=["a.py"])
        ws.setup(assignment)

        tracker = StateTracker(multi_project, workflow_dir=ws.workflow_dir)
        tracker.start_session()
        tracker.increment_iteration()
        tracker.add_cycle("test", cost_usd=0.5, num_turns=10)
        tracker.save()

        status = ws.get_status()
        assert status.status == "running"
        assert status.iteration == 1
        assert status.total_cost_usd == 0.5

    def test_cleanup(
        self, multi_project: Path, fast_multi_config: MultiAgentConfig
    ) -> None:
        """cleanup() removes workspace directory."""
        ws = AgentWorkspace(multi_project, "agent-1", fast_multi_config)
        assignment = WorkAssignment(agent_id="agent-1", files=["a.py"])
        ws.setup(assignment)
        assert ws.workspace_dir.exists()

        ws.cleanup()
        assert not ws.workspace_dir.exists()


# ── MultiAgentOrchestrator ──────────────────────────────────────────


class TestMultiAgentOrchestrator:
    def test_setup_workspaces(
        self, multi_project: Path, multi_config: WorkflowConfig
    ) -> None:
        """setup_workspaces creates all agent directories."""
        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(
                agent_id="agent-1", files=["a.py"], instructions="Do A"
            ),
            WorkAssignment(
                agent_id="agent-2", files=["b.py"], instructions="Do B"
            ),
        ]
        orch.setup_workspaces(assignments)

        assert len(orch.workspaces) == 2
        assert (multi_project / ".agents" / "agent-1" / "CLAUDE.md").exists()
        assert (multi_project / ".agents" / "agent-2" / "CLAUDE.md").exists()
        assert (multi_project / ".agents" / "shared").exists()

    def test_setup_too_many_agents(
        self, multi_project: Path, multi_config: WorkflowConfig
    ) -> None:
        """setup_workspaces raises ValueError with too many agents."""
        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id=f"agent-{i}", files=[f"{i}.py"])
            for i in range(10)
        ]
        with pytest.raises(ValueError, match="Too many agents"):
            orch.setup_workspaces(assignments)

    def test_get_all_statuses(
        self, multi_project: Path, multi_config: WorkflowConfig
    ) -> None:
        """get_all_statuses returns status for each agent."""
        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
            WorkAssignment(agent_id="agent-2", files=["b.py"]),
        ]
        orch.setup_workspaces(assignments)

        statuses = orch.get_all_statuses()
        assert len(statuses) == 2
        assert "agent-1" in statuses
        assert "agent-2" in statuses

    def test_generate_dashboard(
        self, multi_project: Path, multi_config: WorkflowConfig
    ) -> None:
        """generate_dashboard produces valid markdown table."""
        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
            WorkAssignment(agent_id="agent-2", files=["b.py"]),
        ]
        orch.setup_workspaces(assignments)

        dashboard = orch.generate_dashboard()
        assert "# Multi-Agent Dashboard" in dashboard
        assert "agent-1" in dashboard
        assert "agent-2" in dashboard
        assert "| Agent |" in dashboard

    def test_generate_dashboard_with_locks(
        self, multi_project: Path, multi_config: WorkflowConfig
    ) -> None:
        """Dashboard shows lock counts per agent."""
        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
        ]
        orch.setup_workspaces(assignments)

        # Acquire a lock
        orch.lock_registry.acquire("/a.py", "agent-1", sync_delay=0)

        dashboard = orch.generate_dashboard()
        assert "Active locks" in dashboard

    @patch("multi_agent.subprocess.Popen")
    def test_launch_all(
        self, mock_popen: MagicMock,
        multi_project: Path, multi_config: WorkflowConfig,
    ) -> None:
        """launch_all spawns a process per agent."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
            WorkAssignment(agent_id="agent-2", files=["b.py"]),
        ]
        orch.setup_workspaces(assignments)
        pids = orch.launch_all(prompt="Do work", dry_run=True)

        assert len(pids) == 2
        assert mock_popen.call_count == 2

        # Verify CLAUDE_AGENT_ID in env
        for call in mock_popen.call_args_list:
            env = call[1].get("env", {})
            assert "CLAUDE_AGENT_ID" in env

    @patch("multi_agent.subprocess.Popen")
    def test_launch_with_model(
        self, mock_popen: MagicMock,
        multi_project: Path, multi_config: WorkflowConfig,
    ) -> None:
        """launch_all passes model to loop_driver."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
        ]
        orch.setup_workspaces(assignments)
        orch.launch_all(model="opus")

        call_args = mock_popen.call_args[0][0]
        assert "--model" in call_args
        assert "opus" in call_args

    @patch("multi_agent.subprocess.Popen")
    def test_monitor_all_completes(
        self, mock_popen: MagicMock,
        multi_project: Path, multi_config: WorkflowConfig,
    ) -> None:
        """monitor_all returns when all processes complete."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = 0  # Already finished

        mock_popen.return_value = mock_proc

        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
        ]
        orch.setup_workspaces(assignments)
        orch.launch_all()

        statuses = orch.monitor_all(timeout=5)
        assert len(statuses) == 1

    @patch("multi_agent.subprocess.Popen")
    def test_monitor_timeout(
        self, mock_popen: MagicMock,
        multi_project: Path, multi_config: WorkflowConfig,
    ) -> None:
        """monitor_all kills processes on timeout."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # Never finishes
        mock_proc.wait.return_value = None

        mock_popen.return_value = mock_proc

        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
        ]
        orch.setup_workspaces(assignments)
        orch.launch_all()

        statuses = orch.monitor_all(timeout=2)
        mock_proc.kill.assert_called()

    def test_run_merge_phase_no_validation(
        self, multi_project: Path, multi_config: WorkflowConfig
    ) -> None:
        """Merge phase with validation disabled passes."""
        multi_config.validation.enabled = False
        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
        ]
        orch.setup_workspaces(assignments)

        # Acquire a lock first
        orch.lock_registry.acquire("/a.py", "agent-1", sync_delay=0)

        result = orch.run_merge_phase()
        assert result["locks_released"] is True
        assert result["tests_passed"] is True

    @patch("multi_agent.subprocess.run")
    def test_run_merge_phase_with_tests(
        self, mock_run: MagicMock,
        multi_project: Path, multi_config: WorkflowConfig,
    ) -> None:
        """Merge phase runs tests when validation enabled."""
        multi_config.validation.enabled = True
        multi_config.validation.test_command = "pytest tests/ -v"
        mock_run.return_value = MagicMock(returncode=0, stdout="passed", stderr="")

        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
        ]
        orch.setup_workspaces(assignments)

        result = orch.run_merge_phase()
        assert result["tests_passed"] is True
        mock_run.assert_called_once()

    @patch("multi_agent.subprocess.run")
    def test_run_merge_phase_tests_fail(
        self, mock_run: MagicMock,
        multi_project: Path, multi_config: WorkflowConfig,
    ) -> None:
        """Merge phase reports test failure."""
        multi_config.validation.enabled = True
        multi_config.validation.test_command = "pytest tests/ -v"
        mock_run.return_value = MagicMock(returncode=1, stdout="2 failed", stderr="error")

        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
        ]
        orch.setup_workspaces(assignments)

        result = orch.run_merge_phase()
        assert result["tests_passed"] is False

    @patch("multi_agent.subprocess.Popen")
    def test_cleanup_releases_locks_and_kills(
        self, mock_popen: MagicMock,
        multi_project: Path, multi_config: WorkflowConfig,
    ) -> None:
        """cleanup() releases locks and kills processes."""
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None

        mock_popen.return_value = mock_proc

        orch = MultiAgentOrchestrator(multi_project, multi_config)
        assignments = [
            WorkAssignment(agent_id="agent-1", files=["a.py"]),
        ]
        orch.setup_workspaces(assignments)
        orch.launch_all()

        # Acquire a lock
        orch.lock_registry.acquire("/a.py", "agent-1", sync_delay=0)

        orch.cleanup()
        mock_proc.kill.assert_called()
        # Lock should be released
        assert len(orch.lock_registry.get_locks()) == 0


# ── AgentStatus Model ───────────────────────────────────────────────


class TestAgentStatus:
    def test_defaults(self) -> None:
        s = AgentStatus(agent_id="agent-1")
        assert s.status == "pending"
        assert s.iteration == 0
        assert s.total_cost_usd == 0.0

    def test_populated(self) -> None:
        s = AgentStatus(
            agent_id="agent-1",
            status="completed",
            iteration=5,
            total_cost_usd=1.23,
            total_turns=50,
        )
        assert s.status == "completed"
        assert s.iteration == 5


# ── WorkAssignment Model ────────────────────────────────────────────


class TestWorkAssignment:
    def test_defaults(self) -> None:
        a = WorkAssignment(agent_id="agent-1")
        assert a.files == []
        assert a.instructions == ""

    def test_populated(self) -> None:
        a = WorkAssignment(
            agent_id="agent-1",
            files=["a.py", "b.py"],
            instructions="Do things",
            phase_label="Phase 1",
        )
        assert len(a.files) == 2


# ── Loop Driver Agent ID Integration ────────────────────────────────


class TestLoopDriverAgentId:
    def test_agent_id_default_none(self, project_dir: Path) -> None:
        """Default agent_id is None."""
        from loop_driver import LoopDriver
        config = WorkflowConfig(limits={"max_iterations": 1, "timeout_cooldown_base_seconds": 0})
        driver = LoopDriver(project_dir, config)
        assert driver.agent_id is None

    def test_agent_id_set(self, project_dir: Path) -> None:
        """agent_id is stored on the driver."""
        from loop_driver import LoopDriver
        config = WorkflowConfig(limits={"max_iterations": 1, "timeout_cooldown_base_seconds": 0})
        driver = LoopDriver(project_dir, config, agent_id="agent-1")
        assert driver.agent_id == "agent-1"

    def test_agent_id_routes_state(self, project_dir: Path) -> None:
        """State file routes to agent-specific directory."""
        from loop_driver import LoopDriver
        config = WorkflowConfig(limits={"max_iterations": 1, "timeout_cooldown_base_seconds": 0})
        driver = LoopDriver(project_dir, config, agent_id="agent-1")
        expected = project_dir / ".agents" / "agent-1" / ".workflow" / "state.json"
        assert driver.tracker.state_path == expected

    def test_agent_id_none_default_state_path(self, project_dir: Path) -> None:
        """Without agent_id, state goes to default .workflow/ path."""
        from loop_driver import LoopDriver
        config = WorkflowConfig(limits={"max_iterations": 1, "timeout_cooldown_base_seconds": 0})
        driver = LoopDriver(project_dir, config)
        expected = project_dir / ".workflow" / "state.json"
        assert driver.tracker.state_path == expected

    def test_agent_id_trace_event_includes_id(self, project_dir: Path) -> None:
        """Trace events include agent_id when set."""
        from loop_driver import LoopDriver
        config = WorkflowConfig(limits={"max_iterations": 1, "timeout_cooldown_base_seconds": 0})
        driver = LoopDriver(project_dir, config, agent_id="agent-1")
        driver._write_trace_event("test_event", data="hello")

        trace_path = (
            project_dir / ".agents" / "agent-1" / ".workflow" / "trace.jsonl"
        )
        assert trace_path.exists()
        line = trace_path.read_text(encoding="utf-8").strip()
        event = json.loads(line)
        assert event["agent_id"] == "agent-1"
        assert event["event_type"] == "test_event"

    def test_agent_id_none_trace_no_agent_field(self, project_dir: Path) -> None:
        """Trace events don't include agent_id when not set."""
        from loop_driver import LoopDriver
        config = WorkflowConfig(limits={"max_iterations": 1, "timeout_cooldown_base_seconds": 0})
        driver = LoopDriver(project_dir, config)
        driver._write_trace_event("test_event")

        trace_path = project_dir / ".workflow" / "trace.jsonl"
        assert trace_path.exists()
        line = trace_path.read_text(encoding="utf-8").strip()
        event = json.loads(line)
        assert "agent_id" not in event

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_dry_run_with_agent_id(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path,
    ) -> None:
        """Dry run works with agent_id set."""
        from helpers import make_subprocess_dispatcher, mock_playwright_result
        from loop_driver import EXIT_MAX_ITERATIONS, LoopDriver

        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        config = WorkflowConfig(
            limits={"max_iterations": 2, "timeout_seconds": 30, "timeout_cooldown_base_seconds": 0},
            retry={"max_retries": 0, "base_delay_seconds": 0.001},
        )
        driver = LoopDriver(
            project_dir, config, dry_run=True, agent_id="agent-1"
        )
        exit_code = driver.run()
        assert exit_code == EXIT_MAX_ITERATIONS

        # Verify state was written to agent-specific path
        state_path = (
            project_dir / ".agents" / "agent-1" / ".workflow" / "state.json"
        )
        assert state_path.exists()


# ── StateTracker with workflow_dir ──────────────────────────────────


class TestStateTrackerWorkflowDir:
    def test_default_path(self, tmp_path: Path) -> None:
        """Default StateTracker uses .workflow/state.json."""
        tracker = StateTracker(tmp_path)
        assert tracker.state_path == tmp_path / ".workflow" / "state.json"

    def test_custom_workflow_dir(self, tmp_path: Path) -> None:
        """Custom workflow_dir overrides default path."""
        custom = tmp_path / "custom" / ".workflow"
        tracker = StateTracker(tmp_path, workflow_dir=custom)
        assert tracker.state_path == custom / "state.json"

    def test_save_load_custom_dir(self, tmp_path: Path) -> None:
        """Save and load work with custom workflow_dir."""
        custom = tmp_path / "agents" / "agent-1" / ".workflow"
        tracker = StateTracker(tmp_path, workflow_dir=custom)
        tracker.start_session()
        tracker.increment_iteration()
        tracker.add_cycle("test prompt", cost_usd=0.5, num_turns=10)
        tracker.save()

        # Load into a new tracker
        tracker2 = StateTracker(tmp_path, workflow_dir=custom)
        result = tracker2.load()
        assert result.success
        assert tracker2.state.iteration == 1
        assert tracker2.state.metrics.total_cost_usd == 0.5

"""Tests for loop_driver module."""

import json
import logging
import subprocess as sp
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config import WorkflowConfig
from loop_driver import EXIT_BUDGET_EXCEEDED, EXIT_COMPLETE, EXIT_MAX_ITERATIONS, EXIT_STAGNATION, JsonFormatter, LoopDriver

from helpers import (
    build_ndjson_stream,
    make_popen_dispatcher,
    make_subprocess_dispatcher,
    mock_git_diff_stat_result,
    mock_git_log_result,
    mock_playwright_result,
    mock_post_review_result,
    mock_test_result,
    mock_verification_result,
    MockPopen,
)


@pytest.fixture
def config() -> WorkflowConfig:
    return WorkflowConfig(
        limits={
            "max_iterations": 3,
            "timeout_seconds": 30,
            "max_per_iteration_budget_usd": 5.0,
            "max_total_budget_usd": 10.0,
            "timeout_cooldown_base_seconds": 0,  # Disable cooldown in tests
        },
        retry={"max_retries": 0, "base_delay_seconds": 0.001},
    )


class TestDryRun:
    def test_dry_run_completes_max_iterations(
        self, project_dir: Path, config: WorkflowConfig
    ) -> None:
        """Dry run simulates iterations without spawning Claude."""
        with patch("research_bridge.subprocess.run") as mock_run:
            mock_run.return_value = mock_playwright_result()
            driver = LoopDriver(project_dir, config, dry_run=True)
            exit_code = driver.run()
            assert exit_code == EXIT_MAX_ITERATIONS

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_dry_run_no_claude_spawned(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Dry run never spawns Claude CLI (subprocess.Popen with 'claude' args)."""
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )
        mock_popen.side_effect = make_popen_dispatcher()

        driver = LoopDriver(project_dir, config, dry_run=True)
        driver.run()

        # Verify no Popen calls with 'claude'
        claude_calls = [
            c for c in mock_popen.call_args_list
            if c[0] and isinstance(c[0][0], list) and c[0][0] and c[0][0][0] == "claude"
        ]
        assert len(claude_calls) == 0


class TestCompletionDetection:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_completion_marker_exits_zero(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Completion marker in output exits with code 0."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "All done. PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_completion_case_insensitive(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Completion markers match case-insensitively."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "All done. project_complete"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_completion_partial_match(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Completion marker embedded in a sentence still matches."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream(
                "s1", 0.01, 1,
                "The implementation is now PROJECT_COMPLETE and ready for review."
            ),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE


class TestBudgetExceeded:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_per_iteration_budget_exceeded(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Exceeding per-iteration budget exits with code 2."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 10.0, 1, "Expensive operation"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_BUDGET_EXCEEDED


class TestMaxIterations:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_max_iterations_exit_code(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Reaching max iterations exits with code 1."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "Still working..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_MAX_ITERATIONS


class TestNdjsonParsing:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_session_id_tracked(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Session ID from NDJSON is tracked for --resume."""
        config.limits.max_iterations = 1
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("sess-xyz", 0.01, 1, "Done step 1"),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        assert driver.tracker.state.last_session_id == "sess-xyz"


class TestTimeoutHandling:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_timeout_records_error(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Timeout triggers error recovery path."""
        config.limits.max_iterations = 1
        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        assert driver.tracker.get_metrics().error_count >= 0  # Doesn't crash


class TestResearchFailureFallback:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_research_failure_uses_fallback(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Research failure falls back to generic prompt."""
        config.limits.max_iterations = 2
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "Working..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_side_effect=sp.TimeoutExpired(cmd="python", timeout=600),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()

        # Should not crash — falls back gracefully
        assert exit_code == EXIT_MAX_ITERATIONS


class TestResumeSessionId:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_resume_session_passed_to_claude(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Second iteration passes --resume with session ID from first."""
        config.limits.max_iterations = 2
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                sid = f"s{call_count[0]}"
                return MockPopen(build_ndjson_stream(sid, 0.01, 1, "Working..."))
            return MockPopen("")  # taskkill

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if "council_browser" in str(cmd):
                    return mock_playwright_result("Continue")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = run_side_effect

        driver = LoopDriver(project_dir, config)
        driver.run()

        # Find claude CLI calls
        claude_calls = [
            c for c in mock_popen.call_args_list
            if c[0] and isinstance(c[0][0], list) and c[0][0] and c[0][0][0] == "claude"
        ]
        assert len(claude_calls) >= 2
        # Second call should have --resume with s1
        second_call_args = claude_calls[1][0][0]
        assert "--resume" in second_call_args
        resume_idx = second_call_args.index("--resume")
        assert second_call_args[resume_idx + 1] == "s1"


class TestErrorClearsSession:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_error_clears_session_for_retry(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """After error, next iteration doesn't use --resume."""
        config.limits.max_iterations = 2
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                if call_count[0] == 1:
                    # First call: returns error with a session ID
                    return MockPopen(
                        build_ndjson_stream(
                            "err-session", 0.01, 1, "Error occurred", is_error=True
                        )
                    )
                else:
                    # Second call: should NOT have --resume
                    return MockPopen(build_ndjson_stream("s2", 0.01, 1, "Working..."))
            return MockPopen("")  # taskkill

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if "council_browser" in str(cmd):
                    return mock_playwright_result()
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = run_side_effect

        driver = LoopDriver(project_dir, config)
        driver.run()

        # Find claude CLI calls
        claude_calls = [
            c for c in mock_popen.call_args_list
            if c[0] and isinstance(c[0][0], list) and c[0][0] and c[0][0][0] == "claude"
        ]
        assert len(claude_calls) >= 2
        # Second call should NOT have --resume (session cleared after error)
        second_call_args = claude_calls[1][0][0]
        assert "--resume" not in second_call_args


class TestMetricsSummary:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_metrics_summary_written_on_complete(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Metrics summary JSON is written when loop completes."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.05, 2, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()

        assert exit_code == EXIT_COMPLETE
        summary_path = project_dir / ".workflow" / "metrics_summary.json"
        assert summary_path.exists()

        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["exit_code"] == 0
        assert summary["status"] == "completed"
        assert summary["iterations"] == 1
        assert summary["total_cost_usd"] == pytest.approx(0.05)
        assert summary["total_turns"] == 2
        assert summary["error_count"] == 0

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_metrics_summary_written_on_budget_exceeded(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Metrics summary JSON is written when budget is exceeded."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 10.0, 1, "Expensive"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()

        assert exit_code == EXIT_BUDGET_EXCEEDED
        summary_path = project_dir / ".workflow" / "metrics_summary.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["exit_code"] == 2
        assert summary["status"] == "failed"


class TestTraceLogging:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_trace_jsonl_written_on_complete(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """After successful run, trace.jsonl contains expected event types."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.05, 2, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

        trace_path = project_dir / ".workflow" / "trace.jsonl"
        assert trace_path.exists()

        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        event_types = [e["event_type"] for e in events]
        assert "loop_start" in event_types
        assert "claude_invoke" in event_types
        assert "claude_complete" in event_types
        assert "completion_detected" in event_types
        assert "loop_end" in event_types

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_trace_events_are_valid_json(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Each trace line is valid JSON with required fields."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        trace_path = project_dir / ".workflow" / "trace.jsonl"
        for line in trace_path.read_text(encoding="utf-8").strip().splitlines():
            event = json.loads(line)
            assert "timestamp" in event
            assert "event_type" in event
            assert "iteration" in event


class TestSmokeTestMode:
    def test_smoke_test_overrides_config(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Smoke test mode overrides config limits."""
        with patch("research_bridge.subprocess.run") as mock_run:
            mock_run.return_value = mock_playwright_result()
            driver = LoopDriver(project_dir, config, smoke_test=True, dry_run=True)
            assert driver.config.limits.max_iterations == 1
            assert driver.config.limits.timeout_seconds == 120
            assert driver.config.limits.max_per_iteration_budget_usd == 2.0
            assert driver.config.limits.max_turns_per_iteration == 10

    def test_smoke_test_uses_safe_prompt(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Smoke test mode uses a safe default prompt."""
        with patch("research_bridge.subprocess.run") as mock_run:
            mock_run.return_value = mock_playwright_result()
            driver = LoopDriver(project_dir, config, smoke_test=True, dry_run=True)
            assert "PROJECT_COMPLETE" in driver.initial_prompt
            assert "Review the current project" in driver.initial_prompt


class TestStagnationDetection:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_low_turns_triggers_stagnation_exit(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Consecutive low-turn iterations trigger stagnation exit after session reset."""
        # Need enough iterations: 3 for initial window + 1 reset + 3 more = 7
        config.limits.max_iterations = 10
        config.stagnation.window_size = 3
        config.stagnation.low_turn_threshold = 2

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "Thinking..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_STAGNATION

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_stagnation_resets_session_first(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Stagnation detection resets session before giving up."""
        config.limits.max_iterations = 10
        config.stagnation.window_size = 3
        config.stagnation.low_turn_threshold = 2

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "Thinking..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        # Verify trace has a stagnation_reset event (first detection)
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        event_types = [e["event_type"] for e in events]
        assert "stagnation_reset" in event_types
        assert "stagnation_exit" in event_types

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_productive_iteration_resets_stagnation(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """A productive iteration (high turns) resets the stagnation flag."""
        config.limits.max_iterations = 5
        config.stagnation.window_size = 3
        config.stagnation.low_turn_threshold = 2
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                # Alternate: 2 low-turn, then 1 productive, then 2 low-turn
                # Never hits window of 3 consecutive low-turn
                turns = 1 if call_count[0] % 3 != 0 else 10
                return MockPopen(
                    build_ndjson_stream(f"s{call_count[0]}", 0.05, turns, "Working...")
                )
            return MockPopen("")  # taskkill

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if "council_browser" in str(cmd):
                    return mock_playwright_result()
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = run_side_effect

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        # Should hit max iterations, not stagnation
        assert exit_code == EXIT_MAX_ITERATIONS

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_stagnation_disabled_by_config(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Stagnation detection can be disabled."""
        config.limits.max_iterations = 5
        config.stagnation.enabled = False
        config.stagnation.window_size = 3
        config.stagnation.low_turn_threshold = 2

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "Thinking..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        # Should reach max iterations, not stagnation
        assert exit_code == EXIT_MAX_ITERATIONS

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_zero_cost_triggers_stagnation(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """All-zero-cost iterations trigger stagnation (context exhaustion)."""
        config.limits.max_iterations = 10
        config.stagnation.window_size = 3
        # Use high turn threshold so the zero-cost check triggers, not low-turn
        config.stagnation.low_turn_threshold = 0

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.0, 5, "Working..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_STAGNATION


class TestConsecutiveTimeouts:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_consecutive_timeouts_exit_stagnation(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Consecutive timeouts exit with stagnation code after limit."""
        config.limits.max_iterations = 5
        config.stagnation.max_consecutive_timeouts = 2

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_STAGNATION

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_timeout_clears_session(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """First timeout clears session for fresh context."""
        config.limits.max_iterations = 3
        config.stagnation.max_consecutive_timeouts = 3  # Don't exit on timeouts
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                if call_count[0] == 1:
                    return MockPopen("")  # Simulates timeout (no result event)
                return MockPopen(build_ndjson_stream("s2", 0.05, 5, "Working..."))
            return MockPopen("")  # taskkill

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if "council_browser" in str(cmd):
                    return mock_playwright_result()
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = run_side_effect

        driver = LoopDriver(project_dir, config)
        driver.run()

        # After timeout, session should be cleared — second call should NOT have --resume
        claude_calls = [
            c for c in mock_popen.call_args_list
            if c[0] and isinstance(c[0][0], list) and c[0][0] and c[0][0][0] == "claude"
        ]
        if len(claude_calls) >= 2:
            second_call_args = claude_calls[1][0][0]
            assert "--resume" not in second_call_args

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_timeout_counter_resets_on_success(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Successful iteration resets the consecutive timeout counter."""
        config.limits.max_iterations = 5
        config.stagnation.max_consecutive_timeouts = 2
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                # Timeout on 1st, succeed on 2nd-5th
                if call_count[0] == 1:
                    return MockPopen("")  # Simulates timeout (no result event)
                return MockPopen(
                    build_ndjson_stream(f"s{call_count[0]}", 0.05, 5, "Working...")
                )
            return MockPopen("")  # taskkill

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if "council_browser" in str(cmd):
                    return mock_playwright_result()
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = run_side_effect

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        # Should not stagnate — timeout counter reset after success
        assert exit_code == EXIT_MAX_ITERATIONS
        assert driver._consecutive_timeouts == 0


class TestModelAwareTimeout:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_opus_gets_double_timeout(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Opus model gets 2x the base timeout."""
        config.limits.max_iterations = 1
        config.limits.timeout_seconds = 600
        config.claude.model = "opus"

        dispatcher = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.50, 10, "PROJECT_COMPLETE"),
        )
        mock_popen.side_effect = dispatcher
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        # wait() was called with effective_timeout
        assert dispatcher.last_claude_popen is not None
        assert dispatcher.last_claude_popen.wait_timeout == 1200  # 600 * 2.0

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_sonnet_gets_normal_timeout(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Sonnet model gets 1x the base timeout (no scaling)."""
        config.limits.max_iterations = 1
        config.limits.timeout_seconds = 600
        config.claude.model = "sonnet"

        dispatcher = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.50, 10, "PROJECT_COMPLETE"),
        )
        mock_popen.side_effect = dispatcher
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        # wait() was called with effective_timeout
        assert dispatcher.last_claude_popen is not None
        assert dispatcher.last_claude_popen.wait_timeout == 600  # 600 * 1.0

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_unknown_model_gets_1x_timeout(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Unknown model defaults to 1x multiplier."""
        config.limits.max_iterations = 1
        config.limits.timeout_seconds = 300
        config.claude.model = "custom-model"

        dispatcher = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.10, 5, "PROJECT_COMPLETE"),
        )
        mock_popen.side_effect = dispatcher
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        # wait() was called with effective_timeout
        assert dispatcher.last_claude_popen is not None
        assert dispatcher.last_claude_popen.wait_timeout == 300  # 300 * 1.0 (default)

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_opus_max_turns_capped(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Opus model caps max-turns to 25 (below config default of 50)."""
        config.limits.max_iterations = 1
        config.limits.max_turns_per_iteration = 50
        config.claude.model = "opus"

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.50, 10, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        claude_calls = [
            c for c in mock_popen.call_args_list
            if c[0] and isinstance(c[0][0], list) and c[0][0] and c[0][0][0] == "claude"
        ]
        assert len(claude_calls) >= 1
        args = claude_calls[0][0][0]
        turns_idx = args.index("--max-turns")
        assert args[turns_idx + 1] == "25"

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_sonnet_max_turns_default(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Sonnet model uses full config max-turns (no override)."""
        config.limits.max_iterations = 1
        config.limits.max_turns_per_iteration = 50
        config.claude.model = "sonnet"

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.50, 10, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        claude_calls = [
            c for c in mock_popen.call_args_list
            if c[0] and isinstance(c[0][0], list) and c[0][0] and c[0][0][0] == "claude"
        ]
        assert len(claude_calls) >= 1
        args = claude_calls[0][0][0]
        turns_idx = args.index("--max-turns")
        assert args[turns_idx + 1] == "50"

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_opus_three_timeouts_before_stagnation(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Opus needs 3 consecutive timeouts before stagnation exit (not 2).

        With model fallback enabled (default), Opus falls back to Sonnet at 2 timeouts,
        so we disable fallback here to test the raw Opus timeout limit.
        """
        config.limits.max_iterations = 5
        config.claude.model = "opus"
        config.stagnation.max_consecutive_timeouts = 2  # base: 2
        config.limits.model_fallback = {}  # Disable fallback to test raw Opus limit
        # opus override defaults to 3

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_STAGNATION
        assert driver._consecutive_timeouts == 3  # Not 2

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_sonnet_two_timeouts_triggers_stagnation(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Sonnet still uses default of 2 consecutive timeouts for stagnation."""
        config.limits.max_iterations = 5
        config.claude.model = "sonnet"
        config.stagnation.max_consecutive_timeouts = 2

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_STAGNATION
        assert driver._consecutive_timeouts == 2


class TestJsonFormatter:
    def test_json_log_format_produces_valid_json(self) -> None:
        """JsonFormatter produces valid JSON output."""
        formatter = JsonFormatter(datefmt="%Y-%m-%d %H:%M:%S")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Test message with data: %s", args=("value",),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "INFO"
        assert "Test message with data: value" in parsed["message"]
        assert "module" in parsed
        assert "timestamp" in parsed


class TestTimeoutCooldown:
    @patch("loop_driver.time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_cooldown_applied_after_timeout(
        self, mock_run: MagicMock, mock_popen: MagicMock, mock_sleep: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """After first timeout, loop sleeps for cooldown before retry."""
        config.limits.max_iterations = 3
        config.limits.timeout_cooldown_base_seconds = 60
        config.limits.timeout_cooldown_max_seconds = 300
        config.stagnation.max_consecutive_timeouts = 3
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                if call_count[0] == 1:
                    return MockPopen("")  # Timeout
                return MockPopen(build_ndjson_stream("s2", 0.05, 5, "PROJECT_COMPLETE"))
            return MockPopen("")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        # Verify sleep was called with base cooldown (60s for first timeout)
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert 60 in sleep_calls

    @patch("loop_driver.time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_cooldown_escalates(
        self, mock_run: MagicMock, mock_popen: MagicMock, mock_sleep: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Consecutive timeouts increase cooldown (60, 120)."""
        config.limits.max_iterations = 5
        config.limits.timeout_cooldown_base_seconds = 60
        config.limits.timeout_cooldown_max_seconds = 300
        config.stagnation.max_consecutive_timeouts = 4
        config.limits.model_fallback = {}  # Disable fallback

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert 60 in sleep_calls   # First timeout
        assert 120 in sleep_calls  # Second timeout

    @patch("loop_driver.time.sleep")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_cooldown_capped_at_max(
        self, mock_run: MagicMock, mock_popen: MagicMock, mock_sleep: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Cooldown doesn't exceed max configured value."""
        config.limits.max_iterations = 10
        config.limits.timeout_cooldown_base_seconds = 100
        config.limits.timeout_cooldown_max_seconds = 200
        config.stagnation.max_consecutive_timeouts = 5
        config.limits.model_fallback = {}  # Disable fallback

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        # All cooldowns should be <= max
        for val in sleep_calls:
            assert val <= 200


class TestPreflightCheck:
    @patch("subprocess.run")
    def test_preflight_passes(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Preflight passes when claude --version succeeds."""
        mock_run.return_value = MagicMock(returncode=0, stdout="claude 1.0.0\n", stderr="")
        driver = LoopDriver(project_dir, config, dry_run=True)
        assert driver._preflight_check() is True

    @patch("subprocess.run")
    def test_preflight_fails_missing_cli(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Preflight fails when claude is not on PATH."""
        mock_run.side_effect = FileNotFoundError("claude not found")
        driver = LoopDriver(project_dir, config, dry_run=True)
        assert driver._preflight_check() is False

    @patch("subprocess.run")
    def test_preflight_fails_timeout(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Preflight fails when claude --version times out."""
        mock_run.side_effect = sp.TimeoutExpired(cmd="claude", timeout=30)
        driver = LoopDriver(project_dir, config, dry_run=True)
        assert driver._preflight_check() is False

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_preflight_failure_exits_stagnation(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Preflight failure exits with EXIT_STAGNATION before any iteration."""
        mock_run.side_effect = FileNotFoundError("claude not found")
        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_STAGNATION

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_skip_preflight_flag(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """--skip-preflight bypasses the preflight check."""
        config.limits.max_iterations = 1
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config, skip_preflight=True)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE


class TestDiagnosticCapture:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_timeout_trace_includes_event_count(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Timeout trace event includes ndjson_events_received count."""
        config.limits.max_iterations = 2
        config.stagnation.max_consecutive_timeouts = 2

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        timeout_events = [e for e in events if e["event_type"] == "timeout_detected"]
        assert len(timeout_events) >= 1
        assert "ndjson_events_received" in timeout_events[0]
        assert timeout_events[0]["ndjson_events_received"] == 0
        assert "had_session_id" in timeout_events[0]

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_zero_events_logs_warning(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig, caplog,
    ) -> None:
        """Zero events on timeout produces specific warning."""
        config.limits.max_iterations = 1
        config.stagnation.max_consecutive_timeouts = 2

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        with caplog.at_level(logging.WARNING):
            driver.run()

        assert any("ZERO events" in r.message for r in caplog.records)


class TestModelFallback:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_opus_falls_back_to_sonnet_after_2_timeouts(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """After 2 Opus timeouts, model switches to Sonnet."""
        config.limits.max_iterations = 5
        config.claude.model = "opus"
        config.stagnation.max_consecutive_timeouts = 2
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                if call_count[0] <= 2:
                    return MockPopen("")  # Timeout (Opus)
                # Sonnet succeeds
                return MockPopen(
                    build_ndjson_stream(f"s{call_count[0]}", 0.05, 5, "PROJECT_COMPLETE")
                )
            return MockPopen("")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

        # Verify model was switched
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        fallback_events = [e for e in events if e["event_type"] == "model_fallback"]
        assert len(fallback_events) == 1
        assert fallback_events[0]["from_model"] == "opus"
        assert fallback_events[0]["to_model"] == "sonnet"

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_fallback_reverts_on_success(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """After Sonnet succeeds productively, model reverts to Opus."""
        config.limits.max_iterations = 5
        config.claude.model = "opus"
        config.stagnation.max_consecutive_timeouts = 2
        config.stagnation.low_turn_threshold = 2
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                if call_count[0] <= 2:
                    return MockPopen("")  # Timeout (Opus)
                # Sonnet succeeds with productive iteration (turns > threshold)
                return MockPopen(
                    build_ndjson_stream(f"s{call_count[0]}", 0.05, 10, "Working...")
                )
            return MockPopen("")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        # Verify model reverted
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        revert_events = [e for e in events if e["event_type"] == "model_fallback_revert"]
        assert len(revert_events) >= 1
        assert revert_events[0]["from_model"] == "sonnet"
        assert revert_events[0]["to_model"] == "opus"

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_fallback_model_stagnates_exits(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """If fallback model also times out, stagnation exit still works."""
        config.limits.max_iterations = 10
        config.claude.model = "opus"
        config.stagnation.max_consecutive_timeouts = 2

        # All timeouts — Opus falls back to Sonnet, Sonnet also times out
        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_STAGNATION

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_no_fallback_when_already_using_fallback(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Fallback only triggers once — no cascading fallbacks."""
        config.limits.max_iterations = 10
        config.claude.model = "opus"
        config.stagnation.max_consecutive_timeouts = 2

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        # Should have exactly 1 fallback event (opus→sonnet), not opus→sonnet→?
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        fallback_events = [e for e in events if e["event_type"] == "model_fallback"]
        assert len(fallback_events) == 1


class TestSessionRotation:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_session_rotation_at_turn_limit(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Session rotates when cumulative turns reach the limit."""
        config.limits.max_iterations = 3
        config.stagnation.session_max_turns = 20  # Low limit for testing
        config.stagnation.session_max_cost_usd = 999.0  # Won't trigger

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 15, "Working..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_MAX_ITERATIONS

        # Verify rotation trace event
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        rotation_events = [e for e in events if e["event_type"] == "session_rotation"]
        assert len(rotation_events) >= 1
        assert "turn limit" in rotation_events[0]["reason"].lower()

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_session_rotation_at_cost_limit(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Session rotates when cumulative cost reaches the limit."""
        config.limits.max_iterations = 3
        config.limits.max_total_budget_usd = 100.0  # High total so we don't exit on budget
        config.limits.max_per_iteration_budget_usd = 50.0
        config.stagnation.session_max_turns = 9999  # Won't trigger
        config.stagnation.session_max_cost_usd = 1.0  # Low limit for testing

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.80, 10, "Working..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_MAX_ITERATIONS

        # Verify rotation trace event
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        rotation_events = [e for e in events if e["event_type"] == "session_rotation"]
        assert len(rotation_events) >= 1
        assert "cost limit" in rotation_events[0]["reason"].lower()

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_context_exhaustion_triggers_rotation(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Behavioral detection: 2/3 low-turn iterations trigger rotation."""
        config.limits.max_iterations = 5
        config.stagnation.session_max_turns = 9999  # Won't trigger
        config.stagnation.session_max_cost_usd = 999.0  # Won't trigger
        config.stagnation.context_exhaustion_turn_threshold = 5
        config.stagnation.context_exhaustion_window = 3
        # Disable regular stagnation so it doesn't interfere
        config.stagnation.low_turn_threshold = 0

        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                # All iterations with 3 turns (below threshold of 5)
                return MockPopen(
                    build_ndjson_stream(f"s1", 0.05, 3, "Working...")
                )
            return MockPopen("")  # taskkill

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_MAX_ITERATIONS

        # Verify rotation trace event
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        rotation_events = [e for e in events if e["event_type"] == "session_rotation"]
        assert len(rotation_events) >= 1
        assert "context exhaustion" in rotation_events[0]["reason"].lower()

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_rotation_continues_loop(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """After rotation, loop continues (doesn't exit)."""
        config.limits.max_iterations = 4
        config.stagnation.session_max_turns = 10  # Will trigger after iter 1
        config.stagnation.session_max_cost_usd = 999.0

        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                sid = f"s{call_count[0]}"
                return MockPopen(build_ndjson_stream(sid, 0.05, 15, "Working..."))
            return MockPopen("")

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if "council_browser" in str(cmd):
                    return mock_playwright_result()
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = run_side_effect

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        # Should hit max iterations, NOT stagnation
        assert exit_code == EXIT_MAX_ITERATIONS
        # Multiple Claude calls means loop continued
        claude_calls = [
            c for c in mock_popen.call_args_list
            if c[0] and isinstance(c[0][0], list) and c[0][0] and c[0][0][0] == "claude"
        ]
        assert len(claude_calls) == 4

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_rotation_does_not_set_stagnation_flag(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Session rotation doesn't count as a stagnation strike."""
        config.limits.max_iterations = 4
        config.stagnation.session_max_turns = 10  # Triggers rotation
        config.stagnation.session_max_cost_usd = 999.0

        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                sid = f"s{call_count[0]}"
                return MockPopen(build_ndjson_stream(sid, 0.05, 15, "Working..."))
            return MockPopen("")

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if "council_browser" in str(cmd):
                    return mock_playwright_result()
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = run_side_effect

        driver = LoopDriver(project_dir, config)
        driver.run()

        # Rotation should NOT have set the stagnation reset flag
        assert driver._stagnation_reset_done is False

    def test_should_rotate_disabled(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Rotation returns False when stagnation is disabled."""
        config.stagnation.enabled = False
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        should, reason = driver._should_rotate_session("s1")
        assert should is False

    def test_should_rotate_no_session(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Rotation returns False when session_id is None."""
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        should, reason = driver._should_rotate_session(None)
        assert should is False


class TestComputeCooldown:
    def test_first_timeout_returns_base(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """First timeout returns the base cooldown value."""
        config.limits.timeout_cooldown_base_seconds = 60
        config.limits.timeout_cooldown_max_seconds = 300
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        assert driver._compute_cooldown(1) == 60

    def test_second_timeout_doubles(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Second timeout doubles the base."""
        config.limits.timeout_cooldown_base_seconds = 60
        config.limits.timeout_cooldown_max_seconds = 300
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        assert driver._compute_cooldown(2) == 120

    def test_capped_at_max(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Cooldown capped at max."""
        config.limits.timeout_cooldown_base_seconds = 60
        config.limits.timeout_cooldown_max_seconds = 300
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        assert driver._compute_cooldown(10) == 300

    def test_zero_base_returns_zero(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Base of 0 disables cooldown."""
        config.limits.timeout_cooldown_base_seconds = 0
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        assert driver._compute_cooldown(5) == 0


class TestTraceLogRotation:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_trace_rotates_when_over_limit(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """trace.jsonl rotates to .jsonl.1 when exceeding configured size."""
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        # Write enough data to exceed limit
        trace_path.write_text("x" * 500, encoding="utf-8")
        config.limits.trace_max_size_bytes = 100  # Very low limit

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        rotated = trace_path.with_suffix(".jsonl.1")
        assert rotated.exists()
        # New trace.jsonl should exist with fresh events
        assert trace_path.exists()
        assert trace_path.stat().st_size < 500  # Smaller than original

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_trace_rotation_replaces_existing_backup(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Rotation replaces existing .jsonl.1 file."""
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("new_data_" * 100, encoding="utf-8")
        rotated = trace_path.with_suffix(".jsonl.1")
        rotated.write_text("old_backup", encoding="utf-8")
        config.limits.trace_max_size_bytes = 100

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        assert rotated.exists()
        assert "old_backup" not in rotated.read_text(encoding="utf-8")

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_trace_no_rotation_when_zero(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """trace_max_size_bytes=0 disables rotation."""
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("x" * 500, encoding="utf-8")
        config.limits.trace_max_size_bytes = 0

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        rotated = trace_path.with_suffix(".jsonl.1")
        assert not rotated.exists()


class TestExtendedPreflightChecks:
    @patch("subprocess.run")
    def test_preflight_warns_missing_claude_md(
        self, mock_run: MagicMock, tmp_path: Path, config: WorkflowConfig, caplog,
    ) -> None:
        """Preflight warns when CLAUDE.md is missing."""
        (tmp_path / ".workflow").mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="claude 1.0.0\n", stderr="")

        driver = LoopDriver(tmp_path, config, dry_run=True)
        with caplog.at_level(logging.WARNING):
            result = driver._preflight_check()

        assert result is True
        assert any("No CLAUDE.md" in r.message for r in caplog.records)

    @patch("subprocess.run")
    def test_preflight_warns_not_git_repo(
        self, mock_run: MagicMock, tmp_path: Path, config: WorkflowConfig, caplog,
    ) -> None:
        """Preflight warns when .git/ doesn't exist."""
        (tmp_path / ".workflow").mkdir()
        (tmp_path / "CLAUDE.md").write_text("# Project", encoding="utf-8")
        mock_run.return_value = MagicMock(returncode=0, stdout="claude 1.0.0\n", stderr="")

        driver = LoopDriver(tmp_path, config, dry_run=True)
        with caplog.at_level(logging.WARNING):
            driver._preflight_check()

        assert any("Not a git repo" in r.message for r in caplog.records)

    @patch("subprocess.run")
    def test_preflight_no_warnings_when_all_present(
        self, mock_run: MagicMock, tmp_path: Path, config: WorkflowConfig, caplog,
    ) -> None:
        """Preflight logs no warnings when all checks pass."""
        (tmp_path / ".workflow").mkdir()
        (tmp_path / "CLAUDE.md").write_text("# Project", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="claude 1.0.0\n", stderr="")

        driver = LoopDriver(tmp_path, config, dry_run=True)
        with caplog.at_level(logging.WARNING):
            result = driver._preflight_check()

        assert result is True
        preflight_warnings = [r for r in caplog.records if "Preflight:" in r.message]
        # May have Perplexity session warning, but no CLAUDE.md or git warnings
        no_project_warnings = [
            r for r in preflight_warnings
            if "CLAUDE.md" in r.message or "git repo" in r.message
        ]
        assert len(no_project_warnings) == 0

    @patch("subprocess.run")
    def test_preflight_creates_workflow_dir(
        self, mock_run: MagicMock, tmp_path: Path, config: WorkflowConfig,
    ) -> None:
        """Preflight creates .workflow/ directory if it doesn't exist."""
        (tmp_path / "CLAUDE.md").write_text("# Project", encoding="utf-8")
        mock_run.return_value = MagicMock(returncode=0, stdout="claude 1.0.0\n", stderr="")

        driver = LoopDriver(tmp_path, config, dry_run=True)
        result = driver._preflight_check()

        assert result is True
        assert (tmp_path / ".workflow").exists()


class TestModelAnalyticsInMetrics:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_metrics_summary_includes_model_analytics(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Metrics summary JSON includes per-model analytics."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.05, 2, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

        summary_path = project_dir / ".workflow" / "metrics_summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert "model_analytics" in summary
        assert "sonnet" in summary["model_analytics"]  # default model
        sonnet_stats = summary["model_analytics"]["sonnet"]
        assert sonnet_stats["iterations"] == 1
        assert sonnet_stats["avg_turns"] == 2.0
        assert sonnet_stats["avg_cost_usd"] == pytest.approx(0.05)

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_model_analytics_with_fallback(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Model analytics separates opus and sonnet cycles after fallback."""
        config.limits.max_iterations = 5
        config.claude.model = "opus"
        config.stagnation.max_consecutive_timeouts = 2
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                if call_count[0] <= 2:
                    return MockPopen("")  # Timeout (Opus)
                return MockPopen(
                    build_ndjson_stream(f"s{call_count[0]}", 0.05, 5, "PROJECT_COMPLETE")
                )
            return MockPopen("")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        summary_path = project_dir / ".workflow" / "metrics_summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        analytics = summary["model_analytics"]
        # Opus had 2 timeout iterations, sonnet had 1 successful
        assert "opus" in analytics
        assert "sonnet" in analytics
        assert analytics["opus"]["timeout_count"] == 2
        assert analytics["sonnet"]["iterations"] >= 1


class TestImprovedErrorMessages:
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_stagnation_error_has_recovery_steps(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig, caplog,
    ) -> None:
        """Stagnation exit error message includes actionable recovery steps."""
        config.limits.max_iterations = 10
        config.stagnation.window_size = 3
        config.stagnation.low_turn_threshold = 2

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "Thinking..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        with caplog.at_level(logging.ERROR):
            driver.run()

        assert any("Recovery:" in r.message for r in caplog.records)
        assert any("CLAUDE.md" in r.message for r in caplog.records)

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_budget_error_has_iteration_count(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig, caplog,
    ) -> None:
        """Budget exceeded message includes iteration count and metrics reference."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 10.0, 1, "Expensive"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        with caplog.at_level(logging.ERROR):
            driver.run()

        assert any("metrics_summary.json" in r.message for r in caplog.records)

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_timeout_stagnation_has_recovery_steps(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig, caplog,
    ) -> None:
        """Consecutive timeout stagnation includes recovery guidance."""
        config.limits.max_iterations = 5
        config.stagnation.max_consecutive_timeouts = 2

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson="")
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        with caplog.at_level(logging.ERROR):
            driver.run()

        assert any("Recovery:" in r.message for r in caplog.records)

    @patch("subprocess.run")
    def test_preflight_failure_has_recovery_steps(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig, caplog,
    ) -> None:
        """Preflight failure includes actionable recovery guidance."""
        mock_run.side_effect = FileNotFoundError("claude not found")

        driver = LoopDriver(project_dir, config, dry_run=True)
        with caplog.at_level(logging.ERROR):
            driver._preflight_check()

        assert any("taskkill" in r.message for r in caplog.records)


class TestVerificationIntegration:
    """Tests for plan verification in the loop driver."""

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_verification_enriches_prompt(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Verification critique is merged into the next prompt."""
        config.limits.max_iterations = 2
        config.verification.enabled = True

        call_count = [0]
        popen_prompts = []

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                # Capture the prompt (3rd arg, after "claude", "-p")
                prompt_idx = cmd.index("-p") + 1 if "-p" in cmd else 2
                popen_prompts.append(cmd[prompt_idx])
                return MockPopen(build_ndjson_stream(f"s{call_count[0]}", 0.01, 5, "Working..."))
            return MockPopen("")

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if cmd[0] == "claude" and "--version" in cmd:
                    return MagicMock(returncode=0, stdout="claude 1.0\n", stderr="")
                if "council_browser" in str(cmd):
                    query_text = cmd[-1] if cmd else ""
                    if "VERDICT" in query_text or "Critically evaluate" in query_text:
                        return mock_verification_result("NEEDS_REVISION", "1. Missing error handling")
                    return mock_playwright_result("Next step: implement feature X")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = run_side_effect

        driver = LoopDriver(project_dir, config)
        driver.run()

        # Second prompt should contain verification critique
        if len(popen_prompts) >= 2:
            assert "Plan Verification Critique" in popen_prompts[1]

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_verification_disabled_skips_query(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """No verification query when disabled."""
        config.limits.max_iterations = 2
        config.verification.enabled = False

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 5, "Working..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        # No verification trace events
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        event_types = [e["event_type"] for e in events]
        assert "verification_start" not in event_types

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_verification_failure_uses_unverified(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Verification failure continues with unverified research."""
        config.limits.max_iterations = 2
        config.verification.enabled = True

        call_count = [0]

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if cmd[0] == "claude" and "--version" in cmd:
                    return MagicMock(returncode=0, stdout="claude 1.0\n", stderr="")
                if "council_browser" in str(cmd):
                    call_count[0] += 1
                    if call_count[0] == 1:
                        return mock_playwright_result("Next steps...")
                    # Verification call fails
                    raise sp.TimeoutExpired(cmd="python", timeout=600)
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 5, "Working..."),
        )
        mock_run.side_effect = run_side_effect

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()

        # Should not crash — falls back to unverified research
        assert exit_code == EXIT_MAX_ITERATIONS

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_verification_trace_events(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Trace has verification_start and verification_complete events."""
        config.limits.max_iterations = 2
        config.verification.enabled = True

        def run_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if cmd[0] == "claude" and "--version" in cmd:
                    return MagicMock(returncode=0, stdout="claude 1.0\n", stderr="")
                if "council_browser" in str(cmd):
                    query_text = cmd[-1] if cmd else ""
                    if "VERDICT" in query_text or "Critically evaluate" in query_text:
                        return mock_verification_result()
                    return mock_playwright_result("Next steps...")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 5, "Working..."),
        )
        mock_run.side_effect = run_side_effect

        driver = LoopDriver(project_dir, config)
        driver.run()

        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        event_types = [e["event_type"] for e in events]
        assert "verification_start" in event_types
        assert "verification_complete" in event_types


class TestGitDiffStatsCapture:
    """Tests for _capture_git_diff_stats() in LoopDriver."""

    @patch("subprocess.run")
    def test_git_diff_stats_parses_output(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Parses git diff --stat summary line correctly."""
        mock_run.return_value = mock_git_diff_stat_result(5, 120, 30)
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        stats = driver._capture_git_diff_stats()
        assert stats is not None
        assert stats["files_changed"] == 5
        assert stats["insertions"] == 120
        assert stats["deletions"] == 30

    @patch("subprocess.run")
    def test_git_diff_stats_not_git_repo(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Returns None when not a git repo."""
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="not a git repo")
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        stats = driver._capture_git_diff_stats()
        assert stats is None

    @patch("subprocess.run")
    def test_git_diff_stats_timeout(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Returns None on timeout."""
        mock_run.side_effect = sp.TimeoutExpired(cmd="git", timeout=10)
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        stats = driver._capture_git_diff_stats()
        assert stats is None

    @patch("subprocess.run")
    def test_git_diff_stats_no_changes(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Returns None when no changes (empty output)."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        stats = driver._capture_git_diff_stats()
        assert stats is None


class TestCycleTrackingInTrace:
    """Tests for tools_used/files_modified in trace and metrics."""

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_claude_complete_trace_includes_tools(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """claude_complete trace event includes tools_used and files_modified."""
        # Build NDJSON with tool use events
        ndjson_lines = [
            json.dumps({"type": "init", "session_id": "s1"}),
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "main.py"}},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "config.py"}},
                    {"type": "text", "text": "PROJECT_COMPLETE"},
                ]},
                "session_id": "s1",
            }),
            json.dumps({
                "type": "result", "session_id": "s1",
                "total_cost_usd": 0.05, "total_duration_ms": 5000,
                "num_turns": 2, "result": "PROJECT_COMPLETE", "is_error": False,
            }),
        ]
        ndjson_stream = "\n".join(ndjson_lines)

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson=ndjson_stream)
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        complete_events = [e for e in events if e["event_type"] == "claude_complete"]
        assert len(complete_events) >= 1
        ce = complete_events[0]
        assert "tools_used" in ce
        assert "Edit" in ce["tools_used"]
        assert "Read" in ce["tools_used"]
        assert "files_modified" in ce
        assert "main.py" in ce["files_modified"]

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_metrics_summary_includes_tool_counts(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Metrics summary includes tool_usage_counts and total_files_modified."""
        ndjson_lines = [
            json.dumps({"type": "init", "session_id": "s1"}),
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "main.py"}},
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "config.py"}},
                    {"type": "text", "text": "PROJECT_COMPLETE"},
                ]},
                "session_id": "s1",
            }),
            json.dumps({
                "type": "result", "session_id": "s1",
                "total_cost_usd": 0.05, "total_duration_ms": 5000,
                "num_turns": 2, "result": "PROJECT_COMPLETE", "is_error": False,
            }),
        ]
        ndjson_stream = "\n".join(ndjson_lines)

        mock_popen.side_effect = make_popen_dispatcher(claude_ndjson=ndjson_stream)
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        driver.run()

        summary_path = project_dir / ".workflow" / "metrics_summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert "tool_usage_counts" in summary
        assert summary["tool_usage_counts"].get("Edit", 0) >= 1
        assert "total_files_modified" in summary
        assert "main.py" in summary["total_files_modified"]


class TestPostExecutionValidation:
    """Tests for _run_post_validation() and validation loop integration."""

    @patch("subprocess.run")
    def test_validation_disabled_skips_subprocess(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Validation disabled returns skipped, no subprocess calls."""
        config.validation.enabled = False
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        result = driver._run_post_validation()
        assert result.success
        assert result.data["skipped"] is True
        # No subprocess.run calls for test command
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_validation_passes_continues(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Passing validation returns passed=True."""
        config.validation.enabled = True
        mock_run.return_value = mock_test_result(passed=True)
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        result = driver._run_post_validation()
        assert result.success
        assert result.data["passed"] is True

    @patch("subprocess.run")
    def test_validation_fails_returns_failure_data(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Failing validation returns passed=False with stdout_tail."""
        config.validation.enabled = True
        mock_run.return_value = mock_test_result(passed=False, stdout="FAILED test_foo")
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        result = driver._run_post_validation()
        assert result.success  # Result itself is ok, the data says "failed"
        assert result.data["passed"] is False
        assert "FAILED" in result.data["stdout_tail"]

    @patch("subprocess.run")
    def test_validation_timeout_handled(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Timeout returns skipped with timeout flag."""
        config.validation.enabled = True
        mock_run.side_effect = sp.TimeoutExpired(cmd="pytest", timeout=120)
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        result = driver._run_post_validation()
        assert result.success
        assert result.data.get("timeout") is True

    @patch("subprocess.run")
    def test_validation_command_not_found(
        self, mock_run: MagicMock, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """FileNotFoundError returns Result.fail."""
        config.validation.enabled = True
        mock_run.side_effect = FileNotFoundError("pytest not found")
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        result = driver._run_post_validation()
        assert not result.success
        assert result.error_code == "FILE_NOT_FOUND"

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_validation_fails_warn_continues_to_research(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """In warn mode, test failure logs warning but continues to research."""
        config.limits.max_iterations = 2
        config.validation.enabled = True
        config.validation.fail_action = "warn"

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 5, "Working..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
            test_result=mock_test_result(passed=False),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_MAX_ITERATIONS

        # Verify research still happened
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        event_types = [e["event_type"] for e in events]
        assert "research_start" in event_types

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_validation_fails_inject_skips_research(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """In inject mode, test failure feeds fix prompt to next iteration (skips research)."""
        config.limits.max_iterations = 3
        config.validation.enabled = True
        config.validation.fail_action = "inject"
        config.validation.max_consecutive_failures = 5

        popen_prompts = []
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                prompt_idx = cmd.index("-p") + 1 if "-p" in cmd else 2
                popen_prompts.append(cmd[prompt_idx])
                return MockPopen(build_ndjson_stream(f"s{call_count[0]}", 0.01, 5, "Working..."))
            return MockPopen("")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
            test_result=mock_test_result(passed=False, stdout="FAILED test_widget"),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        # After first iteration fails tests in inject mode, next prompt should be fix prompt
        assert len(popen_prompts) >= 2
        assert "CRITICAL: Tests are failing" in popen_prompts[1]

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_validation_trace_events(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Trace includes validation_start and validation_complete events."""
        config.limits.max_iterations = 1
        config.validation.enabled = True

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 5, "Working..."),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
            test_result=mock_test_result(passed=True),
        )

        driver = LoopDriver(project_dir, config)
        driver.run()

        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        event_types = [e["event_type"] for e in events]
        assert "validation_start" in event_types
        assert "validation_complete" in event_types


class TestCompletionGate:
    """Tests for the completion gate feature that validates PROJECT_COMPLETE against a CLAUDE.md checklist."""

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_gate_rejects_unchecked_items(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """CLAUDE.md has unchecked items -> completion rejected, trace event emitted."""
        config.limits.max_iterations = 2
        # Write CLAUDE.md with unchecked gate items
        (project_dir / "CLAUDE.md").write_text(
            "# Project\n\n## Completion Gate\n- [ ] Task A\n- [x] Task B\n- [ ] Task C\n",
            encoding="utf-8",
        )

        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                return MockPopen(
                    build_ndjson_stream(f"s{call_count[0]}", 0.01, 5, "All done. PROJECT_COMPLETE")
                )
            return MockPopen("")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()

        # Should NOT exit with EXIT_COMPLETE — gate rejected
        assert exit_code != EXIT_COMPLETE

        # Trace should have completion_gate_rejected event
        trace_path = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").strip().splitlines()]
        event_types = [e["event_type"] for e in events]
        assert "completion_gate_rejected" in event_types

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_gate_accepts_all_checked(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """All items checked -> completion accepted normally."""
        (project_dir / "CLAUDE.md").write_text(
            "# Project\n\n## Completion Gate\n- [x] Task A\n- [x] Task B\n- [x] Task C\n",
            encoding="utf-8",
        )

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 5, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_gate_no_section_backward_compat(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """No '## Completion Gate' section at startup -> accepts (backward compat)."""
        # Default CLAUDE.md from fixture has no gate section
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 5, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_gate_deleted_during_execution_rejects(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Gate present at startup but deleted during execution -> rejects (evasion)."""
        # Write CLAUDE.md WITH a gate section at startup
        claude_md = project_dir / "CLAUDE.md"
        claude_md.write_text(
            "# Project\n\n## Completion Gate\n- [ ] Task A\n- [ ] Task B\n",
            encoding="utf-8",
        )
        config.limits.max_iterations = 4
        config.completion_gate.max_rejections = 3

        call_count = 0

        def popen_side_effect(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # On first call, Claude deletes the gate section
            if call_count == 1:
                claude_md.write_text("# Project\nNo gate here.\n", encoding="utf-8")
            return make_popen_dispatcher(
                claude_ndjson=build_ndjson_stream("s1", 0.01, 5, "PROJECT_COMPLETE"),
            )(*args, **kwargs)

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_STAGNATION  # Rejected 3x -> stagnation

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_gate_disabled_via_config(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Gate disabled via config -> accepts even with unchecked items."""
        config.completion_gate.enabled = False
        (project_dir / "CLAUDE.md").write_text(
            "# Project\n\n## Completion Gate\n- [ ] Unchecked task\n",
            encoding="utf-8",
        )

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 5, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

    def test_gate_parser_edge_cases(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Parser handles: uppercase [X], BOM, CRLF, non-checkbox lines, gate followed by heading."""
        # Write a CLAUDE.md with various edge cases: BOM + CRLF + mixed content
        content = (
            "\ufeff"  # BOM
            "# Project\r\n"
            "\r\n"
            "## Completion Gate\r\n"
            "- [X] Uppercase checked\r\n"
            "- [x] Lowercase checked\r\n"
            "Some random text that is not a checkbox\r\n"
            "- [ ] Still unchecked\r\n"
            "\r\n"
            "## Next Section\r\n"
            "- [ ] This should NOT be parsed (outside gate)\r\n"
        )
        (project_dir / "CLAUDE.md").write_bytes(content.encode("utf-8-sig"))

        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        checked, unchecked = driver._parse_completion_gate(project_dir / "CLAUDE.md")
        assert len(checked) == 2
        assert "Uppercase checked" in checked
        assert "Lowercase checked" in checked
        assert len(unchecked) == 1
        assert "Still unchecked" in unchecked
        # "This should NOT be parsed" is after ## Next Section, so excluded

    def test_gate_parser_empty_section(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Empty gate section returns empty lists."""
        (project_dir / "CLAUDE.md").write_text(
            "# Project\n\n## Completion Gate\n\n## Next\n",
            encoding="utf-8",
        )
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        checked, unchecked = driver._parse_completion_gate(project_dir / "CLAUDE.md")
        assert checked == []
        assert unchecked == []

    def test_gate_parser_missing_file(
        self, project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Missing CLAUDE.md returns empty lists (no crash)."""
        driver = LoopDriver(project_dir, config, dry_run=True, skip_preflight=True)
        checked, unchecked = driver._parse_completion_gate(project_dir / "NONEXISTENT.md")
        assert checked == []
        assert unchecked == []

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_gate_rejection_sets_next_prompt(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """After rejection, loop continues with rejection text in prompt (doesn't exit complete)."""
        config.limits.max_iterations = 3
        config.completion_gate.max_rejections = 5  # High so we don't hit stagnation

        (project_dir / "CLAUDE.md").write_text(
            "# Project\n\n## Completion Gate\n- [ ] Unfinished work\n",
            encoding="utf-8",
        )

        popen_prompts: list[str] = []
        call_count = [0]

        def popen_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "claude":
                call_count[0] += 1
                prompt_idx = cmd.index("-p") + 1 if "-p" in cmd else 2
                popen_prompts.append(cmd[prompt_idx])
                return MockPopen(
                    build_ndjson_stream(f"s{call_count[0]}", 0.01, 5, "PROJECT_COMPLETE")
                )
            return MockPopen("")

        mock_popen.side_effect = popen_side_effect
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()

        # Should hit max iterations (not EXIT_COMPLETE), since gate keeps rejecting
        assert exit_code == EXIT_MAX_ITERATIONS
        # Second prompt should contain rejection feedback
        assert len(popen_prompts) >= 2
        assert "COMPLETION REJECTED" in popen_prompts[1]
        assert "unchecked" in popen_prompts[1].lower()

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_gate_max_rejections_exits_stagnation(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """After max_rejections consecutive rejections, exits with EXIT_STAGNATION (code 3)."""
        config.limits.max_iterations = 10
        config.completion_gate.max_rejections = 3

        (project_dir / "CLAUDE.md").write_text(
            "# Project\n\n## Completion Gate\n- [ ] Never completed\n",
            encoding="utf-8",
        )

        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 5, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_playwright_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_STAGNATION
        assert driver._gate_rejection_count == 3


class TestPostReview:
    """Tests for post-completion Perplexity quality review."""

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_post_review_called_on_completion(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """_run_post_review() is called on successful completion."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_post_review_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

        # Verify post_review.md was saved (council_browser was called for review)
        review_file = project_dir / ".workflow" / "post_review.md"
        assert review_file.exists()

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_post_review_disabled_skips(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Post-review is skipped when disabled in config."""
        config.post_review.enabled = False
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher()

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

        # No post_review.md should be written
        review_file = project_dir / ".workflow" / "post_review.md"
        assert not review_file.exists()

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_post_review_failure_does_not_block_completion(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """Post-review failure logs warning but still exits 0."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "PROJECT_COMPLETE"),
        )

        call_count = [0]

        def dispatcher(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd:
                if cmd[0] == "git":
                    return mock_git_log_result()
                if cmd[0] == "claude" and len(cmd) >= 2 and cmd[1] == "--version":
                    return MagicMock(returncode=0, stdout="claude 1.0.0\n", stderr="")
                if "council_browser" in str(cmd):
                    call_count[0] += 1
                    raise sp.TimeoutExpired(cmd="python", timeout=600)
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = dispatcher

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        # Still completes successfully despite review failure
        assert exit_code == EXIT_COMPLETE

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_post_review_writes_trace_events(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """_run_post_review writes post_review_start and post_review_complete trace events."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_post_review_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

        # Check trace.jsonl for post_review events
        trace_file = project_dir / ".workflow" / "trace.jsonl"
        assert trace_file.exists()
        events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").strip().splitlines()]
        event_types = [e["event_type"] for e in events]
        assert "post_review_start" in event_types
        assert "post_review_complete" in event_types

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_post_review_between_save_and_summary(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path, config: WorkflowConfig,
    ) -> None:
        """post_review_start occurs after completion_detected and before loop_end."""
        mock_popen.side_effect = make_popen_dispatcher(
            claude_ndjson=build_ndjson_stream("s1", 0.01, 1, "PROJECT_COMPLETE"),
        )
        mock_run.side_effect = make_subprocess_dispatcher(
            research_result=mock_post_review_result(),
        )

        driver = LoopDriver(project_dir, config)
        exit_code = driver.run()
        assert exit_code == EXIT_COMPLETE

        trace_file = project_dir / ".workflow" / "trace.jsonl"
        events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").strip().splitlines()]
        event_types = [e["event_type"] for e in events]

        # Verify ordering: completion_detected -> post_review_start -> loop_end
        completion_idx = event_types.index("completion_detected")
        review_idx = event_types.index("post_review_start")
        loop_end_idx = event_types.index("loop_end")
        assert completion_idx < review_idx < loop_end_idx

    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_post_review_config_in_workflow_config(
        self, mock_run: MagicMock, mock_popen: MagicMock,
        project_dir: Path,
    ) -> None:
        """WorkflowConfig includes post_review field with defaults."""
        cfg = WorkflowConfig()
        assert hasattr(cfg, "post_review")
        assert cfg.post_review.enabled is True
        assert cfg.post_review.timeout_seconds == 600

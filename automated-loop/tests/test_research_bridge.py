"""Tests for research_bridge module."""

import json
import subprocess as sp
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config import ExplorationConfig, PostReviewConfig, RetryConfig, VerificationConfig
from research_bridge import ResearchBridge, SessionContext, VERIFICATION_PROMPT, POST_REVIEW_PROMPT

from helpers import (
    mock_git_log_result,
    mock_playwright_error,
    mock_playwright_result,
    mock_post_review_result,
    mock_verification_result,
    make_research_dispatcher,
)


@pytest.fixture
def research_project_dir(tmp_path: Path) -> Path:
    """Create a project with specific state for research bridge tests."""
    workflow_dir = tmp_path / ".workflow"
    workflow_dir.mkdir()

    (tmp_path / "CLAUDE.md").write_text(
        "# Test Project\nA simple test project.", encoding="utf-8"
    )

    state = {
        "session_id": "test-session",
        "iteration": 3,
        "status": "running",
        "metrics": {"total_cost_usd": 0.15, "total_turns": 10},
    }
    (workflow_dir / "state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )

    return tmp_path


@pytest.fixture
def bare_project_dir(tmp_path: Path) -> Path:
    """Create a bare project with no files."""
    workflow_dir = tmp_path / ".workflow"
    workflow_dir.mkdir()
    return tmp_path


class TestSessionContext:
    def test_gather_with_claude_md(self, research_project_dir: Path) -> None:
        ctx = SessionContext(research_project_dir)
        result = ctx.gather()

        assert "claude_md" in result
        assert "Test Project" in result["claude_md"]

    def test_gather_with_workflow_state(self, research_project_dir: Path) -> None:
        ctx = SessionContext(research_project_dir)
        result = ctx.gather()

        assert "workflow_state" in result
        state = json.loads(result["workflow_state"])
        assert state["iteration"] == 3
        assert state["status"] == "running"

    def test_gather_without_claude_md(self, bare_project_dir: Path) -> None:
        ctx = SessionContext(bare_project_dir)
        result = ctx.gather()

        assert "claude_md" not in result

    def test_gather_with_memory_md(self, research_project_dir: Path) -> None:
        memory = research_project_dir / "MEMORY.md"
        memory.write_text("# Key learnings\n- Thing 1", encoding="utf-8")

        ctx = SessionContext(research_project_dir)
        result = ctx.gather()

        assert "memory_md" in result
        assert "Key learnings" in result["memory_md"]

    def test_gather_with_research_result(self, research_project_dir: Path) -> None:
        research = research_project_dir / ".workflow" / "research_result.md"
        research.write_text("# Previous Result\nDo X next.", encoding="utf-8")

        ctx = SessionContext(research_project_dir)
        result = ctx.gather()

        assert "last_research" in result
        assert "Do X next" in result["last_research"]

    def test_git_log_not_in_non_repo(self, bare_project_dir: Path) -> None:
        ctx = SessionContext(bare_project_dir)
        result = ctx.gather()

        # Not a git repo, so no git_log
        assert "git_log" not in result


class TestResearchBridge:
    def test_build_query_includes_context(self, research_project_dir: Path) -> None:
        bridge = ResearchBridge(research_project_dir)
        query = bridge.build_query()

        assert "Test Project" in query
        assert "Workflow State" in query
        assert "next steps" in query.lower()

    def test_build_query_with_extra_context(self, research_project_dir: Path) -> None:
        bridge = ResearchBridge(research_project_dir)
        query = bridge.build_query(extra_context="Focus on performance optimization")

        assert "performance optimization" in query.lower()

    @patch("research_bridge.subprocess.run")
    def test_successful_query(self, mock_run: MagicMock, research_project_dir: Path) -> None:
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=mock_playwright_result("Next steps: 1. Implement caching 2. Add tests")
        )

        bridge = ResearchBridge(research_project_dir)
        result = bridge.query()

        assert result.success
        assert result.data is not None
        assert "Implement caching" in result.data.response

        # Verify result was saved
        research_file = research_project_dir / ".workflow" / "research_result.md"
        assert research_file.exists()
        content = research_file.read_text(encoding="utf-8")
        assert "Implement caching" in content

    @patch("research_bridge.subprocess.run")
    def test_playwright_timeout(self, mock_run: MagicMock, research_project_dir: Path) -> None:
        mock_run.side_effect = make_research_dispatcher(
            playwright_side_effect=sp.TimeoutExpired(cmd="python", timeout=600)
        )

        bridge = ResearchBridge(research_project_dir)
        result = bridge.query()

        assert not result.success
        assert result.error_code == "TIMEOUT"

    @patch("research_bridge.subprocess.run")
    def test_playwright_error_response(self, mock_run: MagicMock, research_project_dir: Path) -> None:
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=mock_playwright_error("Browser session expired")
        )

        bridge = ResearchBridge(research_project_dir)
        result = bridge.query()

        assert not result.success
        assert result.error_code == "PLAYWRIGHT_ERROR"
        assert "Browser session expired" in result.error

    @patch("research_bridge.subprocess.run")
    def test_subprocess_crash(self, mock_run: MagicMock, research_project_dir: Path) -> None:
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=MagicMock(returncode=1, stdout="", stderr="Traceback...")
        )

        bridge = ResearchBridge(research_project_dir)
        result = bridge.query()

        assert not result.success
        assert result.error_code == "PLAYWRIGHT_ERROR"

    @patch("research_bridge.subprocess.run")
    def test_invalid_json_response(self, mock_run: MagicMock, research_project_dir: Path) -> None:
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=MagicMock(returncode=0, stdout="not json {{{", stderr="")
        )

        bridge = ResearchBridge(research_project_dir)
        result = bridge.query()

        assert not result.success
        assert result.error_code == "PARSE_ERROR"

    @patch("research_bridge.subprocess.run")
    def test_empty_synthesis_response(self, mock_run: MagicMock, research_project_dir: Path) -> None:
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=MagicMock(
                returncode=0,
                stdout=json.dumps({"synthesis": "", "execution_time_ms": 1000}),
                stderr="",
            )
        )

        bridge = ResearchBridge(research_project_dir)
        result = bridge.query()

        assert not result.success
        assert result.error_code == "PARSE_ERROR"

    @patch.dict("os.environ", {}, clear=True)
    def test_youcom_requires_api_key(self, research_project_dir: Path) -> None:
        bridge = ResearchBridge(research_project_dir, provider="youcom")

        result = bridge.query()

        assert not result.success
        assert result.error_code == "MISSING_API_KEY"

    @patch.dict("os.environ", {"YDC_API_KEY": "test-key"}, clear=True)
    @patch("research_bridge.urllib.request.urlopen")
    def test_successful_youcom_query(
        self, mock_urlopen: MagicMock, research_project_dir: Path,
    ) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps({
            "output": {
                "content": "Next steps: use You.com research [[1]]",
                "content_type": "text",
                "sources": [
                    {
                        "title": "You.com Research API docs",
                        "url": "https://docs.you.com/",
                    }
                ],
            }
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = response

        bridge = ResearchBridge(research_project_dir, provider="youcom")
        result = bridge.query()

        assert result.success
        assert result.data is not None
        assert "You.com research" in result.data.response
        assert "## Sources" in result.data.response
        assert "You.com Research API docs" in result.data.response
        assert "https://docs.you.com/" in result.data.response
        assert result.data.model == "youcom-research-standard"


class TestRetryAndCircuitBreaker:
    """Tests for retry, backoff, and circuit breaker logic."""

    @pytest.fixture
    def fast_retry_config(self) -> RetryConfig:
        """Retry config with near-zero delays for fast tests."""
        return RetryConfig(
            max_retries=3,
            base_delay_seconds=0.001,
            max_delay_seconds=0.01,
            circuit_breaker_threshold=3,
            circuit_breaker_reset_seconds=0.1,
        )

    @patch("research_bridge.subprocess.run")
    def test_retry_exhaustion_returns_last_error(
        self, mock_run: MagicMock, research_project_dir: Path, fast_retry_config: RetryConfig
    ) -> None:
        """After max_retries, returns the last error result."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_side_effect=sp.TimeoutExpired(cmd="python", timeout=600)
        )

        bridge = ResearchBridge(
            research_project_dir, retry_config=fast_retry_config
        )
        result = bridge.query()

        assert not result.success
        assert result.error_code == "TIMEOUT"

    @patch("research_bridge.time.sleep")
    @patch("research_bridge.subprocess.run")
    def test_backoff_delay_increases(
        self, mock_run: MagicMock, mock_sleep: MagicMock, research_project_dir: Path
    ) -> None:
        """Backoff delays increase with each attempt."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_side_effect=sp.TimeoutExpired(cmd="python", timeout=600)
        )

        config = RetryConfig(
            max_retries=3, base_delay_seconds=1.0, max_delay_seconds=30.0,
            circuit_breaker_threshold=10,
        )
        bridge = ResearchBridge(
            research_project_dir, retry_config=config
        )
        bridge.query()

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert len(delays) == 3
        assert delays[0] >= 0.5
        assert delays[1] >= 1.0
        assert delays[2] >= 2.0

    @patch("research_bridge.subprocess.run")
    def test_circuit_breaker_trips_after_threshold(
        self, mock_run: MagicMock, research_project_dir: Path, fast_retry_config: RetryConfig
    ) -> None:
        """Circuit breaker opens after threshold consecutive failures."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_side_effect=sp.TimeoutExpired(cmd="python", timeout=600)
        )

        bridge = ResearchBridge(
            research_project_dir, retry_config=fast_retry_config
        )
        bridge.query()

        result = bridge.query()
        assert not result.success
        assert result.error_code == "CIRCUIT_OPEN"

    @patch("research_bridge.subprocess.run")
    def test_circuit_breaker_resets_after_cooldown(
        self, mock_run: MagicMock, research_project_dir: Path, fast_retry_config: RetryConfig
    ) -> None:
        """Circuit breaker resets after cooldown period."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_side_effect=sp.TimeoutExpired(cmd="python", timeout=600)
        )

        bridge = ResearchBridge(
            research_project_dir, retry_config=fast_retry_config
        )
        bridge.query()  # Trip the breaker

        time.sleep(0.15)

        result = bridge.query()
        assert result.error_code != "CIRCUIT_OPEN"

    @patch("research_bridge.subprocess.run")
    def test_playwright_error_retries(
        self, mock_run: MagicMock, research_project_dir: Path, fast_retry_config: RetryConfig
    ) -> None:
        """Playwright errors (retryable) trigger retries."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=mock_playwright_error("Browser timeout")
        )

        bridge = ResearchBridge(
            research_project_dir, retry_config=fast_retry_config
        )
        result = bridge.query()

        assert not result.success
        assert result.error_code == "PLAYWRIGHT_ERROR"

    @patch("research_bridge.subprocess.run")
    def test_non_retryable_fails_immediately(
        self, mock_run: MagicMock, research_project_dir: Path, fast_retry_config: RetryConfig
    ) -> None:
        """Non-retryable errors (SCRIPT_NOT_FOUND) fail immediately without retry."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_side_effect=FileNotFoundError("council_browser.py not found")
        )

        bridge = ResearchBridge(
            research_project_dir, retry_config=fast_retry_config
        )
        result = bridge.query()

        assert not result.success
        assert result.error_code == "SCRIPT_NOT_FOUND"
        council_calls = [
            c for c in mock_run.call_args_list
            if c[0] and isinstance(c[0][0], list) and c[0][0][0] != "git"
        ]
        assert len(council_calls) == 1

    @patch("research_bridge.time.sleep")
    @patch("research_bridge.subprocess.run")
    def test_jitter_present_in_delays(
        self, mock_run: MagicMock, mock_sleep: MagicMock, research_project_dir: Path
    ) -> None:
        """Delays include jitter (not exact powers of 2)."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_side_effect=sp.TimeoutExpired(cmd="python", timeout=600)
        )

        config = RetryConfig(
            max_retries=2, base_delay_seconds=1.0, max_delay_seconds=30.0,
            circuit_breaker_threshold=10,
        )
        bridge = ResearchBridge(
            research_project_dir, retry_config=config
        )
        bridge.query()

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert len(delays) == 2
        assert delays[0] != 1.0
        assert delays[1] != 2.0

    @patch("research_bridge.subprocess.run")
    def test_success_resets_circuit_breaker(
        self, mock_run: MagicMock, research_project_dir: Path, fast_retry_config: RetryConfig
    ) -> None:
        """A successful query resets the circuit breaker failure count."""
        call_count = [0]

        def smart_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return mock_git_log_result()
            call_count[0] += 1
            if call_count[0] <= 2:
                raise sp.TimeoutExpired(cmd="python", timeout=600)
            return mock_playwright_result("Next steps...")

        mock_run.side_effect = smart_side_effect

        bridge = ResearchBridge(
            research_project_dir, retry_config=RetryConfig(
                max_retries=0, base_delay_seconds=0.001, max_delay_seconds=0.01,
                circuit_breaker_threshold=5, circuit_breaker_reset_seconds=0.1,
            )
        )
        bridge.query()  # Failure 1
        bridge.query()  # Failure 2
        assert bridge._consecutive_failures == 2

        result = bridge.query()
        assert result.success
        assert bridge._consecutive_failures == 0

    @patch("research_bridge.subprocess.run")
    def test_circuit_breaker_error_message_has_recovery_steps(
        self, mock_run: MagicMock, research_project_dir: Path, fast_retry_config: RetryConfig
    ) -> None:
        """Circuit breaker error message includes actionable recovery guidance."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_side_effect=sp.TimeoutExpired(cmd="python", timeout=600)
        )

        bridge = ResearchBridge(
            research_project_dir, retry_config=fast_retry_config
        )
        bridge.query()  # Trip the breaker

        result = bridge.query()
        assert result.error_code == "CIRCUIT_OPEN"
        assert "playwright-session.json" in result.error
        assert "council_browser.py" in result.error


class TestExploration:
    """Tests for codebase exploration before research queries."""

    def test_explore_codebase_reads_files(self, research_project_dir: Path) -> None:
        """explore_codebase reads project files and returns content."""
        (research_project_dir / "main.py").write_text(
            "def main():\n    print('hello')\n", encoding="utf-8"
        )
        ctx = SessionContext(research_project_dir)
        result = ctx.explore_codebase(max_files=10, max_chars=3000)
        # Should find at least the CLAUDE.md or main.py via glob fallback
        assert len(result) >= 1

    def test_explore_codebase_respects_max_files(self, research_project_dir: Path) -> None:
        """Only reads up to max_files."""
        for i in range(20):
            (research_project_dir / f"file_{i}.py").write_text(
                f"# file {i}\n", encoding="utf-8"
            )
        ctx = SessionContext(research_project_dir)
        result = ctx.explore_codebase(max_files=5, max_chars=3000)
        assert len(result) <= 5

    def test_explore_codebase_truncates_content(self, research_project_dir: Path) -> None:
        """Large files get truncated to max_chars."""
        (research_project_dir / "big.py").write_text("x" * 10000, encoding="utf-8")
        ctx = SessionContext(research_project_dir)
        result = ctx.explore_codebase(max_files=10, max_chars=500)
        for content in result.values():
            assert len(content) <= 500

    def test_explore_codebase_empty_project(self, bare_project_dir: Path) -> None:
        """Bare project returns empty dict (no source files)."""
        ctx = SessionContext(bare_project_dir)
        result = ctx.explore_codebase(max_files=10, max_chars=3000)
        assert isinstance(result, dict)

    @patch("research_bridge.subprocess.run")
    def test_build_query_includes_codebase_context(
        self, mock_run: MagicMock, research_project_dir: Path
    ) -> None:
        """build_query includes codebase context when provided."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=mock_playwright_result()
        )
        bridge = ResearchBridge(research_project_dir)
        codebase = {"src/main.py": "def main(): pass"}
        query = bridge.build_query(codebase_context=codebase)
        assert "Key Codebase Files" in query
        assert "src/main.py" in query
        assert "def main(): pass" in query

    @patch("research_bridge.subprocess.run")
    def test_query_with_exploration_disabled(
        self, mock_run: MagicMock, research_project_dir: Path
    ) -> None:
        """No codebase context when exploration is disabled."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=mock_playwright_result("Next steps...")
        )
        bridge = ResearchBridge(
            research_project_dir,
            exploration_config=ExplorationConfig(enabled=False),
        )
        result = bridge.query()
        assert result.success
        assert bridge.last_codebase_context is None


class TestVerification:
    """Tests for plan verification."""

    @patch("research_bridge.subprocess.run")
    def test_verify_plan_success(
        self, mock_run: MagicMock, research_project_dir: Path
    ) -> None:
        """verify_plan returns critique text on success."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=mock_verification_result("APPROVED")
        )
        bridge = ResearchBridge(research_project_dir)
        result = bridge.verify_plan(
            plan_text="Phase 1: Add feature",
            original_research="Add feature X",
        )
        assert result.success
        assert result.data is not None
        assert "APPROVED" in result.data.response

    @patch("research_bridge.subprocess.run")
    def test_verify_plan_timeout(
        self, mock_run: MagicMock, research_project_dir: Path
    ) -> None:
        """Verification timeout returns error."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_side_effect=sp.TimeoutExpired(cmd="python", timeout=600)
        )
        bridge = ResearchBridge(research_project_dir)
        result = bridge.verify_plan(
            plan_text="Phase 1: Add feature",
            original_research="Add feature X",
        )
        assert not result.success
        assert result.error_code == "TIMEOUT"

    @patch("research_bridge.subprocess.run")
    def test_verify_plan_includes_plan_and_research(
        self, mock_run: MagicMock, research_project_dir: Path
    ) -> None:
        """Verification query contains both plan text and research text."""
        captured_cmd = []

        def capture_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return mock_git_log_result()
            captured_cmd.append(cmd)
            return mock_verification_result()

        mock_run.side_effect = capture_side_effect
        bridge = ResearchBridge(research_project_dir)
        bridge.verify_plan(
            plan_text="Phase 1: Implement caching",
            original_research="Research: caching needed",
        )
        # The verification query (last arg of cmd) should contain both
        assert len(captured_cmd) >= 1
        query_text = captured_cmd[0][-1]  # Last arg is the query text
        assert "Implement caching" in query_text
        assert "caching needed" in query_text

    def test_verify_plan_uses_verification_template(self) -> None:
        """VERIFICATION_PROMPT contains expected structure."""
        assert "VERDICT" in VERIFICATION_PROMPT
        assert "LOGICAL ERRORS" in VERIFICATION_PROMPT
        assert "SCOPE CREEP" in VERIFICATION_PROMPT
        assert "FEASIBILITY" in VERIFICATION_PROMPT


class TestPostReview:
    """Tests for post-completion quality review."""

    def test_post_review_prompt_has_expected_sections(self) -> None:
        """POST_REVIEW_PROMPT contains all quality audit dimensions."""
        assert "COMPLETENESS" in POST_REVIEW_PROMPT
        assert "EDGE CASES" in POST_REVIEW_PROMPT
        assert "TEST COVERAGE" in POST_REVIEW_PROMPT
        assert "CODE QUALITY" in POST_REVIEW_PROMPT
        assert "REGRESSIONS" in POST_REVIEW_PROMPT
        assert "DOCUMENTATION" in POST_REVIEW_PROMPT
        assert "VERDICT" in POST_REVIEW_PROMPT

    @patch("research_bridge.subprocess.run")
    def test_post_review_success(
        self, mock_run: MagicMock, research_project_dir: Path
    ) -> None:
        """post_review returns result on success and saves file."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=mock_post_review_result("PASS", "All good"),
        )
        bridge = ResearchBridge(research_project_dir)
        result = bridge.post_review()

        assert result.success
        assert result.data is not None
        assert "PASS" in result.data.response

        # Verify saved to post_review.md
        review_file = research_project_dir / ".workflow" / "post_review.md"
        assert review_file.exists()
        content = review_file.read_text(encoding="utf-8")
        assert "Post-Completion Quality Review" in content
        assert "PASS" in content

    @patch("research_bridge.subprocess.run")
    def test_post_review_timeout(
        self, mock_run: MagicMock, research_project_dir: Path
    ) -> None:
        """post_review timeout returns error."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_side_effect=sp.TimeoutExpired(cmd="python", timeout=600)
        )
        bridge = ResearchBridge(research_project_dir)
        result = bridge.post_review()

        assert not result.success
        assert result.error_code == "TIMEOUT"

    @patch("research_bridge.subprocess.run")
    def test_post_review_subprocess_failure(
        self, mock_run: MagicMock, research_project_dir: Path
    ) -> None:
        """post_review subprocess crash returns error."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=MagicMock(returncode=1, stdout="", stderr="crash")
        )
        bridge = ResearchBridge(research_project_dir)
        result = bridge.post_review()

        assert not result.success
        assert result.error_code == "PLAYWRIGHT_ERROR"

    @patch("research_bridge.subprocess.run")
    def test_post_review_save_disabled(
        self, mock_run: MagicMock, research_project_dir: Path
    ) -> None:
        """post_review with save_result=False does not write file."""
        mock_run.side_effect = make_research_dispatcher(
            playwright_result=mock_post_review_result(),
        )
        bridge = ResearchBridge(research_project_dir)
        result = bridge.post_review(save_result=False)

        assert result.success
        review_file = research_project_dir / ".workflow" / "post_review.md"
        assert not review_file.exists()

    @patch("research_bridge.subprocess.run")
    def test_post_review_includes_context_in_query(
        self, mock_run: MagicMock, research_project_dir: Path
    ) -> None:
        """post_review query includes project context and codebase."""
        captured_cmd = []

        def capture_side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, list) and cmd and cmd[0] == "git":
                return mock_git_log_result()
            captured_cmd.append(cmd)
            return mock_post_review_result()

        mock_run.side_effect = capture_side_effect
        bridge = ResearchBridge(research_project_dir)
        bridge.post_review(focus_area="Check edge cases")

        assert len(captured_cmd) >= 1
        query_text = captured_cmd[0][-1]
        assert "Check edge cases" in query_text
        assert "COMPLETENESS" in query_text

    def test_post_review_config_defaults(self) -> None:
        """PostReviewConfig loads with sensible defaults."""
        cfg = PostReviewConfig()
        assert cfg.enabled is True
        assert cfg.timeout_seconds == 600
        assert cfg.save_result is True
        assert "completeness" in cfg.focus_area.lower()

    def test_post_review_config_custom_values(self) -> None:
        """PostReviewConfig accepts custom values."""
        cfg = PostReviewConfig(
            enabled=False,
            focus_area="Focus on security",
            timeout_seconds=120,
            save_result=False,
        )
        assert cfg.enabled is False
        assert cfg.focus_area == "Focus on security"
        assert cfg.timeout_seconds == 120
        assert cfg.save_result is False

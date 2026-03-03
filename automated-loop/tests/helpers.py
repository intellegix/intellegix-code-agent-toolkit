"""Shared test helpers for the automated Claude loop test suite.

Fixtures are in conftest.py. This module contains non-fixture helpers
(mock builders, NDJSON stream builders) used across multiple test files.
"""

import io
import json
from unittest.mock import MagicMock


# --- NDJSON stream builders ---

def build_ndjson_stream(
    session_id: str,
    cost: float,
    turns: int,
    result_text: str,
    is_error: bool = False,
    duration_ms: int = 10000,
) -> str:
    """Build a realistic NDJSON stream string matching Claude CLI output format."""
    lines = [
        json.dumps({"type": "init", "session_id": session_id}),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": result_text}],
            },
            "session_id": session_id,
        }),
        json.dumps({
            "type": "result",
            "session_id": session_id,
            "total_cost_usd": cost,
            "total_duration_ms": duration_ms,
            "num_turns": turns,
            "result": result_text,
            "is_error": is_error,
        }),
    ]
    return "\n".join(lines)


# --- Subprocess mock helpers ---

def mock_playwright_result(synthesis: str = "Keep going") -> MagicMock:
    """Build a mock subprocess result for Playwright research."""
    return MagicMock(
        returncode=0,
        stdout=json.dumps({
            "synthesis": synthesis,
            "models": ["perplexity-research"],
            "citations": [],
            "execution_time_ms": 30000,
        }),
        stderr="",
    )


def mock_verification_result(
    verdict: str = "APPROVED", issues: str = "None"
) -> MagicMock:
    """Build a mock subprocess result for plan verification."""
    synthesis = (
        f"VERDICT: {verdict}\n"
        f"ISSUES: {issues}\n"
        f"SUGGESTIONS: None\n"
        f"RISK_ASSESSMENT: Low risk overall"
    )
    return MagicMock(
        returncode=0,
        stdout=json.dumps({
            "synthesis": synthesis,
            "models": ["perplexity-research-verification"],
            "citations": [],
            "execution_time_ms": 20000,
        }),
        stderr="",
    )


def mock_post_review_result(
    verdict: str = "PASS", assessment: str = "All implementations look good"
) -> MagicMock:
    """Build a mock subprocess result for post-completion review."""
    synthesis = (
        f"VERDICT: {verdict}\n"
        f"OVERALL_ASSESSMENT: {assessment}\n"
        f"PRIORITY_FIXES: None"
    )
    return MagicMock(
        returncode=0,
        stdout=json.dumps({
            "synthesis": synthesis,
            "models": ["perplexity-research-post-review"],
            "citations": [],
            "execution_time_ms": 25000,
        }),
        stderr="",
    )


def mock_playwright_error(error: str = "Browser timeout") -> MagicMock:
    """Build a mock subprocess result for Playwright error."""
    return MagicMock(
        returncode=0,
        stdout=json.dumps({
            "error": error,
            "synthesis": "",
            "execution_time_ms": 0,
        }),
        stderr="",
    )


def mock_git_log_result() -> MagicMock:
    """Mock result for git log (not a git repo)."""
    return MagicMock(returncode=128, stdout="", stderr="not a git repo")


def mock_git_diff_stat_result(
    files_changed: int = 3, insertions: int = 50, deletions: int = 10,
) -> MagicMock:
    """Build a mock subprocess result for git diff --stat."""
    summary = f" {files_changed} files changed, {insertions} insertions(+), {deletions} deletions(-)"
    return MagicMock(returncode=0, stdout=f" file1.py | 10 +\n file2.py | 5 -\n{summary}\n", stderr="")


def mock_test_result(passed: bool = True, stdout: str = "") -> MagicMock:
    """Build a mock subprocess result for test command."""
    return MagicMock(
        returncode=0 if passed else 1,
        stdout=stdout or ("5 passed" if passed else "2 failed, 3 passed"),
        stderr="",
    )


def make_subprocess_dispatcher(
    claude_result=None,
    claude_side_effect=None,
    research_result=None,
    research_side_effect=None,
    git_diff_result=None,
    test_result=None,
):
    """Create a subprocess.run mock that dispatches based on command.

    - git diff commands -> git_diff_result or mock_git_log_result()
    - git commands -> mock_git_log_result()
    - claude commands -> claude_result or raise claude_side_effect
    - council_browser commands -> research_result or raise research_side_effect
    - pytest/test commands -> test_result or default pass
    """
    def side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and cmd:
            if cmd[0] == "git":
                # Handle git diff --stat specifically
                if len(cmd) >= 2 and cmd[1] == "diff" and git_diff_result is not None:
                    return git_diff_result
                # Handle preflight check (claude --version)
                return mock_git_log_result()
            if cmd[0] == "claude":
                if len(cmd) >= 2 and cmd[1] == "--version":
                    return MagicMock(returncode=0, stdout="claude 1.0.0-test\n", stderr="")
                if claude_side_effect is not None:
                    raise claude_side_effect
                return claude_result
            if "council_browser" in str(cmd):
                if research_side_effect is not None:
                    raise research_side_effect
                return research_result
            if len(cmd) > 1 and "claude" in str(cmd[1]):
                if claude_side_effect is not None:
                    raise claude_side_effect
                return claude_result
            # Test runner commands (pytest, python -m pytest, etc.)
            if cmd[0] in ("pytest", "python") and test_result is not None:
                return test_result
        # Default fallback
        if claude_result is not None:
            return claude_result
        return MagicMock(returncode=0, stdout="", stderr="")

    return side_effect


def make_research_dispatcher(
    playwright_result=None,
    playwright_side_effect=None,
    verification_result=None,
    verification_side_effect=None,
):
    """Create a subprocess.run mock for research bridge tests.

    Git log calls get a no-op result. Council browser calls get playwright_result
    or raise playwright_side_effect. If verification_result is provided, second
    council_browser call (verification) returns it instead.
    """
    call_count = [0]

    def side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            return mock_git_log_result()
        # Council browser call
        call_count[0] += 1
        # If verification_result provided, second call is verification
        if verification_result is not None and call_count[0] >= 2:
            if verification_side_effect is not None:
                raise verification_side_effect
            return verification_result
        if playwright_side_effect is not None:
            raise playwright_side_effect
        return playwright_result

    return side_effect


# --- Popen mock for streaming NDJSON (replaces subprocess.run for Claude CLI) ---

class MockPopen:
    """Mock subprocess.Popen that yields NDJSON lines from stdout.

    Used for testing _invoke_claude which reads stdout line-by-line.
    """

    def __init__(self, ndjson_stream: str, returncode: int = 0) -> None:
        self.stdout = io.StringIO(ndjson_stream)
        self.stderr = io.StringIO("")
        self.returncode = returncode
        self.pid = 99999
        self.communicate_timeout: float | None = None
        self.wait_timeout: float | None = None
        self._wait_called = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_timeout = timeout
        return self.stdout.read(), self.stderr.read()

    def wait(self, timeout: float | None = None) -> int:
        if not self._wait_called:
            self.wait_timeout = timeout
            self._wait_called = True
        return self.returncode

    def kill(self) -> None:
        pass


def make_popen_factory(
    ndjson_stream: str, returncode: int = 0
):
    """Create a factory for subprocess.Popen mock (returns MockPopen)."""
    def factory(*args, **kwargs):
        return MockPopen(ndjson_stream, returncode)
    return factory


def make_popen_dispatcher(
    claude_ndjson: str | None = None,
    claude_returncode: int = 0,
    claude_side_effect: Exception | None = None,
):
    """Create a side_effect for subprocess.Popen mock.

    Claude commands return MockPopen with NDJSON stream.
    Non-Claude Popen calls (e.g. taskkill) return a no-op MockPopen.
    Access factory.last_claude_popen after run to inspect the MockPopen instance.
    """
    def factory(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and cmd and cmd[0] == "claude":
            if claude_side_effect is not None:
                raise claude_side_effect
            popen = MockPopen(claude_ndjson or "", claude_returncode)
            factory.last_claude_popen = popen
            return popen
        # taskkill or other subprocess.Popen calls
        return MockPopen("", 0)
    factory.last_claude_popen = None
    return factory

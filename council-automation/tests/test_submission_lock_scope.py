"""Phase 0 Task 0.1 — baseline characterization test.

Documents (via inspect.getsource) that CouncilBrowser.run() uses the
cross-process submission_lock today. This is a baseline/characterization
test, not a behavior spec — it exists so future refactors of run() (e.g.
the Phase 0 RESEARCH_QUEUE_STOPGAP widening, or later the Phase 1 FIFO
queue) can be checked against a known-good starting contract: the lock is
acquired via submission_lock.get_submit_lock() (wrapped by
_acquire_submit_lock) and is released somewhere in run()'s body, with a
release-state flag guarding against double release.

Run with:
    python -m pytest tests/test_submission_lock_scope.py -v
(from council-automation/, so `import council_browser` resolves via cwd
on sys.path per `python -m pytest` semantics).
"""
import inspect

import council_browser
from council_browser import PerplexityCouncil


def _run_source() -> str:
    """Combined source of run() and (if present) _run_impl().

    The Phase 1 FIFO-queue wiring extract-method-refactored run()'s body into
    _run_impl(), leaving run() a thin queue wrapper. These characterization
    checks care that the submit-lock contract exists in the run pipeline, not
    which method holds it — so inspect both.
    """
    src = inspect.getsource(PerplexityCouncil.run)
    impl = getattr(PerplexityCouncil, "_run_impl", None)
    if impl is not None:
        src += "\n" + inspect.getsource(impl)
    return src


def test_submit_lock_used_in_run() -> None:
    """Baseline: run() acquires the cross-process submit lock.

    Today this happens via `await self._acquire_submit_lock()`, which
    wraps `submission_lock.get_submit_lock()`. Mirrors the substring
    check from the Phase 0 design doc (`"_submit_lock" in src`, which
    matches via the `_acquire_submit_lock` method name).
    """
    src = _run_source()
    assert "_submit_lock" in src, "submission lock must be used in run()"
    assert "_acquire_submit_lock" in src


def test_acquire_submit_lock_uses_submission_lock_module() -> None:
    """_acquire_submit_lock() is backed by submission_lock.get_submit_lock."""
    src = inspect.getsource(PerplexityCouncil._acquire_submit_lock)
    assert "get_submit_lock" in src


def test_run_releases_submit_lock() -> None:
    """Baseline: run() releases the acquired submit_lock somewhere in its
    body (either the mid-run explicit release, or the defensive release
    in the outer finally)."""
    src = _run_source()
    assert "submit_lock.release()" in src


def test_run_tracks_submit_lock_release_state() -> None:
    """Baseline: run() tracks whether submit_lock has already been
    released via a `submit_lock_released` flag, so the mid-run release
    and the defensive finally-block release can never both fire."""
    src = _run_source()
    assert "submit_lock_released" in src


def test_council_browser_imports_get_submit_lock() -> None:
    """Baseline: the module-level import wiring for the shared lock."""
    src = inspect.getsource(council_browser)
    assert "from submission_lock import get_submit_lock" in src

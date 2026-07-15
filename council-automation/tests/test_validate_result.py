"""Unit tests for council_browser._validate_result — the post-extraction
truncation/emptiness classifier.

Locks the 2026-07-14 fix: rule (d) (peak-length regression) is GATED to
non-substantial outputs so a complete 6-13K-char structured answer whose
streaming .prose peak was inflated by transient reasoning-trail text is NOT
false-flagged as truncated — while genuine near-empty stalls (<300 chars) and
structural cuts (unclosed fence / dangling token) are still caught at any size.

Run with:
    python -m pytest tests/test_validate_result.py -v
(from council-automation/, so `import council_browser` resolves via cwd.)
"""
import council_browser as cb

V = cb._validate_result
SUB = cb.SUBSTANTIAL_SYNTHESIS_CHARS  # 2500


def test_empty_is_empty() -> None:
    assert V("", 100) == "empty"
    assert V("   ", 100) == "empty"


def test_large_answer_with_inflated_peak_is_ok_not_truncated() -> None:
    # The core false-positive fix: 10K-char complete answer, streaming peak 12K
    # (reasoning-trail inflation) => 15%+ regression, but must NOT flag.
    big = "x" * 10000
    assert V(big, 12000) == "ok"


def test_genuine_tiny_stall_still_flagged() -> None:
    # 197 chars after a peak of 292 = the real Jul-12 failure shape.
    assert V("y" * 197, 292) == "suspect_truncated"


def test_short_complete_answer_is_ok() -> None:
    # e.g. a "443" reply — short, no regression vs a small peak.
    assert V("443", 3) == "ok"
    assert V("443", None) == "ok"


def test_peak_regression_gate_boundary() -> None:
    # Just below the substantial gate: regression still evaluated.
    s = "z" * (SUB - 1)
    assert V(s, int((SUB - 1) / 0.8)) == "suspect_truncated"  # ~20% drop from peak
    # At/above the gate: regression ignored.
    s2 = "z" * SUB
    assert V(s2, int(SUB / 0.5)) == "ok"  # 50% "drop" from peak, but substantial => ok


def test_structural_rules_still_fire_on_large_output() -> None:
    # (a) unclosed code fence must be caught even above the substantial gate.
    assert V("a" * 3000 + "\n```code", 3100) == "suspect_truncated"
    # (b) dangling terminal token must be caught even above the gate.
    assert V("b" * 3000 + " and then:", 3100) == "suspect_truncated"


def test_large_clean_answer_ending_normally_is_ok() -> None:
    assert V("word " * 700 + "final sentence.", 4000) == "ok"

"""Tests for averyloop.signals — measured objective sub-scores.

Pure-function tests on crafted diffs/test results, plus graceful-degradation
checks. No LLM calls.
"""

from __future__ import annotations

import pytest

from averyloop import signals
from averyloop.signals import (
    changed_files_in_diff,
    diff_line_counts,
    parse_pytest_summary,
    test_score as compute_test_score,
    coverage_score,
    complexity_score,
    diff_size_score,
    scope_adherence_score,
    compute_objective_signals,
)


# ── Diff parsing ─────────────────────────────────────────────────────────────

SAMPLE_DIFF = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,3 +1,4 @@
 import os
+import sys
-old_line = 1
 keep = 2
diff --git a/src/b.py b/src/b.py
--- a/src/b.py
+++ b/src/b.py
@@ -0,0 +1,2 @@
+new = True
+also = False
"""


def test_changed_files_in_diff():
    assert changed_files_in_diff(SAMPLE_DIFF) == {"src/a.py", "src/b.py"}


def test_changed_files_ignores_dev_null():
    diff = "--- a/x.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
    assert changed_files_in_diff(diff) == set()


def test_diff_line_counts_excludes_headers():
    counts = diff_line_counts(SAMPLE_DIFF)
    assert counts["added"] == 3   # import sys, new = True, also = False
    assert counts["removed"] == 1  # old_line = 1


def test_parse_pytest_summary():
    assert parse_pytest_summary("=== 12 passed in 0.3s ===") == {
        "passed": 12, "failed": 0, "error": 0,
    }
    out = "3 failed, 9 passed, 1 error in 1.2s"
    parsed = parse_pytest_summary(out)
    assert parsed == {"passed": 9, "failed": 3, "error": 1}


# ── Sub-score functions ──────────────────────────────────────────────────────

def test_test_score_pass_fail():
    assert compute_test_score(True) == 10.0
    assert compute_test_score(False) == 0.0


def test_test_score_penalizes_shrinking_count():
    # Passing but with fewer tests than before — suspicious.
    assert compute_test_score(True, prev_test_count=20, curr_test_count=15) == 5.0
    assert compute_test_score(True, prev_test_count=15, curr_test_count=20) == 10.0


def test_coverage_score_delta():
    assert coverage_score(80.0, 80.0) == 5.0   # neutral
    assert coverage_score(80.0, 83.0) == 8.0   # +3 points
    assert coverage_score(80.0, 70.0) == 0.0   # clamped floor


def test_complexity_score_delta():
    assert complexity_score(5.0, 5.0) == 5.0       # neutral
    assert complexity_score(8.0, 5.0) == 8.0       # reduced complexity → reward
    assert complexity_score(5.0, 9.0) == 1.0       # increased → penalize


def test_diff_size_score():
    assert diff_size_score(10, 5) == 10.0          # small, focused
    assert diff_size_score(70, 0) == 5.0           # 70 lines → 10 - 50/10 = 5
    assert diff_size_score(500, 500) == 0.0        # huge → clamped


def test_scope_adherence_math():
    intended = {"src/a.py"}
    # Two changed files, one in scope → 50% → 5.0
    assert scope_adherence_score({"src/a.py", "src/b.py"}, intended) == 5.0
    # All in scope → 10.0
    assert scope_adherence_score({"src/a.py"}, intended) == 10.0
    # Empty diff is vacuously in-scope.
    assert scope_adherence_score(set(), intended) == 10.0


def test_scope_adherence_on_straying_diff():
    # A diff that touches a file outside the intended scope.
    intended = {"src/target.py"}
    changed = changed_files_in_diff(SAMPLE_DIFF)  # src/a.py, src/b.py
    score = scope_adherence_score(changed, intended)
    assert score == 0.0


# ── Aggregate + graceful degradation ─────────────────────────────────────────

def test_compute_signals_full_set():
    sig = compute_objective_signals(
        tests_passed=True,
        diff=SAMPLE_DIFF,
        intended_files={"src/a.py", "src/b.py"},
        prev_coverage_pct=80.0,
        curr_coverage_pct=82.0,
        prev_complexity=6.0,
        curr_complexity=5.0,
    )
    assert sig.available == {
        "tests": True, "coverage": True, "complexity": True,
        "diff_size": True, "scope": True,
    }
    assert sig.sub_scores["tests"] == 10.0
    assert sig.sub_scores["coverage"] == 7.0
    assert sig.sub_scores["complexity"] == 6.0
    assert sig.sub_scores["scope"] == 10.0


def test_compute_signals_degrades_when_coverage_absent():
    # No coverage/complexity inputs → those signals drop out, no crash.
    sig = compute_objective_signals(
        tests_passed=True,
        diff=SAMPLE_DIFF,
        intended_files={"src/a.py", "src/b.py"},
    )
    assert sig.available["coverage"] is False
    assert sig.available["complexity"] is False
    assert "coverage" not in sig.sub_scores
    assert sig.available["tests"] is True
    assert sig.available["diff_size"] is True


def test_compute_signals_no_diff_drops_scope_and_diffsize():
    sig = compute_objective_signals(tests_passed=False)
    assert sig.available["tests"] is True
    assert sig.sub_scores["tests"] == 0.0
    assert sig.available["diff_size"] is False
    assert sig.available["scope"] is False


def test_to_log_dict_round_trips():
    sig = compute_objective_signals(tests_passed=True, diff=SAMPLE_DIFF,
                                    intended_files={"src/a.py", "src/b.py"})
    d = sig.to_log_dict()
    assert set(d.keys()) == {"sub_scores", "available", "raw"}
    assert d["sub_scores"]["tests"] == 10.0


# ── Optional measurement wrappers degrade to None ────────────────────────────

def test_measure_coverage_returns_none_without_tool(monkeypatch):
    # Force the import to fail and confirm None (not an exception).
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "coverage":
            raise ImportError("no coverage")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert signals.measure_coverage() is None


def test_measure_complexity_returns_none_without_tool(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("radon"):
            raise ImportError("no radon")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert signals.measure_complexity(["src/a.py"]) is None

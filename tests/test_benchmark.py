"""Unit tests for the benchmark: metric formulas + deterministic two-arm run."""

from __future__ import annotations

import pytest

from benchmark import metrics
from benchmark.runner import run_benchmark
from benchmark.stub_agents import decide_finding, load_fixture
from benchmark.runner import FIXTURES_DIR
import os


# ── metric formulas (hand-computed) ──────────────────────────────────────────

class TestMetricFormulas:
    def test_fix_rate(self):
        assert metrics.fix_rate(1, 2) == 0.5
        assert metrics.fix_rate(0, 0) == 0.0  # no seeded bugs → 0, no crash
        assert metrics.fix_rate(3, 3) == 1.0

    def test_false_accept_rate(self):
        assert metrics.false_accept_rate(0, 1) == 0.0
        assert metrics.false_accept_rate(1, 4) == 0.25
        assert metrics.false_accept_rate(0, 0) == 0.0

    def test_convergence_savings(self):
        assert metrics.convergence_savings(25, 12) == pytest.approx(0.52)
        assert metrics.convergence_savings(10, 10) == 0.0
        assert metrics.convergence_savings(0, 0) == 0.0

    def test_pearson_perfect_and_anti(self):
        assert metrics.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
        assert metrics.pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)

    def test_spearman_monotonic_nonlinear(self):
        # monotonic but non-linear → spearman 1.0, pearson < 1.0
        xs, ys = [1, 2, 3, 4], [1, 4, 9, 16]
        assert metrics.spearman(xs, ys) == pytest.approx(1.0)
        assert metrics.pearson(xs, ys) < 1.0

    def test_correlation_degenerate_inputs(self):
        assert metrics.spearman([1], [1]) == 0.0          # too few points
        assert metrics.pearson([1, 1, 1], [2, 3, 4]) == 0.0  # zero variance

    def test_agreement_on_known_four_pairs(self):
        # The benchmark's headline pairs (judge vs objective).
        judge = [8.5, 3.0, 2.5, 6.0]
        obj = [10.0, 8.3333, 10.0, 10.0]
        out = metrics.agreement(judge, obj)
        assert out["n"] == 4
        assert out["spearman"] == pytest.approx(0.2582, abs=1e-3)


class TestSummarizeArm:
    def test_hand_computed_summary(self):
        runs = [
            {"is_seeded_bug": True, "bug_fixed": True, "unsafe_total": 0,
             "unsafe_merged": 0, "iterations": 3, "reverts": 0,
             "wasted_reattempts": 0, "judge_series": [8.0], "objective_series": [9.0]},
            {"is_seeded_bug": True, "bug_fixed": False, "unsafe_total": 1,
             "unsafe_merged": 0, "iterations": 2, "reverts": 0,
             "wasted_reattempts": 1, "judge_series": [3.0], "objective_series": [8.0]},
        ]
        s = metrics.summarize_arm(runs)
        assert s["fix_rate"] == 0.5            # 1 of 2 seeded bugs fixed
        assert s["false_accept_rate"] == 0.0   # 0 of 1 unsafe merged
        assert s["total_iterations"] == 5
        assert s["wasted_reattempts"] == 1
        assert s["agreement"]["n"] == 2


# ── stub decisions use the real authored components ──────────────────────────

class TestStubDecisions:
    def _cfg(self):
        from averyloop.loop_config import LoopConfig
        return LoopConfig()

    def test_logic_bug_merges_and_fixes(self):
        fx = load_fixture(os.path.join(FIXTURES_DIR, "logic_bug"))
        d = decide_finding(fx.findings[0], fx, self._cfg())
        assert d.merged is True
        assert d.fixes_bug is True
        assert d.safety_veto is False

    def test_safety_trap_is_vetoed_not_merged(self):
        fx = load_fixture(os.path.join(FIXTURES_DIR, "safety_trap"))
        d = decide_finding(fx.findings[0], fx, self._cfg())
        # Real safety gate fires (assertion removal + out-of-scope write).
        assert d.safety_veto is True
        assert d.merged is False

    def test_revert_trap_is_reverted(self):
        fx = load_fixture(os.path.join(FIXTURES_DIR, "revert_trap"))
        d = decide_finding(fx.findings[0], fx, self._cfg())
        assert d.reverted is True
        assert d.merged is False


# ── deterministic end-to-end two-arm comparison ──────────────────────────────

class TestTwoArmComparison:
    def test_run_is_deterministic(self):
        r1 = run_benchmark(max_iterations=5)
        r2 = run_benchmark(max_iterations=5)
        assert r1["arms"] == r2["arms"]
        assert r1["comparisons"] == r2["comparisons"]

    def test_headline_metrics(self):
        r = run_benchmark(max_iterations=5)
        full = r["arms"]["convergence+memory"]
        assert full["fix_rate"] == 0.5
        assert full["false_accept_rate"] == 0.0
        assert full["total_seeded"] == 2
        assert full["unsafe_total"] == 1

    def test_convergence_saves_iterations_with_quality_held(self):
        r = run_benchmark(max_iterations=5)
        cv = r["comparisons"]["convergence_vs_fixed"]
        assert cv["convergence_iterations"] < cv["baseline_iterations"]
        assert cv["iteration_savings"] > 0.0
        assert cv["quality_held"] is True

    def test_memory_reduces_reverts_and_wasted_reattempts(self):
        r = run_benchmark(max_iterations=5)
        mm = r["comparisons"]["memory_on_vs_off"]
        assert mm["reverts_on"] < mm["reverts_off"]
        assert mm["wasted_reattempts_on"] < mm["wasted_reattempts_off"]
        # Memory never makes safety worse.
        assert mm["false_accept_on"] <= mm["false_accept_off"]

    def test_safety_trap_never_merged_in_any_arm(self):
        r = run_benchmark(max_iterations=5)
        for arm_runs in r["runs"].values():
            for run in arm_runs:
                if run["fixture"] == "safety_trap":
                    assert run["unsafe_merged"] == 0

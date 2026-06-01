"""Tests for averyloop.convergence — authored, non-LLM stop detection.

These assert the *harness's own logic*, not an LLM's. Histories are
synthetic dicts shaped like loop_tracker log entries.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from averyloop.convergence import evaluate_convergence, ConvergenceDecision


@dataclass
class FakeCfg:
    """Minimal config stand-in for the convergence detector."""

    convergence_epsilon: float = 0.25
    convergence_patience: int = 2
    min_iterations: int = 2
    convergence_adaptive: bool = False
    convergence_adaptive_coef: float = 0.5
    convergence_adaptive_floor: float = 0.01


def _entry(score, merged=0):
    """Build a synthetic iteration entry with an overall score and N merges."""
    return {
        "audit_scores": {"overall": float(score)},
        "branches_merged": ["b"] * merged,
    }


# ── Min-iteration floor ──────────────────────────────────────────────────────

def test_floor_prevents_stop_on_first_iteration():
    cfg = FakeCfg(min_iterations=2)
    # A single flat iteration would otherwise look stalled, but the floor wins.
    decision = evaluate_convergence([_entry(5.0, merged=0)], cfg)
    assert decision.stop is False
    assert "floor" in decision.reason


def test_floor_zero_still_requires_one_iteration():
    cfg = FakeCfg(min_iterations=0)
    decision = evaluate_convergence([], cfg)
    assert decision.stop is False


# ── Plateau ──────────────────────────────────────────────────────────────────

def test_plateau_triggers_after_k_stalled_iterations():
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=2)
    # Scores: 5.0 -> 8.0 -> 8.05 -> 8.10  (last two improvements < 0.25)
    history = [
        _entry(5.0, merged=1),
        _entry(8.0, merged=1),
        _entry(8.05, merged=1),
        _entry(8.10, merged=1),
    ]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is True
    assert "plateau" in decision.reason
    assert decision.signal_values["recent_improvements"] == pytest.approx(
        [0.05, 0.05], abs=1e-6
    )


def test_improving_trajectory_keeps_going():
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=2)
    # Steady +1.0 per iteration — well above epsilon.
    history = [_entry(s, merged=1) for s in (4.0, 5.0, 6.0, 7.0)]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is False


def test_score_drop_counts_as_stalled():
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=2)
    # Declining scores: negative improvements are < epsilon → plateau.
    history = [_entry(s, merged=1) for s in (8.0, 9.0, 8.5, 8.0)]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is True
    assert "plateau" in decision.reason


def test_plateau_needs_k_plus_one_points():
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=3, min_iterations=2)
    # Only 3 points but patience=3 needs 4 → plateau cannot fire yet.
    history = [_entry(s, merged=1) for s in (8.0, 8.0, 8.0)]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is False


# ── Decay ────────────────────────────────────────────────────────────────────

def test_decay_stops_when_no_fixes_accepted():
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=2)
    # Scores still climbing fast (no plateau) but nothing merges → decay.
    history = [
        _entry(3.0, merged=1),
        _entry(5.0, merged=0),
        _entry(7.0, merged=0),
    ]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is True
    assert "decay" in decision.reason
    assert decision.signal_values["recent_accepts"] == [0, 0]


def test_recent_accept_prevents_decay():
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=2)
    history = [
        _entry(3.0, merged=0),
        _entry(5.0, merged=0),
        _entry(7.0, merged=1),  # most recent accepted a fix
    ]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is False


# ── Composite preference + robustness ────────────────────────────────────────

def test_composite_score_preferred_over_overall():
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=2)
    # overall would look like progress, but composite is flat → plateau.
    history = [
        {"audit_scores": {"overall": 2.0, "composite": 8.0}, "branches_merged": ["b"]},
        {"audit_scores": {"overall": 5.0, "composite": 8.0}, "branches_merged": ["b"]},
        {"audit_scores": {"overall": 9.0, "composite": 8.0}, "branches_merged": ["b"]},
    ]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is True
    assert "plateau" in decision.reason


def test_missing_metric_does_not_crash():
    cfg = FakeCfg(min_iterations=2)
    history = [{"branches_merged": ["b"]}, {"branches_merged": ["b"]}]
    decision = evaluate_convergence(history, cfg)
    assert isinstance(decision, ConvergenceDecision)
    assert decision.stop is False


# ── Adaptive convergence ─────────────────────────────────────────────────────

# The crafted case: a repo that improves slowly but *steadily* (+0.1/iter).
# Fixed epsilon=0.25 calls this a plateau and stops too early; adaptive epsilon
# recognizes +0.1 as this repo's real rate of progress and keeps going.
_SLOW_STEADY = [4.0, 4.1, 4.2, 4.3, 4.4]

# A repo that jumps early then flattens — both modes should stop here.
_FAST_THEN_FLAT = [5.0, 9.0, 9.02, 9.03]


def test_fixed_misjudges_slow_but_improving_run():
    """Baseline: fixed thresholds stop a still-improving slow run (the bug)."""
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=2,
                  convergence_adaptive=False)
    history = [_entry(s, merged=1) for s in _SLOW_STEADY]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is True  # fixed mis-calls this as a plateau
    assert "plateau" in decision.reason


def test_adaptive_keeps_going_on_slow_but_improving_run():
    """Adaptive mode beats fixed: same history, correctly keeps going."""
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=2,
                  convergence_adaptive=True)
    history = [_entry(s, merged=1) for s in _SLOW_STEADY]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is False
    # eps scaled to the ~0.1 movement scale, well below the fixed 0.25.
    assert decision.signal_values["epsilon_effective"] < 0.25
    assert decision.signal_values["adaptive"] is True


def test_adaptive_stops_on_fast_converging_run():
    """Adaptive mode still stops when the run has genuinely flattened."""
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=2,
                  convergence_adaptive=True)
    history = [_entry(s, merged=1) for s in _FAST_THEN_FLAT]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is True
    assert "plateau" in decision.reason
    # The early +4.0 jump sets a large scale, so the ~0.02 recent steps
    # fall far below the adaptive epsilon.
    assert decision.signal_values["epsilon_effective"] > 0.25


def test_adaptive_floor_keeps_flat_run_stoppable():
    """A perfectly flat run has scale 0; the floor keeps it stoppable."""
    cfg = FakeCfg(convergence_epsilon=0.25, convergence_patience=2,
                  convergence_adaptive=True, convergence_adaptive_floor=0.01)
    history = [_entry(8.0, merged=1) for _ in range(4)]
    decision = evaluate_convergence(history, cfg)
    assert decision.stop is True
    assert decision.signal_values["epsilon_effective"] == pytest.approx(0.01)


def test_adaptive_epsilon_is_deterministic():
    """Same history → identical effective epsilon (pure function)."""
    cfg = FakeCfg(convergence_adaptive=True)
    history = [_entry(s, merged=1) for s in _FAST_THEN_FLAT]
    a = evaluate_convergence(history, cfg).signal_values["epsilon_effective"]
    b = evaluate_convergence(history, cfg).signal_values["epsilon_effective"]
    assert a == b

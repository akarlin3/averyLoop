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

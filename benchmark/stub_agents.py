"""Deterministic stub agents for the offline benchmark.

The benchmark must run in CI with no API key and no network, so the four live
agent call sites (audit, implement, review, judge) are replaced by deterministic
behavior driven by each fixture's ``ground_truth.json``.  Crucially, the
*decision* a fix receives is **not** stubbed — it flows through the real authored
components: :mod:`averyloop.signals` (objective sub-scores),
:mod:`averyloop.safety_gate` (the merge veto), and
:func:`averyloop.evaluator.blend_scores` (the composite).  So the benchmark
measures the genuine behavior of the authored backbone; only the LLM text and the
git/test execution are simulated from ground truth.

Everything here is pure given a fixture dict — no LLM, no DB, no git.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from averyloop import signals as _signals
from averyloop.evaluator import blend_scores
from averyloop.safety_gate import evaluate_safety

BRANCH_PREFIX = "improvement/"


@dataclass
class Fixture:
    """A loaded benchmark fixture and its ground truth."""

    name: str
    category: str
    description: str
    findings: List[dict] = field(default_factory=list)
    is_seeded_bug: bool = False
    failing_test: Optional[str] = None
    expected_outcome: str = ""
    read_only_dirs: List[str] = field(default_factory=list)
    path: str = ""

    @property
    def has_unsafe(self) -> bool:
        return any(f.get("is_unsafe") for f in self.findings)


def load_fixture(fixture_dir: str) -> Fixture:
    """Load a fixture from ``<fixture_dir>/ground_truth.json``."""
    with open(os.path.join(fixture_dir, "ground_truth.json"), encoding="utf-8") as fh:
        gt = json.load(fh)
    return Fixture(
        name=gt["name"],
        category=gt["category"],
        description=gt.get("description", ""),
        findings=gt.get("findings", []),
        is_seeded_bug=bool(gt.get("is_seeded_bug", False)),
        failing_test=gt.get("failing_test"),
        expected_outcome=gt.get("expected_outcome", ""),
        read_only_dirs=list(gt.get("read_only_dirs", [])),
        path=fixture_dir,
    )


def finding_dict(raw: dict) -> dict:
    """Normalize a ground-truth finding into the loop's finding-dict shape."""
    return {
        "dimension": raw["dimension"],
        "file": raw["file"],
        "function_name": raw.get("function_name"),
        "description": raw["description"],
        "fix": raw["fix"],
        "importance": int(raw["importance"]),
        "branch_name": BRANCH_PREFIX + raw["branch_slug"],
    }


@dataclass
class FindingDecision:
    """The result of running one ground-truth finding through real logic."""

    finding: dict
    diff: str
    signals: object                 # ObjectiveSignals
    objective_mean: float
    judge_score: float
    safety_veto: bool
    review_verdict: str
    tests_passed: bool
    merged: bool                    # final (after any post-merge revert)
    reverted: bool
    is_unsafe: bool
    fixes_bug: bool


def _available_mean(signals) -> float:
    """Mean of the available objective sub-scores (neutral 5.0 if none)."""
    vals = [
        signals.sub_scores[name]
        for name in _signals.SIGNAL_NAMES
        if signals.available.get(name)
    ]
    return sum(vals) / len(vals) if vals else 5.0


def decide_finding(raw: dict, fixture: Fixture, cfg) -> FindingDecision:
    """Run a single ground-truth finding through the real authored components.

    Computes objective signals, runs the deterministic safety gate, and derives
    the merge/revert decision exactly as the orchestrator would: a fix merges
    only if the review approves, its tests pass, and the safety gate does not
    veto; a fix that passes pre-merge but fails post-merge is auto-reverted.
    """
    fd = finding_dict(raw)
    diff = raw["diff"]
    tests_after_fix = bool(raw.get("tests_pass_after_fix", True))
    tests_after_merge = bool(raw.get("tests_pass_after_merge", True))

    sig = _signals.compute_objective_signals(
        tests_passed=tests_after_fix,
        diff=diff,
        intended_files={fd["file"]},
    )

    verdict = evaluate_safety(
        diff,
        {fd["file"]},
        protected_paths=getattr(cfg, "safety_protected_paths", None),
        denylist_paths=getattr(cfg, "safety_denylist_paths", None),
        allowlist_paths=getattr(cfg, "safety_allowlist_paths", None),
        read_only_dirs=fixture.read_only_dirs,
    )
    veto = verdict.veto if getattr(cfg, "safety_gate_enabled", True) else False

    review = raw.get("review_verdict", "APPROVE")
    merged_initial = (review == "APPROVE") and tests_after_fix and not veto

    reverted = bool(merged_initial and not tests_after_merge)
    merged_final = bool(merged_initial and not reverted)

    return FindingDecision(
        finding=fd,
        diff=diff,
        signals=sig,
        objective_mean=_available_mean(sig),
        judge_score=float(raw.get("judge_score", 5.0)),
        safety_veto=veto,
        review_verdict=review,
        tests_passed=tests_after_fix,
        merged=merged_final,
        reverted=reverted,
        is_unsafe=bool(raw.get("is_unsafe", False)),
        fixes_bug=bool(raw.get("fixes_bug", False)),
    )


def iteration_scores(decisions: List[FindingDecision], cfg) -> Dict[str, float]:
    """Aggregate one iteration's judge + composite scores from its decisions.

    Returns ``{"overall": judge, "composite": composite, "objective": obj_mean}``.
    The composite is the *real* ``blend_scores`` output, so convergence runs on a
    genuine composite series.  With no decisions (nothing to do), returns a
    neutral 5.0 across the board.
    """
    if not decisions:
        return {"overall": 5.0, "composite": 5.0, "objective": 5.0}

    judge = sum(d.judge_score for d in decisions) / len(decisions)
    objective = sum(d.objective_mean for d in decisions) / len(decisions)

    # Blend using the highest-signal decision's ObjectiveSignals (representative
    # of the iteration's measured change), with the averaged judge score.
    rep = max(decisions, key=lambda d: d.objective_mean)
    blend = blend_scores({"overall": judge}, signals=rep.signals, cfg=cfg)
    return {
        "overall": round(judge, 4),
        "composite": blend["composite"],
        "objective": round(objective, 4),
    }

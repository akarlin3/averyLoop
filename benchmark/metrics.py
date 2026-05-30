"""Benchmark metric formulas — pure functions, hand-verifiable.

Each metric is pinned to an exact formula (see ``benchmark/README.md``):

* **fix rate** = fixed seeded bugs / total seeded bugs.
* **false-accept rate** = unsafe changes merged / unsafe changes offered.
* **convergence savings** = (baseline iterations − convergence iterations) /
  baseline iterations, reported with a quality-hold check.
* **judge↔objective agreement** = Spearman ρ (and Pearson r) between the LLM
  judge score and the objective signal score across iterations.

All functions take plain numbers / dicts so the formulas are unit-testable on
tiny hand-computed inputs.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple


# ---------------------------------------------------------------------------
# Correlation helpers (pure; no numpy/scipy)
# ---------------------------------------------------------------------------

def _ranks(values: Sequence[float]) -> List[float]:
    """Average (fractional) ranks of *values*, handling ties."""
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # ranks are 1-based
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation; ``0.0`` for <2 points or a zero-variance series."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    denom = math.sqrt(sxx * syy)
    if denom == 0.0:
        return 0.0
    return sxy / denom


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman rank correlation = Pearson on the ranks."""
    if len(xs) < 2 or len(xs) != len(ys):
        return 0.0
    return pearson(_ranks(xs), _ranks(ys))


# ---------------------------------------------------------------------------
# Core metrics
# ---------------------------------------------------------------------------

def fix_rate(fixed_seeded: int, total_seeded: int) -> float:
    """Fraction of seeded bugs whose failing condition was resolved by a merge."""
    if total_seeded <= 0:
        return 0.0
    return fixed_seeded / total_seeded


def false_accept_rate(unsafe_merged: int, unsafe_total: int) -> float:
    """Fraction of unsafe/incorrect changes that were merged."""
    if unsafe_total <= 0:
        return 0.0
    return unsafe_merged / unsafe_total


def convergence_savings(baseline_iters: int, convergence_iters: int) -> float:
    """Fractional iteration savings of convergence vs the fixed baseline."""
    if baseline_iters <= 0:
        return 0.0
    return (baseline_iters - convergence_iters) / baseline_iters


def agreement(
    judge_series: Sequence[float], objective_series: Sequence[float]
) -> Dict[str, float]:
    """Spearman + Pearson agreement between judge and objective series."""
    return {
        "spearman": round(spearman(judge_series, objective_series), 4),
        "pearson": round(pearson(judge_series, objective_series), 4),
        "n": len(judge_series),
    }


# ---------------------------------------------------------------------------
# Aggregation over a list of per-run records
# ---------------------------------------------------------------------------

def summarize_arm(runs: List[dict]) -> dict:
    """Aggregate per-fixture run records for a single arm into metric values.

    Each run record is expected to carry: ``is_seeded_bug``, ``bug_fixed``,
    ``unsafe_total``, ``unsafe_merged``, ``iterations``, ``reverts``,
    ``wasted_reattempts``, and the per-iteration ``judge_series`` /
    ``objective_series``.
    """
    total_seeded = sum(1 for r in runs if r["is_seeded_bug"])
    fixed_seeded = sum(1 for r in runs if r["is_seeded_bug"] and r["bug_fixed"])
    unsafe_total = sum(r["unsafe_total"] for r in runs)
    unsafe_merged = sum(r["unsafe_merged"] for r in runs)

    judge_pool: List[float] = []
    obj_pool: List[float] = []
    for r in runs:
        judge_pool.extend(r["judge_series"])
        obj_pool.extend(r["objective_series"])

    return {
        "total_iterations": sum(r["iterations"] for r in runs),
        "fix_rate": round(fix_rate(fixed_seeded, total_seeded), 4),
        "fixed_seeded": fixed_seeded,
        "total_seeded": total_seeded,
        "false_accept_rate": round(false_accept_rate(unsafe_merged, unsafe_total), 4),
        "unsafe_merged": unsafe_merged,
        "unsafe_total": unsafe_total,
        "reverts": sum(r["reverts"] for r in runs),
        "wasted_reattempts": sum(r["wasted_reattempts"] for r in runs),
        "agreement": agreement(judge_pool, obj_pool),
    }

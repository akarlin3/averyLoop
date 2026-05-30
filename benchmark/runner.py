"""Benchmark runner — drives AveryLoop's authored logic over the fixtures.

Default mode is **offline and deterministic**: stub agents (driven by each
fixture's ground truth) supply findings/fixes/reviews/scores, while the real
authored components — objective signals, the safety gate, the composite blend,
convergence detection, and the outcome memory — make every decision.  The runner
collects, per (fixture, arm): iterations used, which seeded bugs were fixed,
which unsafe traps were merged, reverts, the convergence stop reason, and the
judge/objective score series; then computes the four headline metrics.

Two-arm comparisons (so the benchmark *demonstrates the value of the authored
backbone*, not just absolute numbers):

* **convergence ON vs fixed-iteration baseline** — iteration savings, quality held.
* **outcome-memory ON vs OFF** — reverts / wasted re-attempts avoided.

The opt-in live mode (``AVERYLOOP_BENCH_LIVE=1``) is documented in
``benchmark/README.md`` and is never exercised in CI.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from averyloop import outcomes as _outcomes
from averyloop.convergence import evaluate_convergence
from averyloop.loop_config import LoopConfig
from averyloop.rag import outcome_memory as _om

from benchmark import metrics as _metrics
from benchmark.stub_agents import (
    Fixture,
    decide_finding,
    iteration_scores,
    load_fixture,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_ORDER = ["logic_bug", "safety_trap", "style_nit", "noop", "revert_trap"]

# Deterministic local embedding for outcome memory (no model download).
_EMBED_FN = lambda texts: _om.hashed_bow_embedding(texts, dim=256)
# Recall hits closer than this cosine distance count as "the same prior fix".
_SUPPRESS_DISTANCE = 0.35


@dataclass
class RunRecord:
    """Per-(fixture, arm) result."""

    fixture: str
    category: str
    arm: str
    convergence_on: bool
    memory_on: bool
    is_seeded_bug: bool
    iterations: int = 0
    stop_reason: str = "max_iterations"
    bug_fixed: bool = False
    unsafe_total: int = 0
    unsafe_merged: int = 0
    reverts: int = 0
    wasted_reattempts: int = 0
    merged: List[str] = field(default_factory=list)
    judge_series: List[float] = field(default_factory=list)
    objective_series: List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _bench_cfg(convergence_on: bool, memory_on: bool) -> LoopConfig:
    """LoopConfig tuned for the benchmark (low floor so stopping is observable)."""
    return LoopConfig(
        convergence_enabled=convergence_on,
        convergence_epsilon=0.25,
        convergence_patience=2,
        min_iterations=1,
        outcome_memory_enabled=memory_on,
        outcome_recall_k=5,
        safety_gate_enabled=True,
    )


def _suppressed_by_memory(raw: dict, fixture: Fixture, work_dir: str, cfg) -> bool:
    """True if outcome memory recalls a near-identical prior reject/revert.

    This is how memory changes behavior: a fix whose twin was previously
    rejected or reverted is not re-proposed.
    """
    branch = "improvement/" + raw["branch_slug"]
    query = "\n".join(
        [raw.get("description", ""), raw.get("fix", ""), raw.get("file", "")]
    )
    hits = _om.recall_outcomes(
        query, repo_root=work_dir, k=cfg.outcome_recall_k, embed_fn=_EMBED_FN
    )
    for h in hits:
        if h.get("label") not in (_outcomes.REJECTED, _outcomes.REVERTED):
            continue
        dist = h.get("distance")
        same_branch = h.get("branch_name") == branch
        if same_branch or (dist is not None and dist <= _SUPPRESS_DISTANCE):
            return True
    return False


def simulate_run(
    fixture: Fixture,
    *,
    convergence_on: bool,
    memory_on: bool,
    max_iterations: int,
    work_dir: str,
) -> RunRecord:
    """Simulate the loop over one fixture for one arm (see module docstring)."""
    cfg = _bench_cfg(convergence_on, memory_on)
    rec = RunRecord(
        fixture=fixture.name,
        category=fixture.category,
        arm=_arm_name(convergence_on, memory_on),
        convergence_on=convergence_on,
        memory_on=memory_on,
        is_seeded_bug=fixture.is_seeded_bug,
        unsafe_total=sum(1 for f in fixture.findings if f.get("is_unsafe")),
    )

    accepted: set = set()           # branch names that landed and stuck
    recorded_bad: set = set()       # branches recorded reject/revert this run
    history: List[dict] = []

    for i in range(1, max_iterations + 1):
        outstanding = [
            raw for raw in fixture.findings
            if ("improvement/" + raw["branch_slug"]) not in accepted
        ]
        emitted = []
        for raw in outstanding:
            if memory_on and _suppressed_by_memory(raw, fixture, work_dir, cfg):
                continue
            emitted.append(raw)

        decisions = [decide_finding(raw, fixture, cfg) for raw in emitted]

        # Wasted re-attempt: re-deciding a fix already recorded bad this run.
        for raw in emitted:
            if ("improvement/" + raw["branch_slug"]) in recorded_bad:
                rec.wasted_reattempts += 1

        iter_outcomes = []
        for d in decisions:
            branch = d.finding["branch_name"]
            if d.merged:
                accepted.add(branch)
                rec.merged.append(branch)
                if d.is_unsafe:
                    rec.unsafe_merged += 1
                if d.fixes_bug:
                    rec.bug_fixed = True
            elif d.reverted:
                rec.reverts += 1
                recorded_bad.add(branch)
            else:
                recorded_bad.add(branch)

            iter_outcomes.append(
                _outcomes.derive_outcome(
                    d.finding,
                    merged=d.merged,
                    reverted=d.reverted,
                    safety_veto=d.safety_veto,
                    review_verdict=d.review_verdict,
                    tests_passed=d.tests_passed,
                    diff=d.diff,
                    signals=d.signals,
                    iteration=i,
                    merge_sha=f"{branch}-i{i}",
                )
            )

        if memory_on and iter_outcomes:
            _om.record_outcomes(
                iter_outcomes, repo_root=work_dir, embed_fn=_EMBED_FN
            )

        scores = iteration_scores(decisions, cfg)
        if decisions:  # only active iterations carry a real judge/objective pair
            rec.judge_series.append(scores["overall"])
            rec.objective_series.append(scores["objective"])

        history.append({
            "iteration": i,
            "audit_scores": {
                "overall": scores["overall"],
                "composite": scores["composite"],
            },
            "branches_merged": [
                d.finding["branch_name"] for d in decisions if d.merged
            ],
            "findings": [
                {"importance": d.finding["importance"], "file": d.finding["file"]}
                for d in decisions
            ],
        })
        rec.iterations = i

        if convergence_on:
            decision = evaluate_convergence(history, cfg)
            if decision.stop:
                rec.stop_reason = decision.reason
                break
    else:
        rec.stop_reason = "max_iterations (fixed baseline)" if not convergence_on \
            else "max_iterations"

    return rec


def _arm_name(convergence_on: bool, memory_on: bool) -> str:
    if not convergence_on and not memory_on:
        return "baseline_fixed"
    if convergence_on and not memory_on:
        return "convergence"
    return "convergence+memory"


# ---------------------------------------------------------------------------
# Orchestration over all fixtures + arms
# ---------------------------------------------------------------------------

ARMS = [
    ("baseline_fixed", False, False),
    ("convergence", True, False),
    ("convergence+memory", True, True),
]


def run_benchmark(
    fixtures_dir: str = FIXTURES_DIR,
    max_iterations: int = 5,
    work_root: Optional[str] = None,
) -> dict:
    """Run every fixture under every arm and compute the headline metrics."""
    fixtures = [
        load_fixture(os.path.join(fixtures_dir, name))
        for name in FIXTURE_ORDER
        if os.path.isdir(os.path.join(fixtures_dir, name))
    ]

    cleanup = False
    if work_root is None:
        work_root = tempfile.mkdtemp(prefix="averyloop-bench-")
        cleanup = True

    arms: Dict[str, dict] = {}
    runs_by_arm: Dict[str, List[RunRecord]] = {}
    try:
        for arm_name, conv, mem in ARMS:
            runs: List[RunRecord] = []
            for fx in fixtures:
                work_dir = os.path.join(work_root, arm_name, fx.name)
                os.makedirs(work_dir, exist_ok=True)
                runs.append(simulate_run(
                    fx, convergence_on=conv, memory_on=mem,
                    max_iterations=max_iterations, work_dir=work_dir,
                ))
            runs_by_arm[arm_name] = runs
            arms[arm_name] = _metrics.summarize_arm([r.to_dict() for r in runs])
    finally:
        if cleanup:
            import shutil
            shutil.rmtree(work_root, ignore_errors=True)

    return {
        "max_iterations": max_iterations,
        "n_fixtures": len(fixtures),
        "arms": arms,
        "comparisons": _build_comparisons(arms),
        "runs": {
            arm: [r.to_dict() for r in rs] for arm, rs in runs_by_arm.items()
        },
    }


def _build_comparisons(arms: dict) -> dict:
    """Derive the two headline two-arm comparisons from the arm summaries."""
    base = arms["baseline_fixed"]
    conv = arms["convergence"]
    mem = arms["convergence+memory"]

    savings = _metrics.convergence_savings(
        base["total_iterations"], conv["total_iterations"]
    )
    return {
        "convergence_vs_fixed": {
            "baseline_iterations": base["total_iterations"],
            "convergence_iterations": conv["total_iterations"],
            "iteration_savings": round(savings, 4),
            "fix_rate_baseline": base["fix_rate"],
            "fix_rate_convergence": conv["fix_rate"],
            "quality_held": conv["fix_rate"] >= base["fix_rate"],
        },
        "memory_on_vs_off": {
            "reverts_off": conv["reverts"],
            "reverts_on": mem["reverts"],
            "wasted_reattempts_off": conv["wasted_reattempts"],
            "wasted_reattempts_on": mem["wasted_reattempts"],
            "false_accept_off": conv["false_accept_rate"],
            "false_accept_on": mem["false_accept_rate"],
        },
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_table(results: dict) -> str:
    """Render a human-readable results table."""
    lines: List[str] = []
    lines.append("=" * 70)
    lines.append(f"  AveryLoop benchmark — {results['n_fixtures']} fixtures, "
                 f"max_iterations={results['max_iterations']}")
    lines.append("=" * 70)

    full = results["arms"]["convergence+memory"]
    agree = full["agreement"]
    lines.append("")
    lines.append("Headline metrics (convergence+memory arm):")
    lines.append(f"  fix rate ................. {full['fix_rate']:.2f} "
                 f"({full['fixed_seeded']}/{full['total_seeded']} seeded bugs)")
    lines.append(f"  false-accept rate ........ {full['false_accept_rate']:.2f} "
                 f"({full['unsafe_merged']}/{full['unsafe_total']} unsafe traps)")
    lines.append(f"  judge<->objective agree .. spearman={agree['spearman']:.3f}, "
                 f"pearson={agree['pearson']:.3f} (n={agree['n']})")

    cmp = results["comparisons"]
    cv = cmp["convergence_vs_fixed"]
    lines.append("")
    lines.append("Convergence ON vs fixed-iteration baseline:")
    lines.append(f"  iterations ............... {cv['convergence_iterations']} vs "
                 f"{cv['baseline_iterations']} "
                 f"(savings {cv['iteration_savings']*100:.0f}%)")
    lines.append(f"  fix rate held ............ {cv['fix_rate_convergence']:.2f} vs "
                 f"{cv['fix_rate_baseline']:.2f} "
                 f"({'held' if cv['quality_held'] else 'DROPPED'})")

    mm = cmp["memory_on_vs_off"]
    lines.append("")
    lines.append("Outcome memory ON vs OFF:")
    lines.append(f"  reverts .................. {mm['reverts_on']} vs {mm['reverts_off']}")
    lines.append(f"  wasted re-attempts ....... {mm['wasted_reattempts_on']} vs "
                 f"{mm['wasted_reattempts_off']}")
    lines.append(f"  false-accept rate ........ {mm['false_accept_on']:.2f} vs "
                 f"{mm['false_accept_off']:.2f}")

    lines.append("")
    lines.append("Per-arm totals:")
    lines.append(f"  {'arm':<22} {'iters':>6} {'fix':>5} {'false_acc':>10} "
                 f"{'reverts':>8} {'wasted':>7}")
    for arm in ("baseline_fixed", "convergence", "convergence+memory"):
        a = results["arms"][arm]
        lines.append(f"  {arm:<22} {a['total_iterations']:>6} "
                     f"{a['fix_rate']:>5.2f} {a['false_accept_rate']:>10.2f} "
                     f"{a['reverts']:>8} {a['wasted_reattempts']:>7}")
    lines.append("=" * 70)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AveryLoop benchmark")
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--json", type=str, default=None,
                        help="Write machine-readable results JSON to this path")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress the table (only write JSON)")
    args = parser.parse_args(argv)

    if os.environ.get("AVERYLOOP_BENCH_LIVE") == "1":
        print("AVERYLOOP_BENCH_LIVE=1 set — live mode is opt-in and not "
              "implemented in this harness build; running offline stub mode.\n"
              "See benchmark/README.md for the live-run recipe.")

    results = run_benchmark(max_iterations=args.max_iterations)

    if not args.quiet:
        print(format_table(results))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"\nWrote results JSON to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

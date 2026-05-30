"""Convergence / diminishing-returns detection.

This is *authored* loop logic — the harness decides when to stop on its own,
rather than delegating the call to an LLM or running a fixed iteration count.

The single public entry point, :func:`evaluate_convergence`, is a **pure
function**: it takes the iteration history (a list of ``loop_tracker`` log
entries) plus a config object and returns a structured
:class:`ConvergenceDecision`.  It performs **no** LLM calls, no git access,
and no disk I/O, so it is trivially unit-testable on synthetic histories.

Stop signals implemented:

* **min-iteration floor** — never stop before ``cfg.min_iterations`` so the
  loop cannot quit on iteration 1.
* **plateau** — the progress metric (composite score if present, else the
  judge's overall score) improved by less than ``cfg.convergence_epsilon``
  for ``cfg.convergence_patience`` (``k``) consecutive iterations.  A score
  that *drops* counts as a stalled iteration too.
* **decay** — no fixes were accepted (merged) in the last ``k`` iterations,
  i.e. the marginal objective gain has trended to zero.

``max_iterations`` remains the hard ceiling in ``run_loop``; this module only
decides whether to stop *earlier*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ConvergenceDecision:
    """Result of a convergence evaluation.

    Attributes
    ----------
    stop:
        ``True`` if the loop should stop now, ``False`` to keep going.
    reason:
        Human-readable explanation, logged by ``run_loop``.
    signal_values:
        The raw numbers behind the decision (metric series, recent
        improvements, accept counts, thresholds) so a run is auditable.
    """

    stop: bool
    reason: str
    signal_values: dict = field(default_factory=dict)


def _progress_metric(entry: dict) -> Optional[float]:
    """Extract the per-iteration progress metric from a log entry.

    Prefers the blended ``composite`` score (see ``signals.py`` + the
    evaluator blend); falls back to the raw LLM ``overall`` score.  Returns
    ``None`` when neither is present so the iteration is skipped rather than
    crashing the detector.
    """
    scores = entry.get("audit_scores", {}) or {}
    metric = scores.get("composite")
    if metric is None:
        metric = scores.get("overall")
    if isinstance(metric, (int, float)):
        return float(metric)
    return None


def _accepted_fix_count(entry: dict) -> int:
    """Number of fixes accepted (merged) in an iteration."""
    return len(entry.get("branches_merged", []) or [])


def evaluate_convergence(history: List[dict], cfg=None) -> ConvergenceDecision:
    """Decide whether the improvement loop has converged.

    Parameters
    ----------
    history:
        Completed iteration entries, oldest first (the ``loop_tracker`` log).
    cfg:
        A ``LoopConfig``-like object exposing ``convergence_epsilon``,
        ``convergence_patience``, and ``min_iterations``.  If *None*, the
        cached loop config is loaded.

    Returns
    -------
    ConvergenceDecision
    """
    if cfg is None:
        from averyloop.loop_config import get_config
        cfg = get_config()

    eps = float(cfg.convergence_epsilon)
    k = int(cfg.convergence_patience)
    floor = int(cfg.min_iterations)
    n = len(history)

    signal_values: dict = {
        "n_iterations": n,
        "epsilon": eps,
        "patience": k,
        "min_iterations": floor,
    }

    # ── Min-iteration floor ──────────────────────────────────────────────
    # Bound below by 1 so an absurd floor of 0 can't enable stopping with no
    # history at all.
    if n < max(floor, 1):
        return ConvergenceDecision(
            stop=False,
            reason=f"min-iteration floor ({floor}) not yet reached (iter {n})",
            signal_values=signal_values,
        )

    metrics = [m for m in (_progress_metric(e) for e in history) if m is not None]
    signal_values["metric_series"] = metrics

    # ── Plateau ──────────────────────────────────────────────────────────
    # Need k improvements, i.e. k+1 metric points.
    if len(metrics) >= k + 1:
        improvements = [
            metrics[i] - metrics[i - 1]
            for i in range(len(metrics) - k, len(metrics))
        ]
        signal_values["recent_improvements"] = improvements
        if all(imp < eps for imp in improvements):
            return ConvergenceDecision(
                stop=True,
                reason=(
                    f"plateau: progress < {eps} for {k} consecutive "
                    f"iteration(s) (improvements={improvements})"
                ),
                signal_values=signal_values,
            )

    # ── Decay ────────────────────────────────────────────────────────────
    recent_accepts = [_accepted_fix_count(e) for e in history[-k:]]
    signal_values["recent_accepts"] = recent_accepts
    if len(recent_accepts) >= k and all(a == 0 for a in recent_accepts):
        return ConvergenceDecision(
            stop=True,
            reason=(
                f"decay: no fixes accepted in the last {k} iteration(s) "
                f"(accepts={recent_accepts})"
            ),
            signal_values=signal_values,
        )

    return ConvergenceDecision(
        stop=False,
        reason="continuing — improvement still above epsilon",
        signal_values=signal_values,
    )

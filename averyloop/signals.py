"""Objective-signal evaluator.

Pure, dependency-light functions that turn *measured* ground truth — test
results, diff geometry, coverage/complexity deltas, and scope adherence —
into normalized sub-scores in the range **0–10** (higher is better).  These
sub-scores are blended with the LLM judge's score in ``evaluator.py`` so the
judge becomes one input among measured signals rather than the sole oracle.

Everything here is unit-testable without LLM calls or live git state:

* the *scoring* functions take already-parsed values;
* the *parsing* helpers operate on diff/pytest text;
* the optional *measurement* wrappers (``measure_coverage`` /
  ``measure_complexity``) shell out to ``coverage`` / ``radon`` **only if they
  are importable**, returning ``None`` otherwise.  A ``None`` measurement
  drops the corresponding signal from the blend (graceful degradation) — it
  never raises.

Documented sub-score ranges (all clamped to ``[0, 10]``):

==================  ===========================================================
``tests``           10 = suite passes; 0 = fails; 5 = passes but test count
                    shrank (assertions/tests may have been removed).
``coverage``        5 = no change; +1 point per +1 percentage-point of line
                    coverage gained (and vice-versa).
``complexity``      5 = no change; +1 point per unit of average cyclomatic
                    complexity removed (and vice-versa).
``diff_size``       10 for a focused diff (≤ 20 changed lines), decaying for
                    larger diffs (risk proxy).
``scope``           10 * (fraction of changed files that were in the audit's
                    intended scope).
==================  ===========================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Set

# Names of the objective signals, in blend order.
SIGNAL_NAMES = ("tests", "coverage", "complexity", "diff_size", "scope")


def _clamp(value: float, low: float = 0.0, high: float = 10.0) -> float:
    """Clamp *value* into ``[low, high]``."""
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Diff / pytest parsing (pure)
# ---------------------------------------------------------------------------

_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_PYTEST_PASSED_RE = re.compile(r"(\d+)\s+passed")
_PYTEST_FAILED_RE = re.compile(r"(\d+)\s+failed")
_PYTEST_ERROR_RE = re.compile(r"(\d+)\s+error")


def changed_files_in_diff(diff: str) -> Set[str]:
    """Return the set of file paths touched by a unified *diff*.

    Reads the ``+++ b/<path>`` headers and ignores deletions whose new path is
    ``/dev/null``.
    """
    files = set()
    for match in _DIFF_FILE_RE.finditer(diff or ""):
        path = match.group(1).strip()
        if path and path != "/dev/null":
            files.add(path)
    return files


def diff_line_counts(diff: str) -> Dict[str, int]:
    """Count added/removed content lines in a unified *diff*.

    Header lines (``+++`` / ``---``) are excluded.
    """
    added = removed = 0
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed}


def parse_pytest_summary(text: str) -> Dict[str, int]:
    """Extract ``passed`` / ``failed`` / ``error`` counts from pytest output."""
    text = text or ""

    def _first(rx) -> int:
        m = rx.search(text)
        return int(m.group(1)) if m else 0

    return {
        "passed": _first(_PYTEST_PASSED_RE),
        "failed": _first(_PYTEST_FAILED_RE),
        "error": _first(_PYTEST_ERROR_RE),
    }


# ---------------------------------------------------------------------------
# Sub-score functions (pure, 0–10)
# ---------------------------------------------------------------------------

def test_score(
    passed: bool,
    prev_test_count: Optional[int] = None,
    curr_test_count: Optional[int] = None,
) -> float:
    """Score the test signal.

    * Failing suite → ``0``.
    * Passing suite → ``10``.
    * Passing suite where the test count *shrank* → ``5`` (tests may have been
      deleted to make the suite "pass" — a suspicious win, see the safety
      gate's assertion-removal check).
    """
    if not passed:
        return 0.0
    if (
        prev_test_count is not None
        and curr_test_count is not None
        and curr_test_count < prev_test_count
    ):
        return 5.0
    return 10.0


def coverage_score(prev_pct: float, curr_pct: float) -> float:
    """Score a line-coverage delta around a neutral midpoint of 5.

    +1 point per percentage-point gained; symmetric for losses.
    """
    return _clamp(5.0 + (curr_pct - prev_pct))


def complexity_score(prev_complexity: float, curr_complexity: float) -> float:
    """Score an average-cyclomatic-complexity delta around a midpoint of 5.

    Reducing complexity raises the score; adding complexity lowers it.
    """
    return _clamp(5.0 + (prev_complexity - curr_complexity))


def diff_size_score(added: int, removed: int) -> float:
    """Score diff focus: small diffs are lower-risk and score higher.

    ``≤ 20`` changed lines → ``10``; then ``-1`` per additional 10 lines.
    """
    total = added + removed
    if total <= 20:
        return 10.0
    return _clamp(10.0 - (total - 20) / 10.0)


def scope_adherence_score(
    changed_files: Iterable[str],
    intended_files: Iterable[str],
) -> float:
    """Fraction of *changed_files* that fall within *intended_files*, × 10.

    An empty diff is vacuously in scope (``10``).
    """
    changed = set(changed_files)
    intended = set(intended_files)
    if not changed:
        return 10.0
    in_scope = sum(1 for f in changed if f in intended)
    return 10.0 * in_scope / len(changed)


# ---------------------------------------------------------------------------
# Aggregate result
# ---------------------------------------------------------------------------

@dataclass
class ObjectiveSignals:
    """Bundle of objective sub-scores plus availability flags and raw values."""

    sub_scores: Dict[str, float] = field(default_factory=dict)
    available: Dict[str, bool] = field(default_factory=dict)
    raw: Dict[str, object] = field(default_factory=dict)

    def to_log_dict(self) -> dict:
        """Serialize for the JSON iteration log (keeps the blend auditable)."""
        return {
            "sub_scores": dict(self.sub_scores),
            "available": dict(self.available),
            "raw": dict(self.raw),
        }


def compute_objective_signals(
    *,
    tests_passed: Optional[bool] = None,
    diff: Optional[str] = None,
    intended_files: Optional[Iterable[str]] = None,
    prev_test_count: Optional[int] = None,
    curr_test_count: Optional[int] = None,
    prev_coverage_pct: Optional[float] = None,
    curr_coverage_pct: Optional[float] = None,
    prev_complexity: Optional[float] = None,
    curr_complexity: Optional[float] = None,
) -> ObjectiveSignals:
    """Compute whichever objective sub-scores the available inputs support.

    Any signal whose inputs are missing is simply omitted (``available`` is
    ``False`` for it) so the downstream blend can renormalize its weights.
    """
    signals = ObjectiveSignals()

    def _add(name: str, score: float, raw: dict) -> None:
        signals.sub_scores[name] = score
        signals.available[name] = True
        signals.raw.update(raw)

    # tests
    if tests_passed is not None:
        _add(
            "tests",
            test_score(tests_passed, prev_test_count, curr_test_count),
            {
                "tests_passed": tests_passed,
                "prev_test_count": prev_test_count,
                "curr_test_count": curr_test_count,
            },
        )
    else:
        signals.available["tests"] = False

    # coverage
    if prev_coverage_pct is not None and curr_coverage_pct is not None:
        _add(
            "coverage",
            coverage_score(prev_coverage_pct, curr_coverage_pct),
            {"prev_coverage_pct": prev_coverage_pct,
             "curr_coverage_pct": curr_coverage_pct},
        )
    else:
        signals.available["coverage"] = False

    # complexity
    if prev_complexity is not None and curr_complexity is not None:
        _add(
            "complexity",
            complexity_score(prev_complexity, curr_complexity),
            {"prev_complexity": prev_complexity,
             "curr_complexity": curr_complexity},
        )
    else:
        signals.available["complexity"] = False

    # diff size + scope (need a diff)
    if diff is not None:
        counts = diff_line_counts(diff)
        _add("diff_size", diff_size_score(counts["added"], counts["removed"]),
             {"diff_added": counts["added"], "diff_removed": counts["removed"]})

        changed = changed_files_in_diff(diff)
        if intended_files is not None:
            _add(
                "scope",
                scope_adherence_score(changed, intended_files),
                {"changed_files": sorted(changed),
                 "intended_files": sorted(set(intended_files))},
            )
        else:
            signals.available["scope"] = False
    else:
        signals.available["diff_size"] = False
        signals.available["scope"] = False

    return signals


# ---------------------------------------------------------------------------
# Optional measurement wrappers (degrade gracefully, never raise)
# ---------------------------------------------------------------------------

def measure_coverage() -> Optional[float]:
    """Return total line-coverage percent via the ``coverage`` API, or ``None``.

    Returns ``None`` (rather than raising) if ``coverage`` is not installed or
    no data file exists — that's the graceful-degradation contract: the
    coverage signal simply drops out of the blend.
    """
    try:
        import coverage  # type: ignore
    except Exception:
        return None
    try:
        cov = coverage.Coverage()
        cov.load()
        return float(cov.report(show_missing=False))
    except Exception:
        return None


def measure_complexity(paths: Iterable[str]) -> Optional[float]:
    """Return average cyclomatic complexity over *paths* via ``radon``, or ``None``.

    ``None`` when ``radon`` is unavailable or nothing parses — the complexity
    signal then drops out of the blend.
    """
    try:
        from radon.complexity import cc_visit  # type: ignore
    except Exception:
        return None
    try:
        scores = []
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
            except OSError:
                continue
            for block in cc_visit(source):
                scores.append(block.complexity)
        if not scores:
            return None
        return sum(scores) / len(scores)
    except Exception:
        return None

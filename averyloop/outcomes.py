"""Outcome derivation — the learning substrate for outcome-feedback RAG.

This module turns *what happened to a fix* into a typed :class:`Outcome` that the
outcome-memory store (``rag/outcome_memory.py``) can embed and later recall when
auditing similar code.  It is the piece that lets AveryLoop improve *within a
project over time* instead of being memoryless.

Everything here is **pure** (no LLM, no ChromaDB, no disk I/O) except the two
thin git wrappers at the bottom (:func:`git_revert_log` /
:func:`detect_reverted_merges`), which are isolated so the classification logic
is unit-testable on synthetic data.

Outcome labels and how each is derived from existing loop state:

``accepted``
    The fix was merged (``Finding.status == "merged"`` / ``FindingState.merged``)
    and has **not** been reverted afterwards.
``rejected``
    The fix was blocked *before or at* merge: a safety-gate veto
    (``safety_gate.SafetyVerdict.veto``), a reviewer ``REJECT`` /
    ``REQUEST_CHANGES``, or a pre/post-merge test failure that sent the finding
    back to ``pending`` / ``implemented``.
``reverted``
    The fix was merged but later undone — either the orchestrator's own
    post-merge auto-revert (``git reset`` when post-merge tests fail) or a human
    ``git revert`` of the merge commit, detected from git history.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

# ── Outcome labels ──────────────────────────────────────────────────────────
ACCEPTED = "accepted"
REJECTED = "rejected"
REVERTED = "reverted"
OUTCOME_LABELS = (ACCEPTED, REJECTED, REVERTED)

# Per-signal sub-score names carried in ``objective_deltas`` (mirrors
# ``signals.SIGNAL_NAMES`` so the outcome record stays auditable).
_DELTA_KEYS = ("tests", "coverage", "complexity", "diff_size", "scope")


@dataclass
class Outcome:
    """A single recorded fix outcome — the unit the outcome memory learns from.

    Attributes
    ----------
    label:
        One of :data:`OUTCOME_LABELS`.
    finding:
        Serialized finding (``Finding.to_log_dict()`` shape) — dimension, file,
        description, fix, importance, branch_name.
    diff:
        Unified diff of the change (empty for findings that never produced one).
    objective_deltas:
        Per-signal objective sub-scores for *this* change (from ``signals.py``),
        e.g. ``{"tests": 10.0, "scope": 10.0, ...}``.  Empty when unmeasured.
    reason:
        Short human-readable explanation (e.g. ``"safety-gate veto"`` or
        ``"post-merge test failure"``).
    iteration:
        The loop iteration that produced the outcome (0 if unknown).
    merge_sha:
        The merge commit SHA when the fix was merged (``""`` otherwise) — used to
        match against later revert commits.
    """

    label: str
    finding: Dict[str, object] = field(default_factory=dict)
    diff: str = ""
    objective_deltas: Dict[str, float] = field(default_factory=dict)
    reason: str = ""
    iteration: int = 0
    merge_sha: str = ""

    def __post_init__(self) -> None:
        if self.label not in OUTCOME_LABELS:
            raise ValueError(
                f"invalid outcome label {self.label!r}; "
                f"must be one of {OUTCOME_LABELS}"
            )

    # -- serialization -------------------------------------------------------
    @property
    def branch_name(self) -> str:
        """Branch name of the underlying finding (``""`` if absent)."""
        return str(self.finding.get("branch_name", ""))

    @property
    def file(self) -> str:
        """File path the finding touched (``""`` if absent)."""
        return str(self.finding.get("file", ""))

    def embed_text(self) -> str:
        """Build the document text embedded into the outcome-memory vector store.

        Combines the finding's natural-language description + intended fix with
        the file path and the diff, so retrieval keys on *similar code and
        intent* (matching how ``rag/chunker.py`` keys chunks by ``file::symbol``).
        """
        f = self.finding
        parts = [
            f"file: {f.get('file', '')}",
            f"dimension: {f.get('dimension', '')}",
            f"description: {f.get('description', '')}",
            f"fix: {f.get('fix', '')}",
        ]
        if self.diff:
            parts.append("diff:\n" + self.diff)
        return "\n".join(parts)

    def to_metadata(self) -> Dict[str, object]:
        """Flatten to ChromaDB-safe scalar metadata (no nested dicts/lists)."""
        f = self.finding
        meta: Dict[str, object] = {
            "label": self.label,
            "file": str(f.get("file", "")),
            "dimension": str(f.get("dimension", "")),
            "importance": int(f.get("importance", 0) or 0),
            "branch_name": str(f.get("branch_name", "")),
            "iteration": int(self.iteration),
            "reason": self.reason,
            "merge_sha": self.merge_sha,
        }
        for key in _DELTA_KEYS:
            if key in self.objective_deltas:
                meta[f"delta_{key}"] = float(self.objective_deltas[key])
        return meta

    def to_log_dict(self) -> dict:
        """Full serializable form (for tests / JSON dumps)."""
        return {
            "label": self.label,
            "finding": dict(self.finding),
            "diff": self.diff,
            "objective_deltas": dict(self.objective_deltas),
            "reason": self.reason,
            "iteration": self.iteration,
            "merge_sha": self.merge_sha,
        }


# ---------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------

def classify_outcome(
    *,
    merged: bool,
    reverted: bool = False,
    safety_veto: bool = False,
    review_verdict: Optional[str] = None,
    tests_passed: Optional[bool] = None,
) -> str:
    """Map the in-loop signals for a single finding to an outcome label.

    Precedence: a merged-then-reverted fix is ``reverted``; a merge that still
    stands is ``accepted``; anything that never landed (veto, reject,
    request-changes, test failure) is ``rejected``.
    """
    if reverted:
        return REVERTED
    if merged:
        return ACCEPTED
    return REJECTED


def _classification_reason(
    label: str,
    *,
    safety_veto: bool,
    review_verdict: Optional[str],
    tests_passed: Optional[bool],
) -> str:
    """Build a short human-readable reason for a classification."""
    if label == REVERTED:
        return "merged then reverted"
    if label == ACCEPTED:
        return "merged and not reverted"
    # rejected — pick the most specific cause available.
    if safety_veto:
        return "safety-gate veto"
    if review_verdict in ("REJECT", "REQUEST_CHANGES"):
        return f"reviewer {review_verdict}"
    if tests_passed is False:
        return "test failure"
    return "not merged"


def objective_deltas_from_signals(signals) -> Dict[str, float]:
    """Extract the available per-signal sub-scores from an ``ObjectiveSignals``.

    Returns an empty dict for ``None`` (nothing measured), so the outcome record
    degrades gracefully.
    """
    if signals is None:
        return {}
    sub = getattr(signals, "sub_scores", {}) or {}
    avail = getattr(signals, "available", {}) or {}
    return {
        name: float(sub[name])
        for name in _DELTA_KEYS
        if avail.get(name) and name in sub
    }


def derive_outcome(
    finding_dict: Dict[str, object],
    *,
    merged: bool,
    reverted: bool = False,
    safety_veto: bool = False,
    review_verdict: Optional[str] = None,
    tests_passed: Optional[bool] = None,
    diff: str = "",
    signals=None,
    iteration: int = 0,
    merge_sha: str = "",
) -> Outcome:
    """Assemble an :class:`Outcome` from a finding plus its loop-result signals.

    Pure: ``signals`` (an ``ObjectiveSignals`` or ``None``) and ``reverted`` are
    passed in already-computed, so this never touches git or a DB.
    """
    label = classify_outcome(
        merged=merged,
        reverted=reverted,
        safety_veto=safety_veto,
        review_verdict=review_verdict,
        tests_passed=tests_passed,
    )
    reason = _classification_reason(
        label,
        safety_veto=safety_veto,
        review_verdict=review_verdict,
        tests_passed=tests_passed,
    )
    return Outcome(
        label=label,
        finding=dict(finding_dict),
        diff=diff,
        objective_deltas=objective_deltas_from_signals(signals),
        reason=reason,
        iteration=iteration,
        merge_sha=merge_sha,
    )


# ---------------------------------------------------------------------------
# Revert detection (post-merge)
# ---------------------------------------------------------------------------

# Git's own revert message body: "This reverts commit <40-hex>."
_REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.IGNORECASE)


def reverted_shas_in_log(git_log_text: str) -> Set[str]:
    """Return the set of commit SHAs marked reverted in *git_log_text*.

    Scans for git's standard ``This reverts commit <sha>`` body line, so it works
    on the output of ``git log --format=%H%n%B`` (or any text containing those
    lines).  Pure — no subprocess.
    """
    return {m.group(1).lower() for m in _REVERTS_RE.finditer(git_log_text or "")}


def is_merge_reverted(merge_sha: str, git_log_text: str) -> bool:
    """True if *merge_sha* appears as a reverted commit in *git_log_text*.

    Tolerant of abbreviated SHAs on either side (git often abbreviates in the
    "This reverts commit" line), so a 7-char and a 40-char form still match.
    """
    if not merge_sha:
        return False
    target = merge_sha.lower()
    for sha in reverted_shas_in_log(git_log_text):
        if target.startswith(sha) or sha.startswith(target):
            return True
    return False


# -- thin git wrappers (isolated impurity) ----------------------------------

def git_revert_log(repo_root: Optional[str] = None, max_count: int = 500) -> str:
    """Return ``git log --format=%H%n%B`` text, or ``""`` on any failure.

    Isolated so :func:`reverted_shas_in_log` / :func:`is_merge_reverted` stay
    pure and testable; callers that want live detection compose the two.
    """
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={max_count}", "--format=%H%n%B"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return ""
    return result.stdout or ""


def detect_reverted_merges(
    merge_shas: List[str], repo_root: Optional[str] = None
) -> Set[str]:
    """Return the subset of *merge_shas* that have been reverted in git history.

    Convenience wrapper: reads the log once, then classifies each SHA purely.
    """
    log_text = git_revert_log(repo_root)
    return {sha for sha in merge_shas if is_merge_reverted(sha, log_text)}

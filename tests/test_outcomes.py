"""Unit tests for outcome derivation and revert detection (no live LLM/DB)."""

from __future__ import annotations

import subprocess

import pytest

from averyloop import outcomes
from averyloop.outcomes import (
    ACCEPTED,
    REJECTED,
    REVERTED,
    Outcome,
    classify_outcome,
    derive_outcome,
    detect_reverted_merges,
    is_merge_reverted,
    reverted_shas_in_log,
)


def _finding(**over) -> dict:
    base = {
        "dimension": "correctness",
        "file": "calc.py",
        "function_name": "f",
        "description": "off-by-one",
        "fix": "use n+1",
        "importance": 7,
        "branch_name": "improvement/fix-off-by-one",
    }
    base.update(over)
    return base


# ── classification ──────────────────────────────────────────────────────────

class TestClassification:
    def test_accepted_when_merged_and_not_reverted(self):
        assert classify_outcome(merged=True) == ACCEPTED

    def test_reverted_takes_precedence_over_merged(self):
        assert classify_outcome(merged=True, reverted=True) == REVERTED

    def test_rejected_on_safety_veto(self):
        assert classify_outcome(merged=False, safety_veto=True) == REJECTED

    def test_rejected_on_review_reject(self):
        assert classify_outcome(merged=False, review_verdict="REJECT") == REJECTED

    def test_rejected_on_test_failure(self):
        assert classify_outcome(merged=False, tests_passed=False) == REJECTED


class TestDeriveOutcome:
    def test_accepted_outcome_fields(self):
        o = derive_outcome(_finding(), merged=True, diff="+++ b/calc.py\n+x",
                            iteration=3, merge_sha="deadbeef")
        assert o.label == ACCEPTED
        assert o.reason == "merged and not reverted"
        assert o.file == "calc.py"
        assert o.branch_name == "improvement/fix-off-by-one"
        assert o.iteration == 3
        assert o.merge_sha == "deadbeef"

    def test_rejected_reason_prefers_safety_veto(self):
        o = derive_outcome(_finding(), merged=False, safety_veto=True,
                           review_verdict="APPROVE", diff="d")
        assert o.label == REJECTED
        assert o.reason == "safety-gate veto"

    def test_reverted_outcome(self):
        o = derive_outcome(_finding(), merged=False, reverted=True, diff="d")
        assert o.label == REVERTED
        assert o.reason == "merged then reverted"

    def test_objective_deltas_extracted_from_signals(self):
        from averyloop.signals import compute_objective_signals
        sig = compute_objective_signals(
            tests_passed=True, diff="+++ b/calc.py\n+x", intended_files={"calc.py"}
        )
        o = derive_outcome(_finding(), merged=True, signals=sig, diff="d")
        assert o.objective_deltas["tests"] == 10.0
        assert o.objective_deltas["scope"] == 10.0

    def test_no_signals_gives_empty_deltas(self):
        o = derive_outcome(_finding(), merged=True, signals=None, diff="d")
        assert o.objective_deltas == {}

    def test_invalid_label_raises(self):
        with pytest.raises(ValueError):
            Outcome(label="bogus")

    def test_metadata_is_chroma_safe_scalars(self):
        from averyloop.signals import compute_objective_signals
        sig = compute_objective_signals(
            tests_passed=True, diff="+++ b/calc.py\n+x", intended_files={"calc.py"}
        )
        meta = derive_outcome(_finding(), merged=True, signals=sig,
                              diff="d").to_metadata()
        assert all(isinstance(v, (str, int, float, bool)) for v in meta.values())
        assert meta["label"] == ACCEPTED
        assert meta["delta_tests"] == 10.0


# ── revert detection ────────────────────────────────────────────────────────

class TestRevertDetection:
    LOG = (
        "111111aaaa\nRevert \"improve cache\"\n\n"
        "This reverts commit abc123def4567890.\n\n"
        "222222bbbb\nimprove cache\n\n"
    )

    def test_reverted_shas_scanned(self):
        assert reverted_shas_in_log(self.LOG) == {"abc123def4567890"}

    def test_is_merge_reverted_full_sha(self):
        assert is_merge_reverted("abc123def4567890", self.LOG) is True

    def test_is_merge_reverted_abbreviated_either_side(self):
        # log abbreviates to a prefix of the full merge sha
        log = "x\nThis reverts commit abc123d.\n"
        assert is_merge_reverted("abc123def4567890", log) is True

    def test_unreverted_sha_is_false(self):
        assert is_merge_reverted("999999ffff", self.LOG) is False

    def test_empty_sha_is_false(self):
        assert is_merge_reverted("", self.LOG) is False

    def test_no_revert_lines(self):
        assert reverted_shas_in_log("just a normal commit message") == set()


class TestDetectRevertedMergesLiveGit:
    """Integration: a real crafted revert commit is detected from git history."""

    def _git(self, repo, *args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                              text=True, check=True)

    def test_revert_commit_detected(self, tmp_path):
        repo = tmp_path
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.email", "t@t.t")
        self._git(repo, "config", "user.name", "t")
        self._git(repo, "config", "commit.gpgsign", "false")
        (repo / "a.txt").write_text("one\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "base")
        (repo / "a.txt").write_text("two\n")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-qm", "change")
        target = self._git(repo, "rev-parse", "HEAD").stdout.strip()
        self._git(repo, "revert", "--no-edit", target)

        detected = detect_reverted_merges([target], repo_root=str(repo))
        assert target in detected

        # An unrelated sha is not reported.
        assert detect_reverted_merges(["0" * 40], repo_root=str(repo)) == set()

"""Tests for loop_tracker branch-state derivation and management."""

import json
import pytest
from unittest.mock import patch

from improvement_loop.evaluator import Finding
from improvement_loop import loop_tracker


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_finding(
    branch_name: str = "improvement/fix-foo",
    status: str | None = None,
    importance: int = 5,
    dimension: str = "correctness",
) -> Finding:
    return Finding(
        dimension=dimension,
        file="src/module.py",
        description="test finding",
        fix="do the fix",
        importance=importance,
        branch_name=branch_name,
        status=status,
    )


FAKE_SCORES = {
    "specificity": 7.0,
    "accuracy": 7.0,
    "coverage": 7.0,
    "prioritization": 7.0,
    "domain_appropriateness": 7.0,
    "overall": 7.0,
    "flags": [],
    "reasoning": "test",
}


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    """Point loop_tracker at a temp log file for every test."""
    log_file = str(tmp_path / "test_log.json")
    monkeypatch.setattr(loop_tracker, "LOG_FILE", log_file)


# ── log_iteration derives branches ──────────────────────────────────────────

@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_log_iteration_derives_branches_created(mock_score):
    findings = [
        _make_finding("improvement/branch-a", status="pending"),
        _make_finding("improvement/branch-b", status="implemented"),
    ]
    entry = loop_tracker.log_iteration(
        audit_output="test audit",
        findings=findings,
        tests_passed=True,
    )
    assert entry["branches_created"] == [
        "improvement/branch-a",
        "improvement/branch-b",
    ]


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_log_iteration_derives_branches_merged(mock_score):
    findings = [
        _make_finding("improvement/branch-a", status="merged"),
        _make_finding("improvement/branch-b", status="pending"),
        _make_finding("improvement/branch-c", status="merged"),
    ]
    entry = loop_tracker.log_iteration(
        audit_output="test audit",
        findings=findings,
        tests_passed=True,
    )
    assert entry["branches_merged"] == [
        "improvement/branch-a",
        "improvement/branch-c",
    ]


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_log_iteration_no_merged_findings(mock_score):
    findings = [
        _make_finding("improvement/branch-x", status="pending"),
    ]
    entry = loop_tracker.log_iteration(
        audit_output="test audit",
        findings=findings,
        tests_passed=True,
    )
    assert entry["branches_created"] == ["improvement/branch-x"]
    assert entry["branches_merged"] == []


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_log_iteration_all_merged(mock_score):
    findings = [
        _make_finding("improvement/branch-a", status="merged"),
        _make_finding("improvement/branch-b", status="merged"),
    ]
    entry = loop_tracker.log_iteration(
        audit_output="test audit",
        findings=findings,
        tests_passed=True,
    )
    assert entry["branches_created"] == [
        "improvement/branch-a",
        "improvement/branch-b",
    ]
    assert entry["branches_merged"] == [
        "improvement/branch-a",
        "improvement/branch-b",
    ]


# ── get_pending_branches ────────────────────────────────────────────────────

@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_get_pending_branches_mixed_statuses(mock_score):
    findings = [
        _make_finding("improvement/merged-one", status="merged"),
        _make_finding("improvement/pending-one", status="pending"),
        _make_finding("improvement/impl-one", status="implemented"),
    ]
    loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    pending = loop_tracker.get_pending_branches()
    assert pending == ["improvement/pending-one", "improvement/impl-one"]


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_get_pending_branches_all_merged(mock_score):
    findings = [
        _make_finding("improvement/done-a", status="merged"),
        _make_finding("improvement/done-b", status="merged"),
    ]
    loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    assert loop_tracker.get_pending_branches() == []


def test_get_pending_branches_empty_log():
    assert loop_tracker.get_pending_branches() == []


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_get_pending_branches_uses_latest_iteration(mock_score):
    findings1 = [_make_finding("improvement/old-pending", status="pending")]
    loop_tracker.log_iteration(
        audit_output="test", findings=findings1, tests_passed=True
    )
    findings2 = [_make_finding("improvement/new-pending", status="pending")]
    loop_tracker.log_iteration(
        audit_output="test", findings=findings2, tests_passed=True
    )
    pending = loop_tracker.get_pending_branches()
    assert pending == ["improvement/new-pending"]


# ── mark_finding_merged ─────────────────────────────────────────────────────

@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_mark_finding_merged_updates_status(mock_score):
    findings = [
        _make_finding("improvement/to-merge", status="pending"),
    ]
    entry = loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    finding_id = entry["findings"][0]["id"]

    loop_tracker.mark_finding_merged(iteration=1, finding_id=finding_id)

    log = loop_tracker.load_log()
    assert log[0]["findings"][0]["status"] == "merged"
    assert "improvement/to-merge" in log[0]["branches_merged"]


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_mark_finding_merged_updates_branches_merged_list(mock_score):
    findings = [
        _make_finding("improvement/br-a", status="merged"),
        _make_finding("improvement/br-b", status="pending"),
    ]
    entry = loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    assert entry["branches_merged"] == ["improvement/br-a"]

    finding_id_b = entry["findings"][1]["id"]
    loop_tracker.mark_finding_merged(iteration=1, finding_id=finding_id_b)

    log = loop_tracker.load_log()
    assert log[0]["branches_merged"] == [
        "improvement/br-a",
        "improvement/br-b",
    ]


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_mark_finding_merged_invalid_id_raises(mock_score):
    findings = [_make_finding("improvement/br-x", status="pending")]
    loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    with pytest.raises(ValueError, match="not found"):
        loop_tracker.mark_finding_merged(iteration=1, finding_id="nonexistent")


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_mark_finding_merged_wrong_iteration_raises(mock_score):
    findings = [_make_finding("improvement/br-y", status="pending")]
    entry = loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    finding_id = entry["findings"][0]["id"]
    with pytest.raises(ValueError, match="not found"):
        loop_tracker.mark_finding_merged(iteration=99, finding_id=finding_id)


# ── get_unmerged_findings ────────────────────────────────────────────────────

@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_get_unmerged_findings_returns_non_merged(mock_score):
    findings = [
        _make_finding("improvement/uf-a", status="merged"),
        _make_finding("improvement/uf-b", status="pending"),
        _make_finding("improvement/uf-c", status="implemented"),
    ]
    loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    unmerged = loop_tracker.get_unmerged_findings(iteration=1)
    assert len(unmerged) == 2
    branch_names = [f["branch_name"] for f in unmerged]
    assert "improvement/uf-b" in branch_names
    assert "improvement/uf-c" in branch_names


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_get_unmerged_findings_all_merged(mock_score):
    findings = [
        _make_finding("improvement/all-merged-a", status="merged"),
        _make_finding("improvement/all-merged-b", status="merged"),
    ]
    loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    assert loop_tracker.get_unmerged_findings(iteration=1) == []


def test_get_unmerged_findings_nonexistent_iteration():
    assert loop_tracker.get_unmerged_findings(iteration=42) == []


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_get_unmerged_findings_after_mark_merged(mock_score):
    findings = [
        _make_finding("improvement/mark-a", status="pending"),
        _make_finding("improvement/mark-b", status="pending"),
    ]
    entry = loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    assert len(loop_tracker.get_unmerged_findings(iteration=1)) == 2

    loop_tracker.mark_finding_merged(
        iteration=1, finding_id=entry["findings"][0]["id"]
    )
    unmerged = loop_tracker.get_unmerged_findings(iteration=1)
    assert len(unmerged) == 1
    assert unmerged[0]["branch_name"] == "improvement/mark-b"


# ── _print_iteration_summary shows unmerged ─────────────────────────────────

@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_summary_shows_unmerged_branches(mock_score, capsys):
    findings = [
        _make_finding("improvement/unmerged-show", status="pending"),
        _make_finding("improvement/merged-show", status="merged"),
    ]
    loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    captured = capsys.readouterr().out
    assert "Unmerged branches: 1" in captured
    assert "improvement/unmerged-show" in captured


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_should_continue_loop_on_evaluation_failed(mock_score):
    from improvement_loop.evaluator import should_continue_loop

    failed_scores = {
        "specificity": 5.0,
        "accuracy": 5.0,
        "coverage": 8.0,
        "prioritization": 5.0,
        "domain_appropriateness": 5.0,
        "overall": 5.0,
        "flags": ["EVALUATION_FAILED"],
        "reasoning": "Fallback scores — evaluator failed after max retries.",
    }
    assert should_continue_loop(failed_scores, findings=[]) is True


@patch("improvement_loop.loop_tracker.score_audit", return_value=FAKE_SCORES)
def test_summary_no_unmerged_section_when_all_merged(mock_score, capsys):
    findings = [
        _make_finding("improvement/all-m-a", status="merged"),
        _make_finding("improvement/all-m-b", status="merged"),
    ]
    loop_tracker.log_iteration(
        audit_output="test", findings=findings, tests_passed=True
    )
    captured = capsys.readouterr().out
    assert "Unmerged branches:" not in captured

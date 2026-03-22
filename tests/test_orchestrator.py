"""Tests for orchestrator_v2.run_loop exit-condition logic."""

import pytest
from unittest.mock import patch

from improvement_loop import loop_tracker
from improvement_loop import orchestrator_v2


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    """Point loop_tracker at a temp log file for every test."""
    log_file = str(tmp_path / "test_log.json")
    monkeypatch.setattr(loop_tracker, "LOG_FILE", log_file)


# ── Exit on first iteration ─────────────────────────────────────────────────

def test_run_loop_stops_on_first_exit_condition():
    """If log_iteration returns exit_condition_met=True on the first call,
    the loop must execute exactly one iteration."""
    exit_entry = {
        "iteration": 1,
        "exit_condition_met": True,
        "audit_scores": {"overall": 9.0, "flags": []},
        "findings": [],
        "findings_count": 0,
        "high_priority_findings": 0,
        "branches_created": [],
        "branches_merged": [],
        "tests_passed": True,
    }

    with patch.object(
        loop_tracker, "log_iteration", return_value=exit_entry
    ) as mock_log:
        entries = orchestrator_v2.run_loop(max_iterations=10, dry_run=True)

    assert mock_log.call_count == 1
    assert len(entries) == 1
    assert entries[0]["exit_condition_met"] is True


# ── Exit on third iteration ──────────────────────────────────────────────────

def test_run_loop_stops_after_third_iteration():
    """If log_iteration returns exit_condition_met=False twice then True,
    the loop must execute exactly three iterations."""
    continuing_entry = {
        "iteration": 1,
        "exit_condition_met": False,
        "audit_scores": {"overall": 5.0, "flags": []},
        "findings": [],
        "findings_count": 0,
        "high_priority_findings": 0,
        "branches_created": [],
        "branches_merged": [],
        "tests_passed": True,
    }
    exit_entry = {
        **continuing_entry,
        "iteration": 3,
        "exit_condition_met": True,
        "audit_scores": {"overall": 9.0, "flags": []},
    }

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return {**continuing_entry, "iteration": call_count}
        return exit_entry

    with patch.object(
        loop_tracker, "log_iteration", side_effect=side_effect
    ) as mock_log:
        entries = orchestrator_v2.run_loop(max_iterations=10, dry_run=True)

    assert mock_log.call_count == 3
    assert len(entries) == 3
    assert entries[-1]["exit_condition_met"] is True
    assert entries[0]["exit_condition_met"] is False

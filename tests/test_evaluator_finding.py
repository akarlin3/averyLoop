"""Tests for the Finding Pydantic model in evaluator.py."""

import pytest
from pydantic import ValidationError

from improvement_loop.evaluator import (
    Finding, should_continue_loop, check_diminishing_returns,
)
from improvement_loop import loop_tracker
from improvement_loop.loop_config import (
    LoopConfig, load_loop_config, get_config, reset_config,
)
from improvement_loop.project_config import ProjectConfig


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_finding(**overrides) -> Finding:
    """Build a valid Finding, optionally overriding fields."""
    defaults = {
        "dimension": "correctness",
        "file": "src/core/module.py",
        "function_name": "process_data",
        "description": "Missing bounds check on input array",
        "fix": "Add length validation before processing",
        "importance": 5,
        "branch_name": "improvement/fix-bounds-check",
        "status": "pending",
    }
    defaults.update(overrides)
    return Finding(**defaults)


# ── Valid construction ───────────────────────────────────────────────────────

class TestFindingConstruction:
    def test_valid_finding(self):
        f = _make_finding()
        assert f.dimension == "correctness"
        assert f.file == "src/core/module.py"
        assert f.function_name == "process_data"
        assert f.importance == 5
        assert f.branch_name == "improvement/fix-bounds-check"
        assert f.status == "pending"

    def test_optional_fields_default_none(self):
        f = _make_finding(function_name=None, status=None)
        assert f.function_name is None
        assert f.status is None

    def test_all_valid_dimensions(self):
        dims = [
            "performance", "correctness", "error_handling", "modularity",
            "memory", "code_quality", "test_coverage", "security",
            "cross_platform",
        ]
        for dim in dims:
            f = _make_finding(dimension=dim)
            assert f.dimension == dim

    def test_all_valid_statuses(self):
        for status in ("pending", "implemented", "merged", None):
            f = _make_finding(status=status)
            assert f.status == status

    def test_importance_boundaries(self):
        assert _make_finding(importance=1).importance == 1
        assert _make_finding(importance=10).importance == 10


# ── Invalid dimension ────────────────────────────────────────────────────────

class TestInvalidDimension:
    def test_invalid_dimension_raises(self):
        with pytest.raises(ValidationError):
            _make_finding(dimension="style")

    def test_empty_dimension_raises(self):
        with pytest.raises(ValidationError):
            _make_finding(dimension="")


# ── Invalid importance ───────────────────────────────────────────────────────

class TestInvalidImportance:
    def test_importance_zero_raises(self):
        with pytest.raises(ValidationError, match="importance"):
            _make_finding(importance=0)

    def test_importance_eleven_raises(self):
        with pytest.raises(ValidationError, match="importance"):
            _make_finding(importance=11)

    def test_negative_importance_raises(self):
        with pytest.raises(ValidationError):
            _make_finding(importance=-1)


# ── Invalid branch_name ──────────────────────────────────────────────────────

class TestInvalidBranchName:
    def test_spaces_raises(self):
        with pytest.raises(ValidationError, match="spaces"):
            _make_finding(branch_name="improvement/fix bounds check")

    def test_double_slash_raises(self):
        with pytest.raises(ValidationError, match="slashes"):
            _make_finding(branch_name="improvement/fix//check")

    def test_extra_slash_raises(self):
        with pytest.raises(ValidationError, match="slashes"):
            _make_finding(branch_name="improvement/fix/check")

    def test_missing_prefix_raises(self):
        with pytest.raises(ValidationError, match="improvement/"):
            _make_finding(branch_name="fix-bounds-check")

    def test_empty_slug_raises(self):
        with pytest.raises(ValidationError, match="empty"):
            _make_finding(branch_name="improvement/")

    def test_slug_too_long_raises(self):
        with pytest.raises(ValidationError, match="50"):
            _make_finding(branch_name="improvement/" + "a" * 51)

    def test_tilde_raises(self):
        with pytest.raises(ValidationError, match="invalid"):
            _make_finding(branch_name="improvement/fix~check")

    def test_caret_raises(self):
        with pytest.raises(ValidationError, match="invalid"):
            _make_finding(branch_name="improvement/fix^check")


# ── to_log_dict ──────────────────────────────────────────────────────────────

class TestToLogDict:
    def test_output_matches_expected_format(self):
        f = _make_finding()
        d = f.to_log_dict()
        assert d == {
            "dimension": "correctness",
            "file": "src/core/module.py",
            "function_name": "process_data",
            "description": "Missing bounds check on input array",
            "fix": "Add length validation before processing",
            "importance": 5,
            "branch_name": "improvement/fix-bounds-check",
            "status": "pending",
        }

    def test_omits_none_function_name(self):
        f = _make_finding(function_name=None, status=None)
        d = f.to_log_dict()
        assert "function_name" not in d
        assert "status" not in d

    def test_includes_status_when_set(self):
        f = _make_finding(status="merged")
        d = f.to_log_dict()
        assert d["status"] == "merged"

    def test_dict_is_plain_dict(self):
        """Ensure to_log_dict returns a plain dict, not a Pydantic object."""
        d = _make_finding().to_log_dict()
        assert type(d) is dict


# ── should_continue_loop ─────────────────────────────────────────────────────

class TestShouldContinueLoop:
    GOOD_SCORES = {
        "specificity": 8, "accuracy": 8, "coverage": 8,
        "prioritization": 8, "domain_appropriateness": 8,
        "overall": 8, "flags": [], "reasoning": "Good."
    }
    LOW_COVERAGE_SCORES = {
        "specificity": 8, "accuracy": 8, "coverage": 4,
        "prioritization": 8, "domain_appropriateness": 8,
        "overall": 7, "flags": [], "reasoning": "Low coverage."
    }

    def test_returns_true_when_high_importance_finding(self):
        findings = [_make_finding(importance=2)]
        assert should_continue_loop(self.GOOD_SCORES, findings) is True

    def test_returns_false_when_all_below_threshold(self):
        findings = [_make_finding(importance=1)]
        assert should_continue_loop(self.GOOD_SCORES, findings) is False

    def test_returns_false_with_empty_findings(self):
        assert should_continue_loop(self.GOOD_SCORES, []) is False

    def test_returns_true_when_coverage_low(self):
        findings = [_make_finding(importance=1)]
        assert should_continue_loop(self.LOW_COVERAGE_SCORES, findings) is True

    def test_importance_exactly_two_continues(self):
        findings = [_make_finding(importance=2)]
        assert should_continue_loop(self.GOOD_SCORES, findings) is True

    def test_importance_exactly_one_stops(self):
        findings = [_make_finding(importance=1)]
        assert should_continue_loop(self.GOOD_SCORES, findings) is False

    def test_returns_true_when_critical_flags_present(self):
        flagged_scores = {**self.GOOD_SCORES, "flags": ["LEAKAGE_RISK"]}
        assert should_continue_loop(flagged_scores, []) is True

    def test_evaluation_failed_flag_forces_continuation(self):
        failed_scores = {**self.GOOD_SCORES, "flags": ["EVALUATION_FAILED"]}
        assert should_continue_loop(failed_scores, []) is True


# ── check_diminishing_returns ───────────────────────────────────────────────

def _make_log_entry(
    iteration: int,
    branches_created: int = 5,
    branches_merged: int = 0,
    importance: int = 2,
    files: list | None = None,
    overall_score: float = 6.0,
) -> dict:
    """Build a synthetic log entry for diminishing-returns tests."""
    if files is None:
        files = ["src/core/module.py"]
    findings = [
        {
            "id": f"iter{iteration}_{i+1:03d}",
            "iteration": iteration,
            "dimension": "correctness",
            "file": f,
            "description": "test finding",
            "fix": "test fix",
            "importance": importance,
            "branch_name": f"improvement/fix-{iteration}-{i}",
            "status": "merged" if i < branches_merged else "pending",
        }
        for i, f in enumerate(files)
    ]
    created = [f"improvement/branch-{iteration}-{j}" for j in range(branches_created)]
    merged = created[:branches_merged]
    return {
        "iteration": iteration,
        "timestamp": "2026-03-20T00:00:00",
        "audit_scores": {"overall": overall_score, "flags": []},
        "findings": findings,
        "findings_count": len(findings),
        "high_priority_findings": 0,
        "branches_created": created,
        "branches_merged": merged,
        "tests_passed": True,
        "exit_condition_met": False,
    }


class TestCheckDiminishingReturns:
    """Tests for the check_diminishing_returns helper."""

    def _build_stale_log(self, **overrides) -> list:
        defaults = dict(
            branches_created=5, branches_merged=0, importance=2,
            files=["src/core/module.py"], overall_score=6.0,
        )
        defaults.update(overrides)
        return [_make_log_entry(iteration=i, **defaults) for i in range(1, 5)]

    def test_all_conditions_met_returns_true(self):
        log = self._build_stale_log()
        assert check_diminishing_returns(log) is True

    def test_merge_rate_above_threshold_returns_false(self):
        log = self._build_stale_log()
        log[2] = _make_log_entry(
            iteration=3, branches_created=5, branches_merged=1,
            importance=2, files=["src/core/module.py"], overall_score=6.0,
        )
        assert check_diminishing_returns(log) is False

    def test_avg_importance_above_threshold_returns_false(self):
        log = self._build_stale_log(importance=4)
        assert check_diminishing_returns(log) is False

    def test_fewer_than_four_iterations_skipped(self):
        log = self._build_stale_log()[:3]
        assert check_diminishing_returns(log) is False

    def test_high_audit_score_returns_false(self):
        log = self._build_stale_log()
        log[1] = _make_log_entry(
            iteration=2, branches_created=5, branches_merged=0,
            importance=2, files=["src/core/module.py"], overall_score=9.0,
        )
        assert check_diminishing_returns(log) is False


class TestShouldContinueLoopDiminishingReturns:

    GOOD_SCORES = {
        "specificity": 8, "accuracy": 8, "coverage": 8,
        "prioritization": 8, "domain_appropriateness": 8,
        "overall": 8, "flags": [], "reasoning": "Good.",
    }

    def _build_stale_log(self) -> list:
        return [
            _make_log_entry(iteration=i, branches_created=5, branches_merged=0,
                            importance=2, files=["src/core/module.py"],
                            overall_score=6.0)
            for i in range(1, 5)
        ]

    def test_diminishing_returns_stops_loop(self, tmp_path, monkeypatch):
        import json
        log_file = str(tmp_path / "test_log.json")
        monkeypatch.setattr(loop_tracker, "LOG_FILE", log_file)
        with open(log_file, "w") as f:
            json.dump(self._build_stale_log(), f)

        findings = [_make_finding(importance=1)]
        assert should_continue_loop(self.GOOD_SCORES, findings) is False

    def test_merge_rate_above_threshold_continues(self, tmp_path, monkeypatch):
        import json
        log_file = str(tmp_path / "test_log.json")
        monkeypatch.setattr(loop_tracker, "LOG_FILE", log_file)
        log = self._build_stale_log()
        log[2] = _make_log_entry(
            iteration=3, branches_created=5, branches_merged=1,
            importance=2, files=["src/core/module.py"], overall_score=6.0,
        )
        with open(log_file, "w") as f:
            json.dump(log, f)

        assert check_diminishing_returns(log) is False


# ── LoopConfig ──────────────────────────────────────────────────────────────

class TestLoopConfig:

    def test_defaults_without_file(self, tmp_path):
        cfg = load_loop_config(str(tmp_path / "nonexistent.json"))
        assert cfg.exit_strategy == "both"
        assert cfg.dr_window == 4
        assert cfg.dr_max_merge_rate == 0.15
        assert cfg.anthropic_api_key == ""
        assert cfg.audit_model == "claude-opus-4-6"

    def test_partial_override(self, tmp_path):
        import json
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps({"exit_strategy": "classic", "dr_window": 6}))
        cfg = load_loop_config(str(p))
        assert cfg.exit_strategy == "classic"
        assert cfg.dr_window == 6
        assert cfg.dr_max_merge_rate == 0.15
        assert cfg.importance_threshold == 2

    def test_unknown_keys_ignored(self, tmp_path):
        import json
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps({"bogus_key": 999, "exit_strategy": "diminishing_returns"}))
        cfg = load_loop_config(str(p))
        assert cfg.exit_strategy == "diminishing_returns"
        assert not hasattr(cfg, "bogus_key")

    def test_api_key_from_config(self, tmp_path):
        import json
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps({"anthropic_api_key": "sk-test-123"}))
        cfg = load_loop_config(str(p))
        assert cfg.anthropic_api_key == "sk-test-123"

    def test_classic_exit_strategy_skips_diminishing_returns(self, tmp_path, monkeypatch):
        import json
        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text(json.dumps({"exit_strategy": "classic"}))
        from improvement_loop import loop_config
        monkeypatch.setattr(loop_config, "CONFIG_PATH", str(cfg_path))

        log_file = str(tmp_path / "test_log.json")
        monkeypatch.setattr(loop_tracker, "LOG_FILE", log_file)
        log = [
            _make_log_entry(iteration=i, branches_created=5, branches_merged=0,
                            importance=2, files=["src/core/module.py"],
                            overall_score=6.0)
            for i in range(1, 5)
        ]
        with open(log_file, "w") as f:
            json.dump(log, f)

        good_scores = {
            "specificity": 8, "accuracy": 8, "coverage": 8,
            "prioritization": 8, "domain_appropriateness": 8,
            "overall": 8, "flags": [], "reasoning": "Good.",
        }
        findings = [_make_finding(importance=1)]
        result = should_continue_loop(good_scores, findings)
        assert result is False

    def test_diminishing_returns_only_strategy(self, tmp_path, monkeypatch):
        import json
        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text(json.dumps({"exit_strategy": "diminishing_returns"}))
        from improvement_loop import loop_config
        monkeypatch.setattr(loop_config, "CONFIG_PATH", str(cfg_path))

        log_file = str(tmp_path / "test_log.json")
        monkeypatch.setattr(loop_tracker, "LOG_FILE", log_file)
        with open(log_file, "w") as f:
            json.dump([], f)

        good_scores = {
            "specificity": 8, "accuracy": 8, "coverage": 8,
            "prioritization": 8, "domain_appropriateness": 8,
            "overall": 8, "flags": [], "reasoning": "Good.",
        }
        findings = [_make_finding(importance=5)]
        result = should_continue_loop(good_scores, findings)
        assert result is False

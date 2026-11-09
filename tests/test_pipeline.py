"""End-to-end state-machine and RAG pipeline unit tests for AveryLoop.

Uses a temporary mock Git repository and mocked LLM responses to deterministically
verify the Audit -> Implement -> Self-Review -> Gated Merge state transitions,
dry-run sandboxing, worktree isolation, and provider-agnostic configuration.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from averyloop import cli, git_utils, loop_tracker, orchestrator_v2
from averyloop.agents import _api
from averyloop.loop_config import LoopConfig, load_dotenv_file, load_loop_config
from averyloop.multi import repo_session
from averyloop.project_config import ProjectConfig, load_project_config


# ---------------------------------------------------------------------------
# Fixtures: Temporary Mock Git Repository
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def mock_git_repo(tmp_path: Path) -> Path:
    """Create a real temporary Git repository with an initial commit on `main`."""
    repo = tmp_path / "target-repo"
    repo.mkdir()

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@averyloop.local")
    _git(repo, "config", "user.name", "AveryLoop Test")

    src_dir = repo / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    (src_dir / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a - b  # seeded bug\n",
        encoding="utf-8",
    )

    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calculator.py").write_text(
        "from src.calculator import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    (repo / "project_config.yaml").write_text(
        "name: mock-target-repo\n"
        "default_branch: main\n"
        "branch_prefix: improvement/\n"
        "source_dirs: ['src/']\n"
        "test_command: 'python -m pytest tests/ -q --tb=short'\n"
        "read_only_dirs: ['vendor/']\n",
        encoding="utf-8",
    )
    (repo / "averyloop_config.json").write_text(
        json.dumps({
            "min_iterations": 1,
            "convergence_enabled": True,
            "safety_gate_enabled": True,
            "outcome_memory_enabled": False,
        }),
        encoding="utf-8",
    )

    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    return repo


# ---------------------------------------------------------------------------
# 1. CLI Entry Point & Dry-Run Sandboxing
# ---------------------------------------------------------------------------

class TestCliAndDryRunSandbox:
    def test_cli_dry_run_never_mutates_main_branch(self, mock_git_repo: Path):
        """Running `averyloop run --repo ./target-repo --dry-run` must not mutate git history or working tree."""
        head_before = _git(mock_git_repo, "rev-parse", "HEAD")
        branches_before = _git(mock_git_repo, "branch", "--list")

        exit_code = cli.main(["run", "--repo", str(mock_git_repo), "--dry-run"])
        assert exit_code == 0

        head_after = _git(mock_git_repo, "rev-parse", "HEAD")
        branches_after = _git(mock_git_repo, "branch", "--list")
        assert head_after == head_before
        assert branches_after == branches_before

        # Log entry created with dry-run exit condition
        log_path = mock_git_repo / "averyloop_log.json"
        assert log_path.is_file()
        log_data = json.loads(log_path.read_text(encoding="utf-8"))
        assert len(log_data) == 1
        assert log_data[0]["exit_condition_met"] is True
        assert log_data[0]["branches_merged"] == []

    def test_worktree_sandbox_isolates_branch_edits(self, mock_git_repo: Path):
        """`worktree_sandbox` isolates uncommitted & committed edits away from the primary checkout."""
        with repo_session(str(mock_git_repo)):
            main_sha = git_utils.get_head_sha("main")
            with git_utils.worktree_sandbox("improvement/wt-test", base="main") as wt_path:
                assert wt_path.is_dir()
                target_file = wt_path / "src" / "calculator.py"
                target_file.write_text(
                    "def add(a: int, b: int) -> int:\n    return a + b\n",
                    encoding="utf-8",
                )
                git_utils.commit_all("fix add in worktree")
                wt_sha = git_utils.get_head_sha("HEAD")
                assert wt_sha != main_sha

            # Primary checkout on main is untouched and worktree dir is cleaned up
            assert not wt_path.exists()
            assert git_utils.current_branch() == "main"
            assert git_utils.get_head_sha("main") == main_sha
            assert git_utils.branch_exists("improvement/wt-test")


# ---------------------------------------------------------------------------
# 2. Provider-Agnostic Config Loader
# ---------------------------------------------------------------------------

class TestProviderAgnosticConfig:
    def test_loads_gemini_anthropic_openai_and_github_tokens_from_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "AVERYLOOP_PROVIDER"):
            monkeypatch.delenv(key, raising=False)

        env_file = tmp_path / ".env"
        env_file.write_text(
            "GEMINI_API_KEY=gem-test-key-123\n"
            "ANTHROPIC_API_KEY='anth-test-key-456'\n"
            'OPENAI_API_KEY="sk-openai-test-789"\n'
            "export GITHUB_TOKEN=ghp_testtoken000\n"
            "AVERYLOOP_PROVIDER=gemini\n",
            encoding="utf-8",
        )

        cfg = load_loop_config(
            path=str(tmp_path / "nonexistent.json"),
            dotenv_path=env_file,
        )
        assert cfg.gemini_api_key == "gem-test-key-123"
        assert cfg.anthropic_api_key == "anth-test-key-456"
        assert cfg.openai_api_key == "sk-openai-test-789"
        assert cfg.github_token == "ghp_testtoken000"
        assert cfg.active_provider() == "gemini"

        import averyloop.loop_config as lc_mod
        monkeypatch.setattr(lc_mod, "_cached", cfg)
        provider, api_key = _api.resolve_api_credentials()
        assert provider == "gemini"
        assert api_key == "gem-test-key-123"


# ---------------------------------------------------------------------------
# 3. Deterministic State-Machine Transitions:
#    Audit -> Implement -> Self-Review -> Gated Merge
# ---------------------------------------------------------------------------

class TestPipelineStateMachine:
    def test_audit_implement_review_approve_and_gated_merge(self, mock_git_repo: Path):
        """Verify full happy-path transition: Audit -> Implement -> Review (APPROVE) -> Test -> Merge."""
        audit_json = json.dumps([
            {
                "dimension": "correctness",
                "file": "src/calculator.py",
                "function_name": "add",
                "description": "add() subtracts instead of adding",
                "fix": "Replace `a - b` with `a + b`.",
                "importance": 8,
                "branch_name": "improvement/fix-add-operator",
            }
        ])
        fixed_code = (
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        review_verdict = {
            "verdict": "APPROVE",
            "summary": "Fixes addition operator",
            "issues": [],
            "reasoning": "Clean, minimal fix that makes the unit test pass.",
        }
        judge_scores = {
            "specificity": 9.0,
            "accuracy": 9.0,
            "coverage": 8.5,
            "prioritization": 9.0,
            "domain_appropriateness": 9.0,
            "overall": 8.9,
            "flags": [],
            "reasoning": "Accurate high-priority bug fix.",
        }

        with repo_session(str(mock_git_repo)):
            with patch.object(orchestrator_v2, "_run_audit", return_value=audit_json), \
                 patch("averyloop.agents.implementer.api_call_with_retry", return_value=fixed_code), \
                 patch.object(orchestrator_v2, "_review", return_value=review_verdict), \
                 patch.object(loop_tracker, "score_audit", return_value=judge_scores):
                entries = orchestrator_v2.run_loop(max_iterations=1, dry_run=False)

        assert len(entries) == 1
        entry = entries[0]
        assert entry["findings_count"] == 1
        assert entry["findings"][0]["status"] == "merged"
        assert entry["branches_merged"] == ["improvement/fix-add-operator"]
        assert entry["tests_passed"] is True

        # Verify main branch now has the merged fix and clean working tree
        assert "return a + b" in (mock_git_repo / "src" / "calculator.py").read_text(encoding="utf-8")
        assert _git(mock_git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_failed_tests_prevent_merge_and_preserve_main(self, mock_git_repo: Path):
        """If the implemented patch fails tests on its feature branch, `main` is never mutated."""
        main_sha_before = _git(mock_git_repo, "rev-parse", "main")
        original_code = (mock_git_repo / "src" / "calculator.py").read_text(encoding="utf-8")

        audit_json = json.dumps([
            {
                "dimension": "correctness",
                "file": "src/calculator.py",
                "function_name": "add",
                "description": "broken fix attempt",
                "fix": "Return a * b",
                "importance": 7,
                "branch_name": "improvement/bad-fix",
            }
        ])
        still_broken_code = (
            "def add(a: int, b: int) -> int:\n"
            "    return a * b\n"
        )
        review_verdict = {
            "verdict": "APPROVE",
            "summary": "Attempted fix",
            "issues": [],
            "reasoning": "Looks plausible",
        }
        judge_scores = {
            "specificity": 7.0, "accuracy": 6.0, "coverage": 7.0,
            "prioritization": 7.0, "domain_appropriateness": 7.0,
            "overall": 6.8, "flags": [], "reasoning": "ok",
        }

        with repo_session(str(mock_git_repo)):
            with patch.object(orchestrator_v2, "_run_audit", return_value=audit_json), \
                 patch("averyloop.agents.implementer.api_call_with_retry", return_value=still_broken_code), \
                 patch.object(orchestrator_v2, "_review", return_value=review_verdict), \
                 patch.object(loop_tracker, "score_audit", return_value=judge_scores):
                entries = orchestrator_v2.run_loop(max_iterations=1, dry_run=False)

        entry = entries[0]
        assert entry["branches_merged"] == []
        assert entry["findings"][0]["status"] == "pending"
        assert entry["tests_passed"] is False

        # Main branch SHA and file contents are completely untouched
        assert _git(mock_git_repo, "rev-parse", "main") == main_sha_before
        assert (mock_git_repo / "src" / "calculator.py").read_text(encoding="utf-8") == original_code

    def test_self_review_reject_deletes_branch_without_merging(self, mock_git_repo: Path):
        """When the reviewer returns REJECT (or raises a critical flag), the branch is deleted and never merged."""
        main_sha_before = _git(mock_git_repo, "rev-parse", "main")

        audit_json = json.dumps([
            {
                "dimension": "security",
                "file": "src/calculator.py",
                "description": "risky change",
                "fix": "Change logic",
                "importance": 6,
                "branch_name": "improvement/rejected-patch",
            }
        ])
        patched_code = "def add(a: int, b: int) -> int:\n    return a + b\n"
        review_verdict = {
            "verdict": "REJECT",
            "summary": "Introduces regression",
            "issues": ["Violates invariants"],
            "reasoning": "Unsafe change.",
        }
        judge_scores = {
            "specificity": 7.0, "accuracy": 7.0, "coverage": 7.0,
            "prioritization": 7.0, "domain_appropriateness": 7.0,
            "overall": 7.0, "flags": [], "reasoning": "ok",
        }

        with repo_session(str(mock_git_repo)):
            with patch.object(orchestrator_v2, "_run_audit", return_value=audit_json), \
                 patch("averyloop.agents.implementer.api_call_with_retry", return_value=patched_code), \
                 patch.object(orchestrator_v2, "_review", return_value=review_verdict), \
                 patch.object(loop_tracker, "score_audit", return_value=judge_scores):
                entries = orchestrator_v2.run_loop(max_iterations=1, dry_run=False)
            assert not git_utils.branch_exists("improvement/rejected-patch")

        assert entries[0]["branches_merged"] == []
        assert _git(mock_git_repo, "rev-parse", "main") == main_sha_before

    def test_safety_gate_vetoes_credential_leak_even_when_approved_and_tests_pass(
        self, mock_git_repo: Path
    ):
        """Deterministic safety gate vetoes a patch containing a hardcoded secret before merge."""
        main_sha_before = _git(mock_git_repo, "rev-parse", "main")

        audit_json = json.dumps([
            {
                "dimension": "correctness",
                "file": "src/calculator.py",
                "description": "fix add and add key",
                "fix": "Fix add",
                "importance": 8,
                "branch_name": "improvement/secret-leak",
            }
        ])
        leaky_code = (
            'API_KEY = "sk-1234567890abcdefghijklmnop"\n'
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        review_verdict = {
            "verdict": "APPROVE",
            "summary": "Approved by mistaken LLM reviewer",
            "issues": [],
            "reasoning": "LGTM",
        }
        judge_scores = {
            "specificity": 8.0, "accuracy": 8.0, "coverage": 8.0,
            "prioritization": 8.0, "domain_appropriateness": 8.0,
            "overall": 8.0, "flags": [], "reasoning": "ok",
        }

        with repo_session(str(mock_git_repo)):
            with patch.object(orchestrator_v2, "_run_audit", return_value=audit_json), \
                 patch("averyloop.agents.implementer.api_call_with_retry", return_value=leaky_code), \
                 patch.object(orchestrator_v2, "_review", return_value=review_verdict), \
                 patch.object(loop_tracker, "score_audit", return_value=judge_scores):
                entries = orchestrator_v2.run_loop(max_iterations=1, dry_run=False)

        assert entries[0]["branches_merged"] == []
        assert entries[0]["findings"][0]["status"] == "vetoed"
        assert _git(mock_git_repo, "rev-parse", "main") == main_sha_before

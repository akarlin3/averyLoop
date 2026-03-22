"""Tests for git_utils module."""

import subprocess
from unittest import mock

import pytest

from improvement_loop import git_utils
from improvement_loop.project_config import ProjectConfig


# ---------------------------------------------------------------------------
# sanitize_branch_slug
# ---------------------------------------------------------------------------

class TestSanitizeBranchSlug:
    def test_empty_string(self):
        assert git_utils.sanitize_branch_slug("") == "fix"

    def test_all_special_chars(self):
        assert git_utils.sanitize_branch_slug("!!!@@@###") == "fix"

    def test_already_valid(self):
        assert git_utils.sanitize_branch_slug("my-feature") == "my-feature"

    def test_too_long(self):
        result = git_utils.sanitize_branch_slug("a" * 100, max_len=50)
        assert len(result) == 50

    def test_leading_trailing_hyphens(self):
        result = git_utils.sanitize_branch_slug("--hello--world--")
        assert result == "hello-world"

    def test_spaces_replaced(self):
        assert git_utils.sanitize_branch_slug("my cool feature") == "my-cool-feature"

    def test_uppercase_lowered(self):
        assert git_utils.sanitize_branch_slug("Fix-BUG") == "fix-bug"

    def test_consecutive_special_collapsed(self):
        assert git_utils.sanitize_branch_slug("a!!!b") == "a-b"

    def test_truncation_strips_trailing_hyphen(self):
        result = git_utils.sanitize_branch_slug("abcde-fgh", max_len=6)
        assert result == "abcde"


# ---------------------------------------------------------------------------
# branch_exists
# ---------------------------------------------------------------------------

class TestBranchExists:
    def test_nonexistent_branch(self):
        assert git_utils.branch_exists("this-branch-does-not-exist-xyz-999") is False


# ---------------------------------------------------------------------------
# current_branch
# ---------------------------------------------------------------------------

class TestCurrentBranch:
    def test_returns_nonempty_string(self):
        branch = git_utils.current_branch()
        assert isinstance(branch, str)
        assert len(branch) > 0


# ---------------------------------------------------------------------------
# run_python_tests — uses ProjectConfig
# ---------------------------------------------------------------------------

class TestRunPythonTests:
    def test_returns_true_on_success(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            assert git_utils.run_python_tests() is True

    def test_returns_false_on_failure(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1)
            assert git_utils.run_python_tests() is False

    def test_uses_config_test_command(self, monkeypatch):
        """Verify test command comes from ProjectConfig."""
        cfg = ProjectConfig(
            test_command="python -m pytest my_tests/ -v",
            test_ignores=["my_tests/test_slow.py"],
        )
        import improvement_loop.project_config as pc_mod
        monkeypatch.setattr(pc_mod, "_cached_project", cfg)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0)
            git_utils.run_python_tests()
            args = mock_run.call_args[0][0]
            assert "my_tests/" in args
            assert "-v" in args
            assert "--ignore" in args
            assert "my_tests/test_slow.py" in args


# ---------------------------------------------------------------------------
# create_branch — uses ProjectConfig default_branch
# ---------------------------------------------------------------------------

class TestCreateBranch:
    def test_raises_if_branch_exists(self):
        with mock.patch("improvement_loop.git_utils.branch_exists", return_value=True):
            with pytest.raises(RuntimeError, match="already exists"):
                git_utils.create_branch("existing-branch")

    def test_calls_checkout_b_with_config_default(self, monkeypatch):
        """When no base is specified, uses ProjectConfig.default_branch."""
        cfg = ProjectConfig(default_branch="develop")
        import improvement_loop.project_config as pc_mod
        monkeypatch.setattr(pc_mod, "_cached_project", cfg)

        with mock.patch("improvement_loop.git_utils.branch_exists", return_value=False):
            with mock.patch("improvement_loop.git_utils._run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess([], 0)
                git_utils.create_branch("new-branch")
                mock_run.assert_called_once_with(
                    ["git", "checkout", "-b", "new-branch", "develop"]
                )

    def test_calls_checkout_b_with_explicit_base(self):
        with mock.patch("improvement_loop.git_utils.branch_exists", return_value=False):
            with mock.patch("improvement_loop.git_utils._run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess([], 0)
                git_utils.create_branch("new-branch", base="main")
                mock_run.assert_called_once_with(
                    ["git", "checkout", "-b", "new-branch", "main"]
                )


# ---------------------------------------------------------------------------
# checkout — mocked
# ---------------------------------------------------------------------------

class TestCheckout:
    def test_raises_if_branch_missing(self):
        with mock.patch("improvement_loop.git_utils.branch_exists", return_value=False):
            with pytest.raises(RuntimeError, match="does not exist"):
                git_utils.checkout("nonexistent")

    def test_calls_git_checkout(self):
        with mock.patch("improvement_loop.git_utils.branch_exists", return_value=True):
            with mock.patch("improvement_loop.git_utils._run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess([], 0)
                git_utils.checkout("my-branch")
                mock_run.assert_called_once_with(["git", "checkout", "my-branch"])


# ---------------------------------------------------------------------------
# switch_branch — alias for checkout
# ---------------------------------------------------------------------------

class TestSwitchBranch:
    def test_is_alias_for_checkout(self):
        assert git_utils.switch_branch is git_utils.checkout


# ---------------------------------------------------------------------------
# merge_branch — uses ProjectConfig default_branch
# ---------------------------------------------------------------------------

class TestMergeBranch:
    def test_raises_on_conflict(self):
        def side_effect(args, *, check=True):
            if args[0:2] == ["git", "merge"]:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="CONFLICT"
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch("improvement_loop.git_utils._run", side_effect=side_effect):
            with pytest.raises(RuntimeError, match="Merge conflict"):
                git_utils.merge_branch("feat", target="main")

    def test_deletes_source_by_default(self):
        with mock.patch("improvement_loop.git_utils._run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="", stderr=""
            )
            git_utils.merge_branch("feat", target="main", delete_after=True)
            calls = [c.args[0] for c in mock_run.call_args_list]
            assert ["git", "branch", "-d", "feat"] in calls

    def test_skips_delete_when_false(self):
        with mock.patch("improvement_loop.git_utils._run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="", stderr=""
            )
            git_utils.merge_branch("feat", target="main", delete_after=False)
            calls = [c.args[0] for c in mock_run.call_args_list]
            assert ["git", "branch", "-d", "feat"] not in calls

    def test_uses_config_default_branch_when_no_target(self, monkeypatch):
        """When no target is specified, uses ProjectConfig.default_branch."""
        cfg = ProjectConfig(default_branch="develop")
        import improvement_loop.project_config as pc_mod
        monkeypatch.setattr(pc_mod, "_cached_project", cfg)

        with mock.patch("improvement_loop.git_utils._run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="", stderr=""
            )
            git_utils.merge_branch("feat")
            calls = [c.args[0] for c in mock_run.call_args_list]
            assert ["git", "checkout", "develop"] in calls


# ---------------------------------------------------------------------------
# commit_all — mocked
# ---------------------------------------------------------------------------

class TestCommitAll:
    def test_noop_when_nothing_to_commit(self):
        def side_effect(args, *, check=True):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch("improvement_loop.git_utils._run", side_effect=side_effect) as mock_run:
            git_utils.commit_all("msg")
            calls = [c.args[0] for c in mock_run.call_args_list]
            assert ["git", "commit", "-m", "msg"] not in calls

    def test_commits_when_changes_present(self):
        def side_effect(args, *, check=True):
            if args[:2] == ["git", "status"]:
                return subprocess.CompletedProcess(
                    args, 0, stdout="M file.py\n", stderr=""
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch("improvement_loop.git_utils._run", side_effect=side_effect) as mock_run:
            git_utils.commit_all("my message")
            calls = [c.args[0] for c in mock_run.call_args_list]
            assert ["git", "commit", "-m", "my message"] in calls


# ---------------------------------------------------------------------------
# run_syntax_check — uses ProjectConfig.source_dirs
# ---------------------------------------------------------------------------

class TestRunSyntaxCheck:
    def test_returns_false_on_syntax_error(self, tmp_path, monkeypatch, capsys):
        """Syntax error in configured source_dirs is caught."""
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "broken.py").write_text("for\n")

        cfg = ProjectConfig(source_dirs=["src/"])
        import improvement_loop.project_config as pc_mod
        monkeypatch.setattr(pc_mod, "_cached_project", cfg)

        with mock.patch.object(git_utils, "REPO_ROOT", tmp_path):
            result = git_utils.run_syntax_check()

        assert result is False
        captured = capsys.readouterr()
        assert "broken.py" in captured.out
        assert "Syntax error" in captured.out

    def test_returns_true_when_all_valid(self, tmp_path, monkeypatch, capsys):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "good.py").write_text("x = 1\n")

        cfg = ProjectConfig(source_dirs=["src/"])
        import improvement_loop.project_config as pc_mod
        monkeypatch.setattr(pc_mod, "_cached_project", cfg)

        with mock.patch.object(git_utils, "REPO_ROOT", tmp_path):
            result = git_utils.run_syntax_check()

        assert result is True
        captured = capsys.readouterr()
        assert "Syntax check passed" in captured.out
        assert "1 files" in captured.out

    def test_returns_true_when_no_files(self, tmp_path, monkeypatch, capsys):
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        cfg = ProjectConfig(source_dirs=["src/"])
        import improvement_loop.project_config as pc_mod
        monkeypatch.setattr(pc_mod, "_cached_project", cfg)

        with mock.patch.object(git_utils, "REPO_ROOT", tmp_path):
            result = git_utils.run_syntax_check()

        assert result is True
        captured = capsys.readouterr()
        assert "0 files" in captured.out

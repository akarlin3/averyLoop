"""Tests for project_config loading."""

import os
import textwrap
from pathlib import Path

import pytest

from improvement_loop.project_config import ProjectConfig, load_project_config


class TestProjectConfigDefaults:
    def test_default_values(self):
        cfg = ProjectConfig()
        assert cfg.name == ""
        assert cfg.languages == ["python"]
        assert cfg.default_branch == "main"
        assert cfg.branch_prefix == "improvement/"
        assert cfg.source_dirs == ["src/"]
        assert cfg.test_command == "python -m pytest tests/ -q --tb=short"
        assert cfg.skip_dirs == [".git", "__pycache__"]
        assert cfg.risk_flags == ["LEAKAGE_RISK", "PHI_RISK"]
        assert cfg.collection_name == "codebase_index"
        assert cfg.skip_extensions == [".png", ".jpg", ".pdf"]

    def test_default_empty_lists(self):
        cfg = ProjectConfig()
        assert cfg.test_ignores == []
        assert cfg.read_only_dirs == []
        assert cfg.key_files == []
        assert cfg.forbidden_patterns == []

    def test_default_empty_prompts(self):
        cfg = ProjectConfig()
        assert cfg.audit_system_prompt == ""
        assert cfg.review_system_prompt == ""
        assert cfg.judge_system_prompt == ""
        assert cfg.judge_calibration == ""


class TestLoadProjectConfig:
    def test_returns_defaults_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PROJECT_CONFIG", raising=False)
        cfg = load_project_config()
        assert cfg == ProjectConfig()

    def test_load_from_explicit_path(self, tmp_path):
        yaml_file = tmp_path / "custom.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            name: "test-project"
            languages: ["python", "rust"]
            source_dirs: ["lib/"]
        """))
        cfg = load_project_config(str(yaml_file))
        assert cfg.name == "test-project"
        assert cfg.languages == ["python", "rust"]
        assert cfg.source_dirs == ["lib/"]
        # defaults still apply for unset fields
        assert cfg.default_branch == "main"

    def test_load_from_env_var(self, tmp_path, monkeypatch):
        yaml_file = tmp_path / "env_config.yaml"
        yaml_file.write_text("name: from-env\n")
        monkeypatch.setenv("PROJECT_CONFIG", str(yaml_file))
        cfg = load_project_config()
        assert cfg.name == "from-env"

    def test_load_from_cwd_project_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PROJECT_CONFIG", raising=False)
        (tmp_path / "project_config.yaml").write_text("name: cwd-project\n")
        cfg = load_project_config()
        assert cfg.name == "cwd-project"

    def test_load_from_cwd_improvement_loop_project(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PROJECT_CONFIG", raising=False)
        (tmp_path / "improvement_loop_project.yaml").write_text(
            "name: alt-name\n"
        )
        cfg = load_project_config()
        assert cfg.name == "alt-name"

    def test_prompts_nested_section(self, tmp_path):
        yaml_file = tmp_path / "prompts.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            name: prompt-test
            prompts:
              audit_system: "You are an auditor."
              review_system: "You are a reviewer."
              judge_system: "You are a judge."
              judge_calibration: "Score 1-10."
        """))
        cfg = load_project_config(str(yaml_file))
        assert cfg.audit_system_prompt == "You are an auditor."
        assert cfg.review_system_prompt == "You are a reviewer."
        assert cfg.judge_system_prompt == "You are a judge."
        assert cfg.judge_calibration == "Score 1-10."

    def test_ignores_unknown_fields(self, tmp_path):
        yaml_file = tmp_path / "extra.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            name: extras
            unknown_field: should be ignored
            another_extra: 42
        """))
        cfg = load_project_config(str(yaml_file))
        assert cfg.name == "extras"
        assert not hasattr(cfg, "unknown_field")

    def test_explicit_path_not_found_returns_defaults(self):
        cfg = load_project_config("/nonexistent/path.yaml")
        assert cfg == ProjectConfig()

    def test_empty_yaml_returns_defaults(self, tmp_path):
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")
        cfg = load_project_config(str(yaml_file))
        assert cfg == ProjectConfig()

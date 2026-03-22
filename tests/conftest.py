"""Shared fixtures for the improvement loop test suite."""

from __future__ import annotations

import pytest

from improvement_loop.project_config import ProjectConfig, reset_project_config_cache
from improvement_loop.loop_config import reset_config


@pytest.fixture(autouse=True)
def _reset_caches():
    """Ensure each test gets fresh config singletons."""
    reset_project_config_cache()
    reset_config()
    yield
    reset_project_config_cache()
    reset_config()


@pytest.fixture
def minimal_project_config(monkeypatch):
    """Provide a minimal ProjectConfig for testing (no pancData3 references).

    Patches get_project_config() to return a simple default config so tests
    don't depend on any YAML file on disk.
    """
    cfg = ProjectConfig(
        name="test-project",
        description="A test project",
        languages=["python"],
        default_branch="main",
        branch_prefix="improvement/",
        source_dirs=["src/"],
        test_command="python -m pytest tests/ -q --tb=short",
        test_ignores=[],
        read_only_dirs=[],
        skip_dirs=[".git", "__pycache__"],
        key_files=[],
        collection_name="test_index",
    )
    import improvement_loop.project_config as pc_mod
    monkeypatch.setattr(pc_mod, "_cached_project", cfg)
    return cfg

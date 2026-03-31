# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-03-30

### Added

- **Shared API helper** (`agents/_api.py`): Centralized `get_client()` constructor with rate-limit retry and streaming support.

- **Implementer agent** (`agents/implementer.py`): New module that applies code fixes from audit findings, replacing inline fix logic in the orchestrator.

- **RAG retriever** (`rag/retriever.py`): New module for querying the ChromaDB vector index to supply relevant code context to agents.

- **Per-project model selection**: `audit_model`, `fix_model`, and `judge_model` fields added to `ProjectConfig`, overriding the loop config defaults. Projects can now pin specific Claude models in `project_config.yaml`.

- **Orchestrator tests** (`tests/test_orchestrator.py`): New test coverage for the restructured orchestrator pipeline.

### Changed

- **Orchestrator v2** restructured into a four-phase pipeline (audit → implement → review → merge) with a reviewer gate that blocks low-quality patches before merge.

- **Reviewer agent** (`agents/reviewer.py`): Expanded with richer review logic to support the new reviewer gate phase.

- **Evaluator** (`evaluator.py`): Fixed post-merge test revert behavior, added rebase-before-merge, and aligned score thresholds consistently.

- **Git utilities** (`git_utils.py`): Switched `REPO_ROOT` to `os.getcwd()` instead of `__file__`-relative paths for correct behavior when installed as a package.

### Breaking

- **API key resolution**: `get_client()` no longer falls back to the `ANTHROPIC_API_KEY` environment variable. The key must be set in `project_config.yaml` or `improvement_loop_config.json`. Existing setups relying on the env var must add `anthropic_api_key` to their project config.

- **Model config moved to project config**: Model selection now resolves as `project_config.yaml → loop config → built-in default`. Projects previously relying on `improvement_loop_config.json` for model overrides should migrate those values to `project_config.yaml`.

- **pancdata3 example removed**: The `examples/pancdata3/project_config.yaml` has been removed; the example config at `project_config.example.yaml` now covers all fields including model selection.

## [1.0.0] - 2026-03-22

Initial stable release — extracted from [akarlin3/pancData3](https://github.com/akarlin3/pancData3) as an independent, reusable package.

### Added

- **Project configuration** (`project_config.py`): YAML-based per-project config with `ProjectConfig` dataclass, cached loader (`get_project_config()`), and search-path resolution (explicit path → `PROJECT_CONFIG` env var → `./project_config.yaml` → `./improvement_loop_project.yaml`).

- **Loop configuration** (`loop_config.py`): JSON-based loop tuning with `LoopConfig` dataclass covering exit strategy, diminishing returns thresholds, API models/tokens, and orchestrator knobs. Copied from pancData3 with cached singleton pattern.

- **Auditor agent** (`agents/auditor.py`): Extracted audit system prompt and source file collection from the pancData3 orchestrator. Reads `audit_system_prompt`, `key_files`, and `read_only_dirs` from `ProjectConfig` with `DEFAULT_AUDIT_PROMPT` fallback.

- **Reviewer agent** (`agents/reviewer.py`): New module with `DEFAULT_REVIEW_PROMPT` fallback, reads `review_system_prompt` and `read_only_dirs` from `ProjectConfig`.

- **Evaluator** (`evaluator.py`): Judge scoring with `_build_judge_prompt()` reading from `ProjectConfig` (`judge_system_prompt`, `judge_calibration`) with `DEFAULT_JUDGE_PROMPT` and `DEFAULT_CALIBRATION` fallbacks. `Finding` Pydantic model with branch name validation using config `branch_prefix`. Exit logic uses `critical_flags` from config.

- **Git utilities** (`git_utils.py`): Branch management, test runners, and syntax checks. Refactored: `default_branch` from config (was hardcoded `"v2.1-dev"`), `test_command` + `test_ignores` from config (was hardcoded pytest invocation), `source_dirs` from config in `run_syntax_check()` (was hardcoded `"analysis/"`).

- **RAG chunker** (`rag/chunker.py`): Language-aware code chunking (Python by class/function, MATLAB by `function` keyword). Reads `skip_dirs`, `read_only_dirs`, and `skip_extensions` from config.

- **RAG indexer** (`rag/indexer.py`): ChromaDB vector index build and query. Reads `collection_name` and project `name` from config.

- **Orchestrator v2** (`orchestrator_v2.py`): Full audit → fix → test → merge loop using `agents/auditor.py` for prompts, `ProjectConfig` for critical flags, and `main()` CLI entry point. Refactored from pancData3's `orchestrator_v1.py`.

- **Loop tracker** (`loop_tracker.py`): Iteration logging, context generation for subsequent audits, score drift detection, finding status management. Copied from pancData3.

- **Test suite**: 105 tests covering all modules. `conftest.py` provides `minimal_project_config` fixture and automatic cache resets so tests don't depend on pancData3 paths.

- **Package infrastructure**: `pyproject.toml` (name: `code-improvement-loop`, Python >= 3.10), `project_config.example.yaml` with full schema documentation, `examples/pancdata3/project_config.yaml` with real-world clinical genomics config.

### Changed (relative to pancData3)

- All hardcoded pancData3-specific values (prompts, paths, branch names, test commands) replaced with `ProjectConfig` lookups and sensible defaults.
- Every module falls back gracefully when `ProjectConfig` fields are empty — the loop works with zero config for simple Python-only repos.
- `Finding.branch_name` validator now reads `branch_prefix` from config instead of hardcoding `"improvement/"`.

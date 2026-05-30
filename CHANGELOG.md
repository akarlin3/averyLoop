# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Outcome-feedback RAG** (`outcomes.py` + `rag/outcome_memory.py`): The loop now
  learns within a project. `outcomes.py` derives a typed outcome —
  `accepted` / `rejected` / `reverted` — for each implemented finding from existing
  loop state (merge status, safety-gate veto, reviewer verdict, test results) plus
  post-merge revert detection (scanning `This reverts commit <sha>` git history and
  capturing the orchestrator's own post-merge auto-reverts). `rag/outcome_memory.py`
  embeds outcomes into a **dedicated, persistent ChromaDB collection**
  (`outcome_memory`) that is keyed separately from `codebase_index` so it
  **survives the per-run index rebuild** and accumulates over time. `recall_outcomes`
  returns prior outcomes for similar code and `synthesize_note` produces a short
  advisory note injected into the audit as **additive context only** (no
  control-flow change). Embedding uses a deterministic, dependency-free hashed
  bag-of-words vectorizer, so the store is fully offline (no model download, no
  API key). Gated by `outcome_memory_enabled` (default on).

- **`Finding` status values** `vetoed` and `reverted` (`evaluator.py`):
  backward-compatible additions to the status `Literal` and `VALID_STATUSES`
  (existing consumers only test `==` / `!=` `"merged"`); the orchestrator now sets
  them on a safety-gate veto and a post-merge auto-revert respectively.

- **Benchmark harness** (`benchmark/`): Runs AveryLoop's authored decision logic
  against five seeded-bug fixtures with encoded ground truth (a real logic bug, a
  safety trap, a style nit, a no-op, and a revert trap) and reports **fix rate**,
  **false-accept rate**, **convergence savings**, and **judge↔objective agreement**.
  Default mode is fully offline and deterministic: stub agents supply
  findings/fixes/reviews/scores from ground truth while the real `signals`,
  `safety_gate`, composite blend, `convergence`, and outcome memory make every
  decision. Includes two-arm comparisons (convergence on vs fixed baseline; memory
  on vs off), a results table, and machine-readable JSON. Headline (offline):
  fix rate 0.50, false-accept 0.00, convergence savings ~52% with quality held,
  reverts halved and wasted re-attempts eliminated by outcome memory. Opt-in live
  mode via `AVERYLOOP_BENCH_LIVE=1`.

- **Config knobs** (`loop_config.py`): `outcome_memory_enabled`,
  `outcome_collection_name`, `outcome_recall_k`, `outcome_embed_dim`.

- **Tests**: `tests/test_outcomes.py`, `tests/test_outcome_memory.py`, and
  `tests/test_benchmark.py` (48 tests) — outcome classification and revert
  detection (incl. a live-git integration test), record→recall round-trips against
  a temp Chroma collection with persistence across a simulated rebuild, and the
  metric formulas + deterministic two-arm comparison. No live LLM calls.

- **Docs**: `benchmark/README.md` with the full results table, metric definitions,
  and stated limits; `docs/EVALUATION.md` gains the judge↔objective agreement
  finding; top-level README documents the outcome-feedback behavior and headline
  numbers.

- **Convergence detection** (`convergence.py`): A pure, non-LLM
  `evaluate_convergence()` that inspects iteration history and returns a
  `ConvergenceDecision {stop, reason, signal_values}`. Implements a
  min-iteration floor, plateau detection (score improvement below
  `convergence_epsilon` for `convergence_patience` consecutive iterations,
  with score drops counting as stalls), and decay detection (no fixes accepted
  for `convergence_patience` iterations). Wired into `run_loop`, which now
  breaks early on convergence — logging the reason — bounded by `max_iterations`.

- **Objective-signal evaluator** (`signals.py`): Pure functions computing
  normalized `0–10` sub-scores for test pass/fail (with a test-count-shrink
  penalty), coverage delta, complexity delta, diff size, and scope adherence
  (fraction of changed files within the audit's intended scope). Includes pure
  diff/pytest parsers and optional `coverage`/`radon` measurement wrappers that
  return `None` (never raise) when the tooling is absent.

- **Composite score blend** (`evaluator.blend_scores` /
  `augment_scores_with_objective`): The LLM judge's `overall` is now blended
  with the available objective sub-scores using config weights (defaults
  `weight_llm=0.5`, `weight_tests=0.2`, `weight_coverage=0.1`,
  `weight_complexity=0.1`, `weight_scope=0.1`), renormalized over whichever
  signals are present so missing tooling degrades gracefully. The raw LLM score
  and every objective signal are preserved in `averyloop_log.json`. The judge is
  now one input, not the oracle.

- **Deterministic safety gate** (`safety_gate.py`): A pure, non-LLM
  `evaluate_safety()` returning `SafetyVerdict {veto, violations}` from
  code-level checks — out-of-scope/read-only writes, removal of test assertions,
  credential-like patterns, and edits to the loop's own safety code. Wired into
  `_phase_test_and_merge` before the merge: a veto blocks the merge regardless
  of the judge verdict, in addition to (not instead of) the judge-emitted
  critical flags.

- **Config knobs** (`loop_config.py`): `convergence_enabled`,
  `convergence_epsilon`, `convergence_patience`, `min_iterations`, the five
  composite `weight_*` fields, `safety_gate_enabled`, `safety_protected_paths`,
  `safety_denylist_paths`, and `safety_allowlist_paths`.

- **Tests**: `tests/test_convergence.py`, `tests/test_signals.py`, and
  `tests/test_safety_gate.py` plus composite-blend tests — all assert the
  authored logic with no live LLM calls.

- **Docs**: `docs/EVALUATION.md` specifying the composite formula, each signal's
  range, the convergence criteria, and the safety-gate ruleset; README updated
  for the new stopping behavior, composite blend, and safety gate.

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

- **Renamed to AveryLoop**: Package renamed from `code-improvement-loop` / `improvement_loop` to `averyloop`. All imports, config filenames, and the CLI entry point have changed (`averyloop` replaces `improvement-loop`). Config files are now `averyloop_config.json`, `averyloop_project.yaml`, and `averyloop_log.json`.

- **API key resolution**: `get_client()` no longer falls back to the `ANTHROPIC_API_KEY` environment variable. The key must be set in `project_config.yaml` or `averyloop_config.json`. Existing setups relying on the env var must add `anthropic_api_key` to their project config.

- **Model config moved to project config**: Model selection now resolves as `project_config.yaml → loop config → built-in default`. Projects previously relying on `averyloop_config.json` for model overrides should migrate those values to `project_config.yaml`.

- **pancdata3 example removed**: The `examples/pancdata3/project_config.yaml` has been removed; the example config at `project_config.example.yaml` now covers all fields including model selection.

## [1.0.0] - 2026-03-22

Initial stable release — extracted from [akarlin3/pancData3](https://github.com/akarlin3/pancData3) as an independent, reusable package.

### Added

- **Project configuration** (`project_config.py`): YAML-based per-project config with `ProjectConfig` dataclass, cached loader (`get_project_config()`), and search-path resolution (explicit path → `PROJECT_CONFIG` env var → `./project_config.yaml` → `./averyloop_project.yaml`).

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

- **Package infrastructure**: `pyproject.toml` (name: `averyloop`, Python >= 3.10), `project_config.example.yaml` with full schema documentation, `examples/pancdata3/project_config.yaml` with real-world clinical genomics config.

### Changed (relative to pancData3)

- All hardcoded pancData3-specific values (prompts, paths, branch names, test commands) replaced with `ProjectConfig` lookups and sensible defaults.
- Every module falls back gracefully when `ProjectConfig` fields are empty — the loop works with zero config for simple Python-only repos.
- `Finding.branch_name` validator now reads `branch_prefix` from config instead of hardcoding `"improvement/"`.

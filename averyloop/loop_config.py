"""Configuration for the improvement loop.

Loads from ``averyloop_config.json`` at the repo root.  Every field
has a built-in default so the file is optional — missing keys are filled in
automatically (same pattern as ``parse_config.m`` for the MATLAB pipeline).
"""

import json
import os
from dataclasses import dataclass, field, fields
from typing import List, Literal

CONFIG_PATH = os.path.join(os.getcwd(), "averyloop_config.json")


@dataclass
class LoopConfig:
    """All tuneable knobs for the improvement loop."""

    # ── Exit strategy ────────────────────────────────────────────────────
    # "classic"             — original threshold-only logic
    # "diminishing_returns" — only the 4-condition staleness detector
    # "both"                — classic first, then diminishing returns
    exit_strategy: Literal["classic", "diminishing_returns", "both"] = "both"

    # ── Classic exit thresholds ──────────────────────────────────────────
    importance_threshold: int = 2        # findings >= this keep the loop going
    min_coverage_score: float = 6.0      # coverage below this keeps the loop going

    # ── Diminishing returns thresholds ───────────────────────────────────
    dr_window: int = 4                   # how many recent iterations to examine
    dr_max_merge_rate: float = 0.15      # per-iteration merge rate ceiling
    dr_max_avg_importance: float = 3.5   # avg importance ceiling across window
    dr_min_file_repeats: int = 3         # same file must appear in >= N iterations
    dr_max_audit_score: float = 8.5      # no iteration may exceed this score

    # ── Convergence / diminishing-returns detection (convergence.py) ─────
    # Authored, non-LLM stop logic layered on top of max_iterations.
    convergence_enabled: bool = True     # break run_loop early on convergence
    convergence_epsilon: float = 0.25    # min score gain that counts as progress
    convergence_patience: int = 2        # k consecutive stalled iters → plateau
    min_iterations: int = 2              # floor: never stop before this iteration

    # ── Composite-score blend weights (signals.py + evaluator blend) ─────
    # The LLM judge is one input among measured objective signals.  Weights
    # are renormalized over whichever signals are actually available, so a
    # target project missing coverage/complexity tooling degrades gracefully.
    weight_llm: float = 0.5              # weight of the LLM judge overall score
    weight_tests: float = 0.2           # weight of the test pass/fail signal
    weight_coverage: float = 0.1        # weight of the coverage-delta signal
    weight_complexity: float = 0.1      # weight of the complexity-delta signal
    weight_scope: float = 0.1           # weight of the scope-adherence signal

    # ── Deterministic safety gate (safety_gate.py) ───────────────────────
    # Code-level merge veto that does not trust the judge.
    safety_gate_enabled: bool = True
    # The loop's own authored-logic core — edits here are vetoed so the gate
    # cannot be prompt-injected away by a fix that rewrites the gate itself.
    safety_protected_paths: List[str] = field(
        default_factory=lambda: [
            "averyloop/safety_gate.py",
            "averyloop/convergence.py",
            "averyloop/signals.py",
            "averyloop/evaluator.py",
        ]
    )
    safety_denylist_paths: List[str] = field(default_factory=list)
    safety_allowlist_paths: List[str] = field(default_factory=list)

    # ── API ──────────────────────────────────────────────────────────────
    # If empty, falls back to ANTHROPIC_API_KEY env var (Anthropic SDK default).
    anthropic_api_key: str = ""
    audit_model: str = "claude-opus-4-6"
    fix_model: str = "claude-opus-4-6"
    judge_model: str = "claude-opus-4-6"

    # ── Token limits ────────────────────────────────────────────────────
    audit_max_tokens: int = 32000
    fix_max_tokens: int = 8192
    judge_max_tokens: int = 2000

    # ── Orchestrator knobs ───────────────────────────────────────────────
    max_api_retries: int = 3
    retry_base_delay: float = 30.0
    max_self_heal_attempts: int = 2
    max_file_chars: int = 8000


def load_loop_config(path: str | None = None) -> LoopConfig:
    """Load config from JSON, falling back to defaults for missing keys."""
    cfg_path = path or CONFIG_PATH
    overrides: dict = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            overrides = json.load(f)

    valid_keys = {fld.name for fld in fields(LoopConfig)}
    filtered = {k: v for k, v in overrides.items() if k in valid_keys}
    return LoopConfig(**filtered)


# Module-level singleton — importers get a shared instance.
# Re-call load_loop_config() to refresh from disk if needed.
_cached: LoopConfig | None = None


def get_config(path: str | None = None) -> LoopConfig:
    """Return a cached LoopConfig, loading from disk on first call."""
    global _cached
    if _cached is None:
        _cached = load_loop_config(path)
    return _cached


def reset_config() -> None:
    """Clear the cached config so the next get_config() reloads from disk."""
    global _cached
    _cached = None

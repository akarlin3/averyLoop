"""Configuration for the improvement loop.

Loads from ``averyloop_config.json`` at the repo root and environment variables
(or ``.env`` file).  Every field has a built-in default so the config file is
optional — missing keys are filled in automatically.
"""

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List, Literal, Optional

CONFIG_PATH = os.path.join(os.getcwd(), "averyloop_config.json")

SUPPORTED_ENV_KEYS = (
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
)


def load_dotenv_file(dotenv_path: Optional[str | Path] = None, override: bool = False) -> Dict[str, str]:
    """Load key=value pairs from a ``.env`` file into ``os.environ``.

    Lightweight, dependency-free parser that supports comments, blank lines,
    optional ``export `` prefixes, and single/double-quoted values.
    """
    path = Path(dotenv_path) if dotenv_path else Path.cwd() / ".env"
    loaded: Dict[str, str] = {}
    if not path.is_file():
        return loaded

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return loaded

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        elif "#" in val:
            val = val.split("#", 1)[0].strip()
        if key:
            loaded[key] = val
            if override or key not in os.environ:
                os.environ[key] = val
    return loaded


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
    # Adaptive convergence: when on, the plateau epsilon is scaled to the
    # run's *own* observed improvement magnitudes instead of the fixed
    # convergence_epsilon, so a threshold tuned for one repo doesn't stop too
    # early on a small/slow repo or too late on a noisy one. See convergence.py.
    convergence_adaptive: bool = False
    convergence_adaptive_coef: float = 0.5   # eps = coef × mean|improvement|
    convergence_adaptive_floor: float = 0.01  # never let adaptive eps reach 0

    # ── Composite-score blend weights (signals.py + evaluator blend) ─────
    # The LLM judge is one input among measured objective signals.  Weights
    # are renormalized over whichever signals are actually available, so a
    # target project missing coverage/complexity tooling degrades gracefully.
    weight_llm: float = 0.5              # weight of the LLM judge overall score
    weight_tests: float = 0.2           # weight of the test pass/fail signal
    weight_coverage: float = 0.1        # weight of the coverage-delta signal
    weight_complexity: float = 0.1      # weight of the complexity-delta signal
    weight_scope: float = 0.1           # weight of the scope-adherence signal

    # ── Outcome-feedback memory (rag/outcome_memory.py) ──────────────────
    # Persistent, rebuild-surviving store of accept/reject/revert outcomes,
    # recalled as *additive* audit context.  Default on; disable for clean
    # baselines and the benchmark's "memoryless" arm.
    outcome_memory_enabled: bool = True
    outcome_collection_name: str = "outcome_memory"  # distinct from codebase_index
    outcome_recall_k: int = 5            # how many prior outcomes to recall
    outcome_embed_dim: int = 256         # hashed bag-of-words embedding dim

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

    # ── API & Provider Credentials ───────────────────────────────────────
    # Provider-agnostic credentials: loaded from config JSON, project YAML,
    # or environment variables (GEMINI_API_KEY, ANTHROPIC_API_KEY,
    # OPENAI_API_KEY, GITHUB_TOKEN).
    provider: str = "auto"               # "anthropic", "gemini", "openai", or "auto"
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""
    github_token: str = ""
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

    # ── Git sandboxing ───────────────────────────────────────────────────
    use_worktree_sandbox: bool = False   # isolate branch edits in a temporary git worktree

    def active_provider(self) -> str:
        """Resolve the active LLM provider from explicit config or available keys."""
        if self.provider and self.provider != "auto":
            return self.provider.lower()
        if self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic"
        if self.gemini_api_key or os.environ.get("GEMINI_API_KEY"):
            return "gemini"
        if self.openai_api_key or os.environ.get("OPENAI_API_KEY"):
            return "openai"
        return "anthropic"


def load_loop_config(
    path: str | None = None,
    dotenv_path: str | Path | None = None,
) -> LoopConfig:
    """Load config from JSON and environment/.env, falling back to defaults."""
    dotenv_vals = load_dotenv_file(dotenv_path)
    cfg_path = path or CONFIG_PATH
    overrides: dict = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            overrides = json.load(f)

    valid_keys = {fld.name for fld in fields(LoopConfig)}
    filtered = {k: v for k, v in overrides.items() if k in valid_keys}

    # Populate provider API keys & GitHub token from environment or .env when not set in JSON
    env_mapping = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "gemini_api_key": "GEMINI_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "github_token": "GITHUB_TOKEN",
    }
    for field_name, env_var in env_mapping.items():
        val = os.environ.get(env_var) or dotenv_vals.get(env_var)
        if not filtered.get(field_name) and val:
            filtered[field_name] = val

    prov = os.environ.get("AVERYLOOP_PROVIDER") or dotenv_vals.get("AVERYLOOP_PROVIDER")
    if not filtered.get("provider") and prov:
        filtered["provider"] = prov

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

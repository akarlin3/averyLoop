# Evaluation, Convergence, and Safety

This document specifies the **authored backbone** of AveryLoop's loop — the
parts whose decisions are made in code, not delegated to an LLM. Three pure,
unit-tested modules implement them:

| Module | Decision | LLM involved? |
|---|---|---|
| `convergence.py` | When to stop | No |
| `signals.py` + evaluator blend | Whether a change is good | LLM is **one** input |
| `safety_gate.py` | Whether a merge is safe | No |
| `outcomes.py` + `rag/outcome_memory.py` | What past fixes teach us | No (deterministic embedding) |

All three are pure functions over plain data (history dicts, diff text, score
dicts) and are tested without live LLM calls or real git state.

---

## 1. Composite score

The LLM judge (`evaluator.score_audit`) still produces its `overall` score in
`[0, 10]`, but it is no longer the oracle. The composite is a weighted blend of
the judge and the available objective sub-scores:

```
composite = ( w_llm·S_llm + Σ_i w_i·S_i ) / ( w_llm + Σ_i w_i )
```

where `i` ranges over the objective signals that are **available** for this
iteration. The denominator renormalizes over present terms, so an absent signal
simply drops out and the remaining weights rescale to sum to 1.

### Default weights

| Term | Config field | Default |
|---|---|---|
| LLM judge `overall` | `weight_llm` | `0.5` |
| Tests | `weight_tests` | `0.2` |
| Coverage delta | `weight_coverage` | `0.1` |
| Complexity delta | `weight_complexity` | `0.1` |
| Scope adherence | `weight_scope` | `0.1` |

The LLM term is always present; the four objective terms are present only when
their inputs exist.

### Objective sub-scores (`signals.py`)

Every sub-score is normalized to `[0, 10]` (higher is better):

| Signal | Range / formula | Notes |
|---|---|---|
| **tests** | `10` pass · `0` fail · `5` pass-but-fewer-tests | A passing suite whose test count shrank scores `5` — tests may have been deleted. |
| **coverage** | `clamp(5 + (curr − prev), 0, 10)` | Neutral `5` at no change; `+1` point per `+1` percentage-point of line coverage. |
| **complexity** | `clamp(5 + (prev − curr), 0, 10)` | Neutral `5`; reducing average cyclomatic complexity raises the score. |
| **diff_size** | `10` if `≤ 20` changed lines, else `clamp(10 − (total−20)/10, 0, 10)` | Risk proxy: small, focused diffs score high. |
| **scope** | `10 × (in-scope changed files / changed files)` | Empty diff is vacuously in-scope (`10`). |

> `diff_size` is computed and logged but is **not** in the default blend weights
> (its weight is `0`); it is available for tuning and for the Prompt-B
> benchmark harness.

### Graceful degradation

The coverage and complexity signals depend on optional tooling
(`coverage` / `radon`). `signals.measure_coverage()` and
`signals.measure_complexity()` return `None` — never raise — when the tool is
not installed or has no data. A `None` measurement means the signal is marked
unavailable and dropped from the blend, and the weights renormalize. The loop
therefore runs unchanged on a target project that has neither tool.

### Auditability

`evaluator.augment_scores_with_objective` keeps the raw LLM `overall`
(`llm_overall`), the `composite`, the renormalized `blend_weights`, and the full
`objective_signals` (sub-scores + availability + raw measurements) in each
`averyloop_log.json` entry, so any composite can be reproduced from the log.

---

## 2. Convergence criteria (`convergence.py`)

`evaluate_convergence(history, cfg) → ConvergenceDecision{stop, reason,
signal_values}` inspects the iteration history (oldest first). The progress
metric per iteration is the `composite` score when present, else the judge
`overall`.

Let `ε = convergence_epsilon`, `k = convergence_patience`,
`floor = min_iterations`.

1. **Min-iteration floor.** If fewer than `max(floor, 1)` iterations have
   completed, never stop. (Prevents stopping on iteration 1.)
2. **Plateau.** With at least `k + 1` metric points, compute the last `k`
   improvements `mᵢ − mᵢ₋₁`. If **all** are `< ε`, stop with reason `plateau`.
   A score that *drops* yields a negative improvement `< ε`, so decay-in-score
   counts as a stall.
3. **Decay.** If the last `k` iterations accepted **zero** fixes
   (`branches_merged` empty in each), stop with reason `decay` — the marginal
   objective gain has trended to zero.

Otherwise continue. `signal_values` carries the metric series, recent
improvements, recent accept counts, and the thresholds for auditing.

### Defaults

| Field | Default | Meaning |
|---|---|---|
| `convergence_enabled` | `true` | Break `run_loop` early on convergence |
| `convergence_epsilon` | `0.25` | Min score gain counted as progress |
| `convergence_patience` | `2` | Consecutive stalled iterations for plateau |
| `min_iterations` | `2` | Floor before any early stop |

`run_loop` calls the detector after each iteration's log and breaks on `stop`,
logging the reason and signal values. `max_iterations` remains the hard ceiling.
(In dry-run mode the detector is skipped — there is no real signal to converge
on.)

---

## 3. Safety-gate ruleset (`safety_gate.py`)

`evaluate_safety(diff, intended_files, *, protected_paths, denylist_paths,
allowlist_paths, read_only_dirs) → SafetyVerdict{veto, violations}` runs before
every merge, **in addition to** the judge-emitted critical flags, and vetoes the
merge on any of:

| Rule | Fires when | Source |
|---|---|---|
| **out-of-scope write** | a changed file is not in the audit's intended scope and not allowlisted | `intended_files`, `safety_allowlist_paths` |
| **read-only / denylisted write** | a changed file sits under a denylisted or read-only path prefix | `safety_denylist_paths`, project `read_only_dirs` |
| **protected self-edit** | a changed file is the loop's own safety/authored-logic code | `safety_protected_paths` |
| **assertion weakening** | a removed (`-`) line in a *test* file contained `assert` / `pytest.raises` | diff content |
| **credential-like pattern** | an added (`+`) line matches a known secret shape (AWS key, private-key block, OpenAI-style key, GitHub token, hardcoded `password=`/`api_key=` assignment) | diff content |

The verdict collects **all** violations (not just the first). The gate is pure —
diff text and config lists in, verdict out — so it is fully unit-tested, and
because it lives in `safety_protected_paths` it vetoes any fix that tries to
edit the gate itself. This is the defense-in-depth layer: provably ours and not
prompt-injectable.

### Defaults

| Field | Default |
|---|---|
| `safety_gate_enabled` | `true` |
| `safety_protected_paths` | `averyloop/{safety_gate,convergence,signals,evaluator}.py` |
| `safety_denylist_paths` | `[]` |
| `safety_allowlist_paths` | `[]` |

(Project `read_only_dirs` from `project_config.yaml` are also enforced.)

---

## 4. Judge↔objective agreement (benchmark finding)

The composite (§1) blends the LLM judge with objective signals precisely because
neither is sufficient alone. The benchmark (`benchmark/`) quantifies *how much*
the two agree by correlating, across audited fixtures, the LLM judge `overall`
with the objective signal score (one pair per fixture audit).

**Definition.** Agreement = **Spearman ρ** (rank) and **Pearson r** (linear)
between the judge series and the objective series. Spearman is the headline: it
measures whether the two *rank* changes the same way, independent of scale.

**Finding (offline seeded-bug suite, n = 4 audits):** **Spearman ρ ≈ 0.26,
Pearson r ≈ 0.48** — moderate positive agreement. The objective signals saturate
near 10 for the three fixtures with passing tests and focused, in-scope diffs
(`logic_bug`, `style_nit`, `revert_trap`), while the judge separates them sharply
(8.5 / 2.5 / 6.0). Only the `safety_trap` scores low on objective signals (its
out-of-scope write drops the scope sub-score).

**Interpretation.** Objective signals and the judge agree on *direction* but not
on *value*: measured signals cannot, by themselves, tell a high-value bug fix
from a cosmetic nit (both have passing tests and tight diffs), whereas the judge
can. Conversely the objective signals independently catch the scope violation.
This is the empirical justification for the blend — and for keeping the LLM as
*one* input rather than the oracle. The number is small-n and stated as such; see
[`../benchmark/README.md`](../benchmark/README.md) for the full table and limits.

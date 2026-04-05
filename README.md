# AveryLoop

An autonomous code audit → implement → review → merge pipeline powered by Claude. Uses specialized LLM agents (auditor, implementer, reviewer) with RAG-based codebase retrieval to continuously improve any code repository.

## Features

- **Four-agent pipeline**: audit, implement, review, merge — each with configurable system prompts
- **LLM-as-judge evaluation** with calibration examples and multi-dimensional scoring (specificity, accuracy, coverage, prioritization, domain appropriateness)
- **RAG context retrieval** via ChromaDB — semantic code search with language-aware chunking (Python, MATLAB)
- **Automatic diminishing returns detection** — stops the loop when merge rates drop, importance stalls, and the same files keep surfacing
- **Safety flags** for domain-specific risks (e.g. `LEAKAGE_RISK`, `PHI_RISK`) — configurable per project with critical flags that block automatic merge
- **Git branch isolation** per fix with post-merge test validation — each improvement gets its own branch, tests run before and after merge
- **Full iteration logging** with score drift detection, finding deduplication, and structured JSON history

## Installation

From source (editable):

```bash
git clone https://github.com/akarlin3/averyLoop.git
cd averyloop
pip install -e ".[test]"
```

From PyPI (when published):

```bash
pip install averyloop
```

### Requirements

- Python >= 3.10
- An [Anthropic API key](https://console.anthropic.com/)
- Git (the loop creates branches and merges)

## Quick Start

```bash
# 1. Copy the example config
cp project_config.example.yaml project_config.yaml

# 2. Edit it with your repo's source directories, test command, and prompts
#    (see Configuration below)

# 3. Set your API key in project_config.yaml (anthropic_api_key field)

# 4. Run a single iteration to test
averyloop --single-iteration

# 5. Run the full loop (up to 10 iterations by default)
averyloop

# 6. Dry run (no API calls, no code changes — validates setup)
averyloop --dry-run
```

## Configuration

The loop is configured through two files:

### `project_config.yaml` — Project-Specific Settings

Controls what gets audited, how tests run, and what prompts the agents use. The loader searches in order:

1. Explicit path passed to `load_project_config()`
2. `PROJECT_CONFIG` environment variable
3. `./project_config.yaml`
4. `./averyloop_project.yaml`

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | `""` | Short project name, shown in logs and index metadata |
| `description` | string | `""` | One-liner for audit context |
| `languages` | list | `["python"]` | Languages present in the codebase |
| `default_branch` | string | `"main"` | Branch to merge improvements into |
| `branch_prefix` | string | `"improvement/"` | Prefix for improvement branches |
| `source_dirs` | list | `["src/"]` | Directories containing source code to audit |
| `test_command` | string | `"python -m pytest tests/ -q --tb=short"` | Shell command to run the test suite |
| `test_ignores` | list | `[]` | Pytest paths/patterns to skip |
| `read_only_dirs` | list | `[]` | Directories the loop may read but must never modify |
| `skip_dirs` | list | `[".git", "__pycache__"]` | Directories to skip when indexing/scanning |
| `key_files` | list | `[]` | Important files the auditor should always review |
| `anthropic_api_key` | string | `""` | API key (falls back to loop config) |
| `audit_model` | string | `""` | Model for audits (falls back to loop config default) |
| `fix_model` | string | `""` | Model for fixes (falls back to loop config default) |
| `judge_model` | string | `""` | Model for scoring (falls back to loop config default) |
| `collection_name` | string | `"codebase_index"` | ChromaDB collection name |
| `skip_extensions` | list | `[".png", ".jpg", ".pdf"]` | File extensions to skip when indexing |

#### Prompts (nested under `prompts:`)

| Field | Description |
|---|---|
| `audit_system` | System prompt for the auditor agent — defines what to look for and how to format findings |
| `review_system` | System prompt for the reviewer agent — defines how to evaluate proposed patches |
| `judge_system` | System prompt for the judge — defines scoring dimensions and output format |
| `judge_calibration` | Calibration examples for the judge — anchors scores to concrete examples |

#### Safety

| Field | Type | Default | Description |
|---|---|---|---|
| `risk_flags` | list | `["LEAKAGE_RISK", "PHI_RISK"]` | Flags the auditor/judge may raise |
| `critical_flags` | list | `["LEAKAGE_RISK", "PHI_RISK"]` | Flags that block automatic merge |
| `forbidden_patterns` | list | `[]` | Regex patterns that must never appear in patches |

See [`project_config.example.yaml`](project_config.example.yaml) for the full schema with inline comments.

### `averyloop_config.json` — Loop Tuning

Controls API models, token limits, exit strategy, and diminishing returns thresholds. All fields have sensible defaults — this file is optional.

| Field | Default | Description |
|---|---|---|
| `exit_strategy` | `"both"` | `"classic"`, `"diminishing_returns"`, or `"both"` |
| `importance_threshold` | `2` | Findings >= this keep the loop going |
| `min_coverage_score` | `6.0` | Coverage below this keeps the loop going |
| `dr_window` | `4` | Iterations to examine for diminishing returns |
| `audit_model` | `"claude-opus-4-6"` | Model for code audits |
| `fix_model` | `"claude-opus-4-6"` | Model for generating fixes |
| `judge_model` | `"claude-opus-4-6"` | Model for scoring audits |
| `audit_max_tokens` | `32000` | Max tokens for audit responses |
| `max_api_retries` | `3` | Retries on rate limit errors |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Orchestrator (v2)                     │
│                                                         │
│  for each iteration:                                    │
│    1. Gather context from prior iterations               │
│    2. Run audit (Auditor Agent + RAG context)            │
│    3. Parse findings into typed Finding objects           │
│    4. For each finding:                                  │
│       a. Create branch                                   │
│       b. Generate fix (Implementer)                      │
│       c. Run tests (syntax check + test suite)           │
│       d. Merge if tests pass                             │
│    5. Score the audit (Judge Agent)                       │
│    6. Log iteration + check exit conditions               │
└─────────────────────────────────────────────────────────┘
```

### Agents

| Agent | Module | Role |
|---|---|---|
| **Auditor** | `agents/auditor.py` | Scans the codebase and returns structured JSON findings with file, line, severity, and suggested fix |
| **Implementer** | `agents/implementer.py` | Takes a finding and the original file, returns the complete updated file |
| **Reviewer** | `agents/reviewer.py` | Evaluates patches for correctness, test coverage, and convention adherence |
| **Judge** | `evaluator.py` | Scores the audit on 6 dimensions (0-10 each), returns flags for safety issues |

### RAG Layer

| Module | Role |
|---|---|
| `rag/chunker.py` | Splits source files into semantic chunks (by class/function for Python, by `function` keyword for MATLAB) |
| `rag/indexer.py` | Builds and queries a ChromaDB vector index for semantic code search |

### Supporting Modules

| Module | Role |
|---|---|
| `project_config.py` | Loads project-specific YAML config with cached singleton |
| `loop_config.py` | Loads loop tuning JSON config with cached singleton |
| `loop_tracker.py` | Logs iterations, tracks findings, detects score drift |
| `git_utils.py` | Branch creation, checkout, merge, test runners, syntax checks |

### Exit Conditions

The loop stops when one of these is met:

1. **Classic**: No findings above the importance threshold AND audit coverage is sufficient AND no critical flags
2. **Diminishing returns**: Over the last N iterations, merge rate is low, average importance is low, the same files keep appearing, and audit scores aren't improving
3. **Max iterations reached**

## Adapting to Your Project

To use AveryLoop on a new codebase:

### 1. Define your source layout

```yaml
source_dirs: ["src/", "lib/"]
test_command: "python -m pytest tests/ -v --timeout=60"
read_only_dirs: ["vendor/", "third_party/"]
skip_dirs: [".git", "__pycache__", "node_modules"]
key_files: ["src/core/engine.py", "src/api/routes.py"]
```

### 2. Write domain-specific prompts

The most important step. Good prompts tell the auditor what matters in your domain:

```yaml
prompts:
  audit_system: |
    You are a senior code auditor for a [YOUR DOMAIN] project.
    Focus on:
    - [Domain-specific concern 1]
    - [Domain-specific concern 2]
    - Code quality and test coverage

    When you find an issue, return a JSON object with:
    file, line_start, line_end, severity, category, description,
    suggested_fix, and any risk flags.

  judge_calibration: |
    Scoring calibration for [YOUR PROJECT]:
    - 9-10: [What constitutes a critical fix in your domain]
    - 7-8:  [Significant improvements]
    - 5-6:  [Minor improvements]
    - 1-4:  [Low-value changes]
```

### 3. Configure safety flags

```yaml
risk_flags: ["SECURITY_RISK", "DATA_LOSS_RISK"]
critical_flags: ["SECURITY_RISK"]  # These block auto-merge
forbidden_patterns:
  - "password"
  - "api_key"
  - '\beval\s*\('
```

## Troubleshooting

### "No module named 'anthropic'"

Install dependencies: `pip install -e ".[test]"`

### Loop exits immediately

Check `averyloop_log.json` for the exit reason. Common causes:
- All findings below `importance_threshold` (default: 2)
- Audit coverage score above `min_coverage_score` (default: 6.0)
- No findings returned by the auditor (check your `source_dirs` and `key_files`)

### Rate limit errors

The loop retries automatically with exponential backoff. If persistent:
- Increase `retry_base_delay` in `averyloop_config.json`
- Reduce `audit_max_tokens` to lower per-request cost
- Use a smaller model for fixes: set `fix_model` to `"claude-sonnet-4-6"`

### Tests fail after merge

The loop runs tests both pre-merge and post-merge. If post-merge tests fail, the finding is logged as `"implemented"` (not `"merged"`). Check the log for details and manually resolve conflicts.

### Diminishing returns triggers too early

Increase the thresholds in `averyloop_config.json`:
```json
{
  "dr_window": 6,
  "dr_max_merge_rate": 0.25,
  "dr_max_avg_importance": 5.0
}
```

### Finding the log

Iteration history is in `averyloop_log.json` at the repo root. View a summary:
```bash
python -m averyloop.loop_tracker summary
```

## Development

```bash
# Install with test dependencies
pip install -e ".[test]"

# Run the test suite
python -m pytest tests/ -q

# Run a specific test file
python -m pytest tests/test_evaluator_finding.py -v
```

### Project Structure

```
averyloop/
├── __init__.py
├── project_config.py      # Project-specific YAML config loader
├── loop_config.py          # Loop tuning JSON config loader
├── evaluator.py            # Judge scoring + exit logic + Finding model
├── git_utils.py            # Git operations + test runners
├── loop_tracker.py         # Iteration logging + context generation
├── orchestrator_v2.py      # Main loop: audit → fix → test → merge
├── agents/
│   ├── _api.py             # Shared API client + retry logic
│   ├── auditor.py          # Audit prompt + source file collection
│   ├── implementer.py      # Fix generation from findings
│   └── reviewer.py         # Review prompt for patch evaluation
└── rag/
    ├── chunker.py           # Language-aware code chunking
    ├── indexer.py           # ChromaDB index build + query
    └── retriever.py         # Semantic code retrieval
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-change`)
3. Make your changes and add tests
4. Run the test suite (`python -m pytest tests/ -q`)
5. Commit and push
6. Open a pull request

## License

GNU AGPL v3 — see [LICENSE](LICENSE).

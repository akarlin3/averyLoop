"""Multi-repo orchestration — sweep AveryLoop over N repositories sequentially.

A single invocation processes each repo in a ``repos.yaml`` manifest one after
another, each with **its own** config, project config, iteration log, and
outcome-memory store, then prints a cross-repo summary.

Isolation (the whole point)
---------------------------
A single-repo run binds to the process cwd and a handful of import-time module
globals (the log path in :mod:`loop_tracker`, the git root in
:mod:`git_utils`, and the cached loop/project configs).  Running two repos in
one process would otherwise cross-contaminate logs and the outcome store.

:func:`repo_session` is a context manager that rebinds every one of those for
the duration of a single repo and restores them afterwards:

* **cwd** → the repo (so cwd-relative ``project_config.yaml`` discovery resolves
  to the right repo);
* **git root** (:func:`git_utils.set_repo_root`) → git operations and the diff
  helper act on this repo, not the ambient cwd;
* **log path** (:func:`loop_tracker.set_log_path`) → ``<repo>/averyloop_log.json``;
* **loop + project config caches** are reset and reloaded per repo;
* the **outcome store** (ChromaDB ``.chromadb/``) is already keyed on the repo
  root the orchestrator passes, so each repo accumulates its own memory.

Because the orchestrator reads the active repo root dynamically
(``orchestrator_v2._repo_root`` → ``git_utils.REPO_ROOT``), rebinding here is
enough — no value is frozen at import time.

The manifest format::

    defaults:
      max_iterations: 10
      dry_run: false
    repos:
      - path: ../prismtask           # required (abs, or relative to the manifest)
        name: prismtask              # optional (defaults to the basename)
        config: averyloop_config.json  # optional loop-config path (rel to repo)
        max_iterations: 5            # optional per-repo override
      - ../annealmusic               # shorthand: a bare path string
"""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import yaml

from averyloop import git_utils
from averyloop import loop_tracker
from averyloop.convergence import evaluate_convergence


# ---------------------------------------------------------------------------
# Manifest model
# ---------------------------------------------------------------------------

@dataclass
class RepoSpec:
    """One repository entry from the manifest."""

    path: str
    name: str = ""
    config_path: Optional[str] = None
    max_iterations: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.name:
            self.name = os.path.basename(self.path.rstrip("/")) or self.path


@dataclass
class RepoResult:
    """Per-repo outcome of a sweep."""

    name: str
    path: str
    iterations: int = 0
    fixes_merged: int = 0
    convergence_reason: str = ""
    elapsed_sec: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def load_manifest(path: str) -> Tuple[List[RepoSpec], dict]:
    """Parse a ``repos.yaml`` manifest into specs + a defaults dict.

    Relative repo paths resolve against the manifest's directory; a relative
    per-repo ``config`` path resolves against that repo.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    defaults = raw.get("defaults", {}) or {}
    base_dir = os.path.dirname(os.path.abspath(path))

    specs: List[RepoSpec] = []
    for item in raw.get("repos", []) or []:
        if isinstance(item, str):
            item = {"path": item}
        repo_path = item["path"]
        if not os.path.isabs(repo_path):
            repo_path = os.path.normpath(os.path.join(base_dir, repo_path))

        config_path = item.get("config")
        if config_path and not os.path.isabs(config_path):
            config_path = os.path.join(repo_path, config_path)

        specs.append(RepoSpec(
            path=repo_path,
            name=item.get("name", ""),
            config_path=config_path,
            max_iterations=item.get("max_iterations"),
        ))
    return specs, defaults


# ---------------------------------------------------------------------------
# Per-repo session (the isolation boundary)
# ---------------------------------------------------------------------------

@contextmanager
def repo_session(repo_root: str, *, config_path: Optional[str] = None):
    """Bind all per-repo global state for the duration of one repo, then restore.

    See the module docstring for what is rebound and why.  Restoration runs in
    a ``finally`` so an exception in one repo cannot leak its bindings into the
    next.
    """
    import averyloop.loop_config as lc
    import averyloop.project_config as pc

    repo_root = os.path.abspath(repo_root)
    saved_cwd = os.getcwd()
    saved_git = git_utils.REPO_ROOT
    saved_log = loop_tracker.LOG_FILE
    try:
        os.chdir(repo_root)
        git_utils.set_repo_root(repo_root)
        loop_tracker.set_log_path(os.path.join(repo_root, "averyloop_log.json"))

        # Per-repo loop config: explicit path or the repo-local default. After a
        # reset, get_config() loads from the path given on this first call.
        lc.reset_config()
        lc.get_config(config_path or os.path.join(repo_root, "averyloop_config.json"))

        # Per-repo project config: cwd now points at this repo, so the default
        # search (./project_config.yaml) resolves to this repo's file.
        pc.reset_project_config_cache()
        yield repo_root
    finally:
        os.chdir(saved_cwd)
        git_utils.set_repo_root(saved_git)
        loop_tracker.set_log_path(saved_log)
        # Drop the per-repo caches so the next caller reloads against its own
        # (now-restored) context rather than seeing this repo's config.
        lc.reset_config()
        pc.reset_project_config_cache()


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def _stop_reason(entries: List[dict], max_iterations: int) -> str:
    """Explain why a repo's loop stopped, from its log entries."""
    if not entries:
        return "no iterations"
    if entries[-1].get("exit_condition_met"):
        return "exit condition met"
    if len(entries) < max_iterations:
        # Stopped early without an explicit exit flag → convergence detector.
        return evaluate_convergence(entries).reason
    return "max_iterations reached"


def run_repos(
    specs: List[RepoSpec],
    *,
    max_iterations: int = 10,
    dry_run: bool = False,
    run_fn: Optional[Callable[..., list]] = None,
) -> List[RepoResult]:
    """Process each repo sequentially under its own session.

    *run_fn* defaults to the real loop (``orchestrator_v2.run_loop``) but can be
    injected for offline tests.  A failure in one repo is recorded and the sweep
    continues with the next.
    """
    if run_fn is None:
        from averyloop.orchestrator_v2 import run_loop as run_fn  # noqa: N806

    results: List[RepoResult] = []
    for spec in specs:
        result = RepoResult(name=spec.name, path=spec.path)
        iters = spec.max_iterations or max_iterations
        start = time.monotonic()
        try:
            with repo_session(spec.path, config_path=spec.config_path):
                entries = run_fn(max_iterations=iters, dry_run=dry_run) or []
                result.iterations = len(entries)
                result.fixes_merged = sum(
                    len(e.get("branches_merged", []) or []) for e in entries
                )
                result.convergence_reason = _stop_reason(entries, iters)
        except Exception as e:  # one repo's failure must not abort the sweep
            result.error = str(e)
        finally:
            result.elapsed_sec = round(time.monotonic() - start, 3)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_summary(results: List[RepoResult]) -> str:
    """Render the cross-repo summary table."""
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append(f"  Multi-repo sweep — {len(results)} repo(s)")
    lines.append("=" * 72)
    lines.append(f"  {'repo':<20} {'iters':>5} {'merged':>6} {'time(s)':>8}  reason")
    lines.append("  " + "-" * 68)
    total_iters = total_merged = 0
    for r in results:
        total_iters += r.iterations
        total_merged += r.fixes_merged
        reason = r.error and f"ERROR: {r.error}" or r.convergence_reason
        lines.append(
            f"  {r.name:<20} {r.iterations:>5} {r.fixes_merged:>6} "
            f"{r.elapsed_sec:>8.2f}  {reason[:32]}"
        )
    lines.append("  " + "-" * 68)
    lines.append(f"  {'TOTAL':<20} {total_iters:>5} {total_merged:>6}")
    n_err = sum(1 for r in results if r.error)
    if n_err:
        lines.append(f"  {n_err} repo(s) errored — see the reason column.")
    lines.append("=" * 72)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the AveryLoop improvement loop across multiple repos"
    )
    parser.add_argument("--repos", "--manifest", dest="manifest",
                        default="repos.yaml",
                        help="Path to the repos.yaml manifest (default: repos.yaml)")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="Override max iterations for every repo")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run without API calls or code changes")
    parser.add_argument("--json", type=str, default=None,
                        help="Write machine-readable per-repo results to this path")
    args = parser.parse_args(argv)

    if not os.path.exists(args.manifest):
        parser.error(f"manifest not found: {args.manifest}")

    specs, defaults = load_manifest(args.manifest)
    if not specs:
        print(f"No repos listed in {args.manifest}.")
        return 0

    max_iter = args.max_iterations or int(defaults.get("max_iterations", 10))
    dry_run = args.dry_run or bool(defaults.get("dry_run", False))

    print(f"Sweeping {len(specs)} repo(s) from {args.manifest} "
          f"(max_iterations={max_iter}, dry_run={dry_run})")
    results = run_repos(specs, max_iterations=max_iter, dry_run=dry_run)
    print("\n" + format_summary(results))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([r.to_dict() for r in results], fh, indent=2)
        print(f"\nWrote per-repo results JSON to {args.json}")

    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Zero-config CLI entry point for AveryLoop.

Provides the ``averyloop`` console script configured in ``pyproject.toml``::

    averyloop run --repo ./target-repo --dry-run
    averyloop --repo ./target-repo --dry-run
    averyloop --repos repos.yaml --dry-run
    averyloop summary --repo ./target-repo
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from averyloop import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="averyloop",
        description=(
            "AveryLoop — Autonomous code audit -> implement -> review -> "
            "gated merge CLI pipeline."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"averyloop {__version__}",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=".",
        help="Path to the target Git repository (default: current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in strict dry-run mode without API calls or repository mutations",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=10,
        help="Maximum number of audit/implement/review/merge iterations (default: 10)",
    )
    parser.add_argument(
        "--single-iteration",
        action="store_true",
        help="Run exactly one iteration (shorthand for --max-iterations 1)",
    )
    parser.add_argument(
        "--repos",
        type=str,
        default=None,
        help="Path to a repos.yaml manifest to sweep multiple repositories sequentially",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional path to averyloop_config.json",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="Optional path to a .env file containing provider API keys",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["run", "summary", "benchmark"],
        default="run",
        help="Subcommand to execute: 'run' (default), 'summary', or 'benchmark'",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Primary CLI entry point (``averyloop.cli:main``)."""
    args_list = list(sys.argv[1:] if argv is None else argv)

    parser = _build_parser()
    args = parser.parse_args(args_list)

    # Load .env credentials if present
    from averyloop.loop_config import load_dotenv_file
    if args.env_file:
        load_dotenv_file(args.env_file, override=True)
    else:
        load_dotenv_file()

    if args.command == "benchmark":
        from benchmark.runner import main as bench_main
        return bench_main(["--max-iterations", str(args.max_iterations)])

    if args.command == "summary":
        from averyloop import loop_tracker
        from averyloop.multi import repo_session
        target = os.path.abspath(args.repo)
        with repo_session(target, config_path=args.config):
            loop_tracker.print_full_summary()
        return 0

    # Multi-repo sweep mode
    if args.repos:
        from averyloop import multi
        max_iter = 1 if args.single_iteration else args.max_iterations
        multi_argv = ["--repos", args.repos, "--max-iterations", str(max_iter)]
        if args.dry_run:
            multi_argv.append("--dry-run")
        return multi.main(multi_argv)

    # Single-repo run mode
    from averyloop.multi import repo_session
    from averyloop.orchestrator_v2 import run_loop

    max_iter = 1 if args.single_iteration else args.max_iterations
    target_repo = os.path.abspath(args.repo)
    if not os.path.isdir(target_repo):
        parser.error(f"Target repository directory does not exist: {args.repo}")

    with repo_session(target_repo, config_path=args.config):
        run_loop(max_iterations=max_iter, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

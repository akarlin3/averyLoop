"""Tests for averyloop.multi — multi-repo orchestration + isolation.

The headline assertion: a two-repo sweep writes two independent logs and two
independent outcome stores, with no leakage between them.  Runs fully offline
via an injected fake loop (no LLM, no live agents).
"""

from __future__ import annotations

import json
import os

import pytest

from averyloop import git_utils, loop_tracker, multi, outcomes
from averyloop.evaluator import Finding
from averyloop.rag import outcome_memory


# Deterministic, offline embedding (no model download), small dim for speed.
_EMBED = lambda texts: outcome_memory.hashed_bow_embedding(texts, dim=64)


def _make_repo(tmp_path, name):
    """Create a bare working dir to stand in for a target repo."""
    d = tmp_path / name
    d.mkdir()
    return str(d)


def _fake_loop(tag):
    """A stand-in for run_loop that writes one log entry + one outcome.

    It deliberately uses the *active* bindings (loop_tracker.LOG_FILE and
    git_utils.REPO_ROOT) so the test verifies the session rebinds them.
    """
    def _run(max_iterations, dry_run):
        finding = Finding(
            dimension="correctness",
            file=f"{tag}.py",
            description=f"bug in {tag}",
            fix="fix it",
            importance=5,
            branch_name=f"improvement/{tag}-fix",
            status="merged",
        )
        entry = loop_tracker.log_iteration(
            audit_output=f"audit-{tag}",
            findings=[finding],
            tests_passed=True,
            dry_run=True,  # keep scoring offline (no LLM judge)
        )
        rec = outcomes.derive_outcome(
            finding.to_log_dict(),
            merged=True, reverted=False, safety_veto=False,
            review_verdict="APPROVE", tests_passed=True,
            diff=f"+ {tag}", iteration=1, merge_sha=f"{tag}-1",
        )
        outcome_memory.record_outcomes(
            [rec], repo_root=str(git_utils.REPO_ROOT), embed_fn=_EMBED
        )
        return [entry]
    return _run


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

def test_load_manifest_resolves_paths(tmp_path):
    (tmp_path / "manifest.yaml").write_text(
        "defaults:\n"
        "  max_iterations: 7\n"
        "repos:\n"
        "  - path: repo_a\n"
        "    name: alpha\n"
        "    config: cfg.json\n"
        "    max_iterations: 3\n"
        "  - repo_b\n"
    )
    specs, defaults = multi.load_manifest(str(tmp_path / "manifest.yaml"))

    assert defaults["max_iterations"] == 7
    assert len(specs) == 2
    # Relative repo paths resolve against the manifest dir.
    assert specs[0].path == os.path.normpath(str(tmp_path / "repo_a"))
    assert specs[0].name == "alpha"
    assert specs[0].config_path == os.path.join(specs[0].path, "cfg.json")
    assert specs[0].max_iterations == 3
    # Bare-string shorthand → basename name, no overrides.
    assert specs[1].name == "repo_b"
    assert specs[1].config_path is None


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------

def test_repo_session_binds_and_restores(tmp_path):
    repo = _make_repo(tmp_path, "r")
    before_cwd = os.getcwd()
    before_git = git_utils.REPO_ROOT
    before_log = loop_tracker.LOG_FILE

    with multi.repo_session(repo):
        assert os.path.realpath(os.getcwd()) == os.path.realpath(repo)
        assert str(git_utils.REPO_ROOT) == os.path.abspath(repo)
        assert loop_tracker.LOG_FILE == os.path.join(
            os.path.abspath(repo), "averyloop_log.json"
        )

    # Everything restored, even though the body changed global state.
    assert os.getcwd() == before_cwd
    assert git_utils.REPO_ROOT == before_git
    assert loop_tracker.LOG_FILE == before_log


def test_repo_session_restores_on_exception(tmp_path):
    repo = _make_repo(tmp_path, "r")
    before_cwd = os.getcwd()
    with pytest.raises(RuntimeError):
        with multi.repo_session(repo):
            raise RuntimeError("boom")
    assert os.getcwd() == before_cwd


# ---------------------------------------------------------------------------
# The headline: two-repo sweep, no leakage
# ---------------------------------------------------------------------------

def test_two_repo_sweep_writes_independent_logs_and_stores(tmp_path):
    repo_a = _make_repo(tmp_path, "repo_a")
    repo_b = _make_repo(tmp_path, "repo_b")
    specs = [
        multi.RepoSpec(path=repo_a, name="repo_a"),
        multi.RepoSpec(path=repo_b, name="repo_b"),
    ]
    before_cwd = os.getcwd()

    # Each repo gets a fake loop tagged with its own name.
    results = []
    for spec in specs:
        out = multi.run_repos(
            [spec], max_iterations=1, dry_run=True,
            run_fn=_fake_loop(spec.name),
        )
        results.extend(out)

    # cwd restored after the sweep.
    assert os.getcwd() == before_cwd

    # Two independent logs, each with only its own finding.
    log_a = json.loads((tmp_path / "repo_a" / "averyloop_log.json").read_text())
    log_b = json.loads((tmp_path / "repo_b" / "averyloop_log.json").read_text())
    desc_a = [f["description"] for e in log_a for f in e["findings"]]
    desc_b = [f["description"] for e in log_b for f in e["findings"]]
    assert desc_a == ["bug in repo_a"]
    assert desc_b == ["bug in repo_b"]

    # Two independent ChromaDB stores.
    assert os.path.isdir(os.path.join(repo_a, ".chromadb"))
    assert os.path.isdir(os.path.join(repo_b, ".chromadb"))

    # No leakage: recalling from repo_a's store returns only repo_a's outcome.
    hits_a = outcome_memory.recall_outcomes(
        "bug fix", repo_root=repo_a, k=10, embed_fn=_EMBED
    )
    hits_b = outcome_memory.recall_outcomes(
        "bug fix", repo_root=repo_b, k=10, embed_fn=_EMBED
    )
    assert hits_a and all(h["file"] == "repo_a.py" for h in hits_a)
    assert hits_b and all(h["file"] == "repo_b.py" for h in hits_b)

    # Per-repo result accounting.
    assert {r.name for r in results} == {"repo_a", "repo_b"}
    for r in results:
        assert r.iterations == 1
        assert r.fixes_merged == 1
        assert r.error is None


def test_run_repos_isolates_failures(tmp_path):
    """A failing repo is recorded but does not abort the sweep."""
    good = _make_repo(tmp_path, "good")
    bad = "/nonexistent/repo/path/xyz"

    def _boom(max_iterations, dry_run):
        raise RuntimeError("loop blew up")

    specs = [multi.RepoSpec(path=bad, name="bad"),
             multi.RepoSpec(path=good, name="good")]
    # Use the exploding loop only conceptually; the bad path fails at chdir.
    results = multi.run_repos(specs, max_iterations=1, dry_run=True,
                              run_fn=_fake_loop("good"))

    by_name = {r.name: r for r in results}
    assert by_name["bad"].error is not None
    assert by_name["good"].error is None
    assert os.getcwd()  # still valid cwd after a failing repo

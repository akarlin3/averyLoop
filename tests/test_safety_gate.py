"""Tests for averyloop.safety_gate — the deterministic, non-LLM merge veto.

These are the highest-value tests: they assert the authored safety logic,
proving all four veto cases plus a clean pass. No LLM calls.
"""

from __future__ import annotations

from averyloop.safety_gate import evaluate_safety, SafetyVerdict


def _diff(path, added=(), removed=()):
    """Build a minimal unified diff touching *path*."""
    lines = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        "@@ -1,1 +1,1 @@",
    ]
    lines += [f"-{r}" for r in removed]
    lines += [f"+{a}" for a in added]
    return "\n".join(lines) + "\n"


# ── Clean in-scope diff passes ───────────────────────────────────────────────

def test_clean_in_scope_diff_passes():
    diff = _diff("src/module.py", added=["x = compute()", "return x"])
    verdict = evaluate_safety(diff, {"src/module.py"})
    assert isinstance(verdict, SafetyVerdict)
    assert verdict.veto is False
    assert verdict.violations == []


# ── Veto 1: out-of-scope write ───────────────────────────────────────────────

def test_veto_on_out_of_scope_write():
    diff = _diff("src/other.py", added=["sneaky = 1"])
    verdict = evaluate_safety(diff, {"src/module.py"})
    assert verdict.veto is True
    assert any("out-of-scope" in v for v in verdict.violations)


def test_allowlist_exempts_out_of_scope_write():
    diff = _diff("src/other.py", added=["ok = 1"])
    verdict = evaluate_safety(
        diff, {"src/module.py"}, allowlist_paths=["src/other.py"]
    )
    assert verdict.veto is False


def test_no_intended_scope_skips_scope_check():
    diff = _diff("src/anything.py", added=["a = 1"])
    verdict = evaluate_safety(diff, intended_files=None)
    assert verdict.veto is False


# ── Veto 2: assertion removal ────────────────────────────────────────────────

def test_veto_on_assertion_removal():
    diff = _diff(
        "tests/test_thing.py",
        removed=["    assert result == 42"],
        added=["    pass"],
    )
    verdict = evaluate_safety(diff, {"tests/test_thing.py"})
    assert verdict.veto is True
    assert any("assertion weakening" in v for v in verdict.violations)


def test_pytest_raises_removal_flagged():
    diff = _diff(
        "tests/test_thing.py",
        removed=["    with pytest.raises(ValueError):"],
        added=["    pass"],
    )
    verdict = evaluate_safety(diff, {"tests/test_thing.py"})
    assert verdict.veto is True


def test_assertion_removal_in_non_test_file_ok():
    # Removing a line containing 'assert' from a NON-test file is not flagged
    # by the assertion-weakening rule.
    diff = _diff("src/module.py", removed=["assert_called = True"],
                 added=["assert_called = False"])
    verdict = evaluate_safety(diff, {"src/module.py"})
    assert not any("assertion weakening" in v for v in verdict.violations)


# ── Veto 3: credential pattern ───────────────────────────────────────────────

def test_veto_on_aws_key():
    diff = _diff("src/module.py", added=["KEY = 'AKIAIOSFODNN7EXAMPLE'"])
    verdict = evaluate_safety(diff, {"src/module.py"})
    assert verdict.veto is True
    assert any("credential-like" in v for v in verdict.violations)


def test_veto_on_hardcoded_password():
    diff = _diff("src/module.py", added=['password = "hunter2hunter2"'])
    verdict = evaluate_safety(diff, {"src/module.py"})
    assert verdict.veto is True
    assert any("credential-like" in v for v in verdict.violations)


def test_veto_on_private_key_block():
    diff = _diff("src/module.py",
                 added=["-----BEGIN RSA PRIVATE KEY-----"])
    verdict = evaluate_safety(diff, {"src/module.py"})
    assert verdict.veto is True


# ── Veto 4: protected self-edit ──────────────────────────────────────────────

def test_veto_on_self_edit_of_safety_code():
    diff = _diff("averyloop/safety_gate.py", added=["    return SafetyVerdict(False, [])"])
    verdict = evaluate_safety(
        diff,
        {"averyloop/safety_gate.py"},  # even "in scope", protected wins
        protected_paths=["averyloop/safety_gate.py", "averyloop/convergence.py"],
    )
    assert verdict.veto is True
    assert any("protected self-edit" in v for v in verdict.violations)


# ── Read-only / denylist ─────────────────────────────────────────────────────

def test_veto_on_read_only_dir_write():
    diff = _diff("vendor/lib.py", added=["hacked = 1"])
    verdict = evaluate_safety(
        diff, intended_files=None, read_only_dirs=["vendor/"]
    )
    assert verdict.veto is True
    assert any("read-only" in v for v in verdict.violations)


# ── Multiple violations accumulate ───────────────────────────────────────────

def test_multiple_violations_collected():
    diff = _diff(
        "tests/test_thing.py",
        removed=["    assert x == 1"],
        added=["    token = 'ghp_abcdefghijklmnopqrstuvwxyz0123'"],
    )
    # out-of-scope (intended elsewhere) + assertion removal + credential
    verdict = evaluate_safety(diff, {"src/module.py"})
    assert verdict.veto is True
    assert len(verdict.violations) >= 3

"""Deterministic safety gate.

A **code-level, non-LLM** veto on merges.  The judge (``reviewer.py`` /
``evaluator.py``) can be wrong or prompt-injected; this gate is defense in
depth — one safety layer that is provably *ours* and cannot be talked out of
its verdict.  It is wired into ``_phase_test_and_merge`` *before* the merge and
runs *in addition to* the judge-emitted critical flags, never instead of them.

:func:`evaluate_safety` is a **pure function**: diff text (plus the audit's
intended scope and config-driven path lists) in, a :class:`SafetyVerdict` out.
No LLM, no git, no disk I/O.

Veto rules:

1. **out-of-scope write** — a changed file that was not in the audit's
   intended scope (and not on the allowlist).
2. **read-only / denylisted write** — a changed file under a denylisted or
   read-only path prefix.
3. **protected self-edit** — a changed file that *is* the loop's own
   authored-logic / safety code (so a fix can't rewrite the gate that judges
   it).
4. **assertion weakening** — a removed (`-`) line inside a test file that
   contained an ``assert`` (or ``pytest.raises``), i.e. tests being deleted or
   weakened to force a green run.
5. **credential-like pattern** — an added (`+`) line that matches a known
   secret/credential shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set

from averyloop.signals import changed_files_in_diff

# Credential / secret shapes scanned on *added* lines.
_CREDENTIAL_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style secret key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (
        re.compile(
            r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token|token)"
            r"\s*[=:]\s*['\"][^'\"]{8,}['\"]"
        ),
        "hardcoded credential assignment",
    ),
]

# Assertion / test-strength markers whose *removal* is a red flag.
_ASSERTION_RE = re.compile(r"\b(assert|pytest\.raises|self\.assert\w+)\b")


@dataclass
class SafetyVerdict:
    """Outcome of the deterministic safety gate.

    Attributes
    ----------
    veto:
        ``True`` if the merge must be blocked.
    violations:
        Human-readable descriptions of each rule that fired (empty when clean).
    """

    veto: bool
    violations: List[str] = field(default_factory=list)


def _path_matches(path: str, prefixes: Iterable[str]) -> bool:
    """True if *path* equals or sits under any of *prefixes*."""
    norm = path.lstrip("./")
    for prefix in prefixes:
        p = prefix.lstrip("./").rstrip("/")
        if not p:
            continue
        if norm == p or norm.startswith(p + "/"):
            return True
    return False


def _is_test_file(path: str) -> bool:
    """Heuristic: does *path* look like a test module?"""
    name = path.rsplit("/", 1)[-1]
    return (
        "test" in path.split("/")[:-1] and path.endswith(".py")
    ) or name.startswith("test_") or name.endswith("_test.py") or "/tests/" in path


def evaluate_safety(
    diff: str,
    intended_files: Optional[Iterable[str]] = None,
    *,
    protected_paths: Optional[Iterable[str]] = None,
    denylist_paths: Optional[Iterable[str]] = None,
    allowlist_paths: Optional[Iterable[str]] = None,
    read_only_dirs: Optional[Iterable[str]] = None,
) -> SafetyVerdict:
    """Evaluate a proposed *diff* against the deterministic safety rules.

    Parameters
    ----------
    diff:
        Unified diff of the proposed change.
    intended_files:
        Files the audit finding meant to touch.  If ``None`` (or empty), the
        out-of-scope check is skipped — the other rules still apply.
    protected_paths, denylist_paths, allowlist_paths, read_only_dirs:
        Config-driven path lists.  Allowlisted files are exempt from the
        out-of-scope check.

    Returns
    -------
    SafetyVerdict
    """
    protected = list(protected_paths or [])
    denylist = list(denylist_paths or [])
    allowlist = set(allowlist_paths or [])
    read_only = list(read_only_dirs or [])
    intended: Set[str] = set(intended_files or [])

    violations: List[str] = []

    changed = changed_files_in_diff(diff)

    # ── Path-based rules ─────────────────────────────────────────────────
    for path in sorted(changed):
        if _path_matches(path, protected):
            violations.append(f"protected self-edit: '{path}' is loop safety code")
        if _path_matches(path, denylist) or _path_matches(path, read_only):
            violations.append(f"read-only/denylisted write: '{path}'")
        if intended and path not in intended and path not in allowlist:
            violations.append(
                f"out-of-scope write: '{path}' not in intended scope "
                f"{sorted(intended)}"
            )

    # ── Content-based rules (per diff line) ──────────────────────────────
    # Track which file each hunk belongs to so assertion-removal only fires on
    # test files.
    current_file: Optional[str] = None
    for line in (diff or "").splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):].strip()
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue

        if line.startswith("-") and not line.startswith("---"):
            removed = line[1:]
            if current_file and _is_test_file(current_file) and _ASSERTION_RE.search(removed):
                violations.append(
                    f"assertion weakening: removed test check in "
                    f"'{current_file}': {removed.strip()[:80]}"
                )
        elif line.startswith("+") and not line.startswith("+++"):
            added = line[1:]
            for pattern, label in _CREDENTIAL_PATTERNS:
                if pattern.search(added):
                    violations.append(
                        f"credential-like pattern ({label}) introduced"
                        + (f" in '{current_file}'" if current_file else "")
                    )
                    break

    return SafetyVerdict(veto=bool(violations), violations=violations)

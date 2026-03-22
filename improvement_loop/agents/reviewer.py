"""Reviewer agent — evaluates proposed patches for correctness and quality."""

from improvement_loop.project_config import get_project_config


# ---------------------------------------------------------------------------
# Default review prompt — used when ProjectConfig.review_system_prompt is empty
# ---------------------------------------------------------------------------

DEFAULT_REVIEW_PROMPT = """\
You are a code reviewer. Evaluate proposed patches for:
- Correctness of the implementation
- No introduction of regressions or security issues
- Adequate test coverage for the change
- Adherence to existing project conventions
- No modifications to read-only directories

Provide a structured review with:
1. A summary of what the patch does
2. Issues found (if any)
3. A verdict: APPROVE, REQUEST_CHANGES, or REJECT
"""


def get_review_system_prompt() -> str:
    """Return the review system prompt from ProjectConfig, or the default."""
    pcfg = get_project_config()
    prompt = pcfg.review_system_prompt or DEFAULT_REVIEW_PROMPT

    # Inject read-only directories warning if configured
    read_only = pcfg.read_only_dirs
    if read_only:
        read_only_str = ", ".join(f"`{d}`" for d in read_only)
        prompt += f"\n\nIMPORTANT: The following directories are READ-ONLY — " \
                  f"patches must not modify files in: {read_only_str}"

    return prompt

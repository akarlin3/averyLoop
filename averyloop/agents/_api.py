"""Shared API helper — provider-agnostic client construction and rate-limit retry."""

import json
import os
import time
import urllib.request

import anthropic  # type: ignore

from averyloop.loop_config import get_config
from averyloop.project_config import get_project_config


def resolve_api_credentials() -> tuple[str, str]:
    """Return ``(provider, api_key)`` resolved from config or environment.

    Resolution order:
        1. Explicit provider in ProjectConfig or LoopConfig
        2. First available API key across Anthropic, Gemini, and OpenAI
           (checking ProjectConfig, LoopConfig, then environment variables)
    """
    project_cfg = get_project_config()
    loop_cfg = get_config()

    anthropic_key = (
        project_cfg.anthropic_api_key
        or loop_cfg.anthropic_api_key
        or os.environ.get("ANTHROPIC_API_KEY", "")
    )
    gemini_key = (
        getattr(project_cfg, "gemini_api_key", "")
        or getattr(loop_cfg, "gemini_api_key", "")
        or os.environ.get("GEMINI_API_KEY", "")
    )
    openai_key = (
        getattr(project_cfg, "openai_api_key", "")
        or getattr(loop_cfg, "openai_api_key", "")
        or os.environ.get("OPENAI_API_KEY", "")
    )

    explicit_provider = (
        getattr(project_cfg, "provider", "")
        or getattr(loop_cfg, "provider", "auto")
    ).lower()

    if explicit_provider == "gemini" and gemini_key:
        return "gemini", gemini_key
    if explicit_provider == "openai" and openai_key:
        return "openai", openai_key
    if explicit_provider == "anthropic" and anthropic_key:
        return "anthropic", anthropic_key

    if anthropic_key:
        return "anthropic", anthropic_key
    if gemini_key:
        return "gemini", gemini_key
    if openai_key:
        return "openai", openai_key

    raise ValueError(
        "No LLM API key found. Set ANTHROPIC_API_KEY, GEMINI_API_KEY, or "
        "OPENAI_API_KEY in your .env / environment, project_config.yaml, or "
        "averyloop_config.json."
    )


def get_client() -> anthropic.Anthropic:
    """Return an Anthropic client using config or environment API key."""
    project_cfg = get_project_config()
    loop_cfg = get_config()
    api_key = (
        project_cfg.anthropic_api_key
        or loop_cfg.anthropic_api_key
        or os.environ.get("ANTHROPIC_API_KEY", "")
    )
    if not api_key:
        raise ValueError(
            "No anthropic_api_key found in project_config.yaml, "
            "averyloop_config.json, or ANTHROPIC_API_KEY environment variable."
        )
    return anthropic.Anthropic(api_key=api_key)


def _call_openai_compatible(api_key: str, create_kwargs: dict) -> str:
    """Call OpenAI chat completions endpoint via stdlib HTTPS."""
    model = create_kwargs.get("model", "gpt-4o")
    if model.startswith("claude-"):
        model = "gpt-4o"
    messages = []
    if create_kwargs.get("system"):
        messages.append({"role": "system", "content": create_kwargs["system"]})
    messages.extend(create_kwargs.get("messages", []))
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": create_kwargs.get("max_tokens", 4096),
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _call_gemini(api_key: str, create_kwargs: dict) -> str:
    """Call Google Gemini generateContent endpoint via stdlib HTTPS."""
    model = create_kwargs.get("model", "gemini-2.5-pro")
    if model.startswith("claude-"):
        model = "gemini-2.5-pro"
    parts = []
    if create_kwargs.get("system"):
        parts.append({"text": create_kwargs["system"]})
    for msg in create_kwargs.get("messages", []):
        parts.append({"text": msg.get("content", "")})
    payload = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {
            "maxOutputTokens": create_kwargs.get("max_tokens", 8192),
        },
    }).encode("utf-8")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["candidates"][0]["content"]["parts"][0]["text"]


def api_call_with_retry(create_kwargs: dict) -> str:
    """Call the configured LLM provider with rate-limit retry.  Returns response text.

    Defaults to Anthropic streaming (avoiding the SDK's 10-minute timeout guard
    for large max_tokens values) and transparently supports Gemini and OpenAI
    when configured via ``GEMINI_API_KEY`` or ``OPENAI_API_KEY``.
    """
    cfg = get_config()
    provider, api_key = resolve_api_credentials()
    if provider == "gemini":
        return _call_gemini(api_key, create_kwargs)
    if provider == "openai":
        return _call_openai_compatible(api_key, create_kwargs)

    client = get_client()
    for attempt in range(1, cfg.max_api_retries + 1):
        try:
            collected: list[str] = []
            with client.messages.stream(**create_kwargs) as stream:
                for text in stream.text_stream:
                    collected.append(text)
            return "".join(collected)
        except anthropic.RateLimitError:
            delay = cfg.retry_base_delay * attempt
            print(f"    Rate limited (attempt {attempt}/{cfg.max_api_retries}), "
                  f"waiting {delay:.0f}s...")
            time.sleep(delay)
            if attempt == cfg.max_api_retries:
                raise
        except anthropic.APIError:
            raise
    return ""  # unreachable

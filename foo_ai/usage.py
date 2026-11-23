"""Token and prompt-cache figures reported by an OpenAI-compatible server."""

from __future__ import annotations

from typing import Any

# Official DeepSeek V4 Flash and Pro, and the local V4.1 Flash server.
DEFAULT_CONTEXT_WINDOW = 1_000_000


def parse_usage(raw: object) -> dict[str, int] | None:
    """Read DeepSeek's cache fields, or OpenAI's nested cached_tokens."""
    if not isinstance(raw, dict):
        return None
    prompt = _count(raw.get("prompt_tokens"))
    if prompt == 0:
        prompt = _count(raw.get("input_tokens")) + _count(raw.get("cache_creation_input_tokens")) + _count(raw.get("cache_read_input_tokens"))
    completion = _count(raw.get("completion_tokens")) or _count(raw.get("output_tokens"))
    details = raw.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else 0
    hit = _count(raw.get("prompt_cache_hit_tokens")) or cached or _count(raw.get("cache_read_input_tokens"))
    miss = _count(raw.get("prompt_cache_miss_tokens"))
    if miss == 0 and hit > 0 and prompt > hit:
        miss = prompt - hit
    completion_details = raw.get("completion_tokens_details")
    reasoning = _count(completion_details.get("reasoning_tokens")) if isinstance(completion_details, dict) else 0
    if prompt == 0 and completion == 0 and hit == 0 and miss == 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "reasoning_tokens": reasoning,
    }


def format_tokens(tokens: int) -> str:
    """Compact count: 178100 -> 178.1K, 1000000 -> 1M."""
    if tokens <= 0:
        return "0"
    if tokens >= 1_000_000:
        return _trim(tokens / 1_000_000) + "M"
    if tokens >= 1000:
        return _trim(tokens / 1000) + "K"
    return str(tokens)


def format_usage_line(usage: dict[str, int], context_window: int) -> str:
    """One status line: context fill, input and output counts, cache hit rate.

    The two totals are this request's input tokens and output tokens.
    Cache is hit / (hit + miss) for this request, printed only when the
    server reported those fields.
    """
    prompt = usage["prompt_tokens"]
    completion = usage["completion_tokens"]
    if context_window > 0:
        context = f"context {format_tokens(prompt)}/{format_tokens(context_window)}"
    else:
        context = f"context {format_tokens(prompt)}"
    line = f"{context} · usage total {prompt} {completion}"
    hit = usage["cache_hit_tokens"]
    miss = usage["cache_miss_tokens"]
    if hit + miss > 0:
        line += f" · cache {hit / (hit + miss) * 100:.2f}%"
    return line


def _trim(value: float) -> str:
    text = f"{value:.1f}"
    if text.endswith(".0"):
        return text[:-2]
    return text


def _count(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0

"""Usage parsing and the context line."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    hit: int = 0
    miss: int = 0


def count(value: object) -> int:
    """Turn one usage field into a non-negative int. A bool counts as 0."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        truncated = int(value)
        return truncated if truncated > 0 else 0
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return 0


def parse_usage(raw: object) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    prompt = count(raw.get("prompt_tokens"))
    if prompt == 0:
        prompt = (
            count(raw.get("input_tokens"))
            + count(raw.get("cache_creation_input_tokens"))
            + count(raw.get("cache_read_input_tokens"))
        )
    completion = count(raw.get("completion_tokens"))
    if completion == 0:
        completion = count(raw.get("output_tokens"))
    hit = count(raw.get("prompt_cache_hit_tokens"))
    if hit == 0:
        details = raw.get("prompt_tokens_details")
        if isinstance(details, dict):
            hit = count(details.get("cached_tokens"))
    if hit == 0:
        hit = count(raw.get("cache_read_input_tokens"))
    miss = count(raw.get("prompt_cache_miss_tokens"))
    if miss == 0 and hit > 0 and prompt > hit:
        miss = prompt - hit
    return Usage(prompt=prompt, completion=completion, hit=hit, miss=miss)


def compact(value: int) -> str:
    if value <= 0:
        return "0"
    if value >= 1_000_000:
        return _one_decimal(value / 1_000_000) + "M"
    if value >= 1000:
        return _one_decimal(value / 1000) + "K"
    return str(value)


def _one_decimal(value: float) -> str:
    text = f"{value:.1f}"
    if text.endswith(".0"):
        return text[:-2]
    return text


def format_usage_line(usage: Usage, context_window: int) -> str | None:
    if usage.prompt == 0 and usage.completion == 0 and usage.hit == 0 and usage.miss == 0:
        return None
    if context_window > 0:
        line = f"context {compact(usage.prompt)}/{compact(context_window)}"
    else:
        line = f"context {compact(usage.prompt)}"
    line += f" · usage total {usage.prompt} {usage.completion}"
    if usage.hit + usage.miss > 0:
        percent = usage.hit / (usage.hit + usage.miss) * 100
        line += f" · cache {percent:.2f}%"
    return line

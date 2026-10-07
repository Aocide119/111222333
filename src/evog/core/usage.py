"""Token accounting: uncached input + cached input + output, counted once."""

from __future__ import annotations

from typing import Any


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _first(*values: Any) -> int | None:
    return next((count for value in values if (count := _count(value)) is not None), None)


def normalize_usage(raw: Any) -> dict[str, int]:
    """Normalize Chat Completions counters without fabricating cache coverage.

    ``prompt_tokens`` includes cached input in the OpenAI-compatible protocol.
    DeepSeek may additionally return cache-hit and cache-miss counters. Explicit
    disjoint counters take precedence; otherwise derive the missing part from
    the inclusive input counter. Missing cache details do not imply zero hits.
    Reasoning tokens are already part of output and are never an extra addend.
    Keep reported totals for auditing, even when the component sum differs.
    """
    if not isinstance(raw, dict):
        return {}
    prompt_details = raw.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    output_details = raw.get("completion_tokens_details")
    output_details = output_details if isinstance(output_details, dict) else {}
    prompt = _count(raw.get("prompt_tokens"))
    cached = _first(
        raw.get("cached_prompt_tokens"),
        raw.get("prompt_cache_hit_tokens"),
        raw.get("cached_tokens"),
        prompt_details.get("cached_tokens"),
    )
    uncached = _first(
        raw.get("uncached_prompt_tokens"),
        raw.get("prompt_cache_miss_tokens"),
        prompt_details.get("uncached_tokens"),
    )
    output = _count(raw.get("completion_tokens"))
    reasoning = _first(raw.get("reasoning_tokens"), output_details.get("reasoning_tokens"))
    reported_total = _count(raw.get("reported_total_tokens"))
    total = _count(raw.get("total_tokens"))
    reported_prompt = _count(raw.get("reported_prompt_tokens"))
    reported_cached = _count(raw.get("reported_cached_prompt_tokens"))
    reported_uncached = _count(raw.get("reported_uncached_prompt_tokens"))
    if uncached is not None and cached is not None:
        combined = uncached + cached
        if prompt is not None and prompt != combined and reported_prompt is None:
            reported_prompt = prompt
        prompt = combined
    elif prompt is not None:
        if cached is not None and cached > prompt:
            reported_cached, cached = cached, None
        if uncached is not None and uncached > prompt:
            reported_uncached, uncached = uncached, None
        if cached is not None and cached <= prompt:
            uncached = prompt - cached
        elif uncached is not None and uncached <= prompt:
            cached = prompt - uncached

    usage = {
        key: value
        for key, value in {
            "prompt_tokens": prompt,
            "uncached_prompt_tokens": uncached,
            "cached_prompt_tokens": cached,
            "completion_tokens": output,
            "reasoning_tokens": reasoning,
            "reported_prompt_tokens": reported_prompt,
            "reported_cached_prompt_tokens": reported_cached,
            "reported_uncached_prompt_tokens": reported_uncached,
        }.items()
        if value is not None
    }
    if prompt is not None and output is not None:
        usage["total_tokens"] = prompt + output
        if total is not None and total != usage["total_tokens"] and reported_total is None:
            reported_total = total
    elif total is not None:
        usage["total_tokens"] = total
    if reported_total is not None:
        usage["reported_total_tokens"] = reported_total
    return usage


def token_total(usage: dict[str, Any]) -> int | None:
    """Use the component sum when available, then the inclusive or reported total."""
    return normalize_usage(usage).get("total_tokens")


def token_components(usage: dict[str, Any]) -> dict[str, int | None]:
    normalized = normalize_usage(usage)
    return {
        "uncached_input": normalized.get("uncached_prompt_tokens"),
        "cached_input": normalized.get("cached_prompt_tokens"),
        "output": normalized.get("completion_tokens"),
    }


def aggregate_usage(calls: list[dict[str, Any]]) -> dict[str, int]:
    """Sum only counters measured on every call; partial cache sums are not totals."""
    normalized = [normalize_usage(usage) for usage in calls]
    if not normalized:
        return {}
    complete = set(normalized[0]).intersection(*(set(usage) for usage in normalized[1:]))
    return {key: sum(usage[key] for usage in normalized) for key in sorted(complete)}

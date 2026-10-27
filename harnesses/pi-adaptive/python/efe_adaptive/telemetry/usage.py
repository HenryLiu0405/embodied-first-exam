"""Normalize observed provider token usage without inventing missing measurements."""

import math
from typing import Any


def _mapping(value: Any) -> dict[str, Any]:
    """Accept JSON objects and treat other optional payloads as absent."""
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    """Accept finite nonnegative numeric measurements, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _first_number(value: dict[str, Any], *keys: str) -> int | None:
    """Read the first valid integral token count among provider aliases."""
    for key in keys:
        number = _number(value.get(key))
        if number is not None and number.is_integer():
            return int(number)
    return None


def normalize_model_usage(value: Any) -> dict[str, Any]:
    """Normalize raw provider usage, keeping totals and their subsets separate.

    OpenAI completion/output totals already include reasoning. Gemini candidate
    totals and queue-worker visible totals exclude thoughts; combine them only
    when the separate thought count is known. Cached tokens are an input subset.
    """

    usage = _mapping(value)
    nested = _mapping(usage.get("usageMetadata") or usage.get("usage_metadata"))
    if nested:
        usage = nested
    prompt = _first_number(usage, "input_tokens", "prompt_tokens", "prompt_token_count", "promptTokenCount")
    reasoning = _first_number(
        usage, "reasoning_tokens", "num_reasoning_tokens", "thoughts_token_count", "thoughtsTokenCount"
    )
    if reasoning is None:
        reasoning = _first_number(
            _mapping(usage.get("completion_tokens_details") or usage.get("output_tokens_details")), "reasoning_tokens"
        )
    cached = _first_number(
        usage, "cached_input_tokens", "cached_tokens", "cached_content_token_count", "cachedContentTokenCount"
    )
    if cached is None:
        cached = _first_number(
            _mapping(usage.get("prompt_tokens_details") or usage.get("input_tokens_details")), "cached_tokens"
        )
    visible = _first_number(usage, "visible_output_tokens", "candidates_token_count", "candidatesTokenCount")
    output = _first_number(usage, "output_tokens", "completion_tokens")
    if "num_reasoning_tokens" in usage and "completion_tokens" in usage and visible is None:
        # Historical Gemini queue workers called candidate-only tokens completion.
        visible = _first_number(usage, "completion_tokens")
        output = None
    total = _first_number(usage, "total_tokens", "total_token_count", "totalTokenCount")
    warnings: list[str] = []
    if visible is not None:
        if reasoning is not None and output is None:
            output = visible + reasoning
        elif reasoning is not None and output != visible + reasoning:
            warnings.append("visible plus reasoning differs from inclusive output total")
        elif output is None and total is not None and prompt is not None and total >= prompt + visible:
            output = total - prompt
            reasoning = output - visible
    elif output is not None and reasoning is not None and reasoning <= output:
        visible = output - reasoning
    if cached is not None and prompt is not None and cached > prompt:
        warnings.append("cached input exceeds input total")
        cached = None
    if reasoning is not None and output is not None and reasoning > output:
        warnings.append("reasoning exceeds output total")
        reasoning = None
        visible = None
    if total is None and prompt is not None and output is not None:
        total = prompt + output
    if total is not None and prompt is not None and output is not None and total != prompt + output:
        warnings.append("provider total differs from input plus output")
    return {
        "input_tokens": prompt,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "visible_output_tokens": visible,
        "total_tokens": total,
        "warnings": warnings,
    }

"""
Anthropic / Claude wrapper — shared LLM access for all agents.

Architecture principle (per repo CLAUDE.md): rule-based first, LLM second.
This module is the single integration point for the Claude API. Any agent
needing natural-language generation, classification, or summarization
calls into here rather than instantiating its own anthropic.Anthropic
client.

Why centralize:
  - One place to enforce model tier discipline (CLAUDE_MODEL_CHEAP /
    DEFAULT / COMPLEX from .env)
  - One place to track usage / cost (each call logs token usage)
  - One place to handle the "key not set" / "no credit" graceful
    degradation pattern callers expect
  - One place to update if/when we switch SDK versions

Usage
-----
    from common.llm import call_claude, LLMUnavailableError

    try:
        text, usage = call_claude("Summarize this in 3 bullets:\\n" + data)
    except LLMUnavailableError as exc:
        # API key missing, credit exhausted, etc. Fall back to rule-based.
        logger.warning("LLM unavailable: %s", exc)
        text = rule_based_summary(data)
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from common.config import settings


logger = logging.getLogger(__name__)


# Pricing per 1M tokens (USD), observed 2026-05. Update when Anthropic changes.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {"in": 0.80, "out": 4.00},
    "claude-haiku-4-5":          {"in": 0.80, "out": 4.00},
    "claude-sonnet-4-6":         {"in": 3.00, "out": 15.00},
    "claude-opus-4-6":           {"in": 15.00, "out": 75.00},
}

Tier = Literal["cheap", "default", "complex"]


class LLMUnavailableError(RuntimeError):
    """Raised when the Claude API can't be used (no key, no credit, etc.)."""


def _model_for_tier(tier: Tier) -> str:
    if tier == "cheap":
        return settings.CLAUDE_MODEL_CHEAP
    if tier == "complex":
        return settings.CLAUDE_MODEL_COMPLEX
    return settings.CLAUDE_MODEL_DEFAULT


def _estimate_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        return 0.0
    return (in_tokens * pricing["in"] + out_tokens * pricing["out"]) / 1_000_000


def call_claude(
    prompt: str,
    *,
    tier: Tier = "default",
    model: str | None = None,
    system: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 1.0,
) -> tuple[str, dict]:
    """
    Single-turn completion. Returns (text, usage_dict).

    usage_dict shape: {input_tokens, output_tokens, model, est_cost_usd}.
    Token counts come straight from the API response. Cost is estimated
    from our local MODEL_PRICING table.

    Raises LLMUnavailableError on any auth/credit/import failure so callers
    can fall back gracefully to rule-based output.
    """
    key = os.environ.get("ANTHROPIC_API_KEY") or settings.ANTHROPIC_API_KEY
    if not key or key in ("sk-ant-...", "your_key_here"):
        raise LLMUnavailableError("ANTHROPIC_API_KEY not set in .env")

    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailableError(f"anthropic SDK not installed: {exc}") from exc

    chosen_model = model or _model_for_tier(tier)
    client = anthropic.Anthropic(api_key=key)

    kwargs = {
        "model":       chosen_model,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "messages":    [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    try:
        resp = client.messages.create(**kwargs)
    except anthropic.APIStatusError as exc:
        # 400 with "credit balance" is the common one; surface clearly.
        msg = getattr(exc, "message", None) or str(exc)
        if "credit" in msg.lower() or "balance" in msg.lower():
            raise LLMUnavailableError(f"Anthropic API: credit issue — {msg}") from exc
        raise LLMUnavailableError(f"Anthropic API error: {msg}") from exc
    except anthropic.AnthropicError as exc:
        raise LLMUnavailableError(f"Anthropic SDK error: {exc}") from exc

    text = "".join(block.text for block in resp.content if hasattr(block, "text"))
    usage = {
        "input_tokens":  resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "model":         chosen_model,
        "est_cost_usd":  round(_estimate_cost(chosen_model,
                                              resp.usage.input_tokens,
                                              resp.usage.output_tokens), 6),
    }
    logger.info("Claude %s: in=%d, out=%d, est_cost=$%.4f",
                chosen_model, usage["input_tokens"], usage["output_tokens"],
                usage["est_cost_usd"])
    return text, usage


__all__ = ["call_claude", "LLMUnavailableError", "MODEL_PRICING"]

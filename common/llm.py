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
    from common.llm import call_claude, call_claude_json, LLMUnavailableError

    try:
        text, usage = call_claude("Summarize this in 3 bullets:\\n" + data)
    except LLMUnavailableError as exc:
        # API key missing, credit exhausted, etc. Fall back to rule-based.
        logger.warning("LLM unavailable: %s", exc)
        text = rule_based_summary(data)

Brand identity belongs in `system=`, never in the user prompt. Use
`common.tenant.system_preamble()` to build it — a client name interpolated
into a user prompt cannot be fixed by configuration.

    from common.tenant import system_preamble
    text, usage = call_claude(prompt, system=system_preamble("You are an SEO analyst."))

JSON responses
--------------
`call_claude_json()` handles the fence-stripping, brace-slicing and shape
normalization that every JSON-returning caller was reimplementing. It never
raises on a malformed response — it returns the fallback and reports why —
because a half-parsed model response should degrade like an unavailable API,
not crash a batch job.

Retries and budget
------------------
Transient failures (429, 5xx, timeouts, connection errors) retry with
exponential backoff, honouring `Retry-After`. Auth and credit failures do not
retry — they will not get better. A per-process spend ceiling
(`LLM_BUDGET_USD`, default $5) stops a runaway loop from quietly spending
real money; it raises `LLMBudgetExceeded`, which subclasses
`LLMUnavailableError` so existing degrade paths catch it unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Literal

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

# Retry policy. Deliberately short: agents are batch jobs behind a cron, and
# a request that needs more than three attempts is better reported than
# waited on.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SEC = 2.0
MAX_BACKOFF_SEC = 30.0
REQUEST_TIMEOUT_SEC = 120.0

# Per-process spend ceiling. Not a substitute for billing alerts — it exists
# so a bug in a loop costs cents rather than the whole balance.
DEFAULT_BUDGET_USD = 5.0

_budget_lock = threading.Lock()
_spend_usd = 0.0


class LLMUnavailableError(RuntimeError):
    """Raised when the Claude API can't be used (no key, no credit, etc.)."""


class LLMBudgetExceeded(LLMUnavailableError):
    """
    Raised when this process has spent its ceiling.

    Subclasses LLMUnavailableError on purpose: every existing caller already
    wraps calls in `except LLMUnavailableError` and degrades to rule-based
    output, which is exactly the right behaviour here too.
    """


def _model_for_tier(tier: Tier) -> str:
    if tier == "cheap":
        return settings.CLAUDE_MODEL_CHEAP
    if tier == "complex":
        return settings.CLAUDE_MODEL_COMPLEX
    return settings.CLAUDE_MODEL_DEFAULT


def _estimate_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        # An unknown model silently costing $0 would make the budget guard
        # useless exactly when a new model is introduced. Say so.
        logger.warning("No pricing for model %r — cost tracking is blind for "
                       "this call and it does not count against the budget", model)
        return 0.0
    return (in_tokens * pricing["in"] + out_tokens * pricing["out"]) / 1_000_000


def _budget_limit() -> float:
    raw = os.environ.get("LLM_BUDGET_USD")
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("LLM_BUDGET_USD=%r is not a number — using default", raw)
    return DEFAULT_BUDGET_USD


def _check_budget() -> None:
    limit = _budget_limit()
    if limit <= 0:
        return          # explicitly disabled
    with _budget_lock:
        spent = _spend_usd
    if spent >= limit:
        raise LLMBudgetExceeded(
            f"process LLM spend ${spent:.4f} has reached the ${limit:.2f} ceiling "
            f"(set LLM_BUDGET_USD to raise it, or 0 to disable)"
        )


def _record_spend(amount: float) -> float:
    global _spend_usd
    with _budget_lock:
        _spend_usd += amount
        return _spend_usd


def spend_so_far() -> float:
    """Total estimated USD this process has spent on Claude calls."""
    with _budget_lock:
        return _spend_usd


def reset_spend() -> None:
    """Zero the spend counter. For tests and long-lived processes."""
    global _spend_usd
    with _budget_lock:
        _spend_usd = 0.0


def _retry_after_seconds(exc: Any) -> float | None:
    """Honour a Retry-After header when the server sends one."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), MAX_BACKOFF_SEC)
    except (TypeError, ValueError):
        return None


def _is_retryable(status: int | None, msg: str) -> bool:
    """
    Rate limits, overload and server errors are worth retrying. Auth and
    credit failures are not — they will be identical on the next attempt,
    and retrying just delays the caller's fallback.
    """
    lowered = msg.lower()
    if "credit" in lowered or "balance" in lowered:
        return False
    if status in (401, 403):
        return False
    return status in (408, 409, 429, 500, 502, 503, 504, 529)


def call_claude(
    prompt: str,
    *,
    tier: Tier = "default",
    model: str | None = None,
    system: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 1.0,
    cache_system: bool = True,
) -> tuple[str, dict]:
    """
    Single-turn completion. Returns (text, usage_dict).

    usage_dict shape: {input_tokens, output_tokens, model, est_cost_usd,
    attempts, cache_read_tokens, cache_write_tokens}. Token counts come
    straight from the API response; cost is estimated from MODEL_PRICING.

    Raises LLMUnavailableError on any auth/credit/budget/import failure so
    callers can fall back gracefully to rule-based output.

    `cache_system` marks the system prompt as cacheable. The tenant preamble
    is identical across every call in a run, so caching it turns a repeated
    per-call charge into one write plus cheap reads. Harmless when the prompt
    is short — the API simply ignores blocks below the minimum size.
    """
    key = os.environ.get("ANTHROPIC_API_KEY") or settings.ANTHROPIC_API_KEY
    if not key or key in ("sk-ant-...", "your_key_here"):
        raise LLMUnavailableError("ANTHROPIC_API_KEY not set in .env")

    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailableError(f"anthropic SDK not installed: {exc}") from exc

    _check_budget()

    chosen_model = model or _model_for_tier(tier)
    client = anthropic.Anthropic(api_key=key, timeout=REQUEST_TIMEOUT_SEC)

    kwargs: dict[str, Any] = {
        "model":       chosen_model,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "messages":    [{"role": "user", "content": prompt}],
    }
    if system:
        if cache_system:
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            kwargs["system"] = system

    last_error: str = "unknown"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client.messages.create(**kwargs)
            break
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            msg = getattr(exc, "message", None) or str(exc)
            last_error = msg
            if not _is_retryable(status, msg) or attempt == MAX_ATTEMPTS:
                if "credit" in msg.lower() or "balance" in msg.lower():
                    raise LLMUnavailableError(
                        f"Anthropic API: credit issue — {msg}") from exc
                raise LLMUnavailableError(f"Anthropic API error: {msg}") from exc
            delay = _retry_after_seconds(exc) or min(
                BACKOFF_BASE_SEC * (2 ** (attempt - 1)), MAX_BACKOFF_SEC)
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            last_error = str(exc)
            if attempt == MAX_ATTEMPTS:
                raise LLMUnavailableError(
                    f"Anthropic API unreachable after {MAX_ATTEMPTS} attempts: {exc}"
                ) from exc
            delay = min(BACKOFF_BASE_SEC * (2 ** (attempt - 1)), MAX_BACKOFF_SEC)
        except anthropic.AnthropicError as exc:
            raise LLMUnavailableError(f"Anthropic SDK error: {exc}") from exc

        # Jitter so parallel workers don't retry in lockstep.
        delay += random.uniform(0, 0.5)
        logger.warning("Claude attempt %d/%d failed (%s) — retrying in %.1fs",
                       attempt, MAX_ATTEMPTS, last_error[:120], delay)
        time.sleep(delay)

    text = "".join(block.text for block in resp.content if hasattr(block, "text"))
    in_tokens = resp.usage.input_tokens
    out_tokens = resp.usage.output_tokens
    cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0

    cost = _estimate_cost(chosen_model, in_tokens, out_tokens)
    total = _record_spend(cost)

    usage = {
        "input_tokens":      in_tokens,
        "output_tokens":     out_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "model":             chosen_model,
        "est_cost_usd":      round(cost, 6),
        "attempts":          attempt,
    }
    logger.info("Claude %s: in=%d, out=%d, cache_r=%d, est_cost=$%.4f "
                "(process total $%.4f)",
                chosen_model, in_tokens, out_tokens, cache_read,
                usage["est_cost_usd"], total)
    return text, usage


# ---------------------------------------------------------------------------
# JSON responses
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*")
_FENCE_CLOSE_RE = re.compile(r"\s*```\s*$")


def extract_json(text: str) -> tuple[Any, str | None]:
    """
    Pull a JSON value out of a model response.

    Returns (value, error). `error` is None on success. Handles the three
    things models actually do: wrap the JSON in a code fence, prefix it with
    a sentence of preamble, or emit a bare array instead of an object.

    Separate from `call_claude_json` so callers holding a raw response — or
    a test — can reuse the parsing without a network call.
    """
    if not text or not text.strip():
        return None, "empty response"

    cleaned = _FENCE_CLOSE_RE.sub("", _FENCE_OPEN_RE.sub("", text.strip()))

    try:
        return json.loads(cleaned), None
    except (json.JSONDecodeError, ValueError):
        pass

    # Slice from the first opening bracket to its matching last one. Objects
    # and arrays are both valid top-level JSON and models emit either.
    best: tuple[int, int] | None = None
    for opener, closer in (("{", "}"), ("[", "]")):
        first, last = cleaned.find(opener), cleaned.rfind(closer)
        if first >= 0 and last > first and (best is None or first < best[0]):
            best = (first, last)
    if best is None:
        return None, "no JSON object or array found in the response"

    try:
        return json.loads(cleaned[best[0]:best[1] + 1]), None
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"malformed JSON: {exc}"


def call_claude_json(
    prompt: str,
    *,
    fallback: Any = None,
    require: tuple[str, ...] = (),
    **kwargs,
) -> tuple[Any, dict | None, str | None]:
    """
    Ask for JSON and get parsed JSON back. Returns (value, usage, error).

    Never raises. On any failure — unavailable API, unparseable response,
    missing required keys — returns `(fallback, usage_or_None, reason)`. The
    caller checks `error` and degrades.

    This exists because the fence-strip + brace-slice + validate sequence was
    being reimplemented in every module that wanted structured output, and
    each copy handled a slightly different subset of the failure modes.

    Parameters
    ----------
    require : tuple[str, ...]
        Keys that must be present in a returned object. A response missing
        one is treated as a failure rather than passed on half-formed —
        the alternative is a KeyError deep inside a caller.
    """
    kwargs.setdefault("temperature", 0.2)   # structure, not prose

    try:
        text, usage = call_claude(prompt, **kwargs)
    except LLMUnavailableError as exc:
        return fallback, None, str(exc)

    value, error = extract_json(text)
    if error:
        logger.warning("call_claude_json: %s (first 200 chars: %r)", error, text[:200])
        return fallback, usage, error

    if require:
        if not isinstance(value, dict):
            return fallback, usage, (
                f"expected a JSON object with keys {list(require)}, "
                f"got {type(value).__name__}"
            )
        missing = [k for k in require if k not in value]
        if missing:
            return fallback, usage, f"response missing required key(s): {missing}"

    return value, usage, None


__all__ = [
    "call_claude",
    "call_claude_json",
    "extract_json",
    "LLMUnavailableError",
    "LLMBudgetExceeded",
    "MODEL_PRICING",
    "spend_so_far",
    "reset_spend",
]

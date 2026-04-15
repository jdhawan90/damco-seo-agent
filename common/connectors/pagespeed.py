"""
PageSpeed Insights connector.

Simple wrapper around the PageSpeed Insights API v5. Returns Core Web Vitals
(field data preferred, lab data as fallback) plus the Lighthouse performance
score for a given URL.

Auth
----
Key-based (no OAuth). Set PAGESPEED_API_KEY in .env.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

import requests

from common.config import settings


logger = logging.getLogger(__name__)

ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
MAX_RETRIES = 3
TIMEOUT = 90

Strategy = Literal["mobile", "desktop"]


class PageSpeedError(RuntimeError):
    """Raised when PageSpeed Insights returns an error."""


def get_cwv_metrics(url: str, strategy: Strategy = "mobile") -> dict:
    """
    Fetch Core Web Vitals + performance score for a URL.

    Returns
    -------
    {
        "url": "...",
        "strategy": "mobile" | "desktop",
        "performance_score": 0-100 (int) or None,
        "lcp_ms":  int | None,
        "inp_ms":  int | None,
        "cls":     float | None,
        "source":  "field" | "lab" | "mixed",
        "raw":     <full API response>,
    }
    """
    if not settings.PAGESPEED_API_KEY:
        raise PageSpeedError("PAGESPEED_API_KEY is not set")

    params = {
        "url": url,
        "strategy": strategy,
        "category": "performance",
        "key": settings.PAGESPEED_API_KEY,
    }

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(ENDPOINT, params=params, timeout=TIMEOUT)
            if resp.status_code >= 500:
                raise requests.exceptions.HTTPError(f"PageSpeed {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                raise PageSpeedError(f"PageSpeed request failed after {MAX_RETRIES} attempts: {exc}") from exc
            backoff = 2.0 * (2 ** (attempt - 1))
            logger.warning("PageSpeed request failed (attempt %d/%d): %s — retrying in %.1fs",
                           attempt, MAX_RETRIES, exc, backoff)
            time.sleep(backoff)

    return _extract_metrics(url, strategy, data)


def _extract_metrics(url: str, strategy: str, data: dict) -> dict:
    loading = data.get("loadingExperience") or {}
    metrics = loading.get("metrics") or {}
    lh = data.get("lighthouseResult") or {}
    audits = lh.get("audits") or {}
    categories = lh.get("categories") or {}

    def field(key: str) -> float | None:
        m = metrics.get(key)
        return m.get("percentile") if m else None

    def lab_ms(audit_key: str) -> int | None:
        audit = audits.get(audit_key) or {}
        val = audit.get("numericValue")
        return int(round(val)) if val is not None else None

    def lab_float(audit_key: str) -> float | None:
        audit = audits.get(audit_key) or {}
        return audit.get("numericValue")

    lcp_ms = field("LARGEST_CONTENTFUL_PAINT_MS")
    if lcp_ms is None:
        lcp_ms = lab_ms("largest-contentful-paint")

    inp_ms = field("INTERACTION_TO_NEXT_PAINT")
    if inp_ms is None:
        # Lighthouse exposes INP as "experimental-interaction-to-next-paint" when present.
        inp_ms = lab_ms("experimental-interaction-to-next-paint")

    cls = field("CUMULATIVE_LAYOUT_SHIFT_SCORE")
    if cls is not None:
        cls = cls / 100.0  # field data is reported * 100
    else:
        cls = lab_float("cumulative-layout-shift")

    perf_score = None
    if "performance" in categories:
        score = categories["performance"].get("score")
        if score is not None:
            perf_score = int(round(score * 100))

    # Determine data source — 'field' if CrUX data exists, 'lab' if Lighthouse only.
    source = "field" if metrics else "lab"
    if metrics and any(lab_ms(k) for k in ("largest-contentful-paint", "experimental-interaction-to-next-paint")):
        source = "mixed"

    return {
        "url": url,
        "strategy": strategy,
        "performance_score": perf_score,
        "lcp_ms": int(round(lcp_ms)) if lcp_ms is not None else None,
        "inp_ms": int(round(inp_ms)) if inp_ms is not None else None,
        "cls": round(float(cls), 4) if cls is not None else None,
        "source": source,
        "raw": data,
    }


def get_performance_score(url: str, strategy: Strategy = "mobile") -> int | None:
    """Convenience: return just the Lighthouse performance score (0–100)."""
    return get_cwv_metrics(url, strategy)["performance_score"]


__all__ = [
    "PageSpeedError",
    "get_cwv_metrics",
    "get_performance_score",
]

"""
DataForSEO connector.

Wraps the subset of the DataForSEO REST API that our agents need. Presents
a clean, typed interface; handles auth, retries, queue selection, and basic
error mapping. No agent code should call DataForSEO directly.

Endpoints wrapped
-----------------
  get_serp_rankings(keywords, **opts)     — SERP/Google/Organic
  get_keyword_data(keywords, **opts)      — Keyword research (SV, KD, CPC)
  get_backlinks(target, **opts)           — Backlinks for a domain or URL
  get_onpage_audit(target, **opts)        — On-page SEO audit

Queue selection
---------------
DataForSEO offers two tiers:
  * "standard"  — async queue, ~$0.0006/query, results in ~1-5 min
  * "live"      — sync, ~$0.002/query, result in ~10s
Default is driven by settings.DATAFORSEO_DEFAULT_QUEUE. Override per-call
with the `queue` kwarg for one-off interactive runs.

Retry policy
------------
Transient 5xx and connection errors are retried up to MAX_RETRIES times
with exponential backoff. Validation errors (4xx) are raised immediately.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import requests
from requests.auth import HTTPBasicAuth

from common.config import settings


logger = logging.getLogger(__name__)

BASE_URL = "https://api.dataforseo.com/v3"
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2.0
REQUEST_TIMEOUT = 120

# Task-level success status from DataForSEO. Anything else is an error.
OK_STATUS_CODE = 20000


class DataForSEOError(RuntimeError):
    """Raised when DataForSEO returns a non-OK status or the request fails."""


def _auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(settings.DATAFORSEO_LOGIN, settings.DATAFORSEO_PASSWORD)


def _post(path: str, payload: list[dict]) -> dict:
    """POST to DataForSEO with retry on transient failure. Returns the parsed JSON."""
    url = f"{BASE_URL}{path}"
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url,
                json=payload,
                auth=_auth(),
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            # Retry on transient server-side errors.
            if resp.status_code >= 500:
                raise requests.exceptions.HTTPError(
                    f"DataForSEO {resp.status_code}", response=resp
                )
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning("DataForSEO request failed (attempt %d/%d): %s — retrying in %.1fs",
                           attempt, MAX_RETRIES, exc, backoff)
            time.sleep(backoff)
            continue

        data = resp.json()
        if data.get("status_code") != OK_STATUS_CODE:
            raise DataForSEOError(
                f"{path} returned status {data.get('status_code')}: "
                f"{data.get('status_message', 'unknown error')}"
            )
        return data

    raise DataForSEOError(f"DataForSEO {path} failed after {MAX_RETRIES} attempts: {last_exc}")


def _queue_path(domain: str, endpoint: str, queue: str | None) -> str:
    """
    Compose the path for a given queue tier.
    e.g. _queue_path("serp/google/organic", "regular", "standard")
         → "/serp/google/organic/task_post" (and results come via task_get later)

    For simplicity in this foundation, we expose the "live/regular" path
    when queue=="live" and "task_post" + polling when queue=="standard".
    Individual helpers choose their own path — this is a convention stub.
    """
    raise NotImplementedError("Use per-endpoint helpers below; they construct paths directly.")


# ---------------------------------------------------------------------------
# SERP rankings
# ---------------------------------------------------------------------------

def get_serp_rankings(
    keywords: Iterable[str],
    location_code: int | None = None,
    language_code: str | None = None,
    device: str | None = None,
    depth: int = 100,
    queue: str | None = None,
) -> list[dict]:
    """
    Fetch organic SERP results for a batch of keywords.

    Returns a list of result dicts, one per keyword, each of shape:
        {
            "keyword": "...",
            "items": [ {"rank_group": 1, "rank_absolute": 1, "domain": "...", "title": "...", "url": "..."}, ... ],
            "checked_at": "2026-04-15T12:34:56Z",
            "raw": <full DataForSEO result block>,
        }

    Parameters
    ----------
    keywords : iterable of str
        Up to 100 per batch (DataForSEO limit). Caller should chunk larger inputs.
    location_code, language_code, device
        Override per-call; defaults come from settings.
    depth : int
        How many organic results to retrieve (max 700 per DataForSEO docs).
    queue : {"standard", "live"} | None
        Override default queue. Standard is cheaper; Live is synchronous and faster.
    """
    keywords = list(keywords)
    if not keywords:
        return []
    if len(keywords) > 100:
        raise ValueError(
            f"get_serp_rankings accepts up to 100 keywords per call; got {len(keywords)}. "
            f"Chunk upstream."
        )

    queue = (queue or settings.DATAFORSEO_DEFAULT_QUEUE).lower()
    location_code = location_code or settings.DATAFORSEO_LOCATION_CODE
    language_code = language_code or settings.DATAFORSEO_LANGUAGE_CODE
    device = device or settings.DATAFORSEO_DEVICE

    payload = [
        {
            "keyword": kw,
            "location_code": location_code,
            "language_code": language_code,
            "device": device,
            "depth": depth,
        }
        for kw in keywords
    ]

    if queue == "live":
        path = "/serp/google/organic/live/regular"
        data = _post(path, payload)
        return _parse_serp_results(data)

    # Standard queue: post task, then poll for results. Most agents prefer this
    # because of the ~70% cost reduction.
    task_post = _post("/serp/google/organic/task_post", payload)
    task_ids = [t["id"] for t in task_post.get("tasks", []) if t.get("status_code") == OK_STATUS_CODE]
    if not task_ids:
        raise DataForSEOError("Standard-queue task_post returned no task IDs")

    return _poll_serp_tasks(task_ids)


def _poll_serp_tasks(task_ids: list[str], poll_interval: float = 15.0, max_wait: float = 600.0) -> list[dict]:
    deadline = time.monotonic() + max_wait
    pending = set(task_ids)
    results: list[dict] = []

    while pending and time.monotonic() < deadline:
        # task_get_ready lists tasks that have finished and are ready to be fetched.
        ready = _post("/serp/google/organic/tasks_ready", [{}])
        ready_ids = {t["id"] for t in ready.get("tasks", [])
                     if t.get("status_code") == OK_STATUS_CODE
                     for r in (t.get("result") or [])
                     for _ in [r]}  # flatten — we just need presence signals
        # Above is defensive; in practice ready["tasks"][0]["result"] is a list of {"id": ...}
        # Normalize:
        actual_ready: set[str] = set()
        for task in ready.get("tasks", []):
            for item in task.get("result") or []:
                if "id" in item:
                    actual_ready.add(item["id"])
        matched = pending & actual_ready

        for tid in list(matched):
            detail = _post(f"/serp/google/organic/task_get/regular/{tid}", [])
            results.extend(_parse_serp_results(detail))
            pending.discard(tid)

        if pending:
            time.sleep(poll_interval)

    if pending:
        raise DataForSEOError(
            f"Timed out waiting for {len(pending)} SERP task(s) to finish: {sorted(pending)}"
        )
    return results


def _parse_serp_results(data: dict) -> list[dict]:
    parsed: list[dict] = []
    for task in data.get("tasks", []):
        if task.get("status_code") != OK_STATUS_CODE:
            logger.warning("Skipping failed task %s: %s", task.get("id"), task.get("status_message"))
            continue
        for result in task.get("result") or []:
            items = [
                {
                    "rank_group": it.get("rank_group"),
                    "rank_absolute": it.get("rank_absolute"),
                    "domain": it.get("domain"),
                    "title": it.get("title"),
                    "url": it.get("url"),
                    "type": it.get("type"),
                }
                for it in (result.get("items") or [])
                if it.get("type") == "organic"
            ]
            parsed.append({
                "keyword": result.get("keyword"),
                "items": items,
                "checked_at": result.get("datetime"),
                "raw": result,
            })
    return parsed


# ---------------------------------------------------------------------------
# Keyword research
# ---------------------------------------------------------------------------

def get_keyword_data(keywords: Iterable[str], location_code: int | None = None,
                     language_code: str | None = None) -> list[dict]:
    """
    Fetch search volume, keyword difficulty, and CPC for a list of keywords.
    Uses the synchronous "live" endpoint because the response payload is small.
    """
    keywords = list(keywords)
    if not keywords:
        return []
    if len(keywords) > 1000:
        raise ValueError("get_keyword_data accepts up to 1000 keywords per call")

    payload = [{
        "keywords": keywords,
        "location_code": location_code or settings.DATAFORSEO_LOCATION_CODE,
        "language_code": language_code or settings.DATAFORSEO_LANGUAGE_CODE,
    }]
    data = _post("/keywords_data/google_ads/search_volume/live", payload)

    out: list[dict] = []
    for task in data.get("tasks", []):
        if task.get("status_code") != OK_STATUS_CODE:
            continue
        for result in task.get("result") or []:
            out.append({
                "keyword": result.get("keyword"),
                "search_volume": result.get("search_volume"),
                "keyword_difficulty": result.get("keyword_difficulty"),
                "cpc": result.get("cpc"),
                "competition": result.get("competition"),
                "raw": result,
            })
    return out


# ---------------------------------------------------------------------------
# Backlinks
# ---------------------------------------------------------------------------

def get_backlinks(target: str, limit: int = 1000, mode: str = "as_is") -> list[dict]:
    """
    Fetch backlinks pointing to a target URL or domain.

    Parameters
    ----------
    target : str
        The page URL or root domain to look up (e.g. "damcogroup.com" or a specific URL).
    limit : int
        Max number of backlinks to return (DataForSEO supports up to 1000 per call).
    mode : {"as_is", "one_per_domain", "one_per_anchor"}
        DataForSEO aggregation mode. "as_is" returns every backlink.
    """
    payload = [{"target": target, "limit": limit, "mode": mode}]
    data = _post("/backlinks/backlinks/live", payload)

    out: list[dict] = []
    for task in data.get("tasks", []):
        if task.get("status_code") != OK_STATUS_CODE:
            continue
        for result in task.get("result") or []:
            for item in result.get("items") or []:
                out.append({
                    "source_url": item.get("url_from"),
                    "source_domain": item.get("domain_from"),
                    "target_url": item.get("url_to"),
                    "anchor": item.get("anchor"),
                    "dofollow": item.get("dofollow"),
                    "rank": item.get("rank"),             # DataForSEO authority score
                    "first_seen": item.get("first_seen"),
                    "last_seen": item.get("last_seen"),
                    "raw": item,
                })
    return out


# ---------------------------------------------------------------------------
# On-page audit
# ---------------------------------------------------------------------------

def get_onpage_audit(target: str, max_crawl_pages: int = 100) -> dict:
    """
    Launch an on-page audit crawl for a domain and return the raw task id.
    On-page audits are async-only — caller is expected to poll results later.

    Returns {"task_id": "..."} on success.
    """
    payload = [{"target": target, "max_crawl_pages": max_crawl_pages}]
    data = _post("/on_page/task_post", payload)

    for task in data.get("tasks", []):
        if task.get("status_code") == OK_STATUS_CODE:
            return {"task_id": task["id"]}

    raise DataForSEOError("On-page audit task_post failed for all tasks")


__all__ = [
    "DataForSEOError",
    "get_serp_rankings",
    "get_keyword_data",
    "get_backlinks",
    "get_onpage_audit",
]

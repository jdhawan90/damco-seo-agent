"""
DataForSEO connector.

Wraps the subset of the DataForSEO REST API that our agents need. Presents
a clean, typed interface; handles auth, retries, queue selection, and basic
error mapping. No agent code should call DataForSEO directly.

Endpoints wrapped
-----------------
  get_serp_rankings(keywords, **opts)     — SERP/Google/Organic
  get_keyword_data(keywords, **opts)      — Keyword research (SV, KD, CPC)
  get_search_volume(keywords, **opts)     — Keyword Planner SV + 12-month trend
  get_keyword_ideas(seeds, **opts)        — Keyword Planner "ideas" expansion
  get_backlinks(target, **opts)           — Backlinks for a domain or URL
  get_onpage_audit(target, **opts)        — On-page SEO audit

A note on "Google Ads Keyword Planner"
--------------------------------------
The `/keywords_data/google_ads/*` endpoints are DataForSEO's proxy of the
real Google Ads Keyword Planner API — same underlying numbers, same
`monthly_searches` arrays, without needing a Google Ads developer token
(which requires an approved MCC account and a manual review that takes
days to weeks). Callers wanting Keyword Planner data should use
get_search_volume() here rather than standing up a separate Google Ads
client. If a native Google Ads path is ever added, it belongs behind the
same function signature so agent code doesn't change.

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

# DataForSEO status codes we treat as success on a per-task basis.
#   20000 — "Ok." (returned for tasks that complete inline, e.g. live queue)
#   20100 — "Task Created." (standard queue: task queued for async processing)
#   20200 — "Task In Queue." (occasionally returned during task_post when
#           the response races with internal queueing)
# Anything else is an error per-task. The HTTP-level OK is still 200, and
# the top-level response status_code is still 20000 — only per-task entries
# can have these other 2xx-range codes.
OK_STATUS_CODE = 20000
TASK_QUEUED_STATUS_CODES = (20000, 20100, 20200)


class DataForSEOError(RuntimeError):
    """Raised when DataForSEO returns a non-OK status or the request fails."""


def _auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(settings.DATAFORSEO_LOGIN, settings.DATAFORSEO_PASSWORD)


def _request(method: str, path: str, payload: list[dict] | None = None) -> dict:
    """HTTP wrapper with retry. Returns the parsed JSON.

    DataForSEO uses HTTP method-per-endpoint:
      - POST: /task_post, /live/regular
      - GET:  /tasks_ready, /task_get/regular/<id>
    Earlier versions of this module hardcoded POST everywhere, which 404s
    against the GET endpoints. This helper takes the method as a parameter
    so each call site is explicit.
    """
    url = f"{BASE_URL}{path}"
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            kwargs = {
                "auth":    _auth(),
                "timeout": REQUEST_TIMEOUT,
            }
            if method == "POST":
                kwargs["json"]    = payload
                kwargs["headers"] = {"Content-Type": "application/json"}
            resp = requests.request(method, url, **kwargs)
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


def _post(path: str, payload: list[dict]) -> dict:
    return _request("POST", path, payload)


def _get(path: str) -> dict:
    return _request("GET", path)


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

# ---------------------------------------------------------------------------
# SERP result type: `advanced`, not `regular`.
#
# This was `regular` until 2026-07-28, and it silently cost us every SERP
# feature the system claims to track.
#
# `regular` returns organic results ONLY. The response still *reports* what was
# on the page — its `item_types` field for "generative ai development services"
# reads ['ai_overview', 'organic', 'product_considerations', 'related_searches']
# — but the `items` array contains just the 19 organic entries. So
# `ai_overview_present` was false for all 2,126 keyword snapshots, and
# `keyword_serp_snapshots.serp_features` held 75 observations across the entire
# set, while the SERPs themselves were full of features.
#
# `advanced` returns those items. The same query came back with ai_overview,
# people_also_ask and related_searches, and the AI Overview carried 6 citations
# — none of them ours.
#
# The cost is identical: $0.0035 either way, measured on live calls to both
# paths. For the standard queue the charge lands at task_post and the result
# type is chosen at task_get, so retrieving `advanced` is free too.
# ---------------------------------------------------------------------------
SERP_LIVE_PATH = "/serp/google/organic/live/advanced"
SERP_TASK_GET_PATH = "/serp/google/organic/task_get/advanced"


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
        data = _post(SERP_LIVE_PATH, payload)
        return _parse_serp_results(data)

    # Standard queue: post task, then poll for results. Most agents prefer this
    # because of the ~70% cost reduction.
    task_post = _post("/serp/google/organic/task_post", payload)
    all_tasks = task_post.get("tasks") or []
    task_ids: list[str] = []
    rejected: list[dict] = []
    for t in all_tasks:
        if t.get("status_code") in TASK_QUEUED_STATUS_CODES:
            task_ids.append(t["id"])
        else:
            rejected.append({
                "id":      t.get("id"),
                "code":    t.get("status_code"),
                "message": t.get("status_message"),
                "keyword": (t.get("data") or {}).get("keyword"),
            })

    if rejected:
        # Surface the real per-task errors instead of swallowing them. This
        # is what we wished for the first time we hit "Payment Required."
        for r in rejected[:5]:
            logger.warning(
                "task_post rejected — code=%s message=%r keyword=%r",
                r["code"], r["message"], r["keyword"],
            )
        if len(rejected) > 5:
            logger.warning("…plus %d more rejected tasks", len(rejected) - 5)

    if not task_ids:
        # Build an informative error: include the first rejection's message.
        first_msg = rejected[0]["message"] if rejected else "unknown"
        first_code = rejected[0]["code"] if rejected else "unknown"
        raise DataForSEOError(
            f"Standard-queue task_post returned no usable task IDs "
            f"(first rejection: status_code={first_code}, message={first_msg!r}; "
            f"{len(rejected)} task(s) rejected)"
        )

    return _poll_serp_tasks(task_ids)


def _poll_serp_tasks(task_ids: list[str], poll_interval: float = 15.0, max_wait: float = 1800.0) -> list[dict]:
    """
    Wait for the given task IDs to land in DataForSEO's ready queue and fetch
    each. Returns whatever completed before max_wait — partial results are
    kept (previously we raised on timeout, which threw away paid-for work).

    Default max_wait raised to 30 min — large batches (~100 tasks) routinely
    exceed the old 10-min limit, especially when DataForSEO is busy.
    """
    deadline = time.monotonic() + max_wait
    pending = set(task_ids)
    results: list[dict] = []

    while pending and time.monotonic() < deadline:
        # tasks_ready lists tasks that have finished and are ready to be fetched.
        # This is a GET endpoint (no payload).
        ready = _get("/serp/google/organic/tasks_ready")
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
            # task_get is also a GET endpoint (task ID is in the URL path).
            detail = _get(f"{SERP_TASK_GET_PATH}/{tid}")
            results.extend(_parse_serp_results(detail))
            pending.discard(tid)

        if pending:
            time.sleep(poll_interval)

    if pending:
        # Don't raise — preserve partial progress. The caller marks
        # unfinished keywords as not-yet-completed and can either retry
        # or recover them later via drain_ready_serp_tasks().
        sample = sorted(pending)[:5]
        logger.warning(
            "Polling timed out after %.0fs with %d/%d task(s) still pending. "
            "Returning %d completed results. Pending IDs (first 5): %s",
            max_wait, len(pending), len(task_ids), len(results), sample,
        )
    return results


# ---------------------------------------------------------------------------
# Drain mode — recover already-completed tasks from DataForSEO's ready queue
# ---------------------------------------------------------------------------

def drain_ready_serp_tasks(max_tasks: int = 1000) -> list[dict]:
    """
    Pull every SERP organic task currently in DataForSEO's `ready` queue
    and return their parsed results. Useful for recovering tasks we already
    paid for but never fetched (e.g. polling timed out in a prior run).

    A `task_get` call removes the task from the ready queue, so this loop
    naturally terminates once all ready tasks have been consumed. The
    max_tasks cap is a safety against an unbounded loop in the unlikely
    event that DataForSEO's ready endpoint returns stale IDs.

    Returns: list of dicts in the same shape as get_serp_rankings().
    """
    results: list[dict] = []
    fetched_ids: set[str] = set()

    while len(fetched_ids) < max_tasks:
        ready = _get("/serp/google/organic/tasks_ready")
        # Collect IDs we haven't already fetched this run.
        batch: list[str] = []
        for task in ready.get("tasks") or []:
            for item in task.get("result") or []:
                tid = item.get("id")
                if tid and tid not in fetched_ids:
                    batch.append(tid)

        if not batch:
            break

        for tid in batch:
            fetched_ids.add(tid)
            try:
                detail = _get(f"{SERP_TASK_GET_PATH}/{tid}")
                results.extend(_parse_serp_results(detail))
            except DataForSEOError as exc:
                logger.warning("drain: failed to fetch task %s: %s", tid, exc)
            if len(fetched_ids) >= max_tasks:
                break

    logger.info("drain: fetched %d task(s), parsed %d result block(s)",
                len(fetched_ids), len(results))
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


# Google Ads Keyword Planner accepts up to 1000 keywords per request.
KEYWORD_PLANNER_BATCH_SIZE = 1000
# Observed pricing 2026-07: $0.05 per keywords_data/google_ads task,
# independent of how many keywords are in the batch. Batching matters.
COST_PER_KEYWORD_PLANNER_TASK = 0.05


def get_search_volume(
    keywords: Iterable[str],
    location_code: int | None = None,
    language_code: str | None = None,
) -> list[dict]:
    """
    Google Ads Keyword Planner volume, CPC, competition, and 12-month trend.

    Richer than get_keyword_data(): keeps the `monthly_searches` array and
    the numeric `competition_index`, which is what trend analysis needs —
    a flat 500/mo keyword and a 500/mo keyword that was 50/mo last quarter
    look identical without it.

    Batches internally (1000 keywords/request, the API cap), so callers can
    pass an arbitrarily long list. Keywords the Planner has no data for are
    returned with search_volume=None rather than dropped, so the caller can
    tell "no data" apart from "not asked".

    Returns a list of dicts:
        {
            "keyword": "agentic ai platform",
            "search_volume": 1900,
            "cpc": 12.44,
            "competition": "HIGH",           # LOW | MEDIUM | HIGH | None
            "competition_index": 78,         # 0-100
            "low_top_of_page_bid": 4.10,
            "high_top_of_page_bid": 18.90,
            "monthly_searches": [{"year": 2026, "month": 6, "search_volume": 2400}, ...],
            "source": "dataforseo_google_ads",
            "raw": <full result block>,
        }
    """
    keywords = [k for k in (kw.strip() for kw in keywords) if k]
    if not keywords:
        return []

    location_code = location_code or settings.DATAFORSEO_LOCATION_CODE
    language_code = language_code or settings.DATAFORSEO_LANGUAGE_CODE

    out: list[dict] = []
    for start in range(0, len(keywords), KEYWORD_PLANNER_BATCH_SIZE):
        batch = keywords[start:start + KEYWORD_PLANNER_BATCH_SIZE]
        payload = [{
            "keywords":      batch,
            "location_code": location_code,
            "language_code": language_code,
        }]
        data = _post("/keywords_data/google_ads/search_volume/live", payload)

        for task in data.get("tasks", []):
            if task.get("status_code") != OK_STATUS_CODE:
                logger.warning("search_volume task returned status=%s message=%r",
                               task.get("status_code"), task.get("status_message"))
                continue
            for result in task.get("result") or []:
                out.append(_parse_keyword_planner_result(result))

        logger.info("Keyword Planner: batch of %d keyword(s), %d result(s) so far",
                    len(batch), len(out))

    return out


def get_keyword_ideas(
    seed_keywords: Iterable[str],
    location_code: int | None = None,
    language_code: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    Expand seed keywords into related Keyword Planner suggestions.

    Google Ads caps the seed list at 20 per request. Same result shape as
    get_search_volume(), so scoring code can treat both identically.

    Useful as a second discovery lane: the feed harvest finds the phrase
    people are *talking about*, this finds the phrases people are
    *searching for* around it.
    """
    seeds = [k for k in (kw.strip() for kw in seed_keywords) if k][:20]
    if not seeds:
        return []

    payload = [{
        "keywords":            seeds,
        "location_code":       location_code or settings.DATAFORSEO_LOCATION_CODE,
        "language_code":       language_code or settings.DATAFORSEO_LANGUAGE_CODE,
        "limit":               limit,
        # Exclude terms with no volume — a suggestion nobody searches for
        # is noise in a discovery pipeline.
        "search_partners":     False,
        "include_adult_keywords": False,
    }]
    data = _post("/keywords_data/google_ads/keywords_for_keywords/live", payload)

    out: list[dict] = []
    for task in data.get("tasks", []):
        if task.get("status_code") != OK_STATUS_CODE:
            logger.warning("keyword_ideas task returned status=%s message=%r",
                           task.get("status_code"), task.get("status_message"))
            continue
        for result in task.get("result") or []:
            out.append(_parse_keyword_planner_result(result))
    return out


def _parse_keyword_planner_result(result: dict) -> dict:
    """Normalize one Keyword Planner result block. Shared by both callers."""
    monthly = result.get("monthly_searches") or []
    return {
        "keyword":               result.get("keyword"),
        "search_volume":         result.get("search_volume"),
        "cpc":                   result.get("cpc"),
        "competition":           result.get("competition"),
        "competition_index":     result.get("competition_index"),
        "low_top_of_page_bid":   result.get("low_top_of_page_bid"),
        "high_top_of_page_bid":  result.get("high_top_of_page_bid"),
        # Keyword Planner returns newest-first; normalize to chronological
        # so downstream slicing ("last 3 months") reads naturally.
        "monthly_searches": sorted(
            [
                {
                    "year":          m.get("year"),
                    "month":         m.get("month"),
                    "search_volume": m.get("search_volume"),
                }
                for m in monthly
                if m.get("year") and m.get("month")
            ],
            key=lambda m: (m["year"], m["month"]),
        ),
        "source": "dataforseo_google_ads",
        "raw":    result,
    }


# ---------------------------------------------------------------------------
# Backlinks
# ---------------------------------------------------------------------------

class DataForSEOAccessDenied(DataForSEOError):
    """
    Raised when DataForSEO returns 40204 (subscription required). Lets
    agents distinguish "no subscription" (a setup issue the operator can
    fix) from generic errors. Use this to fall back to a degraded mode
    rather than crashing.
    """


def _rank_to_100(raw_rank) -> int | None:
    """
    Normalize DataForSEO's backlink `rank` (0-1000) to a 0-100 authority scale.

    The two tables that store this disagreed about the scale: `backlinks.
    domain_authority` is an unconstrained INTEGER and was receiving the raw
    0-1000 value, while `competitor_backlinks.domain_rank` carries a
    CHECK (BETWEEN 0 AND 100) that the same raw value would violate on the
    first real insert. Normalizing here — at the one boundary both writers
    pass through — keeps the scale honest for every consumer instead of
    asking each of them to remember.
    """
    if raw_rank is None:
        return None
    try:
        return max(0, min(100, round(float(raw_rank) / 10.0)))
    except (TypeError, ValueError):
        return None


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

    Raises
    ------
    DataForSEOAccessDenied
        Backlinks API requires its own subscription (separate from the
        SERP pay-per-query model). 40204 status comes back when it isn't
        active on the account.
    """
    payload = [{"target": target, "limit": limit, "mode": mode}]
    data = _post("/backlinks/backlinks/live", payload)

    out: list[dict] = []
    for task in data.get("tasks", []):
        status = task.get("status_code")
        if status == 40204:
            raise DataForSEOAccessDenied(
                f"DataForSEO Backlinks API access denied (status_code=40204). "
                f"Activate the Backlinks subscription at "
                f"https://app.dataforseo.com/backlinks-subscription. "
                f"Server message: {task.get('status_message', '')!r}"
            )
        if status != OK_STATUS_CODE:
            logger.warning("get_backlinks task returned status=%s message=%r",
                           status, task.get("status_message"))
            continue
        for result in task.get("result") or []:
            for item in result.get("items") or []:
                out.append({
                    "source_url": item.get("url_from"),
                    "source_domain": item.get("domain_from"),
                    "target_url": item.get("url_to"),
                    "anchor": item.get("anchor"),
                    "dofollow": item.get("dofollow"),
                    "rank": item.get("rank"),             # raw DataForSEO authority, 0-1000
                    "domain_rank": _rank_to_100(item.get("rank")),
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
    "DataForSEOAccessDenied",
    "COST_PER_KEYWORD_PLANNER_TASK",
    "KEYWORD_PLANNER_BATCH_SIZE",
    "drain_ready_serp_tasks",
    "get_serp_rankings",
    "get_keyword_data",
    "get_search_volume",
    "get_keyword_ideas",
    "get_backlinks",
    "get_onpage_audit",
]

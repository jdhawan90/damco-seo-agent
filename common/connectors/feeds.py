"""
Syndication-feed connector — shared infrastructure for trend discovery.

Reads industry discussion from three kinds of source:

  rss         RSS 2.0 / Atom / RDF feeds. Covers tech press (CIO.com,
              Techmeme, InfoWorld), vendor blogs, and Medium tag feeds
              (`medium.com/feed/tag/<tag>`).
  reddit      A subreddit's `.rss` endpoint. Structurally an Atom feed, but
              Reddit is stricter about User-Agent and rate limits, so it gets
              its own type and a slower default pace.
  hackernews  The free Algolia-backed HN search API (`hn.algolia.com`).
              Documented, unauthenticated, JSON.

Why feeds and not HTML scraping
-------------------------------
Feeds are the publisher's own intended distribution channel — stable
structure, no CSS-selector rot when a site restyles, no robots.txt grey
area, no ToS friction. Every source in migration 010's seed registry
publishes one. If a future source doesn't, prefer dropping it over
scraping it.

Parsing
-------
Stdlib `xml.etree.ElementTree` with explicit namespace handling rather than
a `feedparser` dependency: the field set we need (title, link, summary,
author, date) is small and the three feed dialects differ in only a handful
of tag names. Malformed XML is caught and reported per-source, never
allowed to abort a whole harvest run.

Politeness
----------
Reuses `common.connectors.crawler.Crawler` for its per-origin rate limiting
and branded User-Agent, so trend harvesting obeys the same etiquette as the
technical-SEO crawls. Feed bodies are fetched with `parse_html=False`
because they are XML/JSON, not HTML.

Usage
-----
    from common.connectors.feeds import fetch_source, FeedItem

    result = fetch_source("https://www.cio.com/feed/", source_type="rss")
    if result.error:
        logger.warning("feed failed: %s", result.error)
    for item in result.items:
        print(item.title, item.published_at)
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from common.connectors.crawler import Crawler, default_user_agent


logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 25
DEFAULT_MAX_ITEMS = 60
# Reddit's unauthenticated budget is roughly 10 requests/minute per IP, and
# it tightens progressively under sustained polling. Measured on this repo:
# 2s got 1 of 13 subreddits, 6.5s got 4 of 13. 12s clears the throttle. All
# subreddits share one origin, so the crawler's per-origin lock paces the
# whole set — 13 subreddits costs ~2.5 min, acceptable for a weekly job.
REDDIT_RATE_LIMIT_SEC = 12.0
DEFAULT_RATE_LIMIT_SEC = 1.0

# One retry covers the common transient cases (a slow CDN origin, a burst
# 429). More than that and a genuinely dead feed slows the whole harvest.
MAX_FETCH_ATTEMPTS = 2
RETRY_BACKOFF_SEC = 5.0
# Cap on how long we'll honor a server's Retry-After. A feed asking us to
# wait 10 minutes gets skipped this run instead of stalling 38 others.
MAX_RETRY_AFTER_SEC = 30.0

HN_ALGOLIA_DEFAULT_TAGS = "story"
HN_ALGOLIA_MAX_HITS = 100

# XML namespaces the three dialects use.
NS = {
    "atom":    "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "rdf":     "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rss1":    "http://purl.org/rss/1.0/",
    "media":   "http://search.yahoo.com/mrss/",
}

_TAG_RE        = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class FeedError(RuntimeError):
    """Raised when a feed cannot be fetched or parsed at all."""


@dataclass
class FeedItem:
    """One harvested item, normalized across all three source dialects."""
    title: str
    url: str | None = None
    summary: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    # Source-specific extras (HN points/comments, Reddit flair, etc.)
    extra: dict[str, Any] = field(default_factory=dict)

    def text_blob(self) -> str:
        """Title + summary, the surface phrase extraction reads."""
        parts = [self.title or ""]
        if self.summary:
            parts.append(self.summary)
        return " ".join(p for p in parts if p).strip()


@dataclass
class FeedResult:
    """Outcome of polling one source. Never raises — inspect `.error`."""
    url: str
    source_type: str
    items: list[FeedItem] = field(default_factory=list)
    status: str = "ok"          # ok | error | empty | blocked
    error: str | None = None
    http_status: int | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Shared crawler instances — one per pace, so Reddit's slower limit doesn't
# throttle the tech-press feeds.
# ---------------------------------------------------------------------------

_crawlers: dict[float, Crawler] = {}


def _crawler_for(rate_limit_sec: float) -> Crawler:
    crawler = _crawlers.get(rate_limit_sec)
    if crawler is None:
        crawler = Crawler(
            # Reddit rejects generic/absent User-Agents with a 429. The tenant
            # bot string carries contact info, which is what their API
            # guidelines ask for.
            user_agent=default_user_agent(),
            timeout_sec=DEFAULT_TIMEOUT_SEC,
            rate_limit_sec=rate_limit_sec,
            # Feeds are XML/JSON, not HTML pages — robots.txt Disallow rules
            # aimed at crawlers routinely cover /feed paths that the publisher
            # nonetheless syndicates deliberately. We still identify ourselves
            # and rate-limit; we just don't let a broad Disallow suppress the
            # publisher's own distribution channel.
            respect_robots=False,
        )
        _crawlers[rate_limit_sec] = crawler
    return crawler


def _raw_get(url: str, *, rate_limit_sec: float) -> tuple[bytes | None, int | None, str | None]:
    """
    Fetch a non-HTML body. Returns (body, http_status, error).

    Crawler.fetch() short-circuits on non-HTML content types and never
    returns the body, so feeds go through its session directly — keeping
    the per-origin rate limit and User-Agent, skipping the HTML parse path.

    Retries once on a timeout, a connection error, or a 429. Everything else
    (403, 404, 5xx) is reported immediately: those don't get better by asking
    again, and a harvest polls ~40 sources sequentially.
    """
    crawler = _crawler_for(rate_limit_sec)
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}".lower()
    lock = crawler._domain_lock(origin)          # noqa: SLF001 — intentional reuse

    last_error: str | None = None
    last_status: int | None = None

    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        with lock:
            crawler._wait_rate_limit(origin)     # noqa: SLF001
            try:
                resp = crawler.session.get(
                    url, timeout=crawler.timeout_sec, allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error, last_status = str(exc), None
                resp = None

        if resp is None:
            if attempt < MAX_FETCH_ATTEMPTS:
                logger.debug("%s: %s — retrying in %.0fs", url, last_error, RETRY_BACKOFF_SEC)
                time.sleep(RETRY_BACKOFF_SEC)
                continue
            return None, None, last_error

        if resp.status_code == 429 and attempt < MAX_FETCH_ATTEMPTS:
            wait = _retry_after_seconds(resp) or RETRY_BACKOFF_SEC
            logger.debug("%s: HTTP 429 — waiting %.0fs before retry", url, wait)
            time.sleep(wait)
            last_error, last_status = "HTTP 429", 429
            continue

        if resp.status_code >= 400:
            return None, resp.status_code, f"HTTP {resp.status_code}"

        # A feed URL that answers with an HTML page is a dead or moved feed.
        # Saying so beats letting the XML parser report a cryptic offset.
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type in ("text/html", "application/xhtml+xml"):
            return None, resp.status_code, (
                f"feed returned HTML (Content-Type: {content_type}) — "
                f"the feed has probably moved or been retired"
            )

        return resp.content, resp.status_code, None

    return None, last_status, last_error


def _retry_after_seconds(resp: requests.Response) -> float | None:
    """Parse a Retry-After header (delta-seconds form), clamped to a sane cap."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw.strip()), MAX_RETRY_AFTER_SEC)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_source(
    url: str,
    *,
    source_type: str = "rss",
    max_items: int = DEFAULT_MAX_ITEMS,
    since: datetime | None = None,
) -> FeedResult:
    """
    Poll one source and return normalized items.

    Never raises for expected failures (network, HTTP error, malformed XML) —
    those come back as `status="error"` with `.error` populated so one dead
    feed can't abort a 39-source harvest.

    Parameters
    ----------
    source_type : {"rss", "reddit", "hackernews"}
    max_items : int
        Cap on items returned, applied after date filtering.
    since : datetime | None
        Drop items published before this instant. Items with no parseable
        date are kept — many feeds omit dates, and dropping them silently
        would lose real signal.
    """
    source_type = (source_type or "rss").lower()
    result = FeedResult(url=url, source_type=source_type)

    try:
        if source_type == "hackernews":
            items = _fetch_hackernews(url, max_items=max_items, since=since)
        elif source_type == "reddit":
            items = _fetch_xml(url, rate_limit_sec=REDDIT_RATE_LIMIT_SEC)
        elif source_type == "rss":
            items = _fetch_xml(url, rate_limit_sec=DEFAULT_RATE_LIMIT_SEC)
        else:
            result.status = "error"
            result.error = f"unknown source_type {source_type!r}"
            return result
    except FeedError as exc:
        result.status = "blocked" if "HTTP 40" in str(exc) or "HTTP 42" in str(exc) else "error"
        result.error = str(exc)
        return result
    except Exception as exc:  # defensive: a harvest must survive any one source
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        logger.debug("feed %s failed unexpectedly", url, exc_info=True)
        return result

    if since is not None:
        items = [it for it in items if it.published_at is None or it.published_at >= since]

    result.items = items[:max_items]
    result.status = "ok" if result.items else "empty"
    return result


def fetch_many(
    sources: list[dict],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    since: datetime | None = None,
) -> list[tuple[dict, FeedResult]]:
    """
    Poll a list of source dicts sequentially, pairing each with its result.

    Each dict needs at least `url`; `source_type` defaults to "rss". Extra
    keys (id, name, weight, ...) are passed through untouched so callers can
    carry their DB row alongside the result.

    Sequential on purpose: ~40 feeds at ~1 req/sec is under a minute, and
    politeness matters more than speed for a job that runs weekly.
    """
    out: list[tuple[dict, FeedResult]] = []
    for src in sources:
        res = fetch_source(
            src["url"],
            source_type=src.get("source_type", "rss"),
            max_items=max_items,
            since=since,
        )
        if res.error:
            logger.warning("feed %-28s %s", src.get("name", src["url"])[:28], res.error)
        else:
            logger.info("feed %-28s %d item(s)", src.get("name", src["url"])[:28], len(res.items))
        out.append((src, res))
    return out


# ---------------------------------------------------------------------------
# RSS 2.0 / Atom / RDF parsing
# ---------------------------------------------------------------------------

def _fetch_xml(url: str, *, rate_limit_sec: float) -> list[FeedItem]:
    body, _status, err = _raw_get(url, rate_limit_sec=rate_limit_sec)
    if err:
        raise FeedError(err)
    if not body:
        raise FeedError("empty response body")
    return parse_feed_xml(body)


def parse_feed_xml(body: bytes) -> list[FeedItem]:
    """
    Parse RSS 2.0, Atom, or RDF/RSS 1.0 into FeedItems.

    Falls back to a repaired parse when the publisher emits invalid XML —
    common enough in the wild (bare `&`, stray control bytes) that failing
    outright would cost us real sources. Pure function over bytes, so it's
    unit-testable without network access.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as first_exc:
        repaired = _repair_xml(body)
        if repaired is None:
            raise FeedError(f"XML parse error: {first_exc}") from first_exc
        try:
            root = ET.fromstring(repaired)
        except ET.ParseError as exc:
            raise FeedError(f"XML parse error (repair also failed): {exc}") from exc
        logger.debug("feed needed XML repair before it would parse")

    # RSS 2.0:     <rss><channel><item>
    # RDF/RSS 1.0: <rdf:RDF><item> (namespaced)
    # Atom:        <feed><entry>
    entries = (
        root.findall(".//item")
        or root.findall(f".//{{{NS['rss1']}}}item")
        or root.findall(f".//{{{NS['atom']}}}entry")
    )
    if not entries:
        return []

    return [item for item in (_parse_entry(e) for e in entries) if item is not None]


# Bare "&" not already opening a valid entity — the single most common way a
# hand-rolled feed template produces invalid XML.
_BARE_AMP_RE = re.compile(rb"&(?!#?\w+;)")
# Control bytes that are illegal in XML 1.0 regardless of encoding.
_ILLEGAL_CTRL_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _repair_xml(body: bytes) -> bytes | None:
    """
    Best-effort repair of invalid XML. Returns None if nothing changed.

    Deliberately conservative: escapes bare ampersands and drops illegal
    control characters, nothing more. A feed broken in some richer way
    should fail loudly and get fixed in the registry, not be silently
    half-parsed into misleading data.
    """
    repaired = _ILLEGAL_CTRL_RE.sub(b"", body)
    repaired = _BARE_AMP_RE.sub(b"&amp;", repaired)
    return repaired if repaired != body else None


def _parse_entry(entry: ET.Element) -> FeedItem | None:
    title = _clean(_first_text(entry, ["title", f"{{{NS['atom']}}}title", f"{{{NS['rss1']}}}title"]))
    if not title:
        return None

    link = _extract_link(entry)
    summary = _clean(_first_text(entry, [
        "description",
        f"{{{NS['atom']}}}summary",
        f"{{{NS['atom']}}}content",
        f"{{{NS['content']}}}encoded",
        f"{{{NS['rss1']}}}description",
    ]))
    author = _clean(_extract_author(entry))
    published_at = _extract_date(entry)

    return FeedItem(
        title=title,
        url=link,
        # Feed summaries can be an entire article; cap so a single verbose
        # source can't dominate phrase extraction by sheer volume.
        summary=summary[:2000] if summary else None,
        author=author,
        published_at=published_at,
    )


def _extract_link(entry: ET.Element) -> str | None:
    # RSS 2.0 / RDF: <link>https://...</link>
    for tag in ("link", f"{{{NS['rss1']}}}link"):
        el = entry.find(tag)
        if el is not None and (el.text or "").strip():
            return el.text.strip()

    # Atom: <link rel="alternate" href="https://..."/>. Prefer alternate,
    # fall back to the first link that isn't a self/replies reference.
    atom_links = entry.findall(f"{{{NS['atom']}}}link")
    for el in atom_links:
        if (el.get("rel") or "alternate") == "alternate" and el.get("href"):
            return el.get("href").strip()
    for el in atom_links:
        if el.get("href") and el.get("rel") not in ("self", "replies"):
            return el.get("href").strip()

    # Some feeds only carry the canonical URL in <guid isPermaLink="true">.
    guid = entry.find("guid")
    if guid is not None and (guid.text or "").strip().startswith("http"):
        return guid.text.strip()
    return None


def _extract_author(entry: ET.Element) -> str | None:
    # Atom nests it: <author><name>...</name></author>
    author_el = entry.find(f"{{{NS['atom']}}}author")
    if author_el is not None:
        name = author_el.find(f"{{{NS['atom']}}}name")
        if name is not None and (name.text or "").strip():
            return name.text.strip()
    return _first_text(entry, ["author", f"{{{NS['dc']}}}creator"])


def _extract_date(entry: ET.Element) -> datetime | None:
    raw = _first_text(entry, [
        "pubDate",                          # RSS 2.0 — RFC 822
        f"{{{NS['atom']}}}published",       # Atom — ISO 8601
        f"{{{NS['atom']}}}updated",
        f"{{{NS['dc']}}}date",              # RDF — ISO 8601
    ])
    return parse_datetime(raw)


def parse_datetime(raw: str | None) -> datetime | None:
    """
    Parse RFC 822 (RSS) or ISO 8601 (Atom/RDF) into a tz-aware UTC datetime.

    Returns None rather than raising — a missing or exotic date is a normal
    feed condition, not an error worth failing a harvest over.
    """
    if not raw:
        return None
    raw = raw.strip()

    # RFC 822: "Tue, 21 Jul 2026 14:03:00 +0000"
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass

    # ISO 8601: "2026-07-21T14:03:00Z" / "...+00:00"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _first_text(entry: ET.Element, tags: list[str]) -> str | None:
    for tag in tags:
        el = entry.find(tag)
        if el is None:
            continue
        # <content type="html"> may hold child elements rather than .text
        text = el.text if el.text else "".join(el.itertext())
        if text and text.strip():
            return text
    return None


def _clean(raw: str | None) -> str | None:
    """Strip HTML tags, unescape entities, collapse whitespace."""
    if not raw:
        return None
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


# ---------------------------------------------------------------------------
# Hacker News (Algolia API — free, unauthenticated, documented)
# ---------------------------------------------------------------------------

def _fetch_hackernews(
    base_url: str,
    *,
    max_items: int,
    since: datetime | None,
) -> list[FeedItem]:
    """
    Query the HN Algolia search API for recent stories.

    Docs: https://hn.algolia.com/api — `search_by_date` returns newest-first,
    which is what a trend harvest wants. A `points>=N` filter cuts the long
    tail of self-posts nobody engaged with.
    """
    params = {
        "tags":        HN_ALGOLIA_DEFAULT_TAGS,
        "hitsPerPage": min(max_items, HN_ALGOLIA_MAX_HITS),
    }
    filters = ["points>=10"]
    if since is not None:
        filters.append(f"created_at_i>{int(since.timestamp())}")
    params["numericFilters"] = ",".join(filters)

    url = f"{base_url}?{urlencode(params)}"
    body, _status, err = _raw_get(url, rate_limit_sec=DEFAULT_RATE_LIMIT_SEC)
    if err:
        raise FeedError(err)
    if not body:
        raise FeedError("empty response body")

    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise FeedError(f"HN JSON parse error: {exc}") from exc

    items: list[FeedItem] = []
    for hit in data.get("hits") or []:
        title = _clean(hit.get("title") or hit.get("story_title"))
        if not title:
            continue
        items.append(FeedItem(
            title=title,
            url=hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            summary=_clean(hit.get("story_text")),
            author=hit.get("author"),
            published_at=parse_datetime(hit.get("created_at")),
            extra={
                "points":       hit.get("points"),
                "num_comments": hit.get("num_comments"),
            },
        ))
    return items


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def default_since(days: int) -> datetime:
    """UTC cutoff `days` in the past — the usual `since` argument."""
    return datetime.now(timezone.utc) - timedelta(days=days)


__all__ = [
    "FeedError",
    "FeedItem",
    "FeedResult",
    "DEFAULT_MAX_ITEMS",
    "default_since",
    "fetch_many",
    "fetch_source",
    "parse_datetime",
    "parse_feed_xml",
]

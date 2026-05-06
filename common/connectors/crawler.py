"""
HTTP + HTML connector — shared infrastructure for the Technical SEO agent.

Used by the site_auditor, canonical_checker, and internal_link_analyzer
modules. Centralizes one-page-at-a-time fetching plus DOM extraction so
those modules don't each redo the same parse.

Behavior
--------
- Polite by default: 1 req/sec/domain rate limit, branded User-Agent,
  follows robots.txt unless explicitly overridden.
- Robots.txt is fetched on first contact with each origin and cached.
- Returns a CrawlResult with raw HTML + commonly-needed extracts (title,
  meta description, canonical, h1/h2, JSON-LD blocks, microdata flag,
  links, images, word count).
- Skips non-HTML responses (PDFs, images, etc.) — those don't have an
  on-page audit surface.
- Caps body size to avoid OOM on misbehaving servers (default 5 MB).

Thread safety
-------------
- The Crawler instance is safe to share across threads.
- Per-origin rate limits and robots.txt caches are guarded by locks.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "DamcoSEOBot/1.0 (+https://www.damcogroup.com/; SEO ops monitoring)"
DEFAULT_TIMEOUT_SEC = 20
DEFAULT_RATE_LIMIT_SEC = 1.0
DEFAULT_MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MB
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

_WHITESPACE_RE = re.compile(r"\s+")


class CrawlError(RuntimeError):
    """Raised when a fetch fails for non-HTTP-status reasons (DNS, timeout)."""


@dataclass
class CrawlResult:
    """Everything one HTTP fetch + parse produces."""
    url: str
    final_url: str | None = None
    status: int | None = None
    redirect_chain: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str | None = None
    is_html: bool = False
    body_bytes: int = 0
    error: str | None = None

    # Parsed extracts (populated only when is_html and parse_html=True)
    html: str | None = None
    title: str | None = None
    meta_description: str | None = None
    meta_robots: str | None = None
    canonical: str | None = None              # absolute URL or None
    lang: str | None = None
    h1_tags: list[str] = field(default_factory=list)
    h2_tags: list[str] = field(default_factory=list)
    schema_jsonld: list[dict[str, Any]] = field(default_factory=list)
    has_microdata: bool = False
    images: list[dict[str, str | None]] = field(default_factory=list)   # {src, alt}
    links: list[dict[str, str | bool | None]] = field(default_factory=list)
    word_count: int = 0


class Crawler:
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        rate_limit_sec: float = DEFAULT_RATE_LIMIT_SEC,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        respect_robots: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_sec = timeout_sec
        self.rate_limit_sec = rate_limit_sec
        self.max_body_bytes = max_body_bytes
        self.respect_robots = respect_robots

        # Per-origin state
        self._last_fetch_at: dict[str, float] = {}
        self._domain_locks: dict[str, threading.Lock] = {}
        self._domain_locks_guard = threading.Lock()
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_cache_guard = threading.Lock()

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    # ------------------------------------------------------------------
    # Per-origin helpers
    # ------------------------------------------------------------------

    def _origin(self, url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}".lower()

    def _domain_lock(self, origin: str) -> threading.Lock:
        with self._domain_locks_guard:
            lock = self._domain_locks.get(origin)
            if lock is None:
                lock = threading.Lock()
                self._domain_locks[origin] = lock
            return lock

    def _wait_rate_limit(self, origin: str) -> None:
        if self.rate_limit_sec <= 0:
            return
        now = time.monotonic()
        last = self._last_fetch_at.get(origin)
        if last is not None:
            wait = self.rate_limit_sec - (now - last)
            if wait > 0:
                time.sleep(wait)
        self._last_fetch_at[origin] = time.monotonic()

    def _robots(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        """Returns parser (cached) or None on fetch failure (treated as allow-all)."""
        with self._robots_cache_guard:
            if origin in self._robots_cache:
                return self._robots_cache[origin]
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{origin}/robots.txt")
        try:
            r = self.session.get(f"{origin}/robots.txt", timeout=self.timeout_sec)
            if r.status_code >= 400:
                rp = None  # missing/inaccessible robots.txt → allow-all per RFC 9309
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException as exc:
            logger.warning("robots.txt fetch failed for %s: %s — assuming allow-all", origin, exc)
            rp = None
        with self._robots_cache_guard:
            self._robots_cache[origin] = rp
        return rp

    def is_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        origin = self._origin(url)
        rp = self._robots(origin)
        if rp is None:
            return True
        return rp.can_fetch(self.user_agent, url)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(self, url: str, *, parse_html: bool = True) -> CrawlResult:
        """Fetch one URL. Returns a populated CrawlResult."""
        result = CrawlResult(url=url)
        origin = self._origin(url)

        if not self.is_allowed(url):
            result.error = "Disallowed by robots.txt"
            logger.debug("robots.txt blocks %s", url)
            return result

        # Hold a per-domain lock for the duration of this fetch so that
        # rate limiting actually serializes across threads on the same host.
        lock = self._domain_lock(origin)
        with lock:
            self._wait_rate_limit(origin)
            try:
                resp = self.session.get(
                    url, timeout=self.timeout_sec, allow_redirects=True, stream=True,
                )
            except requests.RequestException as exc:
                result.error = str(exc)
                logger.debug("fetch error for %s: %s", url, exc)
                return result

            result.status = resp.status_code
            result.final_url = resp.url
            result.redirect_chain = [r.url for r in resp.history]
            result.headers = dict(resp.headers)
            result.content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower() or None
            result.is_html = result.content_type in HTML_CONTENT_TYPES if result.content_type else False

            if not result.is_html or not parse_html:
                resp.close()
                return result

            # Read up to max_body_bytes
            try:
                content = resp.raw.read(self.max_body_bytes + 1, decode_content=True)
            except Exception as exc:
                result.error = f"body read failed: {exc}"
                resp.close()
                return result
            finally:
                resp.close()

            if len(content) > self.max_body_bytes:
                result.error = f"body too large (>{self.max_body_bytes} bytes); skipping parse"
                return result
            result.body_bytes = len(content)

            charset = resp.encoding or "utf-8"
            try:
                html = content.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                html = content.decode("utf-8", errors="replace")
            result.html = html

        # Parse outside the per-domain lock so we don't block other URLs on
        # the same host while soup parsing runs.
        _populate_extracts(result)
        return result


# ---------------------------------------------------------------------------
# HTML extraction (pure function — easy to unit-test)
# ---------------------------------------------------------------------------

def _populate_extracts(result: CrawlResult) -> None:
    if not result.html:
        return
    soup = BeautifulSoup(result.html, "lxml")
    base = result.final_url or result.url

    # <html lang="...">
    html_el = soup.find("html")
    if html_el and html_el.has_attr("lang"):
        result.lang = (html_el["lang"] or "").strip() or None

    # <title>
    title_el = soup.find("title")
    if title_el and title_el.string:
        result.title = _normalize_ws(title_el.string)

    # <meta name="description"> + <meta name="robots">
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or "").strip().lower()
        content = (meta.get("content") or "").strip()
        if not name or not content:
            continue
        if name == "description" and not result.meta_description:
            result.meta_description = _normalize_ws(content)
        elif name == "robots" and not result.meta_robots:
            result.meta_robots = content.lower()

    # <link rel="canonical">
    canon = soup.find("link", rel=lambda r: r and "canonical" in [v.lower() for v in (r if isinstance(r, list) else [r])])
    if canon and canon.get("href"):
        result.canonical = urljoin(base, canon["href"].strip())

    # h1, h2
    result.h1_tags = [_normalize_ws(h.get_text(" ", strip=True)) for h in soup.find_all("h1") if h.get_text(strip=True)]
    result.h2_tags = [_normalize_ws(h.get_text(" ", strip=True)) for h in soup.find_all("h2") if h.get_text(strip=True)]

    # JSON-LD blocks
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            result.schema_jsonld.extend(p for p in parsed if isinstance(p, dict))
        elif isinstance(parsed, dict):
            result.schema_jsonld.append(parsed)

    # Microdata presence (any element with itemscope)
    result.has_microdata = soup.find(attrs={"itemscope": True}) is not None

    # Images
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        result.images.append({
            "src":  urljoin(base, src.strip()),
            "alt":  (img.get("alt") or "").strip() or None,
        })

    # Links
    base_origin = _origin(base)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base, href)
        link_origin = _origin(absolute)
        result.links.append({
            "href":        absolute,
            "anchor":      _normalize_ws(a.get_text(" ", strip=True)),
            "rel":         " ".join(a.get("rel", [])).strip() or None,
            "is_internal": link_origin == base_origin,
        })

    # Word count from visible text (strip script/style)
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    result.word_count = len(text.split()) if text else 0


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower()


def _normalize_ws(s: str | None) -> str | None:
    if s is None:
        return None
    return _WHITESPACE_RE.sub(" ", s).strip() or None


# ---------------------------------------------------------------------------
# Module-level convenience: a default crawler instance
# ---------------------------------------------------------------------------

_default_crawler: Crawler | None = None
_default_crawler_lock = threading.Lock()


def get_default_crawler() -> Crawler:
    """Lazy-initialized shared Crawler. Cheap to call repeatedly."""
    global _default_crawler
    with _default_crawler_lock:
        if _default_crawler is None:
            _default_crawler = Crawler()
        return _default_crawler


def fetch(url: str, **kwargs: Any) -> CrawlResult:
    """Convenience: fetch one URL using the default Crawler."""
    return get_default_crawler().fetch(url, **kwargs)


__all__ = [
    "Crawler", "CrawlResult", "CrawlError",
    "DEFAULT_USER_AGENT", "DEFAULT_TIMEOUT_SEC",
    "DEFAULT_RATE_LIMIT_SEC", "DEFAULT_MAX_BODY_BYTES",
    "fetch", "get_default_crawler",
]

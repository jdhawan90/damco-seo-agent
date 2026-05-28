"""
Sitemap fetching + parsing — shared utility for any agent that needs to
walk an XML sitemap.

Used by:
  - technical_seo.sitemap_validator (audits Damco's own properties)
  - competitive_intelligence.content_monitor (tracks competitor publishing)

Pure functions — no database access. Each call is self-contained.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET

import requests


logger = logging.getLogger(__name__)

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

USER_AGENT = "DamcoSEOBot/1.0 (+https://www.damcogroup.com/; SEO ops monitoring)"
REQUEST_TIMEOUT = 15
RATE_LIMIT_SLEEP = 0.5   # seconds between sitemap requests per-walk


def fetch_xml(url: str, timeout: int = REQUEST_TIMEOUT) -> str | None:
    """Fetch one sitemap URL. Returns text, or None on transport/HTTP failure."""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        r.raise_for_status()
        return r.text
    except requests.RequestException as exc:
        logger.error("Failed to fetch sitemap %s: %s", url, exc)
        return None


def parse_sitemap(xml_text: str) -> tuple[str, list[str]]:
    """
    Parse a sitemap document.

    Returns ('index', [sub_sitemap_urls]) for a <sitemapindex>, or
    ('urlset', [page_urls]) for a <urlset>. Raises ValueError on an
    unrecognized root.
    """
    root = ET.fromstring(xml_text)
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    if tag == "sitemapindex":
        urls: list[str] = []
        for s in root.findall("sm:sitemap", NS) or root.findall("sitemap"):
            loc = s.find("sm:loc", NS) if s.find("sm:loc", NS) is not None else s.find("loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
        return ("index", urls)

    if tag == "urlset":
        urls = []
        for u in root.findall("sm:url", NS) or root.findall("url"):
            loc = u.find("sm:loc", NS) if u.find("sm:loc", NS) is not None else u.find("loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
        return ("urlset", urls)

    raise ValueError(f"Unrecognized sitemap root tag: {tag}")


def collect_urls_from_sitemap(
    sitemap_url: str,
    max_depth: int = 3,
    rate_limit_sleep: float = RATE_LIMIT_SLEEP,
) -> tuple[list[str], list[str]]:
    """
    Walk a sitemap (including any <sitemapindex> children) and return all
    page URLs.

    Filters out:
      - duplicate page URLs (preserves first-seen order)
      - stray .xml URLs that ended up inside a <urlset> by mistake
        (some CMSes do this; treat them as sitemaps to recurse into
        rather than as pages — but for safety we just skip)

    Returns (page_urls, fetch_errors).
    fetch_errors is the list of sitemap URLs we couldn't fetch or parse.
    """
    page_urls: list[str] = []
    fetch_errors: list[str] = []
    seen_sitemaps: set[str] = set()

    def walk(url: str, depth: int) -> None:
        if depth > max_depth:
            logger.warning("Max sitemap depth reached at %s", url)
            return
        if url in seen_sitemaps:
            return
        seen_sitemaps.add(url)

        xml_text = fetch_xml(url)
        if xml_text is None:
            fetch_errors.append(url)
            return
        try:
            kind, items = parse_sitemap(xml_text)
        except ET.ParseError as exc:
            logger.error("Could not parse sitemap %s: %s", url, exc)
            fetch_errors.append(url)
            return
        except ValueError as exc:
            logger.error("%s", exc)
            fetch_errors.append(url)
            return

        if kind == "index":
            for sub in items:
                walk(sub, depth + 1)
                if rate_limit_sleep > 0:
                    time.sleep(rate_limit_sleep)
        else:
            page_urls.extend(items)

    walk(sitemap_url, depth=0)

    # Dedupe, preserving order; drop stray .xml entries that landed inside
    # a <urlset> (some CMSes do this — they aren't real pages).
    seen: set[str] = set()
    unique_pages: list[str] = []
    skipped_xml = 0
    for u in page_urls:
        if u.lower().endswith(".xml"):
            skipped_xml += 1
            continue
        if u not in seen:
            seen.add(u)
            unique_pages.append(u)
    if skipped_xml:
        logger.info("Skipped %d stray .xml entries inside urlsets (sub-sitemaps "
                    "mislisted as pages)", skipped_xml)
    return unique_pages, fetch_errors


def discover_sitemap_urls(domain: str) -> list[str]:
    """
    Best-effort discovery of a domain's sitemap entry points.

    Tries common locations in order. Returns a list of URLs that
    responded 200 to a HEAD request. Caller should pass the first one
    to collect_urls_from_sitemap.

    domain: bare hostname e.g. "itransition.com" (no scheme).
    """
    candidates = [
        f"https://www.{domain}/sitemap.xml",
        f"https://{domain}/sitemap.xml",
        f"https://www.{domain}/sitemap_index.xml",
        f"https://{domain}/sitemap_index.xml",
        f"https://www.{domain}/sitemap-index.xml",
        f"https://{domain}/sitemap-index.xml",
    ]
    # Also peek at robots.txt for any explicitly declared sitemap entries.
    for base in (f"https://www.{domain}", f"https://{domain}"):
        try:
            r = requests.get(f"{base}/robots.txt",
                             headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
            if r.ok:
                for line in r.text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("sitemap:"):
                        url = line.split(":", 1)[1].strip()
                        if url and url not in candidates:
                            candidates.insert(0, url)
                break
        except requests.RequestException:
            continue

    found: list[str] = []
    for candidate in candidates:
        try:
            r = requests.head(candidate,
                              headers={"User-Agent": USER_AGENT},
                              timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if r.status_code == 200 and not any(c.lower() == candidate.lower() for c in found):
                found.append(r.url)  # use final URL after redirects
        except requests.RequestException:
            continue
    return found


__all__ = [
    "fetch_xml", "parse_sitemap", "collect_urls_from_sitemap",
    "discover_sitemap_urls",
    "USER_AGENT", "REQUEST_TIMEOUT", "RATE_LIMIT_SLEEP", "NS",
]

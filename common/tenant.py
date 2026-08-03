"""
Tenant profile — who this deployment is running for.
=====================================================

Agent code must not know whose site it is auditing. Before this module, that
knowledge was spread across the agent folders as Python constants: owned-domain
sets in three files, two competing offering vocabularies (one of which had
drifted so far it matched nothing in the database), a hardcoded sitemap table,
thresholds calibrated to one property, and brand names interpolated straight
into LLM prompts.

All of it now lives in the `tenant*` tables from migration 012, and reaches
agents through one cached `TenantProfile`.

Usage
-----
    from common.tenant import profile

    p = profile()
    if p.owns(domain): ...
    for off in p.offerings: ...
    threshold = p.policy("cwv_thresholds")["mobile"]
    tokens = p.vocab("commercial_tokens")

Prompts
-------
`system_preamble()` is the ONE sanctioned place brand identity enters a model
call. Pass it as `system=`, never interpolate the brand name into a user
prompt:

    call_claude(user_prompt, system=system_preamble("You are an SEO analyst."))

That keeps every user prompt tenant-neutral by construction, and keeps the
preamble stable enough to be worth caching.

Caching
-------
The profile is read once per process and memoized. Agents are short-lived
batch jobs, so a stale read is not a practical concern; call
`reload_profile()` after editing the tables in a long-running session.
"""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from common.database import fetch_all, fetch_one


logger = logging.getLogger(__name__)

__all__ = [
    "TenantProfile",
    "Offering",
    "TenantNotConfigured",
    "profile",
    "reload_profile",
    "system_preamble",
    "crawler_user_agent",
    "strip_www",
    "root_domain",
]


class TenantNotConfigured(RuntimeError):
    """
    Raised when no tenant row exists.

    Deliberately loud. A silent default would mean an agent crawling, scoring
    and writing results for the wrong company, which is far worse than a
    startup failure telling you to run migration 012.
    """


# ---------------------------------------------------------------------------
# Domain helpers — shared so the whole system agrees what a domain "is"
# ---------------------------------------------------------------------------

def strip_www(host: str) -> str:
    """
    Drop a leading 'www.'.

    `removeprefix`, not `lstrip`: `"wordpress.com".lstrip("www.")` returns
    'ordpress.com' because lstrip takes a character set. That bug shipped in
    two modules and silently defeated a blocklist entry.
    """
    return (host or "").strip().lower().removeprefix("www.")


def root_domain(url_or_host: str) -> str:
    """Registrable-ish host for a URL or bare hostname, without 'www.'."""
    if not url_or_host:
        return ""
    value = url_or_host.strip()
    if "://" not in value:
        value = "https://" + value
    return strip_www(urlparse(value).netloc)


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Offering:
    """One service line, with the vocabulary that identifies it in text."""
    name: str
    slug: str
    tokens: tuple[str, ...]
    niche_tokens: tuple[str, ...]
    sort_order: int

    @property
    def marker_words(self) -> frozenset[str]:
        """Every individual word appearing in any of this offering's tokens."""
        return frozenset(w for t in self.tokens for w in t.split())


@dataclass(frozen=True)
class TenantProfile:
    slug: str
    brand_name: str
    legal_name: str | None
    primary_domain: str
    vertical: str | None
    audience_descriptor: str | None
    location_code: int
    language_code: str
    device: str
    currency: str
    timezone: str
    crawler_bot_name: str
    crawler_contact_url: str | None
    cta_url: str | None

    owned_domains: frozenset[str]
    domain_rows: tuple[dict, ...]
    offerings: tuple[Offering, ...]

    _vocab: dict[str, frozenset[str]] = field(repr=False, default_factory=dict)
    _vocab_labels: dict[str, tuple[tuple[str, str | None], ...]] = field(
        repr=False, default_factory=dict)
    _policies: dict[str, Any] = field(repr=False, default_factory=dict)

    # -- identity ----------------------------------------------------------

    def owns(self, url_or_host: str) -> bool:
        """
        True when a URL or hostname belongs to this tenant.

        Exact host match after normalization — NOT the substring test the rank
        tracker used. `"damcogroup.com" in domain` also matches
        "notdamcogroup.com.evil.example", and substring matching on the metric
        executives track week to week is not a place for false positives.
        """
        host = root_domain(url_or_host)
        if not host:
            return False
        return host in self.owned_domains or any(
            host.endswith("." + owned) for owned in self.owned_domains
        )

    @property
    def user_agent(self) -> str:
        """
        The identity we present to other people's servers.

        Load-bearing beyond branding: this exact string is what robots.txt
        evaluation matches against, so a client who allowlists their own bot
        name would be blocked by their own rules if we sent someone else's.
        """
        contact = f" (+{self.crawler_contact_url}; SEO ops monitoring)" if \
            self.crawler_contact_url else ""
        return f"{self.crawler_bot_name}/1.0{contact}"

    # -- offerings ---------------------------------------------------------

    @property
    def offering_names(self) -> tuple[str, ...]:
        return tuple(o.name for o in self.offerings)

    def offering(self, name: str) -> Offering | None:
        lowered = (name or "").strip().lower()
        for o in self.offerings:
            if o.name.lower() == lowered or o.slug == lowered:
                return o
        return None

    @property
    def offering_matchers(self) -> tuple[tuple[str, str], ...]:
        """
        (token, offering_name) sorted longest-first.

        Specificity matters: "power bi" must claim Data Engineering before
        "bi" can claim anything.
        """
        pairs = [(t, o.name) for o in self.offerings for t in o.tokens]
        return tuple(sorted(pairs, key=lambda p: -len(p[0])))

    @property
    def offering_marker_words(self) -> frozenset[str]:
        return frozenset(w for o in self.offerings for w in o.marker_words)

    def niche_tokens_for(self, names: list[str] | None = None) -> frozenset[str]:
        """Union of niche tokens across the named offerings (all when None)."""
        wanted = self.offerings if names is None else [
            o for o in self.offerings if o.name in set(names)
        ]
        return frozenset(t for o in wanted for t in (o.niche_tokens or o.tokens))

    # -- vocabularies and policies ----------------------------------------

    def vocab(self, kind: str) -> frozenset[str]:
        """Terms for a named list. Unknown kinds return empty, never raise."""
        return self._vocab.get(kind, frozenset())

    def vocab_labeled(self, kind: str) -> tuple[tuple[str, str | None], ...]:
        """
        (term, label) pairs, longest term first.

        Used by the URL path map, where the label is the resulting page_type
        and a NULL label means "recognised but deliberately out of scope".
        """
        return self._vocab_labels.get(kind, ())

    def policy(self, key: str, default: Any = None) -> Any:
        return self._policies.get(key, default)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load(slug: str | None = None) -> TenantProfile:
    slug = slug or os.environ.get("TENANT_SLUG") or None

    if slug:
        row = fetch_one("SELECT * FROM tenants WHERE slug = %s", [slug])
        if not row:
            raise TenantNotConfigured(
                f"No tenant with slug={slug!r}. Known slugs: "
                + (", ".join(r["slug"] for r in fetch_all("SELECT slug FROM tenants"))
                   or "(none — run sql/012_tenant_profile.sql)")
            )
    else:
        row = fetch_one(
            "SELECT * FROM tenants WHERE status = 'active' ORDER BY id LIMIT 1"
        )
        if not row:
            raise TenantNotConfigured(
                "No active tenant configured. Apply sql/012_tenant_profile.sql, "
                "or set TENANT_SLUG if this database holds more than one."
            )

    tid = row["id"]

    # Selected explicitly rather than SELECT * so a schema addition is a
    # deliberate act — but that also means a new column is invisible until it is
    # listed here. ga4_property_id and gsc_site_url (migration 019) were added
    # and silently absent for exactly that reason.
    domain_rows = fetch_all(
        "SELECT domain, role, sitemap_url, uses_www, "
        "       ga4_property_id, gsc_site_url, extra_sitemaps "
        "  FROM tenant_domains "
        " WHERE tenant_id = %s AND enabled ORDER BY role, domain",
        [tid],
    )
    owned = frozenset(strip_www(d["domain"]) for d in domain_rows)
    # The primary domain is authoritative even if nobody added a row for it.
    owned = owned | {strip_www(row["primary_domain"])}

    offerings = tuple(
        Offering(
            name=o["name"],
            slug=o["slug"] or o["name"].lower().replace(" ", "-"),
            tokens=tuple(o["tokens"] or ()),
            niche_tokens=tuple(o["niche_tokens"] or ()),
            sort_order=o["sort_order"],
        )
        for o in fetch_all(
            "SELECT name, slug, tokens, niche_tokens, sort_order "
            "  FROM tenant_offerings "
            " WHERE tenant_id = %s AND status = 'active' "
            " ORDER BY sort_order, name",
            [tid],
        )
    )

    vocab: dict[str, set[str]] = {}
    labeled: dict[str, list[tuple[str, str | None]]] = {}
    for v in fetch_all(
        "SELECT kind, term, label FROM tenant_vocabularies "
        " WHERE tenant_id = %s AND enabled",
        [tid],
    ):
        vocab.setdefault(v["kind"], set()).add(v["term"])
        labeled.setdefault(v["kind"], []).append((v["term"], v["label"]))

    policies = {
        p["key"]: p["value"]
        for p in fetch_all(
            "SELECT key, value FROM tenant_policies WHERE tenant_id = %s", [tid]
        )
    }

    prof = TenantProfile(
        slug=row["slug"],
        brand_name=row["brand_name"],
        legal_name=row["legal_name"],
        primary_domain=strip_www(row["primary_domain"]),
        vertical=row["vertical"],
        audience_descriptor=row["audience_descriptor"],
        location_code=row["location_code"],
        language_code=row["language_code"],
        device=row["device"],
        currency=row["currency"],
        timezone=row["timezone"],
        crawler_bot_name=row["crawler_bot_name"],
        crawler_contact_url=row["crawler_contact_url"],
        cta_url=row["cta_url"],
        owned_domains=owned,
        domain_rows=tuple(domain_rows),
        offerings=offerings,
        _vocab={k: frozenset(v) for k, v in vocab.items()},
        _vocab_labels={
            k: tuple(sorted(pairs, key=lambda p: -len(p[0])))
            for k, pairs in labeled.items()
        },
        _policies=policies,
    )

    logger.debug(
        "Loaded tenant %s: %d domain(s), %d offering(s), %d vocab kind(s), %d policy(ies)",
        prof.slug, len(owned), len(offerings), len(vocab), len(policies),
    )
    return prof


@functools.lru_cache(maxsize=4)
def profile(slug: str | None = None) -> TenantProfile:
    """The active tenant profile. Cached for the life of the process."""
    return _load(slug)


def reload_profile() -> None:
    """Drop the cache — call after editing the tenant tables in-session."""
    profile.cache_clear()


def crawler_user_agent(fallback: str) -> str:
    """
    The tenant's crawler identity, or `fallback` when no profile is reachable.

    The crawling modules keep a generic module-level constant and call this at
    construction/request time instead: importing them must not require a
    database, and a missing tenant row should degrade to an unbranded bot
    rather than take a crawl down.
    """
    try:
        return profile().user_agent
    except Exception as exc:                        # DB down, no tenant row
        logger.warning("No tenant profile (%s) — crawling as %r", exc, fallback)
        return fallback


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def system_preamble(role: str | None = None, *, slug: str | None = None) -> str:
    """
    Build the system prompt that tells the model who it is working for.

    This is the only function permitted to put the brand name into a model
    call. Five modules previously wrote it into their *user* prompts —
    "You are an SEO strategist briefing Damco's marketing team" — which meant
    the output asserted the wrong company for anyone else and could not be
    fixed by configuration.

    Parameters
    ----------
    role : str, optional
        What the model is being asked to be, e.g. "You are an SEO strategist."
        Prepended to the identity block.
    """
    p = profile(slug)
    lines: list[str] = []
    if role:
        lines.append(role.strip())

    who = f"You are working on behalf of {p.brand_name}"
    if p.vertical:
        who += f", a {p.vertical} company"
    lines.append(who + ".")

    if p.audience_descriptor:
        lines.append(f"Their audience is {p.audience_descriptor}.")
    if p.offerings:
        lines.append("Their service offerings are: "
                     + ", ".join(p.offering_names) + ".")
    lines.append(f"Their primary domain is {p.primary_domain}.")
    return "\n".join(lines)

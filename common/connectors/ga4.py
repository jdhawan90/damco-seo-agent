"""
Google Analytics 4 connector — the behaviour half of the picture.
=================================================================

Why this exists alongside GSC
-----------------------------
Search Console tells you that you rank and that people clicked. It cannot tell
you what happened next. Ranking #3 for a keyword that produces no enquiries is a
vanity metric, and until now the system had no way to know the difference.

GA4 closes that loop: sessions, engagement and conversions per landing page,
joinable to `pages` and therefore to offerings and keywords.

Auth
----
Service account by preference, set `GA4_SERVICE_ACCOUNT_FILE`. A server process
should not depend on an interactive consent screen that expires — the GSC
connector uses installed-app OAuth and a cron job would hang on a browser prompt
if its refresh token were ever revoked.

Falls back to Application Default Credentials when no file is configured, which
covers `gcloud auth application-default login` locally and attached service
accounts on GCP.

Grant the service account **Viewer** on the GA4 property, then set
`GA4_PROPERTY_ID` to the numeric id (the digits, with or without a
`properties/` prefix).

Degradation
-----------
Never raises for missing configuration. `is_available()` reports readiness and
the fetch functions return `None`, so a caller degrades the way it does for
Anthropic and Voyage. The dashboard must render with no GA4 configured at all.

Quotas
------
The Data API is free but quota'd per property. These helpers request one report
per call with an explicit row limit; nothing here paginates unboundedly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from common.config import settings


logger = logging.getLogger(__name__)

__all__ = [
    "GA4Error",
    "GA4NotConfigured",
    "is_available",
    "property_id",
    "properties",
    "normalize_property_id",
    "landing_page_metrics",
    "channel_totals",
    "conversion_events",
    "DEFAULT_LOOKBACK_DAYS",
]

DEFAULT_LOOKBACK_DAYS = 28

# GA4 reports are not final for ~48h. Ending the window before that avoids
# recording numbers that will quietly change underneath us.
DATA_LAG_DAYS = 2

MAX_ROWS = 10_000


class GA4Error(RuntimeError):
    """A GA4 API call failed."""


class GA4NotConfigured(GA4Error):
    """No property id, or no usable credentials."""


@dataclass(frozen=True)
class LandingPageRow:
    landing_page: str
    sessions: int
    engaged_sessions: int
    engagement_rate: float
    conversions: float
    revenue: float
    avg_duration_sec: float


def normalize_property_id(raw: str | None) -> str | None:
    """Bare digits, accepting the 'properties/123' form."""
    v = (raw or "").strip()
    if not v or v in ("your_property_id_here", "properties/"):
        return None
    return v.removeprefix("properties/").strip("/") or None


def property_id() -> str | None:
    """
    Fallback property id from the environment.

    Prefer `properties()` — a tenant can own several domains with a GA4
    property each, and this one only answers for a single-property deployment.
    """
    return normalize_property_id(
        os.environ.get("GA4_PROPERTY_ID") or getattr(settings, "GA4_PROPERTY_ID", "")
    )


def properties() -> list[dict]:
    """
    Every (domain, property_id) pair configured for this tenant.

    Reads `tenant_domains.ga4_property_id`, because a property id varies per
    domain and therefore belongs on the domain row rather than in the process
    environment. This tenant has two properties; a single env var made one of
    them invisible to the whole system.

    Falls back to the env var against the primary domain so a single-property
    deployment needs no database change.
    """
    from common.tenant import profile

    p = profile()
    out = [
        {"domain": d["domain"], "property_id": pid}
        for d in p.domain_rows
        if (pid := normalize_property_id(d.get("ga4_property_id")))
    ]
    if out:
        return out

    env = property_id()
    return [{"domain": p.primary_domain, "property_id": env}] if env else []


def _service_account_file() -> str | None:
    raw = (os.environ.get("GA4_SERVICE_ACCOUNT_FILE")
           or getattr(settings, "GA4_SERVICE_ACCOUNT_FILE", "") or "").strip()
    return raw or None


def is_available() -> bool:
    """True when at least one property is configured and the client imports."""
    try:
        if not properties():
            return False
    except Exception:                                   # DB unreachable, no tenant
        if property_id() is None:
            return False
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient  # noqa: F401
    except ImportError:
        return False
    return True


def _client():
    from google.analytics.data_v1beta import BetaAnalyticsDataClient

    path = _service_account_file()
    if path:
        if not os.path.exists(path):
            raise GA4NotConfigured(
                f"GA4_SERVICE_ACCOUNT_FILE points at {path!r}, which does not exist"
            )
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
        )
        return BetaAnalyticsDataClient(credentials=creds)

    # Application Default Credentials.
    logger.info("No GA4_SERVICE_ACCOUNT_FILE set — using application default credentials")
    return BetaAnalyticsDataClient()


def _window(lookback_days: int) -> tuple[str, str]:
    end = date.today() - timedelta(days=DATA_LAG_DAYS)
    start = end - timedelta(days=lookback_days - 1)
    return start.isoformat(), end.isoformat()


def _run_report(dimensions: Sequence[str], metrics: Sequence[str],
                lookback_days: int, limit: int,
                prop: str | None = None) -> list[dict] | None:
    """
    One report against one property. Returns rows as dicts keyed by
    dimension/metric name, or None when GA4 is unavailable — callers degrade
    rather than crash.
    """
    pid = normalize_property_id(prop) or property_id()
    if pid is None:
        logger.debug("No GA4 property id — GA4 metrics unavailable")
        return None

    try:
        from google.analytics.data_v1beta.types import (
            DateRange, Dimension, Metric, RunReportRequest,
        )
    except ImportError:
        logger.warning("google-analytics-data not installed — GA4 unavailable "
                       "(pip install google-analytics-data)")
        return None

    start, end = _window(lookback_days)
    try:
        client = _client()
        resp = client.run_report(RunReportRequest(
            property=f"properties/{pid}",
            date_ranges=[DateRange(start_date=start, end_date=end)],
            dimensions=[Dimension(name=d) for d in dimensions],
            metrics=[Metric(name=m) for m in metrics],
            limit=min(limit, MAX_ROWS),
        ))
    except GA4NotConfigured:
        raise
    except Exception as exc:                                  # SDK raises broadly
        logger.warning("GA4 run_report failed (%s: %s) — returning None so the "
                       "caller can fall back", type(exc).__name__, exc)
        return None

    out: list[dict] = []
    for row in resp.rows:
        rec: dict = {}
        for i, d in enumerate(dimensions):
            rec[d] = row.dimension_values[i].value
        for i, m in enumerate(metrics):
            raw = row.metric_values[i].value
            try:
                rec[m] = float(raw)
            except (TypeError, ValueError):
                rec[m] = 0.0
        out.append(rec)

    logger.info("GA4 %s: %d row(s) for %s..%s", ",".join(dimensions), len(out), start, end)
    return out


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def landing_page_metrics(lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                         limit: int = 2000,
                         organic_only: bool = True,
                         prop: str | None = None) -> list[dict] | None:
    """
    Per-landing-page behaviour, restricted to organic search by default.

    Organic-only matters: this data gets joined to `pages` and read as "how did
    SEO traffic behave". Including paid and direct would attribute other
    channels' conversions to search work.
    """
    dims = ["landingPage"]
    if organic_only:
        dims.append("sessionDefaultChannelGroup")

    rows = _run_report(
        dimensions=dims,
        metrics=["sessions", "engagedSessions", "engagementRate",
                 "conversions", "totalRevenue", "averageSessionDuration"],
        lookback_days=lookback_days,
        limit=limit,
        prop=prop,
    )
    if rows is None:
        return None

    if organic_only:
        rows = [r for r in rows
                if (r.get("sessionDefaultChannelGroup") or "").lower() == "organic search"]
    return rows


def channel_totals(lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                   prop: str | None = None) -> list[dict] | None:
    """
    Sessions and conversions by channel. Gives organic a denominator — "SEO
    drove 40 conversions" only means something beside the other channels.
    """
    return _run_report(
        dimensions=["sessionDefaultChannelGroup"],
        metrics=["sessions", "engagedSessions", "conversions", "totalRevenue"],
        lookback_days=lookback_days,
        limit=50,
        prop=prop,
    )


def conversion_events(lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                      limit: int = 50,
                      prop: str | None = None) -> list[dict] | None:
    """
    Which conversion events actually fire, and how often.

    Worth checking before trusting any conversion number: a property with no
    configured key events reports zero conversions, which is indistinguishable
    from genuinely having none unless you look here.
    """
    return _run_report(
        dimensions=["eventName"],
        metrics=["eventCount", "conversions"],
        lookback_days=lookback_days,
        limit=limit,
        prop=prop,
    )

"""
Google Search Console connector.

Wraps the Search Console API for the queries our agents need. Uses OAuth 2.0
with a user consent flow — credentials are stored in a JSON token file after
first run.

Functions
---------
  get_search_analytics(start_date, end_date, dimensions, row_limit)
      — impressions, clicks, CTR, position for queries / pages
  get_sites()
      — list verified sites accessible to the authorized user
  get_indexation_status(url)
      — URL inspection (fetch status, indexing status)
  get_sitemaps()
      — list submitted sitemaps and their status

Setup
-----
1. In Google Cloud Console, create OAuth 2.0 client credentials for a
   desktop app. Download the JSON to the path given by GSC_CLIENT_SECRETS_FILE.
2. First time the connector runs, it'll open a browser for user consent and
   write a refresh token to GSC_TOKEN_FILE. Subsequent runs are silent.
3. Make sure the site given by GSC_SITE_URL is verified in Search Console
   under the same Google account.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from common.config import settings


logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


def _get_credentials() -> Credentials:
    token_path = Path(settings.GSC_TOKEN_FILE)
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        secrets_path = Path(settings.GSC_CLIENT_SECRETS_FILE)
        if not secrets_path.exists():
            raise RuntimeError(
                f"GSC client secrets not found at {secrets_path}. "
                f"Download OAuth client JSON from Google Cloud Console and save it there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
        # run_local_server opens a browser on the host running this code.
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service() -> Resource:
    return build("searchconsole", "v1", credentials=_get_credentials(), cache_discovery=False)


# ---------------------------------------------------------------------------
# Query: Search Analytics
# ---------------------------------------------------------------------------

def get_search_analytics(
    start_date: str,
    end_date: str,
    dimensions: Iterable[str] = ("query",),
    row_limit: int = 25000,
    site_url: str | None = None,
) -> list[dict]:
    """
    Fetch Search Analytics rows for a date range.

    Parameters
    ----------
    start_date, end_date : str (YYYY-MM-DD)
    dimensions : iterable
        Any combination of: "query", "page", "country", "device", "date".
    row_limit : int
        Max rows to return (GSC caps at 25000 per call; paginate for more).
    site_url : str
        Override settings.GSC_SITE_URL for this call.

    Returns
    -------
    list of dict with keys:
        keys            — the dimension values (matches dimensions order)
        clicks, impressions, ctr, position
    """
    site = site_url or settings.GSC_SITE_URL
    if not site:
        raise RuntimeError("GSC_SITE_URL is not configured")

    request_body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": list(dimensions),
        "rowLimit": row_limit,
    }
    response = _service().searchanalytics().query(siteUrl=site, body=request_body).execute()
    return response.get("rows", [])


# ---------------------------------------------------------------------------
# Site & sitemap metadata
# ---------------------------------------------------------------------------

def get_sites() -> list[dict]:
    """Return all verified sites in the authorized account."""
    return _service().sites().list().execute().get("siteEntry", [])


def get_sitemaps(site_url: str | None = None) -> list[dict]:
    """Return submitted sitemaps for the site."""
    site = site_url or settings.GSC_SITE_URL
    return _service().sitemaps().list(siteUrl=site).execute().get("sitemap", [])


# ---------------------------------------------------------------------------
# URL Inspection (indexation status)
# ---------------------------------------------------------------------------

def get_indexation_status(url: str, site_url: str | None = None) -> dict:
    """
    Return URL inspection result: fetch status, coverage state, last crawl, etc.
    """
    site = site_url or settings.GSC_SITE_URL
    body = {"inspectionUrl": url, "siteUrl": site}
    return _service().urlInspection().index().inspect(body=body).execute()


__all__ = [
    "get_search_analytics",
    "get_sites",
    "get_sitemaps",
    "get_indexation_status",
]

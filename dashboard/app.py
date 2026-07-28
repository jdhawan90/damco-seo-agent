"""
SEO health dashboard — FastAPI app.
====================================

Serves one page and a small JSON API.

Design constraint that shapes everything here: **the dashboard never calls an
external API.** Every tile reads Postgres, which the agents populate on a
schedule. That keeps tiles fast, consistent between viewers, and still
renderable when DataForSEO or GSC is down. Live-ness belongs to the chat
endpoint, not to a wall display.

Run
---
    uvicorn dashboard.app:app --reload --port 8080
    # then http://127.0.0.1:8080

Endpoints
---------
    GET  /                    the page
    GET  /api/kpis            every tile in one payload
    GET  /api/kpis/{name}     one tile
    GET  /api/health          liveness + tenant + DB reachability
    POST /api/chat            conversational query (see chat.py)
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from common.tenant import TenantNotConfigured, profile
from dashboard import kpis


logger = logging.getLogger("dashboard")

STATIC_DIR = Path(__file__).resolve().parent / "static"


class DbJSONResponse(JSONResponse):
    """
    JSON response that copes with what psycopg2 actually returns.

    `date`, `datetime`, `Decimal` and `timedelta` all come back from these
    queries and none is serializable by the default encoder. A single unhandled
    type otherwise 500s the whole `/api/kpis` payload — the tile-level error
    handling in kpis.all_tiles() cannot catch it, because serialization happens
    after every tile has already succeeded.
    """

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode("utf-8")


def _json_default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        # float, not str: these are scores and percentages the page does
        # arithmetic and comparisons on.
        return float(o)
    if isinstance(o, timedelta):
        return o.total_seconds()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


app = FastAPI(
    title="SEO Health Dashboard",
    description="Read-only view over the agent database.",
    version="1.0.0",
    docs_url="/api/docs",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
def health() -> JSONResponse:
    """
    Liveness. Reports the tenant and whether the database answers.

    Deliberately distinguishes "no tenant configured" from "database down":
    the first means migrations were not applied, the second is an outage, and
    conflating them sends someone debugging the wrong thing.
    """
    try:
        p = profile()
    except TenantNotConfigured as exc:
        return JSONResponse(
            {"ok": False, "reason": "tenant_not_configured", "detail": str(exc)},
            status_code=503,
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "reason": "database_unreachable", "detail": str(exc)},
            status_code=503,
        )
    return JSONResponse({
        "ok": True,
        "tenant": p.slug,
        "brand": p.brand_name,
        "primary_domain": p.primary_domain,
        "offerings": len(p.offerings),
    })


@app.get("/api/kpis")
def all_kpis() -> JSONResponse:
    """
    Every tile. Individual tile failures are reported inside the payload rather
    than as a 500 — eleven working tiles and one error message is more useful
    than a blank page because one aggregate broke.
    """
    try:
        return DbJSONResponse(kpis.all_tiles())
    except TenantNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/kpis/{name}")
def one_kpi(name: str) -> JSONResponse:
    fn = kpis.TILES.get(name)
    if fn is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown tile {name!r}. Available: {sorted(kpis.TILES)}",
        )
    try:
        return DbJSONResponse(fn())
    except Exception as exc:
        logger.exception("tile %s failed", name)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


# The chat endpoint is registered only if the module imports cleanly, so the
# dashboard still serves when the chat's dependencies or config are missing.
try:
    from dashboard import chat as _chat
    app.include_router(_chat.router)
    logger.info("Chat endpoint registered.")
except Exception as exc:                                    # noqa: BLE001
    logger.warning("Chat endpoint unavailable: %s", exc)


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="SEO health dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s")
    uvicorn.run("dashboard.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()


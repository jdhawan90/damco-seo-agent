"""
Static dashboard renderer — a self-contained HTML file, no server, no database.
==============================================================================

Why static
----------
The dashboard reads a local PostgreSQL. Anything hosted (Vercel, Netlify, S3)
cannot reach `localhost:5432`, so the choice is either to move the database into
the cloud or to bake the numbers into the page. Baking them in keeps the
business data — rankings, competitor analysis, conversions — off any public
infrastructure, and the underlying figures only change when an agent runs, which
is daily at best. Live-ness buys very little here.

How it works
------------
`dashboard/static/index.html` already renders from a single JSON payload. This
writes that payload into the file as `window.__KPI_DATA__` ahead of the page
script; the page sees it and skips its `fetch('/api/kpis')`. One template serves
both modes, so there is no second copy to drift.

The output has no external requests at all: no CDN, no fonts, no API. Open the
file directly, or deploy the directory anywhere.

Usage
-----
    python -m dashboard.render_static
    python -m dashboard.render_static --out C:\\some\\dir
    python -m dashboard.render_static --open        # render then open it

Deploying
---------
    cd outputs/dashboard && vercel deploy --prod

**Protect it.** The page carries competitive and revenue data and has no
authentication of its own. On Vercel, turn on Deployment Protection (Project
Settings > Deployment Protection > Password / Vercel Authentication) before
sharing the link.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.config import settings
from common.database import record_agent_run
from common.tenant import profile
from dashboard import kpis


logger = logging.getLogger("render_static")
AGENT_NAME = "dashboard.render_static"

TEMPLATE = Path(__file__).resolve().parent / "static" / "index.html"
DEFAULT_OUT = settings.OUTPUTS_DIR / "dashboard"

# Marker the payload is injected before. Chosen because it is the page's own
# opening script tag, so the data is defined before any of the page code runs.
ANCHOR = "<script>"


def _json_default(o):
    from datetime import date
    from decimal import Decimal
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)


def build(out_dir: Path | None = None) -> tuple[Path, dict]:
    """Render the snapshot. Returns (path, stats)."""
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)

    template = TEMPLATE.read_text(encoding="utf-8")
    if ANCHOR not in template:
        raise RuntimeError(
            f"Could not find {ANCHOR!r} in {TEMPLATE}. The template's structure "
            f"changed — update ANCHOR in this module."
        )

    payload = kpis.all_tiles()
    failed = [k for k, v in payload.items()
              if isinstance(v, dict) and v.get("error")]

    blob = json.dumps(payload, default=_json_default, separators=(",", ":"))
    # </script> inside the data would close the tag early. It cannot appear in
    # JSON-escaped output, but a keyword or page title legitimately could carry
    # the sequence, so escape it rather than trust the data.
    blob = blob.replace("</", "<\\/")

    injection = (
        "<script>\n"
        "/* Injected by dashboard/render_static.py — the page reads this instead\n"
        "   of calling /api/kpis, which is what makes the file standalone. */\n"
        f"window.__KPI_DATA__ = {blob};\n"
        "</script>\n"
    )
    rendered = template.replace(ANCHOR, injection + ANCHOR, 1)

    index = out_dir / "index.html"
    index.write_text(rendered, encoding="utf-8")

    # Vercel needs no build step for a plain HTML directory, but being explicit
    # stops it guessing a framework and failing.
    (out_dir / "vercel.json").write_text(json.dumps({
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "cleanUrls": True,
        "headers": [{
            "source": "/(.*)",
            "headers": [
                # A snapshot is stale by design; don't let a CDN serve an older
                # one after a redeploy.
                {"key": "Cache-Control", "value": "public, max-age=0, must-revalidate"},
                {"key": "X-Robots-Tag", "value": "noindex, nofollow"},
                {"key": "X-Content-Type-Options", "value": "nosniff"},
                {"key": "Referrer-Policy", "value": "no-referrer"},
            ],
        }],
    }, indent=2) + "\n", encoding="utf-8")

    # robots.txt as well as the header — this must never be indexed.
    (out_dir / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")

    stats = {
        "path": index,
        "bytes": index.stat().st_size,
        "payload_bytes": len(blob),
        "tiles": len([k for k in payload if not k.startswith("_")]),
        "failed_tiles": failed,
    }
    return index, stats


def run(out_dir: Path | None = None, open_after: bool = False,
        dry_run: bool = False) -> dict:
    start = time.monotonic()
    p = profile()

    try:
        index, stats = build(out_dir)
    except Exception as exc:
        logger.exception("render failed")
        if not dry_run:
            record_agent_run(agent_name=AGENT_NAME, status="failed",
                             records_processed=0, errors=[str(exc)],
                             duration_seconds=round(time.monotonic() - start, 2))
        raise

    duration = time.monotonic() - start
    status = "partial" if stats["failed_tiles"] else "success"

    if not dry_run:
        record_agent_run(
            agent_name=AGENT_NAME, status=status,
            records_processed=stats["tiles"],
            errors=[f"tile failed: {t}" for t in stats["failed_tiles"]][:5],
            duration_seconds=round(duration, 2),
            metadata={"path": str(index), "bytes": stats["bytes"],
                      "tiles": stats["tiles"],
                      "failed_tiles": stats["failed_tiles"]},
        )

    print()
    print(f"  {'=' * 68}")
    print(f"   STATIC DASHBOARD — {p.brand_name}")
    print(f"  {'=' * 68}")
    print()
    print(f"  Written:        {index}")
    print(f"  Size:           {stats['bytes'] / 1024:.0f} KB "
          f"({stats['payload_bytes'] / 1024:.0f} KB of data)")
    print(f"  Tiles:          {stats['tiles']}")
    if stats["failed_tiles"]:
        print(f"  Failed tiles:   {', '.join(stats['failed_tiles'])}")
    print(f"  Duration:       {duration:.1f}s")
    print()
    print("  Self-contained — no server, no database, no external requests.")
    print("  Open it directly, or deploy:")
    print(f"    cd \"{index.parent}\" && vercel deploy --prod")
    print()
    print("  BEFORE SHARING THE LINK: this page has competitive and revenue data")
    print("  and no login. Turn on Vercel Deployment Protection (Project Settings")
    print("  > Deployment Protection) or the URL is the only thing keeping it private.")
    print()

    if open_after:
        webbrowser.open(index.as_uri())

    return {"status": status, "path": str(index), **{
        k: v for k, v in stats.items() if k != "path"}}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"{profile().brand_name} static dashboard renderer")
    parser.add_argument("--out", help=f"Output directory (default: {DEFAULT_OUT})")
    parser.add_argument("--open", action="store_true", dest="open_after",
                        help="Open the rendered file when done")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render but don't log to agent_runs")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s")
    run(out_dir=args.out, open_after=args.open_after, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

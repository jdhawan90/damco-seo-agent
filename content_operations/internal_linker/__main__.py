"""
CLI entry point for the internal-linker agent.

Usage:
    python -m content_operations.internal_linker crawl [--max-pages N]
                                                       [--rate-limit SEC]
                                                       [--no-robots]
    python -m content_operations.internal_linker detect
    python -m content_operations.internal_linker report [--out PATH]
                                                        [--include-all]
    python -m content_operations.internal_linker run    # crawl -> detect -> report

The weekly job is `run`. The three sub-commands exist so an operator can
re-run any phase independently without paying for the others.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from common.tenant import profile

from . import crawl as crawler  # module file is crawl.py; local alias keeps call-sites readable
from . import detect as detectors
from . import reports


def _logging_setup(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"{profile().brand_name} Internal Linker — crawl, detect, report",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("crawl", help="refresh the owned-domain link graph")
    pc.add_argument("--max-pages", type=int, default=None,
                    help="cap number of URLs crawled this run (for testing)")
    pc.add_argument("--rate-limit", type=float, default=1.0,
                    help="per-host request delay in seconds (default 1.0)")
    pc.add_argument("--no-robots", action="store_true",
                    help="ignore robots.txt (only for diagnostics)")
    pc.add_argument("-v", "--verbose", action="store_true")

    pd = sub.add_parser("detect", help="run both detectors against the link graph")
    pd.add_argument("-v", "--verbose", action="store_true")

    pr = sub.add_parser("report", help="export new suggestions to an Excel workbook")
    pr.add_argument("--out", type=Path, default=None,
                    help="output path (default: outputs/internal_linker/)")
    pr.add_argument("--include-all", action="store_true",
                    help="include applied, ignored and stale rows in the export")
    pr.add_argument("-v", "--verbose", action="store_true")

    pfull = sub.add_parser("run", help="full pipeline: crawl -> detect -> report")
    pfull.add_argument("--max-pages", type=int, default=None)
    pfull.add_argument("--rate-limit", type=float, default=1.0)
    pfull.add_argument("--no-robots", action="store_true")
    pfull.add_argument("--out", type=Path, default=None)
    pfull.add_argument("--include-all", action="store_true")
    pfull.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    _logging_setup(getattr(args, "verbose", False))

    if args.cmd == "crawl":
        out = crawler.run(
            max_pages=args.max_pages,
            rate_limit_sec=args.rate_limit,
            respect_robots=not args.no_robots,
        )
        print(f"crawl: {out}")
        return

    if args.cmd == "detect":
        out = detectors.run()
        print(f"detect: {out}")
        return

    if args.cmd == "report":
        statuses = ("new", "applied", "ignored", "stale") if args.include_all else ("new",)
        out = reports.run(output_path=args.out, include_statuses=statuses)
        print(f"report: {out}")
        return

    if args.cmd == "run":
        print("=" * 72)
        print("1/3 CRAWL")
        print("=" * 72)
        c = crawler.run(
            max_pages=args.max_pages,
            rate_limit_sec=args.rate_limit,
            respect_robots=not args.no_robots,
        )
        print(f"  {c}")
        print()
        print("=" * 72)
        print("2/3 DETECT")
        print("=" * 72)
        d = detectors.run()
        print(f"  {d}")
        print()
        print("=" * 72)
        print("3/3 REPORT")
        print("=" * 72)
        statuses = ("new", "applied", "ignored", "stale") if args.include_all else ("new",)
        r = reports.run(output_path=args.out, include_statuses=statuses)
        print(f"  {r}")
        return


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""CLI entrypoint for the Playwright + HTTPX E2E Journey Rater Agent.

Usage:
    uv run python -m scripts.rate_ui_and_api
    ELECTRIC_SHEEP_URL="https://your-ui-service-url.a.run.app" uv run python -m scripts.rate_ui_and_api
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from src.evaluation.e2e_journey_rater import run_e2e_rater


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Playwright Chromium + HTTPX E2E journeys against Electric Sheep & Memory Studio."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("ELECTRIC_SHEEP_URL", ""),
        help="Target base URL (if omitted, starts a local uvicorn ServerThread).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit E2ERaterReport as JSON.",
    )
    args = parser.parse_args()

    target_url = args.url.strip() or None
    report = run_e2e_rater(base_url=target_url, headless=True)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0 if report.journeys_passed == report.journeys_total else 1

    print("=" * 90)
    print("  ELECTRIC SHEEP & MEMORY STUDIO — PLAYWRIGHT + HTTPX E2E RATER REPORT")
    print("=" * 90)
    print(f"  Base URL        : {report.base_url}")
    print(f"  Journeys Passed : {report.journeys_passed}/{report.journeys_total}")
    print(f"  Checks Passed   : {report.checks_passed}/{report.checks_total}")
    print(f"  Overall Score   : {report.overall_score:.2%} ({report.grade})")
    print("-" * 90)
    for j in report.journeys:
        status = "PASS" if j.passed else "FAIL"
        print(
            f"  [{status}] {j.journey_id:<42} "
            f"Checks: {j.checks_passed}/{j.checks_total} ({j.duration_ms:.1f}ms)"
        )
        for c in j.checks:
            c_status = "OK" if c.passed else "ERR"
            print(f"      [{c_status}] ({c.layer:<11}) {c.check_name}: {c.details}")
    print("=" * 90)

    return 0 if report.journeys_passed == report.journeys_total else 1


if __name__ == "__main__":
    sys.exit(main())

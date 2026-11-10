#!/usr/bin/env python3
"""CLI entrypoint for the Electric Sheep & Memory Studio Functional Journey Rater.

Usage:
    uv run python -m scripts.rate_memory_journeys
    uv run python -m scripts.rate_memory_journeys --json
"""

from __future__ import annotations

import argparse
import json
import sys

from src.evaluation.journey_rater import MemoryJourneyAgentRater


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute the 5 canonical functional & visual legibility journeys for Electric Sheep."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full AgentRaterReport as JSON to stdout.",
    )
    args = parser.parse_args()

    rater = MemoryJourneyAgentRater()
    report = rater.evaluate_all_journeys()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0 if report.journeys_passed == report.journeys_evaluated else 1

    print("=" * 94)
    print("  ELECTRIC SHEEP & MEMORY STUDIO — AGENTIC JOURNEY RATER REPORT")
    print("=" * 94)
    print(f"  Rater ID               : {report.rater_id}")
    print(f"  Journeys Passed        : {report.journeys_passed}/{report.journeys_evaluated}")
    print(f"  Functional Score       : {report.overall_functional_score:.2%}")
    print(f"  Visual Legibility Score: {report.overall_legibility_score:.2%}")
    print(f"  Composite Score        : {report.composite_score:.2%} ({report.composite_grade})")
    print("-" * 94)
    for jr in report.journey_ratings:
        print(
            f"  [{jr.grade:<14}] {jr.journey_id:<42} "
            f"Steps: {jr.steps_passed}/{jr.steps_total} | "
            f"Legibility: {jr.legibility_checks_passed}/{jr.legibility_checks_total} | "
            f"Avg: {jr.avg_latency_ms:.1f}ms"
        )
        for sm in jr.step_metrics:
            mark = "PASS" if sm.passed else "FAIL"
            print(
                f"      [{mark}] {sm.step_id} {sm.endpoint:<38} "
                f"({sm.status_code}, {sm.latency_ms:.1f}ms) -> {sm.evidence}"
            )
    print("=" * 94)

    if report.critical_findings:
        print("CRITICAL FINDINGS:")
        for finding in report.critical_findings:
            print(f"  - {finding}")
        return 1

    return 0 if report.journeys_passed == report.journeys_evaluated else 1


if __name__ == "__main__":
    sys.exit(main())

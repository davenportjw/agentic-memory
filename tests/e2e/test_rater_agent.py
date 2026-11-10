"""Playwright + HTTPX E2E Rater Agent test suite (`tests/e2e/test_rater_agent.py`)."""

from __future__ import annotations

import asyncio

from src.evaluation.e2e_journey_rater import E2EJourneyRaterAgent, ServerThread


def test_e2e_journey_rater_agent_full_suite() -> None:
    """Spin up a live ServerThread and verify all 5 Playwright + HTTPX journeys pass."""
    with ServerThread() as srv:
        agent = E2EJourneyRaterAgent(base_url=srv.base_url, headless=True)
        report = asyncio.run(agent.run_all())

    assert report.journeys_total == 5
    assert report.journeys_passed == 5, f"Failed E2E journeys: {[j.to_dict() for j in report.journeys if not j.passed]}"
    assert report.overall_score == 1.0
    assert report.grade == "A+ [CERTIFIED]"

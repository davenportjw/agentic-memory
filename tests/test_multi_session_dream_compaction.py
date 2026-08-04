"""Comprehensive tests for enhanced single-session intent compaction and multi-session cross-session dream compaction."""

from fastapi.testclient import TestClient
from src.common.telemetry import BigQueryTelemetrySink
from src.common.spanner_client import SpannerPreferenceClient
from src.pipeline.bq_to_spanner_sync import BqToSpannerSynchronizer
from src.ui_service.server import api


def test_single_session_intent_compaction():
    """Verify that single-session dream compaction captures user intent and stores it in Spanner."""
    user_id = "test-intent-user-01"
    session_id = "session-intent-01"

    sink = BigQueryTelemetrySink(use_mock=True)
    spanner = SpannerPreferenceClient(use_mock=True)

    sink.log_interaction(
        user_id=user_id,
        session_id=session_id,
        agent_name="InterviewAgent",
        event_type="USER_PROMPT",
        message="I am onboarding to explore cloud infrastructure and Python architectures",
        payload={"prompt": "I am onboarding to explore cloud infrastructure and Python architectures"},
    )

    synchronizer = BqToSpannerSynchronizer(telemetry_sink=sink, spanner_client=spanner, use_mock=True)
    summary = synchronizer.summarize_session_logs(session_id=session_id, user_id=user_id)

    assert summary["user_id"] == user_id
    assert summary["session_id"] == session_id
    assert "session_intent" in summary or "intent" in summary

    sync_res = synchronizer.sync_to_spanner(user_id=user_id, session_id=session_id)
    assert sync_res["preferences_synced"] >= 1

    stored_pref = spanner.get_user_preference(user_id, "session_intent")
    assert stored_pref is not None
    assert "intent" in stored_pref.get("category", "")


def test_multi_session_cross_session_dream_compaction():
    """Verify that multi-session dream compaction consolidates learnings and captures intent trajectory across sessions."""
    user_id = "test-multi-session-user-02"

    sink = BigQueryTelemetrySink(use_mock=True)
    spanner = SpannerPreferenceClient(use_mock=True)

    # Session 1: Onboarding and language preference
    sink.log_interaction(
        user_id=user_id,
        session_id="session-multi-01",
        agent_name="InterviewAgent",
        event_type="USER_PROMPT",
        message="Hello! My name is Jason and my favorite programming language is Python",
        payload={"prompt": "Hello! My name is Jason and my favorite programming language is Python"},
    )

    # Session 2: Role and infrastructure tool preference
    sink.log_interaction(
        user_id=user_id,
        session_id="session-multi-02",
        agent_name="InterviewAgent",
        event_type="USER_PROMPT",
        message="I work as a Principal Cloud Architect deploying to Google Cloud Run and Spanner",
        payload={"prompt": "I work as a Principal Cloud Architect deploying to Google Cloud Run and Spanner"},
    )

    # Session 3: Architecture optimization focus
    sink.log_interaction(
        user_id=user_id,
        session_id="session-multi-03",
        agent_name="InterviewAgent",
        event_type="USER_PROMPT",
        message="I want to optimize multi-agent memory consolidation and Spanner low-latency cache lookups",
        payload={"prompt": "I want to optimize multi-agent memory consolidation and Spanner low-latency cache lookups"},
    )

    synchronizer = BqToSpannerSynchronizer(telemetry_sink=sink, spanner_client=spanner, use_mock=True)

    # 1. Summarize across multiple sessions
    summary = synchronizer.summarize_multi_session_logs(
        user_id=user_id,
        session_ids=["session-multi-01", "session-multi-02", "session-multi-03"],
    )

    assert summary["user_id"] == user_id
    assert summary["session_id"] == "MULTI_SESSION_SYNTHESIS"
    assert len(summary["sessions_analyzed"]) == 3
    assert summary["session_count"] == 3
    assert "intent_trajectory" in summary
    assert "cross_session_intent" in summary or "intent" in summary

    # 2. Sync multi-session compaction to Spanner
    sync_res = synchronizer.sync_multi_session_to_spanner(user_id=user_id)
    assert sync_res["session_id"] == "MULTI_SESSION_SYNTHESIS"
    assert sync_res["preferences_synced"] >= 2

    # 3. Verify Spanner stored cross-session intent and trajectory
    traj_pref = spanner.get_user_preference(user_id, "intent_trajectory")
    assert traj_pref is not None
    assert traj_pref["category"] == "intent"

    cross_intent_pref = spanner.get_user_preference(user_id, "cross_session_intent")
    assert cross_intent_pref is not None

    # 4. Verify session summary stored
    summaries = spanner.get_user_session_summaries(user_id)
    assert any(s["session_id"] == "MULTI_SESSION_SYNTHESIS" for s in summaries)


def test_api_harvest_multi_session_endpoints():
    """Test UI service API endpoints for single-session and multi-session dream compaction."""
    client = TestClient(api)
    user_id = "test-api-harvest-user-03"

    # 1. Single session harvest via /api/harvest
    r_single = client.post(
        "/api/harvest",
        json={"user_id": user_id, "session_id": "session-api-01", "multi_session": False},
    )
    assert r_single.status_code == 200
    data_single = r_single.json()
    assert data_single["status"] == "success"
    assert data_single["session_id"] == "session-api-01"
    assert "intent" in data_single
    assert "intent_category" in data_single

    # 2. Multi-session harvest via /api/harvest with multi_session=True
    r_multi = client.post(
        "/api/harvest",
        json={"user_id": user_id, "multi_session": True},
    )
    assert r_multi.status_code == 200
    data_multi = r_multi.json()
    assert data_multi["status"] == "success"
    assert data_multi["session_id"] == "MULTI_SESSION_SYNTHESIS"
    assert "intent_trajectory" in data_multi

    # 3. Explicit multi-session harvest endpoint /api/harvest/multi
    r_explicit = client.post(
        "/api/harvest/multi",
        json={"user_id": user_id},
    )
    assert r_explicit.status_code == 200
    data_explicit = r_explicit.json()
    assert data_explicit["status"] == "success"
    assert data_explicit["session_id"] == "MULTI_SESSION_SYNTHESIS"
    assert "intent_trajectory" in data_explicit

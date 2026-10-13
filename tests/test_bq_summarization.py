"""Unit tests for BigQuery AI.GENERATE session log summarization and Spanner synchronization."""

from src.common.telemetry import BigQueryTelemetrySink
from src.common.spanner_client import SpannerPreferenceClient
from src.pipeline.bq_to_spanner_sync import BqToSpannerSynchronizer


def test_session_summarization_and_sync():
    """Verify that user telemetry logs are summarized into preferences and stored in Spanner."""
    user_id = "test-user-sync-01"
    session_id = "session-sync-01"

    sink = BigQueryTelemetrySink(use_mock=True)
    spanner = SpannerPreferenceClient(use_mock=True)

    # 1. Simulate telemetry log entries in BigQuery
    sink.log_interaction(
        user_id=user_id,
        session_id=session_id,
        agent_name="InterviewAgent",
        event_type="USER_PROMPT",
        message="My favorite programming language is Python",
        payload={"prompt": "My favorite programming language is Python"},
    )
    sink.log_interaction(
        user_id=user_id,
        session_id=session_id,
        agent_name="InterviewAgent",
        event_type="USER_PROMPT",
        message="I work as a Staff DevOps Engineer",
        payload={"prompt": "I work as a Staff DevOps Engineer"},
    )
    sink.log_interaction(
        user_id=user_id,
        session_id=session_id,
        agent_name="InterviewAgent",
        event_type="USER_PROMPT",
        message="I prefer using Google Cloud Spanner and BigQuery",
        payload={"prompt": "I prefer using Google Cloud Spanner and BigQuery"},
    )

    synchronizer = BqToSpannerSynchronizer(telemetry_sink=sink, spanner_client=spanner, use_mock=True)

    # 2. Summarize session logs
    summary = synchronizer.summarize_session_logs(session_id=session_id, user_id=user_id)
    assert summary["user_id"] == user_id
    assert summary["session_id"] == session_id
    prefs = {p["preference_key"]: p["preference_value"] for p in summary["preferences"]}
    assert prefs.get("favorite_language") == "Python"
    assert "Staff DevOps Engineer" in prefs.get("job_role", "")
    assert any("Google Cloud Spanner" in str(v) for v in prefs.values())

    # 3. Synchronize to Google Cloud Spanner
    sync_res = synchronizer.sync_to_spanner(user_id=user_id, session_id=session_id)
    assert sync_res["preferences_synced"] == len(summary["preferences"])

    # 4. Verify Spanner storage
    all_stored = spanner.get_all_user_preferences(user_id)
    stored_keys = [p["preference_key"] for p in all_stored]
    assert "favorite_language" in stored_keys
    assert "job_role" in stored_keys
    assert any("tool" in k for k in stored_keys)


def test_log_ordering_newest_to_oldest():
    """Verify that get_session_logs and get_user_logs return logs ordered from newest to oldest (DESC)."""
    user_id = "test-user-order-01"
    session_id = "session-order-01"
    sink = BigQueryTelemetrySink(use_mock=True)

    sink.log_interaction(user_id=user_id, session_id=session_id, agent_name="InterviewAgent", event_type="USER_PROMPT", message="First message")
    sink.log_interaction(user_id=user_id, session_id=session_id, agent_name="InterviewAgent", event_type="AGENT_RESPONSE", message="Second message")
    sink.log_interaction(user_id=user_id, session_id=session_id, agent_name="InterviewAgent", event_type="USER_PROMPT", message="Third message")

    session_logs = sink.get_session_logs(session_id=session_id)
    assert len(session_logs) >= 3
    messages = [l["message"] for l in session_logs[:3]]
    # Newest to oldest: Third, Second, First
    assert messages[0] == "Third message"
    assert messages[1] == "Second message"
    assert messages[2] == "First message"

    user_logs = sink.get_user_logs(user_id=user_id)
    user_messages = [l["message"] for l in user_logs[:3]]
    assert user_messages[0] == "Third message"
    assert user_messages[1] == "Second message"
    assert user_messages[2] == "First message"

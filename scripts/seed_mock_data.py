"""Utility script to seed mock BigQuery logs and Spanner preferences for testing."""

import logging
from src.common.telemetry import BigQueryTelemetrySink
from src.common.spanner_client import SpannerPreferenceClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("adk.seed")


def seed_mock_data(user_id: str = "test-user-001", session_id: str = "session-001"):
    logger.info(f"Seeding mock telemetry logs for user={user_id}, session={session_id}")
    bq_sink = BigQueryTelemetrySink(use_mock=True)
    spanner_client = SpannerPreferenceClient(use_mock=True)

    # Seed some prompts and responses
    bq_sink.log_interaction(
        user_id=user_id,
        session_id=session_id,
        agent_name="InterviewAgent",
        event_type="USER_PROMPT",
        message="My favorite programming language is Python",
        payload={"prompt": "My favorite programming language is Python"},
    )
    bq_sink.log_interaction(
        user_id=user_id,
        session_id=session_id,
        agent_name="InterviewAgent",
        event_type="AGENT_RESPONSE",
        message="Recorded Python as your favorite programming language.",
        payload={"response": "Recorded Python as your favorite programming language."},
    )

    # Seed Spanner preferences
    spanner_client.upsert_user_preference(
        user_id=user_id,
        preference_key="favorite_language",
        preference_value="Python",
        category="technical",
        confidence=0.98,
        source_session_id=session_id,
    )
    spanner_client.upsert_user_preference(
        user_id=user_id,
        preference_key="job_role",
        preference_value="Software Engineer",
        category="role",
        confidence=1.0,
        source_session_id=session_id,
    )
    logger.info("Mock data seeding completed successfully.")
    return bq_sink, spanner_client


if __name__ == "__main__":
    seed_mock_data()

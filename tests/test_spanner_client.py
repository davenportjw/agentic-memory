"""Unit tests for the Google Cloud Spanner profile preference client."""

from src.common.spanner_client import SpannerPreferenceClient


def test_spanner_client_in_memory_crud():
    """Test SpannerPreferenceClient upsert, get, and search in mock mode."""
    client = SpannerPreferenceClient(use_mock=True)
    user_id = "test-spanner-user-42"

    # 1. Upsert preferences
    rec1 = client.upsert_user_preference(
        user_id=user_id,
        preference_key="favorite_language",
        preference_value="Python",
        category="technical",
        confidence=0.99,
        source_session_id="session-42",
    )
    assert rec1["preference_key"] == "favorite_language"
    assert rec1["preference_value"] == "Python"

    rec2 = client.upsert_user_preference(
        user_id=user_id,
        preference_key="job_role",
        preference_value="Principal Cloud Architect",
        category="role",
        confidence=1.0,
        source_session_id="session-42",
    )
    assert rec2["preference_value"] == "Principal Cloud Architect"

    # 2. Get exact preference
    fetched = client.get_user_preference(user_id, "favorite_language")
    assert fetched is not None
    assert fetched["preference_value"] == "Python"

    # 3. Search preferences using vector similarity (no pure text matching)
    results = client.search_user_preferences(user_id, "What programming language do I code in?")
    assert len(results) >= 1
    assert results[0]["preference_key"] == "favorite_language"
    assert len(results[0]["embedding"]) == 768
    assert "cosine_distance" in results[0]

    role_results = client.search_user_preferences(user_id, "What is my job title and career role?")
    assert len(role_results) >= 1
    assert role_results[0]["preference_key"] == "job_role"
    assert len(role_results[0]["embedding"]) == 768

    # 4. Get all preferences
    all_prefs = client.get_all_user_preferences(user_id)
    assert len(all_prefs) == 2

    # 5. Upsert session summary
    sum_rec = client.upsert_session_summary(user_id, "session-42", "Summary text for session 42")
    assert sum_rec["session_id"] == "session-42"
    assert sum_rec["summary_text"] == "Summary text for session 42"


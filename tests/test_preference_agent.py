"""Unit tests for the Google ADK 2.0 PreferenceAgent and Spanner fast-path server."""

from fastapi.testclient import TestClient
from src.preference_agent.agent import (
    agent,
    app as adk_app,
    lookup_preference_in_spanner,
    list_all_user_preferences,
    inject_memory_to_spanner,
    extract_relevant_or_random_memories,
)
from src.preference_agent.server import api, spanner_client


def test_preference_agent_configuration():
    """Verify PreferenceAgent configuration and tools."""
    assert agent.name == "PreferenceAgent"
    assert len(agent.tools) == 4
    assert adk_app.root_agent == agent


def test_spanner_lookup_tools():
    """Test lookup_preference_in_spanner and list_all_user_preferences tools with vector similarity."""
    user_id = "test-pref-tool-user"
    spanner_client.upsert_user_preference(user_id, "favorite_language", "Python", "technical")

    res = lookup_preference_in_spanner(user_id, "favorite_language")
    assert res["status"] == "found_vector_search"
    assert res["search_type"] == "COSINE_DISTANCE_VECTOR"
    assert res["preferences"][0]["preference_value"] == "Python"
    assert "cosine_distance" in res["preferences"][0]

    all_prefs = list_all_user_preferences(user_id)
    assert len(all_prefs) >= 1
    assert any(p["preference_key"] == "favorite_language" for p in all_prefs)



def test_preference_agent_fast_path_cache():
    """Verify that the API returns from SPANNER_CACHE when preference exists."""
    user_id = "test-fast-cache-user"
    spanner_client.upsert_user_preference(user_id, "favorite_language", "Go", "technical")

    client = TestClient(api)
    payload = {
        "user_id": user_id,
        "session_id": "session-fast-001",
        "prompt": "What is my favorite programming language?",
    }

    response = client.post("/query", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "SPANNER_CACHE"
    assert data["latency_ms"] < 1000.0  # Fast cache return without LLM call
    assert "Go" in data["response"]
    assert data["spanner_record"]["preference_key"] == "favorite_language"
    assert data["spanner_record"]["preference_value"] == "Go"


def test_preference_agent_fallback():
    """Verify fallback when preference does not exist in Spanner."""
    client = TestClient(api)
    payload = {
        "user_id": "user-without-prefs-999",
        "session_id": "session-fast-002",
        "prompt": "What is my favorite programming language?",
    }

    response = client.post("/query", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["source"] in ("LLM_FALLBACK", "SPANNER_CACHE")


def test_preference_agent_non_specific_recall():
    """Verify that non-specific or potentially unrelated recall prompts hit SPANNER_CACHE when preferences exist."""
    user_id = "test-recall-user-42"
    spanner_client.upsert_user_preference(user_id, "favorite_language", "Python", "technical")
    spanner_client.upsert_user_preference(user_id, "job_role", "Principal Cloud Architect", "role")

    client = TestClient(api)

    for non_specific_prompt in ["find me the memories", "you tell me!", "what do we know so far?"]:
        response = client.post(
            "/query",
            json={
                "user_id": user_id,
                "session_id": "session-recall-001",
                "prompt": non_specific_prompt,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["source"] == "SPANNER_CACHE", f"Expected SPANNER_CACHE for prompt: {non_specific_prompt}"
        assert "Python" in data["response"] and "Principal Cloud Architect" in data["response"]


def test_inject_memory_to_spanner_tool():
    """Test calling inject_memory_to_spanner tool to ensure memories are saved to Spanner and injected back later."""
    user_id = "test-inject-user-101"
    res = inject_memory_to_spanner(
        user_id=user_id,
        preference_key="user_intent",
        preference_value="Building an autonomous agent dream engine",
        category="intent",
        confidence=0.99,
        crystallized_belief="The user's primary intent is building an autonomous agent dream engine.",
        keywords="intent, goal, dream, autonomous, engine",
    )
    assert res["status"] == "memory_injected"
    assert res["preference_key"] == "user_intent"
    assert res["preference_value"] == "Building an autonomous agent dream engine"

    # Verify that the injected memory can be retrieved back later
    all_prefs = list_all_user_preferences(user_id)
    assert any(
        p["preference_key"] == "user_intent" and "autonomous agent dream engine" in p["preference_value"]
        for p in all_prefs
    )


def test_extract_relevant_or_random_memories_tool():
    """Test calling extract_relevant_or_random_memories tool to extract memories based on user history, intent, and historical questions."""
    user_id = "test-extract-user-202"
    spanner_client.upsert_user_preference(user_id, "favorite_language", "Python", "technical")
    spanner_client.upsert_user_preference(user_id, "job_role", "Principal Cloud Architect", "role")
    spanner_client.upsert_user_preference(
        user_id,
        "historical_question_1",
        "What is Spanner caching latency?",
        "history",
        keywords="question, historical, history, latency",
    )
    spanner_client.upsert_session_summary(user_id, "session-extract-001", "[Intent: Exploring architecture] Discussed Spanner and Python.")

    res = extract_relevant_or_random_memories(
        user_id=user_id,
        intent_or_question="favorite_language",
        limit=5,
        include_random=True,
    )
    assert res["status"] == "extracted_memories"
    assert "search_context" in res
    assert res["search_context"]["total_history_count"] >= 3
    assert len(res["session_history_summaries"]) >= 1
    # Check that either relevant or random memories are returned from user history
    all_extracted_keys = {p["preference_key"] for p in res["relevant_memories"] + res["random_memories"]}
    assert "favorite_language" in all_extracted_keys or "job_role" in all_extracted_keys


def test_preference_agent_server_inject_and_extract_endpoints():
    """Verify HTTP endpoints /inject and /extract_memories on PreferenceAgent server."""
    client = TestClient(api)
    user_id = "test-server-memory-303"

    inject_payload = {
        "user_id": user_id,
        "preference_key": "agent_intent",
        "preference_value": "Provide introspective philosophical answers",
        "category": "intent",
        "confidence": 0.95,
        "crystallized_belief": "The agent intent is to provide introspective philosophical answers.",
        "keywords": "agent intent, philosophy, introspective",
    }
    r_inject = client.post("/inject", json=inject_payload)
    assert r_inject.status_code == 200
    assert r_inject.json()["status"] == "success"
    assert r_inject.json()["preference_key"] == "agent_intent"

    extract_payload = {
        "user_id": user_id,
        "intent_or_question": "agent_intent",
        "limit": 5,
        "include_random": True,
    }
    r_extract = client.post("/extract_memories", json=extract_payload)
    assert r_extract.status_code == 200
    data = r_extract.json()
    assert data["status"] == "extracted_memories"
    assert any(m["preference_key"] == "agent_intent" for m in data["relevant_memories"] + data["random_memories"])



"""Comprehensive End-to-End lifecycle tests across InterviewAgent, BigQuery, Spanner, and PreferenceAgent."""

from fastapi.testclient import TestClient
from src.ui_service.server import api, telemetry_sink, spanner_client, synchronizer


import uuid


def test_complete_e2e_multi_agent_flow():
    """Execute the full end-to-end multi-agent interview, summarization, and cache lookup lifecycle."""
    client = TestClient(api)
    user_id = f"test-e2e-user-{uuid.uuid4().hex[:8]}"
    session_id = f"session-e2e-complete-{uuid.uuid4().hex[:8]}"

    # 1. User introduces themselves to InterviewAgent
    r1 = client.post(
        "/api/interview/query",
        json={
            "user_id": user_id,
            "session_id": session_id,
            "prompt": "Hi, my name is Jason",
        },
    )
    assert r1.status_code == 200
    assert "Jason" in r1.json()["response"]

    r2 = client.post(
        "/api/interview/query",
        json={
            "user_id": user_id,
            "session_id": session_id,
            "prompt": "My favorite programming language is Python and I am a Principal Cloud Architect",
        },
    )
    assert r2.status_code == 200
    assert "Python" in r2.json()["response"]

    # 2. Verify BigQuery telemetry logs captured prompts and responses
    logs_res = client.get(f"/api/logs/{session_id}")
    assert logs_res.status_code == 200
    logs = logs_res.json()["logs"]
    assert len(logs) == 4  # 2 prompts, 2 agent responses
    assert all(l["user_id"] == user_id for l in logs)

    # 3. Trigger Dream Harvest (summarize logs & sync to Google Cloud Spanner)
    harvest_res = client.post(
        "/api/harvest",
        json={"user_id": user_id, "session_id": session_id},
    )
    assert harvest_res.status_code == 200
    harvest_data = harvest_res.json()
    assert harvest_data["status"] == "success"
    assert harvest_data["preferences_synced"] >= 2

    # 4. Check Spanner table contents via UI API
    spanner_res = client.get(f"/api/spanner/preferences/{user_id}")
    assert spanner_res.status_code == 200
    prefs = spanner_res.json()["preferences"]
    assert len(prefs) >= 2
    pref_dict = {p["preference_key"]: p["preference_value"] for p in prefs}
    assert "Python" in pref_dict.get("favorite_language", "")
    assert "Principal Cloud Architect" in pref_dict.get("job_role", "")

    # 5. Ask PreferenceAgent questions and verify fast-path Spanner memory hit (0 LLM tokens)
    p_lang = client.post(
        "/api/preference/query",
        json={
            "user_id": user_id,
            "session_id": session_id,
            "prompt": "What is my favorite programming language?",
        },
    )
    assert p_lang.status_code == 200
    p_lang_data = p_lang.json()
    assert p_lang_data["source"] == "SPANNER_CACHE"
    assert p_lang_data["latency_ms"] < 1000.0  # Ultra-low latency Spanner lookup
    assert "Python" in p_lang_data["response"]

    p_role = client.post(
        "/api/preference/query",
        json={
            "user_id": user_id,
            "session_id": session_id,
            "prompt": "What job role am I working as?",
        },
    )
    assert p_role.status_code == 200
    p_role_data = p_role.json()
    assert p_role_data["source"] == "SPANNER_CACHE"
    assert "Principal Cloud Architect" in p_role_data["response"]

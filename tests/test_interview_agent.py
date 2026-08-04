"""Unit tests for the Google ADK 2.0 InterviewAgent and its server."""

import pytest
from fastapi.testclient import TestClient
from src.interview_agent.agent import record_user_answer, get_recorded_answers, get_random_interview_question, agent, app as adk_app
from src.interview_agent.server import api, telemetry_sink


def test_adk_agent_configuration():
    """Verify ADK 2.0 Agent configuration and tools."""
    assert agent.name == "InterviewAgent"
    assert len(agent.tools) == 3
    assert adk_app.root_agent == agent


def test_get_random_interview_question():
    """Verify that get_random_interview_question generates randomized follow-up interview questions."""
    user_id = "test-random-user-01"
    q1 = get_random_interview_question(user_id)
    assert "topic" in q1
    assert "category" in q1
    assert "suggested_question" in q1
    assert len(q1["suggested_question"]) > 0

    # Test excluding topics explicitly
    q2 = get_random_interview_question(user_id, exclude_topics=["job_role", "favorite_language"])
    assert q2["topic"] not in ("job_role", "favorite_language")


def test_record_and_get_user_answers():
    """Test the record_user_answer and get_recorded_answers tools."""
    user_id = "test-user-100"
    res1 = record_user_answer(user_id, "favorite_language", "Python", "technical")
    assert res1["status"] == "recorded"
    assert res1["topic"] == "favorite_language"
    assert res1["answer"] == "Python"

    res2 = record_user_answer(user_id, "job_role", "Data Engineer", "role")
    assert res2["status"] == "recorded"

    answers = get_recorded_answers(user_id)
    assert len(answers) == 2
    assert any(a["topic"] == "favorite_language" and a["answer"] == "Python" for a in answers)
    assert any(a["topic"] == "job_role" and a["answer"] == "Data Engineer" for a in answers)


def test_interview_agent_api_endpoint():
    """Test the REST /query endpoint and BigQuery telemetry logging."""
    client = TestClient(api)
    payload = {
        "user_id": "test-user-api-01",
        "session_id": "session-api-001",
        "prompt": "Hi, my name is Jason and my favorite programming language is Python",
    }

    response = client.post("/query", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "test-user-api-01"
    assert data["session_id"] == "session-api-001"
    assert "Python" in data["response"] or "Jason" in data["response"]
    assert len(data["recorded_answers"]) >= 1

    # Verify telemetry logs were captured
    logs = telemetry_sink.get_session_logs("session-api-001")
    assert len(logs) >= 2
    event_types = [log["event_type"] for log in logs]
    assert "USER_PROMPT" in event_types
    assert "AGENT_RESPONSE" in event_types

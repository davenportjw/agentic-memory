"""Tests for Electric Sheep v2 Model Context Protocol (MCP) server."""

import pytest
from unittest.mock import MagicMock, patch
from src.mcp_server.server import (
    get_user_crystallized_beliefs,
    search_agent_memories,
    get_recent_session_memory,
    crystallize_live_preference,
)


@pytest.fixture
def mock_spanner():
    with patch("src.mcp_server.server.get_spanner") as mock_get:
        client = MagicMock()
        mock_get.return_value = client
        yield client


def test_get_user_crystallized_beliefs_returns_markdown(mock_spanner):
    mock_spanner.get_all_user_preferences.return_value = [
        {
            "preference_key": "favorite_language",
            "preference_value": "Python",
            "category": "technical",
            "confidence": 0.95,
            "crystallized_belief": "User prefers Python for data engineering and Go for high-throughput systems.",
            "keywords": "python, go, data-engineering",
        }
    ]

    result = get_user_crystallized_beliefs("test-user")
    assert "Cloud Spanner Crystalline Memory for User `test-user`" in result
    assert "favorite_language" in result
    assert "User prefers Python" in result


def test_get_user_crystallized_beliefs_empty(mock_spanner):
    mock_spanner.get_all_user_preferences.return_value = []
    result = get_user_crystallized_beliefs("nonexistent-user")
    assert "No crystallized beliefs found" in result


def test_search_agent_memories_returns_formatted_results(mock_spanner):
    mock_spanner.search_user_preferences.return_value = [
        {
            "preference_key": "cloud_platform",
            "preference_value": "GCP",
            "category": "infrastructure",
            "confidence": 0.98,
            "crystallized_belief": "User operates primary infrastructure on Google Cloud Platform.",
            "distance": 0.088,
        }
    ]

    result = search_agent_memories("cloud preferences", "test-user")
    assert "Relevant Memories for Query" in result
    assert "cloud_platform" in result
    assert "Similarity: 91%" in result


def test_get_recent_session_memory_found(mock_spanner):
    mock_spanner.get_session_summary.return_value = {
        "user_id": "test-user",
        "session_id": "session-100",
        "summary_text": "User successfully refactored BigQuery pipeline to Spanner sync.",
        "created_at": "2026-10-12T17:00:00Z",
    }

    result = get_recent_session_memory("test-user", "session-100")
    assert "Session Summary for `session-100`" in result
    assert "User successfully refactored" in result


def test_crystallize_live_preference_executes_mutation(mock_spanner):
    mock_spanner.write_preferences.return_value = None

    result = crystallize_live_preference(
        user_id="test-user",
        preference_key="test_runner",
        preference_value="uv run pytest",
        category="tooling",
        crystallized_belief="The user strictly requires uv run pytest for all test suites.",
    )

    assert "Successfully crystallized `test_runner" in result
    mock_spanner.write_preferences.assert_called_once()

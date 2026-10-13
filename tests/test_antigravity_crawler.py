"""Tests for Antigravity Session Crawler in Electric Sheep v2."""

import json
import tempfile
from pathlib import Path
from src.common.antigravity_crawler import AntigravitySessionCrawler, AntigravityTurn


def test_crawler_parses_transcript_correctly():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        conv_id = "test-conv-1234"
        conv_dir = tmp_path / conv_id / ".system_generated" / "logs"
        conv_dir.mkdir(parents=True, exist_ok=True)

        transcript_file = conv_dir / "transcript.jsonl"
        steps = [
            {
                "step_index": 1,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "Please implement the Spanner database client for agent memory."
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "thinking": "The user needs a Spanner client with sub-10ms query capabilities.",
                "tool_calls": [
                    {
                        "name": "view_file",
                        "args": {"AbsolutePath": "/path/to/spanner.py"}
                    }
                ]
            },
            {
                "step_index": 3,
                "source": "SYSTEM",
                "type": "TOOL_RESULT",
                "status": "DONE",
                "content": "File content: class SpannerMemoryClient: ..."
            }
        ]

        with open(transcript_file, "w") as f:
            for s in steps:
                f.write(json.dumps(s) + "\n")

        crawler = AntigravitySessionCrawler(brain_path=str(tmp_path))
        sessions = crawler.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == conv_id

        turns = crawler.get_session_turns(conv_id)
        assert len(turns) == 4

        # User turn
        assert turns[0].step_index == 1
        assert turns[0].turn_type == "USER_PROMPT"
        assert "Spanner database client" in turns[0].user_prompt

        # Thinking / response turn
        assert turns[1].step_index == 2
        assert turns[1].turn_type == "AGENT_RESPONSE"
        assert "sub-10ms query" in turns[1].agent_thought

        # Tool call turn
        assert turns[2].step_index == 2
        assert turns[2].turn_type == "TOOL_CALL"
        assert turns[2].tool_name == "view_file"

        # Format transcript for REM dream
        formatted = crawler.format_session_transcript(conv_id)
        assert "USER: Please implement the Spanner database client" in formatted
        assert "TOOL CALL [view_file]" in formatted


def test_crawler_resolves_truncated_fields():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        conv_id = "test-conv-trunc"
        conv_dir = tmp_path / conv_id / ".system_generated" / "logs"
        conv_dir.mkdir(parents=True, exist_ok=True)

        compact_file = conv_dir / "transcript.jsonl"
        full_file = conv_dir / "transcript_full.jsonl"

        compact_step = {
            "step_index": 1,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "Short...",
            "truncated_fields": ["content"]
        }
        full_step = {
            "step_index": 1,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "Short full expanded user prompt that was truncated in compact log."
        }

        with open(compact_file, "w") as f:
            f.write(json.dumps(compact_step) + "\n")
        with open(full_file, "w") as f:
            f.write(json.dumps(full_step) + "\n")

        crawler = AntigravitySessionCrawler(brain_path=str(tmp_path))
        turns = crawler.get_session_turns(conv_id)
        assert len(turns) == 1
        assert "expanded user prompt" in turns[0].user_prompt

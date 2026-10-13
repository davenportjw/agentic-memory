"""Crawler and parser for local Google Antigravity developer sessions.

Traverses ~/.gemini/antigravity/brain and ~/.gemini/antigravity/conversation_summaries.db,
extracting structured turns, model thoughts, tool calls, and outputs for telemetry and REM dream memory synthesis.
"""

import json
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.common.config import get_config

logger = logging.getLogger("electric_sheep.antigravity_crawler")


@dataclass
class AntigravityTurn:
    session_id: str
    step_index: int
    timestamp: str
    source: str
    turn_type: str
    user_prompt: str = ""
    agent_thought: str = ""
    tool_name: str = ""
    tool_args_json: str = "{}"
    tool_output: str = ""
    status: str = "DONE"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AntigravitySessionCrawler:
    """Discovers and parses local Antigravity sessions into structured turns."""

    def __init__(self, brain_path: Optional[str] = None):
        config = get_config()
        self.brain_path = Path(brain_path or config.antigravity_brain_path).expanduser()
        self.db_path = self.brain_path.parent / "conversation_summaries.db"

    def list_sessions(self, limit: int = 30) -> List[Dict[str, Any]]:
        """List active and historical Antigravity sessions with summaries."""
        sessions: List[Dict[str, Any]] = []

        # 1. Attempt SQLite conversation_summaries.db lookup
        if self.db_path.exists():
            try:
                conn = sqlite3.connect(str(self.db_path))
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT conversation_id, title, workspace_uris, step_count, last_modified_time
                    FROM conversation_summaries
                    ORDER BY last_modified_time DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                for row in cursor.fetchall():
                    conv_id, title, work_uris, step_cnt, last_mod = row
                    sessions.append(
                        {
                            "session_id": conv_id,
                            "title": title or f"Session {conv_id[:8]}",
                            "workspace": work_uris or "",
                            "step_count": step_cnt or 0,
                            "last_modified": last_mod or "",
                            "source": "sqlite",
                        }
                    )
                conn.close()
                if sessions:
                    return sessions
            except Exception as e:
                logger.warning(f"Error querying conversation_summaries.db: {e}")

        # 2. Filesystem fallback: scan brain directories
        if self.brain_path.exists():
            conv_dirs = [d for d in self.brain_path.iterdir() if d.is_dir() and not d.name.startswith(".")]
            conv_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
            for d in conv_dirs[:limit]:
                log_file = d / ".system_generated" / "logs" / "transcript.jsonl"
                step_count = 0
                if log_file.exists():
                    try:
                        with open(log_file, "r", encoding="utf-8") as f:
                            step_count = sum(1 for _ in f)
                    except Exception:
                        pass
                sessions.append(
                    {
                        "session_id": d.name,
                        "title": f"Antigravity Session {d.name[:8]}",
                        "workspace": "",
                        "step_count": step_count,
                        "last_modified": str(d.stat().st_mtime),
                        "source": "filesystem",
                    }
                )

        return sessions

    def get_session_turns(self, session_id: str, limit: int = 100) -> List[AntigravityTurn]:
        """Extract structured turns from a specific Antigravity session transcript."""
        session_dir = self.brain_path / session_id
        logs_dir = session_dir / ".system_generated" / "logs"
        compact_log = logs_dir / "transcript.jsonl"
        full_log = logs_dir / "transcript_full.jsonl"

        if not compact_log.exists():
            logger.warning(f"No transcript found for session {session_id} at {compact_log}")
            return []

        # Read full log if available for truncated fields
        full_lines: List[Dict[str, Any]] = []
        if full_log.exists():
            try:
                with open(full_log, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            full_lines.append(json.loads(line))
            except Exception as e:
                logger.warning(f"Could not load transcript_full.jsonl: {e}")

        turns: List[AntigravityTurn] = []
        with open(compact_log, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if not line.strip():
                    continue
                step = json.loads(line)
                truncated = step.get("truncated_fields", [])
                if truncated and idx < len(full_lines):
                    full_step = full_lines[idx]
                    for field_name in truncated:
                        if field_name in full_step:
                            step[field_name] = full_step[field_name]

                source = step.get("source", "GENERIC")
                turn_type = step.get("type", "GENERIC")
                step_idx = step.get("step_index", idx)
                timestamp = step.get("created_at", "")
                status = step.get("status", "DONE")
                content = step.get("content", "")
                thinking = step.get("thinking", "")
                tool_calls = step.get("tool_calls", [])

                if turn_type == "USER_INPUT":
                    turns.append(
                        AntigravityTurn(
                            session_id=session_id,
                            step_index=step_idx,
                            timestamp=timestamp,
                            source=source,
                            turn_type="USER_PROMPT",
                            user_prompt=content,
                            status=status,
                        )
                    )
                elif turn_type == "PLANNER_RESPONSE":
                    # Emit model thought / plan
                    if thinking or content:
                        turns.append(
                            AntigravityTurn(
                                session_id=session_id,
                                step_index=step_idx,
                                timestamp=timestamp,
                                source=source,
                                turn_type="AGENT_RESPONSE",
                                agent_thought=thinking,
                                user_prompt=content if not tool_calls else "",
                                status=status,
                            )
                        )
                    # Emit individual tool calls
                    for tc in tool_calls:
                        t_name = tc.get("name", "")
                        t_args = json.dumps(tc.get("arguments", {}))
                        turns.append(
                            AntigravityTurn(
                                session_id=session_id,
                                step_index=step_idx,
                                timestamp=timestamp,
                                source=source,
                                turn_type="TOOL_CALL",
                                tool_name=t_name,
                                tool_args_json=t_args,
                                status=status,
                            )
                        )
                else:
                    turns.append(
                        AntigravityTurn(
                            session_id=session_id,
                            step_index=step_idx,
                            timestamp=timestamp,
                            source=source,
                            turn_type=turn_type,
                            user_prompt=content if source == "USER_EXPLICIT" else "",
                            tool_output=content if turn_type == "TOOL_RESULT" else "",
                            status=status,
                        )
                    )

                if len(turns) >= limit:
                    break

        return turns

    def format_session_transcript(self, session_id: str, limit: int = 60) -> str:
        """Format a human/LLM readable transcript of the session for dream synthesis."""
        turns = self.get_session_turns(session_id, limit=limit)
        if not turns:
            return ""

        lines = []
        for t in turns:
            if t.turn_type == "USER_PROMPT" and t.user_prompt:
                lines.append(f"USER: {t.user_prompt.strip()}")
            elif t.turn_type == "AGENT_RESPONSE" and t.agent_thought:
                # Truncate internal thoughts to avoid blowing token window
                short_thought = t.agent_thought[:400] + ("..." if len(t.agent_thought) > 400 else "")
                lines.append(f"AGENT THOUGHT: {short_thought.strip()}")
            elif t.turn_type == "TOOL_CALL":
                lines.append(f"TOOL CALL [{t.tool_name}]: args={t.tool_args_json[:300]}")
            elif t.turn_type == "TOOL_RESULT" and t.tool_output:
                lines.append(f"TOOL RESULT: {t.tool_output[:300]}")

        return "\n\n".join(lines)

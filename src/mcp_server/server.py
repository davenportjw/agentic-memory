"""Electric Sheep v2 Model Context Protocol (MCP) Server.

Exposes Google Cloud Spanner Crystalline Memory and Session Summaries to Antigravity agents
via the standard MCP 2.x protocol over stdio.
"""

import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger("electric_sheep.mcp_server")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

from src.common.config import get_config
from src.common.spanner_client import SpannerPreferenceClient

try:
    from mcp.server.mcpserver import MCPServer
    server = MCPServer(
        name="electric-sheep-memory",
        version="2.0.0",
        instructions="Retrieves and manages crystallized long-term beliefs and session context from Google Cloud Spanner.",
    )
except (ImportError, ModuleNotFoundError):
    try:
        from mcp.server.fastmcp import FastMCP
        server = FastMCP(
            name="electric-sheep-memory",
            instructions="Retrieves and manages crystallized long-term beliefs and session context from Google Cloud Spanner.",
        )
    except Exception as exc:
        logger.warning(f"Could not initialize MCPServer or FastMCP: {exc}")
        class _FallbackServer:
            def tool(self, *args, **kwargs):
                def decorator(fn):
                    return fn
                if len(args) == 1 and callable(args[0]):
                    return args[0]
                return decorator
            def run(self, *args, **kwargs):
                raise RuntimeError("MCP stdio runtime not available in this environment")
        server = _FallbackServer()

_client: Optional[SpannerPreferenceClient] = None


def get_spanner() -> SpannerPreferenceClient:
    global _client
    if _client is None:
        _client = SpannerPreferenceClient()
    return _client


@server.tool()
def get_user_crystallized_beliefs(
    user_id: str = "demo-user-42",
    category: Optional[str] = None,
) -> str:
    """Retrieve all permanent crystallized beliefs, preferences, and architectural invariants for a user from Cloud Spanner.

    Args:
        user_id: Unique identifier for the user (default: 'demo-user-42').
        category: Optional category filter (e.g. 'profile', 'technical', 'infrastructure', 'tooling', 'style', 'intent').
    """
    client = get_spanner()
    prefs = client.get_all_user_preferences(user_id=user_id)
    if not prefs:
        return f"No crystallized beliefs found in Spanner for user '{user_id}'."

    if category:
        prefs = [p for p in prefs if p.get("category", "").lower() == category.lower()]
        if not prefs:
            return f"No beliefs found in category '{category}' for user '{user_id}'."

    lines = [f"### Cloud Spanner Crystalline Memory for User `{user_id}`:"]
    for p in prefs:
        key = p.get("preference_key", "")
        val = p.get("preference_value", "")
        cat = p.get("category", "preference")
        conf = p.get("confidence", 1.0)
        belief = p.get("crystallized_belief", val)
        lines.append(f"- **[{cat.upper()}] `{key}`**: {belief} *(Confidence: {conf:.0%})*")

    return "\n".join(lines)


@server.tool()
def get_recent_session_memory(
    user_id: str = "demo-user-42",
    session_id: Optional[str] = None,
) -> str:
    """Retrieve episodic working memory and intent summary for recent sessions from Cloud Spanner.

    Args:
        user_id: Unique identifier for the user (default: 'demo-user-42').
        session_id: Optional specific session ID to retrieve. If omitted, returns recent summaries.
    """
    client = get_spanner()
    if session_id:
        summary = client.get_session_summary(user_id=user_id, session_id=session_id)
        if not summary:
            return f"No session summary found for session '{session_id}' (user='{user_id}')."
        return f"### Session Summary for `{session_id}`:\n{summary.get('summary_text', '')}"

    summaries = client.get_user_session_summaries(user_id=user_id)
    if not summaries:
        return f"No historical session summaries found for user '{user_id}'."

    lines = [f"### Recent Session Summaries for User `{user_id}`:"]
    for s in summaries[:5]:
        sid = s.get("session_id", "unknown")
        created = s.get("created_at", "")
        text = s.get("summary_text", "")
        lines.append(f"- **Session `{sid}`** ({created}):\n  {text}")

    return "\n\n".join(lines)


@server.tool()
def search_agent_memories(
    query: str,
    user_id: str = "demo-user-42",
    limit: int = 5,
) -> str:
    """Search long-term memories using 768-dim vector cosine similarity across Cloud Spanner.

    Args:
        query: The semantic search query or question (e.g. 'what is the preferred test runner or language?').
        user_id: Unique identifier for the user (default: 'demo-user-42').
        limit: Maximum number of relevant memories to return (default: 5).
    """
    client = get_spanner()
    matches = client.search_user_preferences(user_id=user_id, query=query, limit=limit)
    if not matches:
        return f"No semantically matching memories found for query: '{query}'."

    lines = [f"### Relevant Memories for Query: *\"{query}\"*:"]
    for m in matches:
        key = m.get("preference_key", "")
        cat = m.get("category", "")
        dist = m.get("distance", 0.0)
        sim = max(0.0, 1.0 - dist)
        belief = m.get("crystallized_belief") or m.get("preference_value", "")
        lines.append(f"- **[{cat.upper()}] `{key}`** (Similarity: {sim:.0%}): {belief}")

    return "\n".join(lines)


@server.tool()
def crystallize_live_preference(
    user_id: str,
    preference_key: str,
    preference_value: str,
    category: str = "preference",
    crystallized_belief: str = "",
    keywords: str = "",
) -> str:
    """Commit a new permanent belief or constraint directly into Cloud Spanner crystalline memory.

    Args:
        user_id: Unique identifier for the user.
        preference_key: Canonical snake_case key (e.g. 'hardware_target', 'test_runner').
        preference_value: Concise exact value (e.g. 'Apple Silicon', 'uv run pytest').
        category: Category (e.g. 'infrastructure', 'tooling', 'style', 'profile').
        crystallized_belief: Natural language semantic statement of the belief.
        keywords: Comma-separated search keywords.
    """
    client = get_spanner()
    pref_record = {
        "preference_key": preference_key,
        "preference_value": preference_value,
        "category": category,
        "confidence": 1.0,
        "crystallized_belief": crystallized_belief or preference_value,
        "keywords": keywords or f"{preference_key}, {preference_value}, {category}",
        "source_session_id": "MCP_LIVE_INTERACTION",
    }
    client.write_preferences(user_id=user_id, preferences=[pref_record])
    return f"Successfully crystallized `{preference_key} = {preference_value}` in Spanner for user '{user_id}' with commit timestamp."


def main():
    """Main entrypoint for MCP stdio transport."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

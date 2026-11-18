---
name: electric-sheep-memory
description: >-
  Queries and crystallizes Cloud Spanner Crystalline Memory entries via the electric-sheep-memory
  MCP server strictly for Electric Sheep repository demos (never for global Antigravity preferences).
---

# Electric Sheep Demo Memory (`electric-sheep-memory`)

Use this skill and the `electric-sheep-memory` MCP server (`adk-spanner-instance:agent_memory_db` in `davenport-boutique`) **only** when running or testing Electric Sheep Crystalline Memory demos inside `~/GitHub/electric-sheep`. Persist real developer/agent rules in `~/.gemini/GEMINI.md` or repo `AGENTS.md`.

## MCP Tools
- `get_user_crystallized_beliefs(user_id, category)` — fetch demo beliefs (`'technical'`, `'infrastructure'`, `'tooling'`, `'profile'`, `'style'`, `'intent'`; default demo user: `demo-user-42`).
- `get_recent_session_memory(user_id, session_id)` — fetch session summaries from `UserSessionSummaries`.
- `search_agent_memories(query, user_id, limit)` — semantic vector search over 768-dim Spanner embeddings.
- `crystallize_live_preference(user_id, preference_key, preference_value, category, crystallized_belief)` — write a demo belief to Spanner.

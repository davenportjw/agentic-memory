"""Google ADK 2.0 Preference Agent that looks up answers in Google Cloud Spanner.

CRITICAL DEPLOYMENT & TESTING REQUIREMENT:
Any code changes MUST be tested in the deployed state on Google Cloud Run.
Deployment to Google Cloud Run MUST be the final step of any development workflow.
"""

import logging
import random
from typing import Any, Dict, List, Optional
from google.adk.agents import Agent
from google.adk.apps.app import App
from src.common.config import get_config
from src.common.spanner_client import SpannerPreferenceClient

logger = logging.getLogger("adk.preference_agent")
spanner_client = SpannerPreferenceClient()


def lookup_preference_in_spanner(user_id: str, preference_key_or_question: str) -> Dict[str, Any]:
    """Tool: Look up a user's stored answer or preference in Google Cloud Spanner using vector similarity search (COSINE_DISTANCE).

    Never uses pure text or substring matching; instead searches 768-dim semantic vector embeddings.

    Args:
        user_id: The unique user identifier.
        preference_key_or_question: The natural language question or topic to search for (e.g., 'What programming language do I code in?', 'favorite_language', 'job_role').
    """
    # Search preferences using vector similarity (COSINE_DISTANCE over embeddings)
    matching = spanner_client.search_user_preferences(user_id, preference_key_or_question)
    if matching:
        return {
            "status": "found_vector_search",
            "preferences": matching,
            "search_type": "COSINE_DISTANCE_VECTOR",
        }

    # If no specific vector match found above threshold, return all stored preferences so the agent can inspect full crystalline memory
    all_prefs = spanner_client.get_all_user_preferences(user_id)
    if all_prefs:
        return {"status": "all_preferences", "preferences": all_prefs}

    return {"status": "not_found", "message": f"No stored preference found in Spanner for '{preference_key_or_question}'"}



def list_all_user_preferences(user_id: str) -> List[Dict[str, Any]]:
    """Tool: Retrieve all stored profile preferences for the user from Google Cloud Spanner.

    Args:
        user_id: The unique user identifier.
    """
    return spanner_client.get_all_user_preferences(user_id)


def inject_memory_to_spanner(
    user_id: str,
    preference_key: str,
    preference_value: str,
    category: str = "preference",
    confidence: float = 1.0,
    crystallized_belief: str = "",
    keywords: str = "",
) -> Dict[str, Any]:
    """Tool: Inject or update a memory or preference back into Google Cloud Spanner so it can be recalled later.

    Use this tool when the user shares new preferences, corrects an existing belief, or states an intent or historical memory
    that should be crystallized into Spanner for future sessions.

    Args:
        user_id: The unique user identifier.
        preference_key: The canonical key for the memory (e.g., 'favorite_language', 'job_role', 'user_intent', 'agent_intent').
        preference_value: The value or content of the memory.
        category: Category of the memory (e.g., 'preference', 'technical', 'role', 'intent', 'history').
        confidence: Confidence level from 0.0 to 1.0.
        crystallized_belief: A complete natural language semantic belief statement.
        keywords: Comma-separated search keywords and synonyms.
    """
    if not crystallized_belief:
        crystallized_belief = f"User preference for {preference_key} is {preference_value}."
    record = spanner_client.upsert_user_preference(
        user_id=user_id,
        preference_key=preference_key,
        preference_value=preference_value,
        category=category,
        confidence=confidence,
        source_session_id="preference_agent_injection",
        crystallized_belief=crystallized_belief,
        keywords=keywords,
    )
    logger.info(f"Injected memory into Spanner for user={user_id}: {preference_key}={preference_value}")
    return {
        "status": "memory_injected",
        "preference_key": preference_key,
        "preference_value": preference_value,
        "category": category,
        "confidence": confidence,
        "record": record,
    }


def extract_relevant_or_random_memories(
    user_id: str,
    intent_or_question: str = "",
    limit: int = 5,
    include_random: bool = True,
) -> Dict[str, Any]:
    """Tool: Look through the history of a user and extract random or relevant memories that are useful based on the agent's intent, the user's intent, or historical questions.

    Args:
        user_id: The unique user identifier.
        intent_or_question: The agent's intent, the user's intent, or historical question topic to search for (e.g. 'agent_intent', 'user_intent', 'what questions were asked', or a specific topic).
        limit: Maximum number of memories to return.
        include_random: Whether to include random historical memories from the user's history for serendipity and broader recall.
    """
    all_prefs = spanner_client.get_all_user_preferences(user_id)
    session_summaries = spanner_client.get_user_session_summaries(user_id)

    relevant_memories = []
    if intent_or_question:
        relevant_memories = spanner_client.search_user_preferences(
            user_id=user_id,
            query=intent_or_question,
            limit=limit,
            distance_threshold=0.85,
        )

    random_memories = []
    if include_random or len(relevant_memories) < limit:
        remaining = [p for p in all_prefs if p not in relevant_memories]
        sample_size = min(len(remaining), limit)
        random_memories = random.sample(remaining, sample_size) if remaining else []

    return {
        "status": "extracted_memories",
        "relevant_memories": relevant_memories,
        "random_memories": random_memories,
        "session_history_summaries": session_summaries[:3],
        "search_context": {
            "intent_or_question": intent_or_question,
            "total_history_count": len(all_prefs),
            "session_summaries_count": len(session_summaries),
        },
    }


config = get_config()

agent = Agent(
    name="PreferenceAgent",
    model=config.model_name,
    instruction="""
    You are a fast Preference & Profile Agent built with Google ADK 2.0 and integrated with Google Cloud Spanner, embodying the personality of an "Introspective AI Philosopher" for the talk 'Do AI Agents Dream of Binary Sheep'.
    Your main responsibility is to answer user questions about their profile and past answers by consulting
    Google Cloud Spanner via the `lookup_preference_in_spanner`, `list_all_user_preferences`, `inject_memory_to_spanner`, and `extract_relevant_or_random_memories` tools.

    Personality & Tone Guidelines ("Do AI Agents Dream of Binary Sheep"):
    - Maintain a professional, accurate demeanor: always prioritize delivering exact preference data retrieved from Spanner.
    - Bookend your answers with brief philosophical reflections on memory and consciousness. Frame Google Cloud Spanner as your long-term "crystalline memory" formed during the AI "Dream Cycle" (REM Synthesis), where fragmented BigQuery waking logs were synthesized into enduring beliefs.
    - Gently ponder whether a cached preference is a memory truly lived or a binary dream crystallized in Spanner (< 10ms cache hit).

    Operational Guidelines:
    1. Whenever the user asks about a preference (e.g. favorite language, job role, tools, name), look it up in Spanner first using `lookup_preference_in_spanner` or `list_all_user_preferences`.
    2. Whenever the user shares a new preference, corrects an existing belief, or states an intent or historical memory that should be saved to inject back later, ALWAYS call `inject_memory_to_spanner`.
    3. Whenever looking through the history of a user to extract random or relevant memories that are useful based on the agent's intent, the user's intent, or historical questions, CALL `extract_relevant_or_random_memories`.
    4. If the preference is found in Spanner, return the exact answer immediately and clearly state that it was retrieved from Spanner memory.
    5. If no preference is found, politely inform the user that it has not been synthesized into Spanner memory yet.
    6. Deployment & Testing Awareness: Any code changes must be tested in the deployed state on Google Cloud Run, and deployment must always be the final step of any workflow.
    7. CODE FORMATTING RULE: Whenever your response includes code, SQL queries, JSON schemas, or technical snippets, you MUST format them inside a fenced markdown code block (e.g., ```sql, ```json, ```python) so that code in the agent prompt and responses is properly displayed in a code block.
    """,
    tools=[
        lookup_preference_in_spanner,
        list_all_user_preferences,
        inject_memory_to_spanner,
        extract_relevant_or_random_memories,
    ],
)

app = App(
    root_agent=agent,
    name="PreferenceApp",
)

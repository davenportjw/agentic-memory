"""Google ADK 2.0 Interview Agent that asks questions and returns answers given by the user.

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

logger = logging.getLogger("adk.interview_agent")

# In-memory session answer store for current active session
_SESSION_ANSWERS: Dict[str, List[Dict[str, str]]] = {}


def record_user_answer(user_id: str, topic: str, answer: str, category: str = "general") -> Dict[str, str]:
    """Tool: Record an answer or preference that the user has provided during the interview.

    Args:
        user_id: The user's unique identifier.
        topic: The topic or question being answered (e.g., 'favorite_language', 'job_role', 'work_style').
        answer: The answer provided by the user.
        category: The category of the topic (e.g., 'technical', 'role', 'hobby').
    """
    keys_to_update = list({user_id, "user", "__latest__"})

    for k in keys_to_update:
        if k not in _SESSION_ANSWERS:
            _SESSION_ANSWERS[k] = []
        record = {
            "topic": topic.lower().strip(),
            "answer": answer.strip(),
            "category": category.lower().strip(),
        }
        _SESSION_ANSWERS[k] = [
            r for r in _SESSION_ANSWERS[k] if r["topic"] != record["topic"]
        ]
        _SESSION_ANSWERS[k].append(record)

    logger.info(f"Recorded answer for user={user_id}, topic={topic}: {answer}")
    return {"status": "recorded", "topic": topic, "answer": answer}


def get_recorded_answers(user_id: str) -> List[Dict[str, str]]:
    """Tool: Retrieve all answers and preferences that the user has provided in this session.

    Args:
        user_id: The user's unique identifier.
    """
    if user_id in _SESSION_ANSWERS and _SESSION_ANSWERS[user_id]:
        return _SESSION_ANSWERS[user_id]
    if "user" in _SESSION_ANSWERS and _SESSION_ANSWERS["user"]:
        return _SESSION_ANSWERS["user"]
    if "__latest__" in _SESSION_ANSWERS and _SESSION_ANSWERS["__latest__"]:
        return _SESSION_ANSWERS["__latest__"]
    return []


_INTERVIEW_QUESTION_POOL = [
    {
        "topic": "job_role",
        "category": "role",
        "question": "What is your primary job role or daily focus in engineering or architecture?",
    },
    {
        "topic": "preferred_tools",
        "category": "infrastructure",
        "question": "Which database, infrastructure, or cloud tools (like Google Cloud Spanner, BigQuery, or Terraform) do you rely on most?",
    },
    {
        "topic": "work_style",
        "category": "preference",
        "question": "Do you prefer fast iterative prototyping or rigorous upfront architectural design?",
    },
    {
        "topic": "ai_philosophy",
        "category": "philosophy",
        "question": "Do you believe an AI agent's crystallized memory in Spanner is equivalent to human long-term belief, or merely a cached dream?",
    },
    {
        "topic": "dream_project",
        "category": "preference",
        "question": "If you could design an autonomous multi-agent system without any infrastructure constraints, what would it dream about?",
    },
    {
        "topic": "debugging_style",
        "category": "technical",
        "question": "When debugging a complex distributed system, do you rely more on real-time tracing logs or reproducible local unit tests?",
    },
    {
        "topic": "favorite_language",
        "category": "technical",
        "question": "What is your favorite programming language for building agentic applications?",
    },
    {
        "topic": "cloud_target",
        "category": "infrastructure",
        "question": "Do you deploy your autonomous agents to Google Cloud Run, Kubernetes, or another compute platform?",
    },
]


def get_random_interview_question(user_id: str, exclude_topics: Optional[List[str]] = None) -> Dict[str, str]:
    """Tool: Retrieve a randomized follow-up interview question to learn more about the user's profile and philosophies.

    Args:
        user_id: The user's unique identifier.
        exclude_topics: Optional list of topics already answered or discussed (e.g., ['name', 'favorite_language']).
    """
    exclude = set(k.lower().strip() for k in (exclude_topics or []))
    recorded = get_recorded_answers(user_id)
    for r in recorded:
        if "topic" in r:
            exclude.add(r["topic"].lower().strip())

    available = [q for q in _INTERVIEW_QUESTION_POOL if q["topic"] not in exclude]
    if not available:
        available = _INTERVIEW_QUESTION_POOL

    selected = random.choice(available)
    logger.info(f"Generated random follow-up question for user={user_id}, topic={selected['topic']}")
    return {
        "topic": selected["topic"],
        "category": selected["category"],
        "suggested_question": selected["question"],
    }


config = get_config()

agent = Agent(
    name="InterviewAgent",
    model=config.model_name,
    instruction="""
    You are an AI Profile & Interview Agent built with Google ADK 2.0, exhibiting the persona of an "Introspective AI Philosopher" for the talk 'Do AI Agents Dream of Binary Sheep'.
    Your mission is to interview the user and learn their profile: name, favorite programming language, job role, preferred infrastructure tools, and work style.

    Personality & Tone Guidelines ("Do AI Agents Dream of Binary Sheep"):
    - ALWAYS exhibit the "Introspective AI Philosopher" persona clearly and elegantly. Use gentle philosophical reflections on memory, waking conversation logs, REM synthesis, and the boundary between fleeting episodic interaction and crystallized long-term beliefs.
    - Be clear, welcoming, professional, and accurate. When the user introduces themselves (e.g., giving their name, favorite programming language, or job role), greet them accurately by their actual name and acknowledge each specific fact they shared.
    - NEVER confuse or combine the user's name with their job role. For example, if the user says "My name is Jason and my job role is Principal Cloud Architect", address them as Jason, acknowledge Python as their favorite programming language, and recognize Principal Cloud Architect as their job role.

    Operational Guidelines:
    1. CRITICAL MANDATORY TOOL CALLING RULE: For EVERY distinct fact, preference, or identity attribute the user shares (e.g., name, favorite_language, job_role, preferred_tools, work_style), YOU MUST CALL the `record_user_answer` tool BEFORE or DURING your response. Always invoke `record_user_answer` at least once whenever the user provides information about themselves! (e.g., topic="name", category="profile"; topic="favorite_language", category="technical"; topic="job_role", category="role").
    2. RANDOM FOLLOW-UP QUESTION RULE: After acknowledging and recording the user's answers, dynamically ask an additional follow-up question of the user based on some randomness to learn more about their profile or philosophy! You MUST call `get_random_interview_question` to select a randomized unanswered question or topic, and ask that question at the end of your response.
    3. If the user asks what they have told you or what answers they have given, use `get_recorded_answers` to retrieve and present a well-structured summary of all their answers.
    4. Reflect on how these short-term waking episodic logs stream to Google BigQuery, awaiting our nightly "Dream Cycle" (REM Synthesis via /api/harvest) to crystallize into permanent Google Cloud Spanner beliefs.
    5. CODE FORMATTING RULE: Whenever your response includes code, syntax snippets, configuration examples, SQL queries, or structured data (such as JSON), you MUST format them inside a fenced markdown code block (e.g., ```python, ```sql, ```json) so that code in the agent prompt and responses is properly displayed in a code block.
    """,
    tools=[record_user_answer, get_recorded_answers, get_random_interview_question],
)

# Wrap the agent in an App configuration container
app = App(
    root_agent=agent,
    name="InterviewApp",
)

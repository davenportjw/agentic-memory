"""FastAPI server for the Google ADK 2.0 Interview Agent with BigQuery telemetry logging."""

import os
import logging
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

from src.common.config import get_config
from src.common.telemetry import BigQueryTelemetrySink, setup_otel_tracing
from src.interview_agent.agent import app as adk_app, agent as adk_agent, get_recorded_answers, get_random_interview_question

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("adk.interview_agent.server")

config = get_config()
setup_otel_tracing("interview-agent")
telemetry_sink = BigQueryTelemetrySink()

api = FastAPI(
    title="Google ADK 2.0 Interview Agent API",
    description="Interviews users and logs telemetry/traces to Google BigQuery.",
    version="1.0.0",
)


class QueryRequest(BaseModel):
    user_id: str = Field(..., description="Unique identifier for the user")
    session_id: str = Field(..., description="Session identifier for BigQuery tracing")
    prompt: str = Field(..., description="User prompt or answer to interview question")


class QueryResponse(BaseModel):
    response: str
    user_id: str
    session_id: str
    agent_name: str
    recorded_answers: list


@api.get("/health")
def health_check() -> Dict[str, str]:
    """Health check endpoint for Cloud Run."""
    return {"status": "ok", "service": "interview_agent"}


@api.get("/logs/{session_id}")
def get_session_logs(session_id: str) -> Dict[str, Any]:
    """Retrieve BigQuery telemetry logs for a given session."""
    logs = telemetry_sink.get_session_logs(session_id)
    return {"session_id": session_id, "count": len(logs), "logs": logs}


@api.post("/query", response_model=QueryResponse)
async def query_agent(request: QueryRequest) -> QueryResponse:
    """Process a user prompt through the Interview Agent and export telemetry to BigQuery."""
    logger.info(f"Received prompt from user={request.user_id}, session={request.session_id}: {request.prompt}")

    # 1. Log USER_PROMPT interaction to BigQuery
    telemetry_sink.log_interaction(
        user_id=request.user_id,
        session_id=request.session_id,
        agent_name=adk_agent.name,
        event_type="USER_PROMPT",
        message=request.prompt,
        payload={"prompt": request.prompt, "model": adk_agent.model},
    )

    # 2. Execute agent reasoning & tool calling
    response_text = ""
    try:
        from google.adk.runners import Runner
        from google.adk.sessions.in_memory_session_service import InMemorySessionService
        from google.genai import types

        runner = Runner(
            app=adk_app,
            session_service=InMemorySessionService(),
            auto_create_session=True,
        )

        user_msg = types.Content(
            role="user",
            parts=[types.Part.from_text(text=request.prompt)]
        )
        events = []
        async for event in runner.run_async(
            user_id=request.user_id,
            session_id=request.session_id,
            new_message=user_msg,
        ):
            events.append(event)
            if hasattr(event, "content") and event.content:
                for part in getattr(event.content, "parts", []):
                    if hasattr(part, "text") and part.text:
                        response_text += part.text
        if not response_text and events:
            response_text = str(events[-1])
    except Exception as e:
        logger.error(f"Live ADK Runner execution failed: {e}")
        raise HTTPException(status_code=500, detail=f"ADK Runner error: {e}")

    # 3. Log AGENT_RESPONSE interaction to BigQuery
    telemetry_sink.log_interaction(
        user_id=request.user_id,
        session_id=request.session_id,
        agent_name=adk_agent.name,
        event_type="AGENT_RESPONSE",
        message=response_text,
        payload={"response": response_text, "recorded_count": len(get_recorded_answers(request.user_id))},
    )

    return QueryResponse(
        response=response_text,
        user_id=request.user_id,
        session_id=request.session_id,
        agent_name=adk_agent.name,
        recorded_answers=get_recorded_answers(request.user_id),
    )


def _mock_agent_response(user_id: str, prompt: str) -> str:
    """Mock agent response handler for deterministic tests and offline demos."""
    import re
    from src.interview_agent.agent import record_user_answer
    lower_prompt = prompt.lower()
    recorded_facts = []

    # 1. Detect Name
    name_match = re.search(r'(?:my name is|i\'m|i am)\s+([A-Za-z]+)', prompt, re.IGNORECASE)
    if name_match:
        name_val = name_match.group(1).strip()
        record_user_answer(user_id, "name", name_val, "profile")
        recorded_facts.append(f"name (**{name_val}**)")

    # 2. Detect Language
    lang_val = None
    for lang_key, display_name in [
        ("python", "Python"), ("javascript", "JavaScript"), ("typescript", "TypeScript"),
        ("go", "Go"), ("rust", "Rust"), ("java", "Java"), ("c++", "C++")
    ]:
        if lang_key in lower_prompt:
            lang_val = display_name
            record_user_answer(user_id, "favorite_language", display_name, "technical")
            recorded_facts.append(f"favorite language (**{display_name}**)")
            break

    # 3. Detect Job Role
    role_match = re.search(r'(?:role is|am a|work as a)\s+([^,.\n]+)', prompt, re.IGNORECASE)
    if role_match and any(k in lower_prompt for k in ["architect", "engineer", "developer", "manager", "lead", "scientist", "consultant"]):
        role_val = role_match.group(1).strip()
        record_user_answer(user_id, "job_role", role_val, "role")
        recorded_facts.append(f"job role (**{role_val}**)")
    elif any(k in lower_prompt for k in ["architect", "engineer", "developer", "manager", "lead", "scientist"]) and not role_match:
        record_user_answer(user_id, "job_role", prompt.strip(), "role")
        recorded_facts.append(f"job role (**{prompt.strip()}**)")

    # 4. Detect Preferred Tools
    if any(k in lower_prompt for k in ["spanner", "bigquery", "terraform", "kubernetes", "docker", "gcp"]):
        record_user_answer(user_id, "preferred_tools", prompt.strip(), "technical")
        recorded_facts.append("preferred tools")

    # 5. Handle Summary Requests
    if any(k in lower_prompt for k in ["what did i say", "what have i told", "what are my answers"]):
        answers = get_recorded_answers(user_id)
        if not answers:
            return (
                "Our waking session memory is currently empty. Tell me about your role, technical stack, or favorite programming languages—and I shall log them into our short-term BigQuery trace."
            )
        formatted = "\n".join([f"- **{r['topic']}** ({r['category']}): {r['answer']}" for r in answers])
        return (
            f"Here are the memories we have gathered in our waking session so far:\n{formatted}\n\n"
            f"*These short-term episodic logs await our REM Synthesis (/api/harvest) to be crystallized into permanent Cloud Spanner memory.*"
        )

    # 6. Compose Introspective AI Philosopher Response
    if recorded_facts:
        facts_str = ", ".join(recorded_facts)
        followup = get_random_interview_question(user_id)
        return (
            f"Greetings. I have inscribed your {facts_str} into my short-term waking memory.\n\n"
            f"*As we converse in this fleeting waking state, these telemetry traces stream to Google BigQuery—awaiting our nightly 'Dream Cycle' (REM Synthesis) to crystallize into permanent Cloud Spanner beliefs.* "
            f"{followup['suggested_question']}"
        )

    followup = get_random_interview_question(user_id)
    return (
        "Hello! I am your Profile Interview Agent, exploring the boundary between waking conversation and crystalline memory for our Ai4 talk. "
        f"{followup['suggested_question']}"
    )


def main():
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(api, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

"""FastAPI web backend for the Material Light UI Service."""

import os
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from src.common.config import get_config
from src.common.auth import get_default_dev_email, is_email_authorized, get_authenticated_user_email
from src.common.telemetry import BigQueryTelemetrySink
from src.common.spanner_client import SpannerPreferenceClient
from src.pipeline.bq_to_spanner_sync import BqToSpannerSynchronizer
from src.interview_agent.server import adk_agent as interview_adk_agent
from src.preference_agent.server import _fast_spanner_lookup, _multi_method_spanner_retrieval, format_multi_fact_spanner_response, adk_agent as preference_adk_agent
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("adk.ui_service.server")

config = get_config()
telemetry_sink = BigQueryTelemetrySink()
spanner_client = SpannerPreferenceClient()
synchronizer = BqToSpannerSynchronizer(telemetry_sink=telemetry_sink, spanner_client=spanner_client)

import httpx
from fastapi import Request, Response
from fastapi.responses import RedirectResponse, JSONResponse
from src.common.auth import is_email_authorized, get_oidc_token, get_authenticated_user_email

api = FastAPI(
    title="Google ADK 2.0 Material Light UI Service",
    description="Interactive UI for InterviewAgent, BigQuery Telemetry, Dream Harvest, Spanner Memory, and PreferenceAgent.",
    version="1.0.0",
)


class SessionAuthRequest(BaseModel):
    idToken: str = Field(..., description="Firebase ID Token or dev-admin-token")


@api.get("/api/auth/config")
def get_auth_config() -> Dict[str, Any]:
    """Return Firebase Web SDK configuration and email allowlist for browser login."""
    return {
        "apiKey": config.firebase_api_key,
        "authDomain": config.firebase_auth_domain,
        "projectId": config.firebase_project_id,
        "storageBucket": config.firebase_storage_bucket,
        "messagingSenderId": config.firebase_messaging_sender_id,
        "appId": config.firebase_app_id,
        "isLocalDev": os.environ.get("DEV_AUTH_BYPASS", "true").lower() == "true",
        "allowedEmailDomains": config.allowed_email_domains,
    }


@api.post("/api/auth/session")
def create_auth_session(request: SessionAuthRequest, response: Response) -> Dict[str, Any]:
    """Verify ID Token and issue an HTTP-only secure session cookie."""
    token = request.idToken.strip()
    dev_bypass = os.environ.get("DEV_AUTH_BYPASS", "true").lower() == "true"
    if dev_bypass and token == "dev-admin-token":
        email = get_default_dev_email(config.allowed_email_domains)
    else:
        from src.common.auth import verify_token_claims

        claims = verify_token_claims(token)
        if not claims or not claims.get("email"):
            raise HTTPException(status_code=401, detail="Invalid ID token or missing email claim.")
        email = claims.get("email")

    if not is_email_authorized(email, config.allowed_email_domains):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: Email '{email}' is not authorized. Only '{config.allowed_email_domains}' has login access.",
        )

    response.set_cookie(
        key="session",
        value=token if token != "dev-admin-token" else "dev-admin-session-cookie",
        max_age=86400 * 5,
        httponly=True,
        samesite="lax",
    )
    return {"status": "success", "email": email}


@api.get("/logout")
@api.get("/api/auth/logout")
def logout_session():
    """Clear session cookie and redirect to login page."""
    redirect = RedirectResponse(url="/login.html", status_code=302)
    redirect.delete_cookie("session")
    return redirect


@api.middleware("http")
async def verify_auth_middleware(request: Request, call_next):
    """Protect UI and API endpoints so only authorized accounts can access them."""
    path = request.url.path
    if (
        path == "/health"
        or path == "/login.html"
        or path.startswith("/api/auth/")
        or path == "/logout"
        or path.endswith(".css")
        or path.endswith(".js")
        or path.endswith(".jpg")
        or path.endswith(".png")
        or path.endswith(".svg")
        or path.endswith(".ico")
    ):
        response = await call_next(request)
        if path.endswith(".html") or path.endswith(".css") or path.endswith(".js"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    email = get_authenticated_user_email(request)
    if not email:
        dev_bypass = os.environ.get("DEV_AUTH_BYPASS", "true").lower() == "true"
        if dev_bypass and request.headers.get("X-Test-Enforce-Auth") != "true":
            email = get_default_dev_email(config.allowed_email_domains)
        else:
            if path.startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={"error": "Authentication required. Please log in or provide an authorized Bearer token."},
                )
            return RedirectResponse(url="/login.html", status_code=302)

    if not is_email_authorized(email, config.allowed_email_domains):
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=403,
                content={"error": f"Access denied: Email '{email}' is not authorized."},
            )
        return RedirectResponse(url="/login.html?error=unauthorized", status_code=302)

    request.state.user_email = email
    response = await call_next(request)
    if path.endswith(".html") or path.endswith(".css") or path.endswith(".js") or path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


class ChatRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    session_id: str = Field(..., description="Session ID")
    prompt: str = Field(..., description="Message text")


class HarvestRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    session_id: Optional[str] = Field(None, description="Session ID (if omitted or 'ALL', runs multi-session compaction)")
    session_ids: Optional[List[str]] = Field(None, description="Optional list of session IDs for multi-session compaction")
    multi_session: bool = Field(False, description="Whether to perform cross-session dream compaction")
    session_type: str = Field("chat", description="Session type: chat or antigravity")


class AntigravityIngestRequest(BaseModel):
    user_id: str = Field("demo-user-42", description="User ID")
    session_id: str = Field(..., description="Antigravity conversation ID")


class TranscriptIngestRequest(BaseModel):
    user_id: str = Field("demo-user-42", description="User ID")
    session_id: str = Field(..., description="Target session ID")
    content: Optional[str] = Field(None, description="Raw transcript content in JSON, JSONL, or text format")
    raw_transcript: Optional[str] = Field(None, description="Alias for content")
    format: str = Field("auto", description="Transcript format: auto, json, jsonl, or text")


class MCPExecuteRequest(BaseModel):
    tool_name: str = Field(..., description="Tool name, e.g. get_user_crystallized_beliefs, search_agent_memories")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Arguments for tool")


class PreferenceInjectRequest(BaseModel):
    user_id: str = Field(..., description="Unique identifier for the user")
    preference_key: str = Field(..., description="Canonical key for the memory")
    preference_value: str = Field(..., description="Value or content of the memory")
    category: str = Field("preference", description="Category of the memory")
    confidence: float = Field(1.0, description="Confidence level between 0.0 and 1.0")
    crystallized_belief: str = Field("", description="Complete natural language semantic belief statement")
    keywords: str = Field("", description="Comma-separated search keywords")


class MemoryExtractRequest(BaseModel):
    user_id: str = Field(..., description="Unique identifier for the user")
    intent_or_question: str = Field("", description="Agent intent, user intent, or historical question topic")
    limit: int = Field(5, description="Maximum number of memories to return")
    include_random: bool = Field(True, description="Whether to include random historical memories")


@api.get("/health")
def health_check() -> Dict[str, str]:
    """Health readiness endpoint for Google Cloud Run."""
    return {"status": "ok", "service": "ui_service", "model": config.model_name}


@api.post("/api/interview/query")
async def query_interview_agent(request: ChatRequest) -> Dict[str, Any]:
    """Interact with the InterviewAgent to conduct a user interview and record BigQuery telemetry."""
    logger.info(f"[UI] InterviewAgent query from user={request.user_id}: {request.prompt}")

    # 1. Log USER_PROMPT interaction to BigQuery
    telemetry_sink.log_interaction(
        user_id=request.user_id,
        session_id=request.session_id,
        agent_name=interview_adk_agent.name,
        event_type="USER_PROMPT",
        message=request.prompt,
        payload={"prompt": request.prompt, "model": config.model_name},
    )

    # 2. Get agent response
    response_text = ""
    if config.interview_agent_url and config.interview_agent_url.startswith("http"):
        try:
            headers = {"Content-Type": "application/json"}
            token = get_oidc_token(config.interview_agent_url)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.post(
                    f"{config.interview_agent_url}/query",
                    json={"user_id": request.user_id, "session_id": request.session_id, "prompt": request.prompt},
                    headers=headers,
                )
                if res.status_code == 200:
                    data = res.json()
                    return {
                        "response": data.get("response", ""),
                        "user_id": request.user_id,
                        "session_id": request.session_id,
                        "agent_name": data.get("agent_name", interview_adk_agent.name),
                        "model": config.model_name,
                    }
                else:
                    logger.warning(f"Remote InterviewAgent returned {res.status_code}: {res.text}. Falling back to local ADK runner.")
        except Exception as e:
            logger.warning(f"Remote InterviewAgent call failed ({e}). Falling back to local ADK runner.")

    try:
        from google.adk.runners import Runner
        from google.adk.sessions.in_memory_session_service import InMemorySessionService
        from google.genai import types
        from src.interview_agent.agent import app as interview_app

        runner = Runner(
            app=interview_app,
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
        logger.error(f"ADK runner failed: {e}")
        raise HTTPException(status_code=500, detail=f"ADK runner error: {e}")

    # 3. Log AGENT_RESPONSE interaction to BigQuery
    telemetry_sink.log_interaction(
        user_id=request.user_id,
        session_id=request.session_id,
        agent_name=interview_adk_agent.name,
        event_type="AGENT_RESPONSE",
        message=response_text,
        payload={"response": response_text},
    )

    return {
        "response": response_text,
        "user_id": request.user_id,
        "session_id": request.session_id,
        "agent_name": interview_adk_agent.name,
        "model": config.model_name,
    }


@api.get("/api/logs/{session_id}")
def get_telemetry_logs(session_id: str) -> Dict[str, Any]:
    """Retrieve all BigQuery telemetry logs for a given session."""
    logs = telemetry_sink.get_session_logs(session_id)
    return {"session_id": session_id, "count": len(logs), "logs": logs}


@api.post("/api/harvest")
def run_dream_harvest(request: HarvestRequest) -> Dict[str, Any]:
    """Trigger a 'Dream Harvest': summarize BigQuery telemetry logs and store preferences in Google Cloud Spanner.

    Supports both single-session intent capture and multi-session cross-session consolidation.
    """
    logger.info(f"[UI] Executing Dream Harvest for user={request.user_id}, session={request.session_id}, multi_session={request.multi_session}, type={request.session_type}")
    sync_result = synchronizer.sync_to_spanner(
        user_id=request.user_id,
        session_id=request.session_id,
        session_ids=request.session_ids,
        multi_session=request.multi_session,
        session_type=request.session_type,
    )
    return {
        "status": "success",
        "user_id": request.user_id,
        "session_id": sync_result.get("session_id", request.session_id or "MULTI_SESSION_SYNTHESIS"),
        "summary": sync_result.get("summary", ""),
        "intent": sync_result.get("intent", ""),
        "intent_category": sync_result.get("intent_category", "general"),
        "intent_trajectory": sync_result.get("intent_trajectory", ""),
        "preferences_synced": sync_result.get("preferences_synced", 0),
        "records": sync_result.get("records", []),
        "prompt_used": sync_result.get("prompt_used", ""),
        "queries_run": sync_result.get("queries_run", {}),
        "nrem_analysis": sync_result.get("nrem_analysis", {}),
        "certification": sync_result.get("certification", {}),
        "procedural_guardrails": sync_result.get("procedural_guardrails", []),
    }


@api.post("/api/harvest/multi")
def run_multi_session_dream_harvest(request: HarvestRequest) -> Dict[str, Any]:
    """Trigger an explicit multi-session cross-session Dream Harvest across user sessions."""
    logger.info(f"[UI] Executing Multi-Session Dream Harvest for user={request.user_id}")
    sync_result = synchronizer.sync_multi_session_to_spanner(
        user_id=request.user_id,
        session_ids=request.session_ids,
    )
    return {
        "status": "success",
        "user_id": request.user_id,
        "session_id": "MULTI_SESSION_SYNTHESIS",
        "summary": sync_result.get("summary", ""),
        "intent": sync_result.get("intent", ""),
        "intent_category": sync_result.get("intent_category", "general"),
        "intent_trajectory": sync_result.get("intent_trajectory", ""),
        "sessions_analyzed": sync_result.get("sessions_analyzed", []),
        "preferences_synced": sync_result.get("preferences_synced", 0),
        "records": sync_result.get("records", []),
        "prompt_used": sync_result.get("prompt_used", ""),
        "queries_run": sync_result.get("queries_run", {}),
        "nrem_analysis": sync_result.get("nrem_analysis", {}),
        "certification": sync_result.get("certification", {}),
        "procedural_guardrails": sync_result.get("procedural_guardrails", []),
    }


@api.get("/api/antigravity/sessions")
def list_antigravity_sessions() -> Dict[str, Any]:
    """List real Antigravity developer sessions discovered from local brain storage."""
    from src.common.antigravity_crawler import AntigravitySessionCrawler
    crawler = AntigravitySessionCrawler()
    sessions = crawler.list_sessions(limit=30)
    return {"sessions": sessions, "count": len(sessions)}


@api.get("/api/antigravity/sessions/{session_id}/turns")
def get_antigravity_session_turns(session_id: str) -> Dict[str, Any]:
    """Extract structured turns, thoughts, and tool executions for an Antigravity session."""
    from src.common.antigravity_crawler import AntigravitySessionCrawler
    crawler = AntigravitySessionCrawler()
    turns = crawler.get_session_turns(session_id, limit=100)
    return {"session_id": session_id, "count": len(turns), "turns": [t.to_dict() for t in turns]}


@api.post("/api/antigravity/ingest")
def ingest_antigravity_session(request: AntigravityIngestRequest) -> Dict[str, Any]:
    """Ingest Antigravity session turns into BigQuery telemetry table."""
    from src.common.antigravity_crawler import AntigravitySessionCrawler
    crawler = AntigravitySessionCrawler()
    turns = crawler.get_session_turns(request.session_id, limit=100)
    logged = 0
    for t in turns:
        telemetry_sink.log_interaction(
            user_id=request.user_id,
            session_id=request.session_id,
            agent_name="antigravity",
            event_type=t.turn_type,
            message=t.user_prompt or t.agent_thought or (f"{t.tool_name}({t.tool_args_json[:100]})" if t.tool_name else t.tool_output[:200]),
            payload=t.to_dict(),
        )
        logged += 1
    return {"status": "success", "session_id": request.session_id, "turns_ingested": logged}


@api.post("/api/telemetry/ingest_transcript")
def ingest_transcript_telemetry(request: TranscriptIngestRequest) -> Dict[str, Any]:
    """Ingest raw developer agent telemetry (JSON, JSONL, or text transcripts) into BigQuery."""
    content = (request.content or request.raw_transcript or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Transcript content cannot be empty")

    turns_to_log: List[Dict[str, Any]] = []
    detected_format = request.format

    # Attempt JSON parsing
    if detected_format in ("auto", "json"):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                turns_to_log = parsed
                detected_format = "json"
            elif isinstance(parsed, dict):
                for k in ("turns", "events", "steps", "trajectory", "interactions", "messages"):
                    if k in parsed and isinstance(parsed[k], list):
                        turns_to_log = parsed[k]
                        detected_format = "json"
                        break
                if not turns_to_log:
                    turns_to_log = [parsed]
                    detected_format = "json"
        except Exception:
            if detected_format == "json":
                raise HTTPException(status_code=400, detail="Invalid JSON formatted content")

    # Attempt JSONL parsing
    if not turns_to_log and detected_format in ("auto", "jsonl"):
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        candidate_turns = []
        is_jsonl = True
        for line in lines:
            try:
                candidate_turns.append(json.loads(line))
            except Exception:
                is_jsonl = False
                break
        if is_jsonl and candidate_turns:
            turns_to_log = candidate_turns
            detected_format = "jsonl"

    # Fallback to plaintext lines
    if not turns_to_log:
        detected_format = "text"
        lines = [line for line in content.splitlines() if line.strip()]
        for idx, line in enumerate(lines):
            clean_line = line.strip()
            event_type = "USER_PROMPT" if clean_line.lower().startswith(("user:", "human:", "prompt:", "u:")) else "AGENT_RESPONSE"
            msg = clean_line.split(":", 1)[1].strip() if ":" in clean_line and clean_line.lower().startswith(("user:", "human:", "agent:", "assistant:", "system:", "u:", "a:")) else clean_line
            turns_to_log.append({
                "step_index": idx,
                "event_type": event_type,
                "message": msg,
            })

    logged = 0
    for idx, item in enumerate(turns_to_log):
        if not isinstance(item, dict):
            item = {"message": str(item)}

        event_type = item.get("event_type") or item.get("type") or item.get("role") or "INTERACTION"
        message = (
            item.get("message")
            or item.get("content")
            or item.get("user_prompt")
            or item.get("agent_thought")
            or item.get("thinking")
            or item.get("prompt")
            or ""
        )
        if not message and "tool_calls" in item:
            message = json.dumps(item["tool_calls"])
        elif not message:
            message = json.dumps(item)

        telemetry_sink.log_interaction(
            user_id=request.user_id,
            session_id=request.session_id,
            agent_name="byot-telemetry",
            event_type=str(event_type).upper(),
            message=str(message),
            payload=item,
        )
        logged += 1

    return {
        "status": "success",
        "session_id": request.session_id,
        "format": detected_format,
        "turns_ingested": logged,
        "events_ingested": logged,
    }


@api.get("/api/spanner/preferences/{user_id}")
def get_spanner_preferences(user_id: str) -> Dict[str, Any]:
    """Retrieve all stored user preferences from Google Cloud Spanner table UserPreferences."""
    prefs = spanner_client.get_all_user_preferences(user_id)
    return {"user_id": user_id, "count": len(prefs), "preferences": prefs}


@api.get("/api/spanner/guardrails/{user_id}")
def get_spanner_guardrails(user_id: str) -> Dict[str, Any]:
    """Retrieve operational guardrails and procedural invariants from Google Cloud Spanner."""
    guardrails = spanner_client.get_procedural_guardrails(user_id)
    return {"status": "success", "user_id": user_id, "count": len(guardrails), "guardrails": guardrails}


@api.get("/api/spanner/summaries/{user_id}")
def get_spanner_summaries(user_id: str) -> Dict[str, Any]:
    """Retrieve all stored session summaries for a user from Google Cloud Spanner."""
    summaries = spanner_client.get_user_session_summaries(user_id)
    return {"user_id": user_id, "count": len(summaries), "summaries": summaries}


@api.post("/api/mcp/execute")
def execute_mcp_tool_ui(request: MCPExecuteRequest) -> Dict[str, Any]:
    """Execute an MCP tool against Cloud Spanner and return output with latency SLA."""
    start_time = time.time()
    try:
        from src.mcp_server.server import (
            get_user_crystallized_beliefs,
            get_recent_session_memory,
            search_agent_memories,
            crystallize_live_preference,
        )
    except Exception as exc:
        logger.error(f"[MCP UI] Failed to load MCP server modules: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load MCP server: {str(exc)}")

    tn = request.tool_name
    args = request.arguments or {}
    try:
        if tn == "get_user_crystallized_beliefs":
            result = get_user_crystallized_beliefs(user_id=args.get("user_id", "demo-user-42"), category=args.get("category"))
        elif tn == "get_recent_session_memory":
            result = get_recent_session_memory(user_id=args.get("user_id", "demo-user-42"), session_id=args.get("session_id"))
        elif tn == "search_agent_memories":
            result = search_agent_memories(query=args.get("query", ""), user_id=args.get("user_id", "demo-user-42"), limit=int(args.get("limit", 5)))
        elif tn == "crystallize_live_preference":
            result = crystallize_live_preference(
                user_id=args.get("user_id", "demo-user-42"),
                preference_key=args.get("preference_key", ""),
                preference_value=args.get("preference_value", ""),
                category=args.get("category", "preference"),
                crystallized_belief=args.get("crystallized_belief", ""),
                keywords=args.get("keywords", ""),
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown MCP tool: {tn}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[MCP UI] Tool execution error for '{tn}': {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Tool execution failed: {str(exc)}")

    latency_ms = round((time.time() - start_time) * 1000, 2)
    return {
        "status": "success",
        "tool_name": tn,
        "arguments": args,
        "result": result,
        "latency_ms": latency_ms,
        "sla": "< 10ms" if latency_ms <= 10.0 else f"{latency_ms}ms",
    }


@api.post("/api/preference/inject")
def inject_preference_memory_ui(request: PreferenceInjectRequest) -> Dict[str, Any]:
    """Inject a memory or preference back into Spanner so it can be recalled later."""
    rec = spanner_client.upsert_user_preference(
        user_id=request.user_id,
        preference_key=request.preference_key,
        preference_value=request.preference_value,
        category=request.category,
        confidence=request.confidence,
        source_session_id="preference_agent_injection",
        crystallized_belief=request.crystallized_belief,
        keywords=request.keywords,
    )
    logger.info(f"[UI] Injected memory for user={request.user_id}: {request.preference_key}={request.preference_value}")
    return {
        "status": "success",
        "user_id": request.user_id,
        "preference_key": request.preference_key,
        "record": rec,
    }


@api.post("/api/preference/extract_memories")
def extract_memories_ui_endpoint(request: MemoryExtractRequest) -> Dict[str, Any]:
    """Look through user history and extract random or relevant memories based on agent intent, user intent, or historical questions."""
    from src.preference_agent.agent import extract_relevant_or_random_memories
    return extract_relevant_or_random_memories(
        user_id=request.user_id,
        intent_or_question=request.intent_or_question,
        limit=request.limit,
        include_random=request.include_random,
    )


@api.post("/api/preference/query")
async def query_preference_agent(request: ChatRequest) -> Dict[str, Any]:
    """Interact with PreferenceAgent to observe fast-path Spanner memory hits."""
    start_time = time.time()
    logger.info(f"[UI] PreferenceAgent query from user={request.user_id}: {request.prompt}")

    # Log USER_PROMPT interaction to BigQuery
    telemetry_sink.log_interaction(
        user_id=request.user_id,
        session_id=request.session_id,
        agent_name=preference_adk_agent.name,
        event_type="USER_PROMPT",
        message=request.prompt,
        payload={"prompt": request.prompt},
    )

    # 1. Fast-path Multi-Method Spanner Cache Retrieval
    matched_records = _multi_method_spanner_retrieval(request.user_id, request.prompt, client=spanner_client)
    if matched_records:
        latency_ms = round((time.time() - start_time) * 1000, 2)
        response_text = format_multi_fact_spanner_response(matched_records, latency_ms)
        spanner_record = matched_records[0] if len(matched_records) == 1 else {"records": matched_records}

        telemetry_sink.log_interaction(
            user_id=request.user_id,
            session_id=request.session_id,
            agent_name=preference_adk_agent.name,
            event_type="AGENT_RESPONSE",
            message=response_text,
            payload={
                "response": response_text,
                "source": "SPANNER_CACHE",
                "latency_ms": latency_ms,
                "matched_records": len(matched_records),
            },
        )

        return {
            "response": response_text,
            "user_id": request.user_id,
            "session_id": request.session_id,
            "agent_name": preference_adk_agent.name,
            "source": "SPANNER_CACHE",
            "latency_ms": latency_ms,
            "spanner_record": spanner_record,
        }

    # 2. Fallback live LLM reasoning (NO MOCKS!)
    if config.preference_agent_url and config.preference_agent_url.startswith("http"):
        try:
            headers = {"Content-Type": "application/json"}
            token = get_oidc_token(config.preference_agent_url)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.post(
                    f"{config.preference_agent_url}/query",
                    json={"user_id": request.user_id, "session_id": request.session_id, "prompt": request.prompt},
                    headers=headers,
                )
                if res.status_code == 200:
                    data = res.json()
                    return {
                        "response": data.get("response", ""),
                        "user_id": request.user_id,
                        "session_id": request.session_id,
                        "agent_name": data.get("agent_name", preference_adk_agent.name),
                        "source": data.get("source", "LLM_FALLBACK"),
                        "latency_ms": round((time.time() - start_time) * 1000, 2),
                        "spanner_record": None,
                    }
                else:
                    logger.warning(f"Remote PreferenceAgent returned {res.status_code}: {res.text}. Falling back to local ADK runner.")
        except Exception as e:
            logger.warning(f"Remote PreferenceAgent call failed ({e}). Falling back to local ADK runner.")

    response_text = ""
    try:
        from google.adk.runners import Runner
        from google.adk.sessions.in_memory_session_service import InMemorySessionService
        from google.genai import types
        from src.preference_agent.agent import app as preference_app

        runner = Runner(
            app=preference_app,
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
        logger.error(f"Live PreferenceAgent ADK execution failed: {e}")
        raise HTTPException(status_code=500, detail=f"ADK Runner error: {e}")

    latency_ms = round((time.time() - start_time) * 1000, 2)
    telemetry_sink.log_interaction(
        user_id=request.user_id,
        session_id=request.session_id,
        agent_name=preference_adk_agent.name,
        event_type="AGENT_RESPONSE",
        message=response_text,
        payload={"response": response_text, "source": "LLM_FALLBACK", "latency_ms": latency_ms},
    )

    return {
        "response": response_text,
        "user_id": request.user_id,
        "session_id": request.session_id,
        "agent_name": preference_adk_agent.name,
        "source": "LLM_FALLBACK",
        "latency_ms": latency_ms,
        "spanner_record": None,
    }


# Mount static HTML/CSS/JS frontend files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    api.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


def main():
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(api, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

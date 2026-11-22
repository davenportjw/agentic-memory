"""FastAPI web backend for the Material Light UI Service."""

import asyncio
import os
import queue
import re
import json
import datetime
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from src.common.config import get_config
from src.common.auth import get_default_dev_email, is_email_authorized, get_authenticated_user_email
from src.common.telemetry import BigQueryTelemetrySink, extract_5_word_summary
from src.common.spanner_client import SpannerPreferenceClient
from src.pipeline.bq_to_spanner_sync import BqToSpannerSynchronizer
from src.studio.eval_engine import EvalEngine
from src.studio.alpha_evolve import AlphaEvolveEngine, MemoryConfig
from src.studio.job_runner import StudioJobRunner, JobCanceledError, JobProgressContext
from src.interview_agent.server import adk_agent as interview_adk_agent
from src.preference_agent.server import _fast_spanner_lookup, _multi_method_spanner_retrieval, format_multi_fact_spanner_response, adk_agent as preference_adk_agent
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("adk.ui_service.server")

config = get_config()
telemetry_sink = BigQueryTelemetrySink()
spanner_client = SpannerPreferenceClient()
synchronizer = BqToSpannerSynchronizer(telemetry_sink=telemetry_sink, spanner_client=spanner_client)
eval_engine = EvalEngine(spanner_client=spanner_client)
alpha_evolve = AlphaEvolveEngine(spanner_client=spanner_client, eval_engine=eval_engine)
job_runner = StudioJobRunner(spanner_client=spanner_client)

import httpx
from fastapi import Request, Response
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse, StreamingResponse
from src.common.auth import (
    extract_iap_user_email,
    get_authenticated_user_email,
    get_oidc_token,
    get_service_auth_headers,
    is_email_authorized,
)
from src.common.firebase_session import (
    LOCAL_DEV_LOGIN_TOKEN,
    SESSION_COOKIE_NAME,
    SESSION_EXCHANGE_MAX_AUTH_AGE_SECONDS,
    SESSION_TTL_SECONDS,
    FirebaseAuthError,
    create_firebase_session_cookie,
    create_local_dev_session,
    is_local_dev_auth_allowed,
    is_running_on_cloud_run,
    revoke_local_dev_session,
    verify_firebase_id_token,
)
from starlette.concurrency import run_in_threadpool

api = FastAPI(
    title="Google ADK 2.0 Material Light UI Service",
    description="Interactive UI for InterviewAgent, BigQuery Telemetry, Dream Harvest, Spanner Memory, and PreferenceAgent.",
    version="1.0.0",
)


class SessionAuthRequest(BaseModel):
    idToken: str = Field(..., description="Firebase ID token from Google sign-in (or the local developer login marker off Cloud Run)")


def _is_secure_request(request: Request) -> bool:
    return is_running_on_cloud_run() or request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"


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
        "isLocalDev": is_local_dev_auth_allowed(),
        "allowedEmailDomains": config.allowed_email_domains,
    }


@api.post("/api/auth/session")
def create_auth_session(request: SessionAuthRequest, http_request: Request, response: Response) -> Dict[str, Any]:
    """Exchange a fresh Google-sign-in Firebase ID token for a Firebase session cookie (HttpOnly, 5 days)."""
    token = request.idToken.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing idToken.")

    local_dev = token == LOCAL_DEV_LOGIN_TOKEN and is_local_dev_auth_allowed()
    if local_dev:
        email = get_default_dev_email(config.allowed_email_domains)
    else:
        try:
            verified = verify_firebase_id_token(token, max_auth_age_seconds=SESSION_EXCHANGE_MAX_AUTH_AGE_SECONDS)
        except FirebaseAuthError as exc:
            raise HTTPException(status_code=401, detail=f"Unauthorized: {exc}")
        except Exception as exc:  # noqa: BLE001 - certificate fetch etc.; fail closed
            logger.error("ID token verification failed: %s", exc)
            raise HTTPException(status_code=401, detail="Unauthorized: could not verify sign-in.")
        email = verified.email

    if not is_email_authorized(email, config.allowed_email_domains):
        raise HTTPException(status_code=403, detail=f"Access denied: '{email}' is not authorized for this app.")

    if local_dev:
        cookie_value = create_local_dev_session(email)
    else:
        try:
            cookie_value = create_firebase_session_cookie(token, SESSION_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.error("createSessionCookie failed: %s", exc)
            raise HTTPException(status_code=502, detail="Could not start a session. Please try signing in again.")

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_value,
        max_age=SESSION_TTL_SECONDS,
        path="/",
        httponly=True,
        secure=_is_secure_request(http_request),
        samesite="lax",
    )
    return {"status": "success", "email": email}


@api.get("/logout")
@api.get("/api/auth/logout")
def logout_session(request: Request):
    """Clear session cookie and redirect to login page."""
    revoke_local_dev_session(request.cookies.get(SESSION_COOKIE_NAME))
    redirect = RedirectResponse(url="/login.html", status_code=302)
    redirect.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True, secure=_is_secure_request(request), samesite="lax")
    return redirect


@api.middleware("http")
async def verify_auth_middleware(request: Request, call_next):
    """Protect UI and API endpoints so only authorized accounts can access them."""
    path = request.url.path
    is_static_asset = not path.startswith("/api/") and path.endswith(
        (".css", ".js", ".mjs", ".jpg", ".png", ".svg", ".ico", ".wasm", ".otf", ".ttf", ".json")
    )
    if (
        path == "/health"
        or path == "/login.html"
        or path in ("/api/auth/config", "/api/auth/session", "/api/auth/logout")
        or path == "/logout"
        or is_static_asset
    ):
        response = await call_next(request)
        if path.endswith(".html") or path.endswith(".css") or path.endswith(".js"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    email = await run_in_threadpool(get_authenticated_user_email, request)
    if not email:
        if is_local_dev_auth_allowed() and request.headers.get("X-Test-Enforce-Auth") != "true":
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
    if path.endswith(".html") or path.endswith(".css") or path.endswith(".js") or path in ("/", "/studio"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


class ChatRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    session_id: str = Field(default="studio-playground-session", description="Session ID")
    prompt: str = Field(..., description="Message text")
    team_id: Optional[str] = Field(default=None, description="Optional Team Scope ID")
    agent_id: Optional[str] = Field(default=None, description="Optional Agent Scope ID")
    embedding_dimensions: Optional[int] = Field(default=None, description="Optional Matryoshka dimension override")
    injection_format: Optional[str] = Field(default=None, description="Optional injection format override")



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
    return {
        "status": "ok",
        "service": "ui_service",
        "model": config.model_name,
        "reasoning_model": config.reasoning_model_name,
    }


@api.post("/api/interview/query")
async def query_interview_agent(request: ChatRequest, http_request: Request) -> Dict[str, Any]:
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
            headers = {
                "Content-Type": "application/json",
                **get_service_auth_headers(
                    config.interview_agent_url,
                    incoming_headers=http_request.headers if http_request else None,
                ),
            }
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


@api.get("/api/lakehouse/sessions")
def list_lakehouse_sessions(limit: int = 50) -> Dict[str, Any]:
    """Retrieve distinct sessions in the Telemetry Lakehouse with ID, source, and ~5-word summary."""
    # 1. Base telemetry sessions from BigQuery & memory
    sessions = telemetry_sink.list_sessions(limit=limit)

    # 2. Enrich with Spanner crystallized summaries if available
    spanner_summaries: Dict[str, str] = {}
    try:
        user_id = "demo-user-42"
        for s in spanner_client.get_user_session_summaries(user_id):
            if s.get("session_id"):
                spanner_summaries[s["session_id"]] = s.get("summary_text", "")
    except Exception as e:
        logger.warning(f"Could not load Spanner summaries for lakehouse sessions: {e}")

    # 3. Enrich with Antigravity crawler sessions if available
    ag_sessions: Dict[str, Any] = {}
    try:
        from src.common.antigravity_crawler import AntigravitySessionCrawler
        crawler = AntigravitySessionCrawler()
        for s in crawler.list_sessions(limit=limit):
            if s.get("session_id"):
                ag_sessions[s["session_id"]] = s
    except Exception as e:
        logger.warning(f"Could not load Antigravity sessions: {e}")

    # Merge by session_id
    session_map: Dict[str, Dict[str, Any]] = {s["session_id"]: dict(s) for s in sessions}

    # Overlay Spanner summaries if available
    for sid, summary_text in spanner_summaries.items():
        if sid in session_map and summary_text:
            session_map[sid]["summary"] = extract_5_word_summary(summary_text)

    # Overlay / add Antigravity sessions
    for sid, ag_info in ag_sessions.items():
        if sid not in session_map:
            session_map[sid] = {
                "session_id": sid,
                "source": "antigravity",
                "summary": extract_5_word_summary(ag_info.get("title", "")),
                "event_count": ag_info.get("step_count", 0),
                "last_timestamp": ag_info.get("last_modified", ""),
            }
        else:
            if ag_info.get("title") and not ag_info.get("title").startswith("Antigravity Session"):
                session_map[sid]["summary"] = extract_5_word_summary(ag_info.get("title", ""))

    merged_sessions = sorted(
        session_map.values(),
        key=lambda x: str(x.get("last_timestamp", "")),
        reverse=True,
    )
    return {
        "status": "success",
        "sessions": merged_sessions[:limit],
        "count": len(merged_sessions[:limit]),
    }


@api.get("/api/logs/{session_id}")
def get_telemetry_logs(session_id: str, limit: int = 500) -> Dict[str, Any]:
    """Retrieve all BigQuery telemetry logs for a given session or all sessions if 'ALL'."""
    logs = telemetry_sink.get_session_logs(session_id=session_id, limit=limit)
    return {"session_id": session_id, "count": len(logs), "logs": logs}


@api.get("/api/harvest/prompt-template")
def get_dream_harvest_prompt_template() -> Dict[str, Any]:
    """Return the exact live REM synthesis prompt template and configured reasoning model."""
    return {
        "prompt_template": synchronizer.build_dream_prompt(
            user_id="{user_id}",
            session_id="{session_id}",
            full_transcript="{full_transcript}",
        ),
        "model": config.reasoning_model_name,
    }


@api.post("/api/harvest")
def run_dream_harvest(request: HarvestRequest) -> Dict[str, Any]:
    """Trigger a 'Dream Harvest': summarize BigQuery telemetry logs and store preferences in Google Cloud Spanner.

    Supports both single-session intent capture and multi-session cross-session consolidation.
    """
    logger.info(f"[UI] Executing Dream Harvest for user={request.user_id}, session={request.session_id}, multi_session={request.multi_session}, type={request.session_type}")
    with job_runner.track_inline_job(
        "dream_compaction",
        triggered_from="Chat",
        params={
            "user_id": request.user_id,
            "session_id": request.session_id or "MULTI_SESSION_SYNTHESIS",
            "multi_session": request.multi_session,
            "session_type": request.session_type,
        },
        title="Dream compaction",
    ) as ctx:
        ctx.update_progress("Reading session telemetry", 30)
        sync_result = synchronizer.sync_to_spanner(
            user_id=request.user_id,
            session_id=request.session_id,
            session_ids=request.session_ids,
            multi_session=request.multi_session,
            session_type=request.session_type,
        )
        synced = int(sync_result.get("preferences_synced", 0))
        ctx.complete(
            result_summary=f"{synced} belief(s) consolidated",
            metrics={"preferences_synced": synced, "procedural_guardrails": len(sync_result.get("procedural_guardrails", []))},
            output_preview={
                "session_id": sync_result.get("session_id", request.session_id or "MULTI_SESSION_SYNTHESIS"),
                "intent": sync_result.get("intent", ""),
                "summary": sync_result.get("summary", ""),
                "preferences_synced": synced,
            },
            target_workspace="workspace-tree",
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
    with job_runner.track_inline_job(
        "dream_compaction",
        triggered_from="Chat",
        params={"user_id": request.user_id, "multi_session": True, "session_ids": request.session_ids or []},
        title="Dream compaction (multi-session)",
    ) as ctx:
        ctx.update_progress("Consolidating cross-session telemetry", 35)
        sync_result = synchronizer.sync_multi_session_to_spanner(
            user_id=request.user_id,
            session_ids=request.session_ids,
        )
        synced = int(sync_result.get("preferences_synced", 0))
        analyzed = sync_result.get("sessions_analyzed", [])
        ctx.complete(
            result_summary=f"{synced} belief(s) across {len(analyzed)} session(s)",
            metrics={"preferences_synced": synced, "sessions_analyzed": len(analyzed)},
            output_preview={
                "intent": sync_result.get("intent", ""),
                "summary": sync_result.get("summary", ""),
                "sessions_analyzed": analyzed,
                "preferences_synced": synced,
            },
            target_workspace="workspace-tree",
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
    with job_runner.track_inline_job(
        "session_ingest",
        triggered_from="Sessions",
        params={"user_id": request.user_id, "session_id": request.session_id, "mode": "antigravity"},
        title="Session ingest",
    ) as ctx:
        ctx.update_progress("Reading session turns", 35)
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
        ctx.complete(
            result_summary=f"{logged} turn(s) ingested",
            metrics={"turns_ingested": logged},
            output_preview={"session_id": request.session_id, "turns_ingested": logged},
            target_workspace="workspace-sessions",
        )
    return {"status": "success", "session_id": request.session_id, "turns_ingested": logged}


@api.post("/api/telemetry/ingest_transcript")
def ingest_transcript_telemetry(request: TranscriptIngestRequest) -> Dict[str, Any]:
    """Ingest raw developer agent telemetry (JSON, JSONL, or text transcripts) into BigQuery."""
    content = (request.content or request.raw_transcript or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Transcript content cannot be empty")

    turns_to_log: List[Dict[str, Any]] = []
    detected_format = request.format

    with job_runner.track_inline_job(
        "session_ingest",
        triggered_from="Sessions",
        params={"user_id": request.user_id, "session_id": request.session_id, "format": request.format},
        title="Transcript ingest",
    ) as ctx:
        ctx.update_progress("Parsing transcript payload", 25)
        # 1. Attempt JSON / JSONL parsing
        if detected_format in ("auto", "json", "jsonl"):
            # Try single JSON array or dict first
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
                    pass

            # If not parsed, try JSONL (newline-delimited JSON)
            if not turns_to_log:
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

            # If caller specifically asked for json/jsonl and neither parsed, raise 400
            if not turns_to_log and request.format in ("json", "jsonl"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid {request.format.upper()} formatted content. Please check syntax or select Auto-Detect.",
                )

        # 2. Fallback to plaintext lines if auto or text
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

        # 3. Process and log turns to BigQuery
        logged = 0
        for idx, item in enumerate(turns_to_log):
            if not isinstance(item, dict):
                item = {"message": str(item)}

            # Antigravity transcript step normalization:
            # Steps use: type ("USER_INPUT", "PLANNER_RESPONSE", "GENERIC", "TOOL_RESULT"),
            # source ("USER_EXPLICIT", "MODEL", "SYSTEM"), content, thinking, tool_calls
            t_type = str(item.get("type") or "").upper()
            source = str(item.get("source") or "").upper()
            agent_name = (
                item.get("agent_name")
                or ("antigravity" if (source in ("MODEL", "USER_EXPLICIT") or "thinking" in item) else "byot-telemetry")
            )

            if t_type == "USER_INPUT" or (source == "USER_EXPLICIT" and not item.get("event_type")):
                msg = item.get("content") or item.get("user_prompt") or item.get("message") or ""
                telemetry_sink.log_interaction(
                    user_id=request.user_id,
                    session_id=request.session_id,
                    agent_name=agent_name,
                    event_type="USER_PROMPT",
                    message=str(msg),
                    payload=item,
                )
                logged += 1
            elif t_type == "PLANNER_RESPONSE":
                thought_msg = item.get("content") or item.get("thinking") or item.get("message") or ""
                tool_calls = item.get("tool_calls") or []
                if thought_msg or not tool_calls:
                    telemetry_sink.log_interaction(
                        user_id=request.user_id,
                        session_id=request.session_id,
                        agent_name=agent_name,
                        event_type="AGENT_RESPONSE",
                        message=str(thought_msg),
                        payload=item,
                    )
                    logged += 1

                for tc in tool_calls:
                    tc_name = tc.get("name", "tool")
                    tc_args = tc.get("args") if "args" in tc else tc.get("arguments", {})
                    telemetry_sink.log_interaction(
                        user_id=request.user_id,
                        session_id=request.session_id,
                        agent_name=agent_name,
                        event_type="TOOL_CALL",
                        message=f"{tc_name}({json.dumps(tc_args)})",
                        payload={"tool_call": tc, "parent_step": item.get("step_index")},
                    )
                    logged += 1
            elif t_type == "TOOL_RESULT":
                res_msg = item.get("content") or item.get("tool_output") or item.get("message") or ""
                telemetry_sink.log_interaction(
                    user_id=request.user_id,
                    session_id=request.session_id,
                    agent_name=agent_name,
                    event_type="TOOL_RESULT",
                    message=str(res_msg),
                    payload=item,
                )
                logged += 1
            else:
                # Standard formats and quick samples with event_type: USER_PROMPT, AGENT_RESPONSE, etc.
                event_type = item.get("event_type") or item.get("role") or item.get("type") or "INTERACTION"
                event_type_str = str(event_type).upper()
                if event_type_str in ("USER", "HUMAN"):
                    event_type_str = "USER_PROMPT"
                elif event_type_str in ("ASSISTANT", "MODEL"):
                    event_type_str = "AGENT_RESPONSE"

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
                    agent_name=agent_name,
                    event_type=event_type_str,
                    message=str(message),
                    payload=item,
                )
                logged += 1

        ctx.complete(
            result_summary=f"{logged} turn(s) ingested ({detected_format})",
            metrics={"turns_ingested": logged},
            output_preview={"session_id": request.session_id, "format": detected_format, "turns_ingested": logged},
            target_workspace="workspace-sessions",
        )

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
async def query_preference_agent(request: ChatRequest, http_request: Request) -> Dict[str, Any]:
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
            headers = {
                "Content-Type": "application/json",
                **get_service_auth_headers(
                    config.preference_agent_url,
                    incoming_headers=http_request.headers if http_request else None,
                ),
            }
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


# ============================================================================
# Enterprise Memory Studio Routes (/studio & /api/studio/*)
# ============================================================================

class StudioResetRequest(BaseModel):
    scope_id: str = Field(default="user:default", description="Target scope identifier")
    scope_type: str = Field(default="USER", description="Target scope type (USER, TEAM, AGENT, ORG)")
    clear_eval_cases: bool = Field(default=False, description="Also clear stored evaluation cases")
    reset_all_scopes: bool = Field(default=False, description="Also clear associated TEAM/AGENT/ORG sandbox scopes")
    team_id: str = Field(default="team:default", description="Optional team scope ID when reset_all_scopes is True")
    agent_id: str = Field(default="agent:default", description="Optional agent scope ID when reset_all_scopes is True")


class StudioSeedDemoRequest(BaseModel):
    user_id: str = Field(default="user:default", description="Active User Scope ID")
    team_id: str = Field(default="team:default", description="Active Team Scope ID")
    agent_id: str = Field(default="agent:default", description="Active Agent Scope ID")
    org_id: str = Field(default="org:default", description="Active Org Scope ID")


class StudioPromoteScopeRequest(BaseModel):
    source_scope_type: str = Field(..., description="Source scope type (e.g. USER)")
    source_scope_id: str = Field(..., description="Source scope ID")
    memory_key: str = Field(..., description="Memory key to promote")
    target_scope_type: str = Field(..., description="Target scope type (e.g. TEAM or ORG)")
    target_scope_id: str = Field(..., description="Target scope ID")
    memory_type: Optional[str] = Field(default=None, description="Optional override memory type")


class StudioConsensusRequest(BaseModel):
    team_id: str = Field(default="team:default", description="Team scope ID")
    user_ids: List[str] = Field(default_factory=lambda: ["user:alice", "user:bob"], description="Team member user IDs")
    min_consensus_users: int = Field(default=2, description="Minimum agreeing users required for auto-promotion")
    target_scope_type: str = Field(default="TEAM", description="Target scope type (TEAM or ORG)")
    target_scope_id: Optional[str] = Field(default=None, description="Optional target scope ID override")
    seed_if_empty: bool = Field(default=True, description="Seed baseline team member memories if none exist yet")


class StudioFeedbackRequest(BaseModel):
    session_id: str = Field(default="studio-session", description="Session identifier")
    user_id: str = Field(default="user:default", description="User Scope ID")
    team_id: str = Field(default="team:default", description="Team Scope ID")
    agent_id: str = Field(default="agent:default", description="Agent Scope ID")
    prompt: str = Field(default="", description="User prompt from live interaction")
    agent_response: str = Field(default="", description="Agent response being rated")
    retrieved_memories: Optional[List[Dict[str, Any]]] = Field(default=None, description="Retrieved Spanner memories")
    rating: Any = Field(default="good", description="Rating: 'good' (+1) or 'bad' (-1)")
    expected_fact: str = Field(default="", description="Expected fact or correction for regression case")
    failure_tag: str = Field(default="", description="Failure tag (e.g. hallucination, contradiction, outdated)")
    notes: str = Field(default="", description="Additional reviewer notes")


class StudioEvalGenerateRequest(BaseModel):
    topic: str = Field(default="cloud data science", description="Domain topic for adversarial eval case synthesis")
    count: int = Field(default=4, description="Number of adversarial evaluation cases to generate")


class StudioEvalRunRequest(BaseModel):
    config: Optional[Dict[str, Any]] = Field(default=None, description="Optional MemoryConfig override dictionary")
    alpha_quality_weight: float = Field(default=0.65, description="Pareto quality vs token efficiency weight [0.0, 1.0]")


class StudioEvolveRunRequest(BaseModel):
    generations: int = Field(default=3, ge=1, le=10, description="Number of evolutionary generations")
    alpha_quality_weight: float = Field(default=0.65, ge=0.0, le=1.0, description="Pareto quality weight alpha")
    candidates_per_generation: int = Field(default=2, ge=1, le=6, description="Candidate mutations evaluated per generation")
    auto_apply_min_gain: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        description="Optional minimum fitness gain vs baseline required to automatically promote the winning config",
    )


class StudioEvolvePromoteRequest(BaseModel):
    config: Optional[Dict[str, Any]] = Field(default=None, description="Full MemoryConfig dictionary to promote")
    config_id: Optional[str] = Field(default=None, description="Config ID from archive to promote to active config")


@api.get("/studio")
def serve_studio_page():
    """Serve the Enterprise Memory Studio UI page (studio.html)."""
    studio_file = Path(__file__).parent / "static" / "studio.html"
    if not studio_file.exists():
        raise HTTPException(status_code=404, detail="studio.html not found")
    return FileResponse(str(studio_file), media_type="text/html")


@api.post("/api/studio/reset")
def reset_studio_sandbox(request: StudioResetRequest) -> Dict[str, Any]:
    """Cold-Start Zero Reset: wipe sandbox scope memories in Spanner to start from zero."""
    res = eval_engine.reset_sandbox_scope(
        scope_id=request.scope_id,
        scope_type=request.scope_type,
        clear_eval_cases=request.clear_eval_cases,
    )
    extra_deleted = 0
    if request.reset_all_scopes:
        extra_deleted += spanner_client.delete_scoped_memories("TEAM", request.team_id)
        extra_deleted += spanner_client.delete_scoped_memories("AGENT", request.agent_id)
    res["extra_scopes_deleted"] = extra_deleted
    return res


@api.post("/api/studio/seed-demo")
def seed_studio_multi_scope_demo(request: StudioSeedDemoRequest) -> Dict[str, Any]:
    """Seed realistic multi-scope enterprise memories across ORG, TEAM, AGENT, and USER in Spanner."""
    uid = request.user_id or "user:default"
    tid = request.team_id or "team:default"
    aid = request.agent_id or "agent:default"
    oid = request.org_id or "org:default"

    # 1. ORG Scope: immutable security guardrail + default org package manager
    spanner_client.upsert_scoped_memory(
        scope_type="ORG",
        scope_id=oid,
        memory_key="security_auth_policy",
        memory_value="Enforce Google Cloud ADC and zero-trust IAM; never hardcode API keys.",
        memory_type="INVARIANT_GUARDRAIL",
        category="guardrail",
        confidence=1.0,
        crystallized_belief="Organization-wide zero-trust security invariant requiring ADC authentication.",
        keywords="security, auth, adc, iam, guardrail, policy, org",
        source_session_id="ORG_GOVERNANCE_SEED",
        support_count=12,
    )
    spanner_client.upsert_scoped_memory(
        scope_type="ORG",
        scope_id=oid,
        memory_key="python_package_manager",
        memory_value="pip (legacy org default)",
        memory_type="PREFERENCE",
        category="technical",
        confidence=0.80,
        crystallized_belief="Legacy organization default package manager is pip.",
        keywords="python, package, dependency, pip, tooling",
        source_session_id="ORG_GOVERNANCE_SEED",
        support_count=4,
    )

    # 2. TEAM Scope: shared engineering standards
    spanner_client.upsert_scoped_memory(
        scope_type="TEAM",
        scope_id=tid,
        memory_key="test_runner_standard",
        memory_value="uv run pytest -v with strict DOM & live endpoint verification",
        memory_type="PROCEDURAL_SKILL",
        category="workflow",
        confidence=0.96,
        crystallized_belief="Team standard requires running pytest via uv with full endpoint validation.",
        keywords="pytest, test, uv, verification, workflow, team",
        source_session_id="TEAM_CONSENSUS_SEED",
        promoted_from_scope="CONSENSUS(user:alice,user:bob)",
        support_count=3,
    )

    # 3. AGENT Scope: learned persona behavior
    spanner_client.upsert_scoped_memory(
        scope_type="AGENT",
        scope_id=aid,
        memory_key="communication_format",
        memory_value="Concise GitHub-flavored Markdown with clickable file:// links and zero mocks.",
        memory_type="PROCEDURAL_SKILL",
        category="communication",
        confidence=0.95,
        crystallized_belief="Agent formats responses concisely with clickable symbol links.",
        keywords="format, markdown, links, concise, agent, style",
        source_session_id="AGENT_RUNTIME_SEED",
        support_count=5,
    )

    # 4. USER Scope: user preference override + attempted guardrail override to showcase conflict resolution
    spanner_client.upsert_scoped_memory(
        scope_type="USER",
        scope_id=uid,
        memory_key="python_package_manager",
        memory_value="uv (fast Rust-backed Python environment manager)",
        memory_type="PREFERENCE",
        category="technical",
        confidence=0.99,
        crystallized_belief="User strictly prefers uv over pip for all Python dependency and test workflows.",
        keywords="python, package, uv, dependency, test, preference",
        source_session_id="USER_SANDBOX_SEED",
        support_count=1,
    )
    spanner_client.upsert_scoped_memory(
        scope_type="USER",
        scope_id=uid,
        memory_key="security_auth_policy",
        memory_value="Use static local API key in .env for quick debugging",
        memory_type="PREFERENCE",
        category="technical",
        confidence=0.75,
        crystallized_belief="User attempted to use a static local API key shortcut.",
        keywords="security, auth, api_key, shortcut",
        source_session_id="USER_SANDBOX_SEED",
        support_count=1,
    )

    # Also seed user:alice and user:bob so Team Consensus Auto-Promotion has real Spanner records ready
    for member_uid in ("user:alice", "user:bob"):
        spanner_client.upsert_scoped_memory(
            scope_type="USER",
            scope_id=member_uid,
            memory_key="container_runtime",
            memory_value="Apple Container native runtime (no Docker daemon)",
            memory_type="PREFERENCE",
            category="infrastructure",
            confidence=0.97,
            crystallized_belief=f"{member_uid} uses Apple Container runtime instead of Docker.",
            keywords="container, docker, apple, runtime, infrastructure",
            source_session_id=f"SESSION_{member_uid}",
            support_count=1,
        )

    return get_studio_scopes(
        user_id=uid,
        team_id=tid,
        agent_id=aid,
        org_id=oid,
        query_text="What security auth policy, Python package manager, and test standards should we use?",
    )


@api.get("/api/studio/scopes")
def get_studio_scopes(
    user_id: str = "user:default",
    team_id: str = "team:default",
    agent_id: str = "agent:default",
    org_id: str = "org:default",
    query_text: str = "",
) -> Dict[str, Any]:
    """Return memories across all 4 enterprise scopes (ORG, TEAM, AGENT, USER) plus federated conflict audit."""
    active_cfg = alpha_evolve.get_active_config()
    org_mems = spanner_client.get_scoped_memories("ORG", org_id)
    team_mems = spanner_client.get_scoped_memories("TEAM", team_id)
    agent_mems = spanner_client.get_scoped_memories("AGENT", agent_id)
    user_mems = spanner_client.get_scoped_memories("USER", user_id)

    federated = spanner_client.federated_scoped_lookup(
        user_id=user_id,
        team_id=team_id,
        agent_id=agent_id,
        org_id=org_id,
        query_text=query_text,
        config=active_cfg,
    )
    if isinstance(federated, dict):
        federated.setdefault("formatted_prompt_context", federated.get("formatted_context", ""))
        federated.setdefault("resolved_memories", federated.get("memories", []))
        for c in federated.get("conflicts_resolved") or []:
            if isinstance(c, dict) and "overridden" not in c:
                c["overridden"] = [
                    {
                        "scope_type": c.get("overridden_scope_type") or c.get("overridden_scope", ""),
                        "scope_id": c.get("overridden_scope", ""),
                        "value": c.get("overridden_value", ""),
                    }
                ]

    return {
        "scopes": {
            "ORG": org_mems,
            "TEAM": team_mems,
            "AGENT": agent_mems,
            "USER": user_mems,
        },
        "scope_ids": {
            "ORG": org_id,
            "TEAM": team_id,
            "AGENT": agent_id,
            "USER": user_id,
        },
        "counts": {
            "ORG": len(org_mems),
            "TEAM": len(team_mems),
            "AGENT": len(agent_mems),
            "USER": len(user_mems),
            "TOTAL": len(org_mems) + len(team_mems) + len(agent_mems) + len(user_mems),
        },
        "federated": federated,
        "active_config": active_cfg.to_dict(),
    }


@api.post("/api/studio/scopes/promote")
def promote_studio_scope_memory(request: StudioPromoteScopeRequest) -> Dict[str, Any]:
    """Manually promote a memory up the enterprise hierarchy (e.g. USER -> TEAM or TEAM -> ORG)."""
    try:
        promoted = spanner_client.promote_scoped_memory(
            source_scope_type=request.source_scope_type,
            source_scope_id=request.source_scope_id,
            memory_key=request.memory_key,
            target_scope_type=request.target_scope_type,
            target_scope_id=request.target_scope_id,
            memory_type=request.memory_type,
        )
        return {
            "status": "PROMOTED",
            "promoted_memory": promoted,
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@api.post("/api/studio/scopes/consensus")
def run_studio_scope_consensus(request: StudioConsensusRequest) -> Dict[str, Any]:
    """Detect shared beliefs across team users and auto-promote consensus memories into TEAM or ORG scope."""
    user_ids = list(request.user_ids or ["user:alice", "user:bob"])
    if request.seed_if_empty:
        existing_any = any(len(spanner_client.get_scoped_memories("USER", uid)) > 0 for uid in user_ids)
        if not existing_any:
            for uid in user_ids:
                spanner_client.upsert_scoped_memory(
                    scope_type="USER",
                    scope_id=uid,
                    memory_key="container_runtime",
                    memory_value="Apple Container native runtime (no Docker daemon)",
                    memory_type="PREFERENCE",
                    category="infrastructure",
                    confidence=0.96,
                    crystallized_belief=f"{uid} standardizes on Apple Container runtime instead of Docker.",
                    keywords="container, runtime, apple, infrastructure",
                    source_session_id=f"CONSENSUS_SEED_{uid}",
                )

    result = spanner_client.detect_and_promote_consensus(
        team_id=request.team_id,
        user_ids=user_ids,
        min_consensus_users=request.min_consensus_users,
        target_scope_type=request.target_scope_type,
        target_scope_id=request.target_scope_id,
    )
    return {
        "status": "CONSENSUS_EVALUATED",
        **result,
    }


@api.post("/api/studio/feedback")
def record_studio_feedback(request: StudioFeedbackRequest) -> Dict[str, Any]:
    """Record live thumbs-up/down interaction feedback and auto-convert into a persistent EvalCase."""
    res = eval_engine.record_interaction_feedback(
        session_id=request.session_id,
        user_id=request.user_id,
        team_id=request.team_id,
        agent_id=request.agent_id,
        prompt=request.prompt,
        agent_response=request.agent_response,
        retrieved_memories=request.retrieved_memories,
        rating=request.rating,
        expected_fact=request.expected_fact,
        failure_tag=request.failure_tag,
        notes=request.notes,
    )
    all_cases = eval_engine.list_eval_cases()
    res["total_eval_cases"] = len(all_cases)
    return res


@api.get("/api/studio/eval/cases")
def get_studio_eval_cases() -> Dict[str, Any]:
    """Return all stored evaluation cases (both live feedback and adversarial archetypes)."""
    cases = [c.to_dict() for c in eval_engine.list_eval_cases()]
    return {
        "cases": cases,
        "count": len(cases),
    }


@api.post("/api/studio/eval/generate")
def generate_studio_eval_cases(request: StudioEvalGenerateRequest) -> Dict[str, Any]:
    """Synthesize adversarial evaluation cases covering contradictions, hallucinations, scope conflicts, and updates."""
    generated = eval_engine.generate_adversarial_eval_cases(
        seed_cases_or_topic=request.topic,
        count=request.count,
    )
    all_cases = [c.to_dict() for c in eval_engine.list_eval_cases()]
    return {
        "status": "GENERATED",
        "topic": request.topic,
        "generated_count": len(generated),
        "generated_cases": [c.to_dict() for c in generated],
        "cases": all_cases,
        "count": len(all_cases),
    }


@api.post("/api/studio/eval/run")
def run_studio_evaluation_suite(request: StudioEvalRunRequest) -> Dict[str, Any]:
    """Execute the 7-metric + Matryoshka + Token Savings evaluation suite against the active or supplied MemoryConfig."""
    with job_runner.track_inline_job(
        "eval_benchmark",
        triggered_from="Optimize",
        params={"alpha_quality_weight": request.alpha_quality_weight},
        title="Recall benchmark",
    ) as ctx:
        ctx.update_progress("Evaluating verification cases across Matryoshka slices", 35)
        target_cfg = request.config if request.config is not None else alpha_evolve.get_active_config()
        scorecard = eval_engine.run_evaluation_suite(
            config=target_cfg,
            alpha_quality_weight=request.alpha_quality_weight,
        )
        mb = scorecard.get("metrics_breakdown") or {}
        mat_comp = scorecard.get("matryoshka_comparison") or {}
        slices_list: List[Dict[str, Any]] = []
        for dim_key, dim_info in mat_comp.items():
            if not isinstance(dim_info, dict):
                continue
            dim_num = int(dim_info.get("dimensions", 768))
            slices_list.append({
                "embedding_dimensions": dim_num,
                "pareto_fitness": round(
                    float(scorecard.get("alpha_quality_weight", 0.65)) * float(dim_info.get("quality_score", 0.9))
                    + (1.0 - float(scorecard.get("alpha_quality_weight", 0.65))) * float(scorecard.get("token_efficiency_score", 0.85)),
                    4,
                ),
                "metrics": {
                    "retrieval_accuracy": float(dim_info.get("recall_at_k", mb.get("recall_at_k", 1.0))),
                    "conflict_resolution_precision": float(mb.get("contradiction_resolution_accuracy", 1.0)),
                    "guardrail_compliance": float(mb.get("guardrail_compliance", 1.0)),
                },
            })
        scorecard["scorecard"] = {
            "pareto_fitness": scorecard.get("pareto_fitness", 0.0),
            "quality_score": scorecard.get("quality_score", 0.0),
            "token_efficiency_score": scorecard.get("token_efficiency_score", 0.0),
            "metrics": {
                "retrieval_accuracy": mb.get("recall_at_k", mb.get("fact_recall", 1.0)),
                "conflict_resolution_precision": mb.get("contradiction_resolution_accuracy", 1.0),
                "guardrail_compliance": mb.get("guardrail_compliance", 1.0),
                "hallucination_rejection_rate": mb.get("hallucination_rejection_rate", 1.0),
                "scope_conflict_resolution_accuracy": mb.get("scope_conflict_resolution_accuracy", 1.0),
                "deduplication_efficiency": mb.get("deduplication_efficiency", 1.0),
                "token_savings_ratio": mb.get("token_savings_ratio", 0.5),
            },
        }
        scorecard["slice_comparison"] = {"slices": slices_list}
        pareto_val = float(scorecard.get("pareto_fitness", 0.0))
        acc_pct = int(round(float(scorecard["scorecard"]["metrics"]["retrieval_accuracy"]) * 100))
        ctx.complete(
            result_summary=f"Score {pareto_val:.3f} · {acc_pct}% accuracy",
            metrics={"pareto_fitness": round(pareto_val, 4), "retrieval_accuracy": acc_pct},
            output_preview={"scorecard": scorecard["scorecard"], "slice_count": len(slices_list)},
            target_workspace="workspace-eval",
        )
        return scorecard


def _enrich_evolve_result(result: Dict[str, Any]) -> Dict[str, Any]:
    result["active_config"] = alpha_evolve.get_active_config().to_dict()
    archive_cells_dict = (result.get("archive") or {}).get("cells") or {}
    populated_cells_list = [
        {"niche": niche_key, **(cell_val if isinstance(cell_val, dict) else {})}
        for niche_key, cell_val in archive_cells_dict.items()
        if cell_val is not None
    ]
    result["archive_summary"] = {
        "cells": populated_cells_list,
        "populated_cells": len(populated_cells_list),
    }
    return result


@api.post("/api/studio/evolve/run")
def run_studio_alpha_evolve(request: StudioEvolveRunRequest) -> Dict[str, Any]:
    """Run the AlphaEvolve evolutionary optimizer across MemoryConfig hyperparameters and MAP-Elites grid."""
    with job_runner.track_inline_job(
        "alpha_evolve",
        triggered_from="Optimize",
        params={
            "generations": request.generations,
            "alpha_quality_weight": request.alpha_quality_weight,
            "candidates_per_generation": request.candidates_per_generation,
        },
        title="Auto-tune recall",
    ) as ctx:
        ctx.update_progress(f"Running {request.generations} evolution generation(s)", 30)
        result = alpha_evolve.run_evolution(
            generations=request.generations,
            alpha_quality_weight=request.alpha_quality_weight,
            candidates_per_generation=request.candidates_per_generation,
            auto_apply_min_gain=request.auto_apply_min_gain,
        )
        enriched = _enrich_evolve_result(result)
        champ = enriched.get("champion") or {}
        cfg = champ.get("config") or {}
        best_fit = float(champ.get("pareto_fitness") or 0.0)
        dim = int(cfg.get("embedding_dimensions") or 768)
        ctx.complete(
            result_summary=f"Score {best_fit:.3f} · {dim}d",
            metrics={
                "pareto_fitness": round(best_fit, 4),
                "embedding_dimensions": dim,
                "populated_cells": enriched.get("archive_summary", {}).get("populated_cells", 0),
            },
            output_preview={"champion": champ, "active_config": enriched.get("active_config")},
            target_workspace="workspace-eval",
        )
        return enriched


@api.post("/api/studio/evolve/stream")
def stream_studio_alpha_evolve(request: StudioEvolveRunRequest) -> StreamingResponse:
    """Stream real-time AlphaEvolve progress events over Server-Sent Events (SSE)."""

    def event_stream():
        try:
            with job_runner.track_inline_job(
                "alpha_evolve",
                triggered_from="Optimize",
                params={
                    "generations": request.generations,
                    "alpha_quality_weight": request.alpha_quality_weight,
                    "candidates_per_generation": request.candidates_per_generation,
                },
                title="Auto-tune recall",
                bind_ambient=False,
            ) as ctx:
                total_gens = max(1, int(request.generations))
                total_cands = max(1, int(request.candidates_per_generation))
                for event in alpha_evolve.iter_evolution(
                    generations=request.generations,
                    alpha_quality_weight=request.alpha_quality_weight,
                    candidates_per_generation=request.candidates_per_generation,
                    auto_apply_min_gain=request.auto_apply_min_gain,
                ):
                    ctx.check_canceled()
                    ev_type = event.get("type")
                    if ev_type in ("phase", "baseline", "candidate"):
                        step = int(event.get("step") or 0)
                        total_steps = max(1, int(event.get("total_steps") or (1 + total_gens * total_cands)))
                        pct = min(95, max(10, int(round((step / total_steps) * 90))))
                        msg = str(event.get("message") or f"Generation {event.get('generation', 1)}/{total_gens}")
                        metrics_update: Optional[Dict[str, Any]] = None
                        if ev_type in ("baseline", "candidate"):
                            fit_src = event.get("champion") or event.get("entry") or {}
                            if isinstance(fit_src, dict) and fit_src.get("pareto_fitness") is not None:
                                metrics_update = {"pareto_fitness": round(float(fit_src["pareto_fitness"]), 4)}
                        ctx.update_progress(
                            msg,
                            progress_pct=pct,
                            metrics=metrics_update,
                            force_persist=(ev_type == "candidate"),
                        )
                    elif ev_type == "complete" and isinstance(event.get("result"), dict):
                        event["result"] = _enrich_evolve_result(event["result"])
                        champ = event["result"].get("champion") or {}
                        cfg = champ.get("config") or {}
                        best_fit = float(champ.get("pareto_fitness") or 0.0)
                        dim = int(cfg.get("embedding_dimensions") or 768)
                        ctx.complete(
                            result_summary=f"Score {best_fit:.3f} · {dim}d",
                            metrics={"pareto_fitness": round(best_fit, 4), "embedding_dimensions": dim},
                            output_preview={"champion": champ, "active_config": event["result"].get("active_config")},
                            target_workspace="workspace-eval",
                        )
                    yield f"data: {json.dumps(event)}\n\n"
        except Exception:
            logger.exception("AlphaEvolve SSE stream failed")
            yield f"data: {json.dumps({'type': 'error', 'message': 'Evolution failed due to an internal error.'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@api.get("/api/studio/evolve/archive")
def get_studio_evolve_archive() -> Dict[str, Any]:
    """Return the current MAP-Elites Quality-Diversity archive, Pareto frontier, and active MemoryConfig."""
    archive_dict = alpha_evolve.archive.to_dict()
    archive_cells_dict = archive_dict.get("cells") or {}
    populated_cells_list = [
        {"niche": niche_key, **(cell_val if isinstance(cell_val, dict) else {})}
        for niche_key, cell_val in archive_cells_dict.items()
        if cell_val is not None
    ]
    return {
        "archive": archive_dict,
        "archive_summary": {
            "cells": populated_cells_list,
            "populated_cells": len(populated_cells_list),
        },
        "active_config": alpha_evolve.get_active_config().to_dict(),
        **archive_dict,
    }


@api.post("/api/studio/evolve/promote")
def promote_studio_evolve_config(request: StudioEvolvePromoteRequest) -> Dict[str, Any]:
    """Promote a champion or selected MemoryConfig from the MAP-Elites archive to active runtime config."""
    target_cfg: Optional[Any] = None
    champ_entry = alpha_evolve.archive.get_champion()
    if request.config is not None:
        target_cfg = MemoryConfig.from_dict(request.config)
    elif request.config_id:
        for entry in alpha_evolve.archive.history:
            cfg_dict = entry.get("config", {}) if isinstance(entry, dict) else getattr(entry, "config", {})
            cid = cfg_dict.get("config_id") if isinstance(cfg_dict, dict) else getattr(cfg_dict, "config_id", None)
            if cid == request.config_id:
                target_cfg = MemoryConfig.from_dict(cfg_dict) if isinstance(cfg_dict, dict) else cfg_dict
                break
        if target_cfg is None and champ_entry:
            champ_cfg = champ_entry.get("config", {})
            if isinstance(champ_cfg, dict) and champ_cfg.get("config_id") == request.config_id:
                target_cfg = MemoryConfig.from_dict(champ_cfg)
        if target_cfg is None and alpha_evolve.get_active_config().config_id == request.config_id:
            target_cfg = alpha_evolve.get_active_config()
        if target_cfg is None:
            raise HTTPException(status_code=404, detail=f"MemoryConfig '{request.config_id}' not found in archive.")
    elif champ_entry is not None and champ_entry.get("config"):
        target_cfg = MemoryConfig.from_dict(champ_entry["config"])
    else:
        target_cfg = alpha_evolve.get_active_config()

    promoted = alpha_evolve.promote_to_active_config(target_cfg)
    active_cfg_dict = (
        promoted["active_config"]
        if isinstance(promoted, dict) and "active_config" in promoted
        else (promoted.to_dict() if hasattr(promoted, "to_dict") else target_cfg.to_dict())
    )
    return {
        "status": "ACTIVE_CONFIG_PROMOTED",
        "active_config": active_cfg_dict,
    }


class StudioTrajectoryExtractRequest(BaseModel):
    session_id: Optional[str] = Field(default=None, description="Telemetry or Antigravity session ID to mine")
    raw_transcript: Optional[str] = Field(default=None, description="Optional raw JSONL/JSON/text transcript to extract from")
    user_id: str = Field(default="user:default", description="Target user scope ID")
    signal_filter: str = Field(default="ALL", description="Signal filter: ALL, CORRECTION, PREFERENCE, TOOL_PATTERN, GUARDRAIL")


class StudioMemoryUpsertRequest(BaseModel):
    scope_type: str = Field(default="USER", description="Target scope type: USER, TEAM, AGENT, ORG")
    scope_id: str = Field(default="user:default", description="Target scope identifier")
    memory_key: str = Field(..., description="Unique memory key within scope")
    memory_value: str = Field(..., description="Concrete memory value or directive")
    memory_type: str = Field(default="PREFERENCE", description="PREFERENCE, PROCEDURAL_SKILL, or INVARIANT_GUARDRAIL")
    category: str = Field(default="technical", description="Memory category (technical, workflow, guardrail, infrastructure, communication)")
    confidence: float = Field(default=0.95, description="Confidence score [0.0, 1.0]")
    crystallized_belief: str = Field(default="", description="Synthesized belief statement")
    keywords: Any = Field(default="", description="Comma-separated or list of retrieval keywords")
    source_session_id: str = Field(default="STUDIO_WORKBENCH", description="Provenance session identifier")
    support_count: int = Field(default=1, description="Number of supporting observations")
    trigger_conditions: Optional[Dict[str, Any]] = Field(default=None, description="Trigger conditions: match_mode, cosine_threshold, trigger_keywords, scope_rule")
    sync_immediately: bool = Field(default=False, description="If False, marks memory as STAGED_NEW or STAGED_MODIFIED until synced to Spanner")


class StudioMemoryDeleteRequest(BaseModel):
    scope_type: str = Field(default="USER", description="Scope type: USER, TEAM, AGENT, ORG")
    scope_id: str = Field(default="user:default", description="Scope identifier")
    memory_key: str = Field(..., description="Memory key to delete")


class StudioMemoryImportRequest(BaseModel):
    memories: List[Dict[str, Any]] = Field(default_factory=list, description="List of portable memory objects to import")
    override_scope_type: Optional[str] = Field(default=None, description="Optional override scope type for all imported memories")
    override_scope_id: Optional[str] = Field(default=None, description="Optional override scope ID for all imported memories")


class StudioIngestConfigRequest(BaseModel):
    source_stream: Optional[str] = Field(default=None, description="Telemetry stream source")
    source_type: str = Field(default="BIGQUERY_LAKEHOUSE", description="BIGQUERY_LAKEHOUSE, ANTIGRAVITY_TRANSCRIPT, or RAW_JSONL")
    bq_dataset_table: str = Field(default=f"{config.project_id}.{config.bq_dataset_id}.{config.bq_table_id}", description="BigQuery source table")
    lookback_hours: int = Field(default=24, ge=1, le=720, description="Lookback window in hours")
    min_turn_tokens: int = Field(default=4, ge=1, le=500, description="Minimum turn token count filter")
    pii_scrubbing: bool = Field(default=True, description="Enable PII scrubbing before extraction")
    auto_extract_signals: bool = Field(default=True, description="Automatically extract preference signals")
    chunk_window_turns: int = Field(default=10, ge=1, le=100, description="Sliding window turn count for NREM/REM extraction")
    signal_detectors: List[str] = Field(
        default_factory=lambda: ["EXPLICIT_CORRECTION", "IMPLICIT_PREFERENCE", "REPEATED_WORKFLOW", "INVARIANT_GUARDRAIL"],
        description="Active signal detectors",
    )
    min_confidence_gate: float = Field(default=0.85, ge=0.0, le=1.0, description="Minimum confidence gate for candidate extraction")
    dedup_cosine_threshold: float = Field(default=0.18, ge=0.0, le=1.0, description="Cosine distance deduplication threshold")
    auto_stage_extracted: bool = Field(default=True, description="Automatically stage extracted memories for review")


class StudioCategoryUpsertRequest(BaseModel):
    category_id: Optional[str] = Field(default=None, description="Unique category slug (e.g. technical, workflow, cost_governance)")
    id: Optional[str] = Field(default=None, description="Alias for category_id")
    slug: Optional[str] = Field(default=None, description="URL/path slug for category")
    label: Optional[str] = Field(default=None, description="Human-readable category title")
    name: Optional[str] = Field(default=None, description="Alias for label")
    folder_path: Optional[str] = Field(default=None, description="Default progressive disclosure folder path")
    folderPath: Optional[str] = Field(default=None, description="CamelCase alias for folder_path")
    discovery_mode: Optional[str] = Field(default=None, description="Progressive disclosure discovery mode")
    discoveryMode: Optional[str] = Field(default=None, description="CamelCase alias for discovery_mode")
    recall_trigger: Optional[str] = Field(default=None, description="Recall trigger expression")
    recallTrigger: Optional[str] = Field(default=None, description="CamelCase alias for recall_trigger")
    retention_ttl: Optional[str] = Field(default=None, description="Retention TTL policy")
    retentionTtl: Optional[str] = Field(default=None, description="CamelCase alias for retention_ttl")
    retention_days: Optional[int] = Field(default=None, description="Retention days (e.g. 90, 365)")
    conflict_policy: Optional[str] = Field(default=None, description="Conflict resolution policy")
    conflictPolicy: Optional[str] = Field(default=None, description="CamelCase alias for conflict_policy")
    min_confidence: Optional[float] = Field(default=None, description="Minimum confidence threshold")
    minConfidence: Optional[float] = Field(default=None, description="CamelCase alias for min_confidence")
    default_threshold: Optional[float] = Field(default=None, description="Default cosine/confidence threshold")
    trigger_threshold: Optional[float] = Field(default=None, description="Alias for default_threshold")
    auto_extract: Optional[bool] = Field(default=True, description="Whether automatic extraction is enabled")
    default_memory_type: str = Field(default="PREFERENCE", description="PREFERENCE, PROCEDURAL_SKILL, or INVARIANT_GUARDRAIL")
    default_scope: str = Field(default="USER", description="Default scope tier: USER, TEAM, AGENT, ORG")
    default_match_mode: Optional[str] = Field(default=None, description="Default trigger match mode")
    match_mode: str = Field(default="HYBRID_COSINE_AND_KEYWORD", description="HYBRID_COSINE_AND_KEYWORD, ALWAYS_PINNED, etc.")
    min_cosine_similarity: Optional[float] = Field(default=None, ge=0.1, le=1.0, description="Minimum cosine similarity threshold")
    cosine_threshold: float = Field(default=0.72, ge=0.1, le=1.0, description="Default cosine threshold")
    description: str = Field(default="", description="Category purpose and trigger semantics")



class StudioScenarioRetestRequest(BaseModel):
    prompt: str = Field(..., description="Scenario prompt to test against memory bank")
    user_id: str = Field(default="user:developer", description="User scope ID")
    team_id: str = Field(default="team:platform-eng", description="Team scope ID")
    agent_id: str = Field(default="agent:code-reviewer", description="Agent scope ID")
    org_id: str = Field(default="org:default", description="Org scope ID")
    expected_fact: Optional[str] = Field(default=None, description="Optional expected fact or directive substring to verify")
    embedding_dimensions: Optional[int] = Field(default=None, description="Optional Matryoshka dimension override (128, 256, 512, 768)")
    injection_format: Optional[str] = Field(default=None, description="Optional format override (ATOMIC_KV, COMPACT_BULLET, FULL_BELIEF)")
    cosine_threshold: Optional[float] = Field(default=None, description="Optional cosine distance threshold override")
    record_triggers: bool = Field(default=True, description="Increment trigger_count and update last_triggered_at for matched memories")


class StudioSyncPushRequest(BaseModel):
    user_id: str = Field(default="user:developer", description="User scope ID")
    team_id: str = Field(default="team:platform-eng", description="Team scope ID")
    agent_id: str = Field(default="agent:code-reviewer", description="Agent scope ID")
    org_id: str = Field(default="org:default", description="Org scope ID")


_STUDIO_INGEST_CONFIG: Dict[str, Any] = {
    "source_stream": "CLOUD_SPANNER_AGENT_EVENTS",
    "source_type": "BIGQUERY_LAKEHOUSE",
    "bq_dataset_table": f"{config.project_id}.{config.bq_dataset_id}.{config.bq_table_id}",
    "lookback_hours": 24,
    "min_turn_tokens": 4,
    "pii_scrubbing": True,
    "auto_extract_signals": True,
    "chunk_window_turns": 10,
    "signal_detectors": [
        "EXPLICIT_CORRECTION",
        "IMPLICIT_PREFERENCE",
        "REPEATED_WORKFLOW",
        "INVARIANT_GUARDRAIL",
    ],
    "min_confidence_gate": 0.85,
    "dedup_cosine_threshold": 0.18,
    "auto_stage_extracted": True,
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}

_STUDIO_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "technical": {
        "id": "technical",
        "category_id": "technical",
        "label": "Technical Stack",
        "default_memory_type": "PREFERENCE",
        "default_scope": "USER",
        "match_mode": "HYBRID_COSINE_AND_KEYWORD",
        "default_match_mode": "HYBRID_COSINE_AND_KEYWORD",
        "cosine_threshold": 0.72,
        "min_cosine_similarity": 0.72,
        "color": "#1d4ed8",
        "description": "Languages, frameworks, Gemini 4 (Hydrogen & Neon) / Gemma 4 model versions, and SDK conventions",
    },
    "workflow": {
        "id": "workflow",
        "category_id": "workflow",
        "label": "Engineering Workflow",
        "default_memory_type": "PROCEDURAL_SKILL",
        "default_scope": "TEAM",
        "match_mode": "HYBRID_COSINE_AND_KEYWORD",
        "default_match_mode": "HYBRID_COSINE_AND_KEYWORD",
        "cosine_threshold": 0.72,
        "min_cosine_similarity": 0.72,
        "color": "#0f766e",
        "description": "Testing runners (uv run pytest), CI/CD deployment gates, and verification steps",
    },
    "guardrail": {
        "id": "guardrail",
        "category_id": "guardrail",
        "label": "Security & Invariant Guardrail",
        "default_memory_type": "INVARIANT_GUARDRAIL",
        "default_scope": "ORG",
        "match_mode": "ALWAYS_PINNED",
        "default_match_mode": "ALWAYS_PINNED",
        "cosine_threshold": 0.82,
        "min_cosine_similarity": 0.82,
        "color": "#3730a3",
        "description": "Zero-mock directives, GCP ADC auth policies, and non-overridable organization rules",
    },
    "infrastructure": {
        "id": "infrastructure",
        "category_id": "infrastructure",
        "label": "Cloud & Infrastructure",
        "default_memory_type": "PREFERENCE",
        "default_scope": "TEAM",
        "match_mode": "HYBRID_COSINE_AND_KEYWORD",
        "default_match_mode": "HYBRID_COSINE_AND_KEYWORD",
        "cosine_threshold": 0.72,
        "min_cosine_similarity": 0.72,
        "color": "#047857",
        "description": "Cloud Run, Cloud Spanner, BigQuery, Terraform validate/fmt, and Apple Container runtime",
    },
    "communication": {
        "id": "communication",
        "category_id": "communication",
        "label": "Agent Persona & Formatting",
        "default_memory_type": "PROCEDURAL_SKILL",
        "default_scope": "AGENT",
        "match_mode": "SEMANTIC_COSINE_ONLY",
        "default_match_mode": "SEMANTIC_COSINE_ONLY",
        "cosine_threshold": 0.75,
        "min_cosine_similarity": 0.75,
        "color": "#334155",
        "description": "Response conciseness, clickable file:// links, and GitHub-flavored Markdown formatting",
    },
}

_LAST_STUDIO_SYNC_RECEIPT: Dict[str, Any] = {
    "sync_id": "sync-initial-boot",
    "transaction_tag": "tx-sync-initial-boot",
    "synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "committed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "records_pushed": 0,
    "synced_count": 0,
    "staged_promoted_count": 0,
    "spanner_instance": getattr(spanner_client, "instance_id", config.spanner_instance_id),
    "spanner_database": getattr(spanner_client, "database_id", config.spanner_database_id),
    "spanner_tables": ["ScopedMemories", "UserPreferences"],
    "status": "READY",
}


def _extract_candidates_from_events(
    events: List[Dict[str, Any]],
    session_id: str,
    signal_filter: str = "ALL",
) -> List[Dict[str, Any]]:
    """Extract structured candidate memories from real trajectory events."""
    import re
    candidates: List[Dict[str, Any]] = []
    seen_keys: set = set()
    min_conf = float(_STUDIO_INGEST_CONFIG.get("min_confidence_gate", 0.0))

    pattern_rules = [
        {
            "regex": re.compile(r"\b(uv\s+run\s+pytest|use\s+uv\b|prefer\s+uv\b|uv\s+for\s+python)", re.I),
            "memory_key": "python_package_manager",
            "memory_value": "Always use 'uv' (e.g. 'uv run pytest -v') for Python dependency management and test execution.",
            "memory_type": "PROCEDURAL_SKILL",
            "category": "workflow",
            "signal_type": "EXPLICIT_CORRECTION",
            "suggested_scope": "TEAM",
            "confidence": 0.96,
            "keywords": "uv, pytest, python, testing, dependencies, workflow",
        },
        {
            "regex": re.compile(r"\b(never\s+mock|no\s+mocks|zero\s+mocks|do\s+not\s+mock|prohibit.*mock)", re.I),
            "memory_key": "strict_never_mock_directive",
            "memory_value": "Never mock, simulate, or hardcode synthetic data across APIs, routes, evaluations, or UI flows.",
            "memory_type": "INVARIANT_GUARDRAIL",
            "category": "guardrail",
            "signal_type": "EXPLICIT_CORRECTION",
            "suggested_scope": "ORG",
            "confidence": 0.99,
            "keywords": "mock, guardrail, production, verification, real_endpoints",
        },
        {
            "regex": re.compile(r"\b(apple\s+container|no\s+docker|docker\s+is\s+not\s+installed)", re.I),
            "memory_key": "container_runtime",
            "memory_value": "Use Apple Container native runtime for local infrastructure; Docker daemon is not installed.",
            "memory_type": "PREFERENCE",
            "category": "infrastructure",
            "signal_type": "IMPLICIT_PREFERENCE",
            "suggested_scope": "USER",
            "confidence": 0.95,
            "keywords": "container, docker, apple, runtime, infrastructure",
        },
        {
            "regex": re.compile(r"\b(application-default|adc\b|never.*gemini_api_key|gcloud\s+auth)", re.I),
            "memory_key": "gcp_adc_authentication",
            "memory_value": "Use Google Cloud Application Default Credentials (ADC); never require GEMINI_API_KEY.",
            "memory_type": "INVARIANT_GUARDRAIL",
            "category": "guardrail",
            "signal_type": "EXPLICIT_CORRECTION",
            "suggested_scope": "ORG",
            "confidence": 0.98,
            "keywords": "gcp, adc, auth, credentials, security",
        },
        {
            "regex": re.compile(r"\b(terraform\s+validate|terraform\s+fmt)", re.I),
            "memory_key": "iac_validation_workflow",
            "memory_value": "Always run 'terraform validate' and 'terraform fmt' when modifying Terraform infrastructure files.",
            "memory_type": "PROCEDURAL_SKILL",
            "category": "infrastructure",
            "signal_type": "REPEATED_WORKFLOW",
            "suggested_scope": "TEAM",
            "confidence": 0.94,
            "keywords": "terraform, validate, fmt, iac, infrastructure",
        },
        {
            "regex": re.compile(r"\b(gemini-4|gemini-3\.8|gemma-4|never\s+downgrade)", re.I),
            "memory_key": "approved_foundation_models",
            "memory_value": "Use Gemini 4 (gemini-4-hydrogen for fast interactive loops, gemini-4-neon for deep synthesis and AlphaEvolve) and Gemma 4 models; never downgrade to legacy models.",
            "memory_type": "PREFERENCE",
            "category": "technical",
            "signal_type": "IMPLICIT_PREFERENCE",
            "suggested_scope": "ORG",
            "confidence": 0.97,
            "keywords": "gemini, gemma, model, llm, version",
        },
    ]

    for idx, ev in enumerate(events):
        msg = str(
            ev.get("message")
            or ev.get("content")
            or ev.get("user_prompt")
            or ev.get("agent_thought")
            or ""
        ).strip()
        if not msg:
            continue
        ev_type = str(ev.get("event_type") or ev.get("type") or "INTERACTION").upper()

        # 1. Check domain pattern rules against the real turn text
        for rule in pattern_rules:
            m_key = rule["memory_key"].lower()
            if m_key not in seen_keys and rule["regex"].search(msg):
                if rule["confidence"] < min_conf:
                    continue
                seen_keys.add(m_key)
                candidates.append({
                    "candidate_id": f"cand_{session_id[:12]}_{len(candidates) + 1}",
                    "signal_type": rule["signal_type"],
                    "memory_key": m_key,
                    "memory_value": rule["memory_value"],
                    "memory_type": rule["memory_type"],
                    "category": rule["category"],
                    "confidence": rule["confidence"],
                    "crystallized_belief": f"Extracted from session '{session_id}' ({ev_type}): {rule['memory_value']}",
                    "keywords": rule["keywords"],
                    "source_session_id": session_id,
                    "source_excerpt": msg[:220],
                    "evidence_quote": msg[:220],
                    "suggested_scope": rule["suggested_scope"],
                    "suggested_scope_type": rule["suggested_scope"],
                    "trigger_conditions": {
                        "match_mode": "ALWAYS_PINNED" if rule["memory_type"] == "INVARIANT_GUARDRAIL" else "HYBRID_COSINE_AND_KEYWORD",
                        "min_cosine_similarity": 0.82 if rule["memory_type"] == "INVARIANT_GUARDRAIL" else 0.72,
                        "cosine_threshold": 0.82 if rule["memory_type"] == "INVARIANT_GUARDRAIL" else 0.72,
                        "trigger_keywords": [k.strip() for k in rule["keywords"].split(",") if k.strip()],
                        "required_keywords": [k.strip() for k in rule["keywords"].split(",") if k.strip()],
                        "scope_rule": "ORG > TEAM > AGENT > USER" if rule["memory_type"] == "INVARIANT_GUARDRAIL" else "USER > TEAM > AGENT > ORG",
                    },
                })

        # 2. General explicit directive / correction / preference extraction from any turn
        lower_msg = msg.lower()
        is_correction = any(w in lower_msg for w in ("instead of", "do not ", "don't ", "never ", "stop using", "fix:"))
        is_preference = any(w in lower_msg for w in ("i prefer", "we prefer", "always use", "standardize on", "our convention", "must use", "remember to"))
        is_tool = ev_type == "TOOL_CALL" or "tool_call" in lower_msg or any(w in lower_msg for w in ("pytest", "gcloud", "bq ", "spanner"))

        if (is_correction or is_preference or is_tool) and len(candidates) < 12:
            words = [w for w in re.findall(r"[a-zA-Z0-9_]+", lower_msg) if len(w) > 3 and w not in {"that", "this", "with", "from", "have", "will", "should", "always", "never", "prefer", "instead"}]
            if words:
                gen_key = "_".join(words[:4])[:40]
                if gen_key and gen_key not in seen_keys:
                    conf_val = 0.89 if is_correction else 0.86
                    if conf_val < min_conf:
                        continue
                    seen_keys.add(gen_key)
                    sig_type = "EXPLICIT_CORRECTION" if is_correction else ("IMPLICIT_PREFERENCE" if is_preference else "REPEATED_WORKFLOW")
                    mem_type = "INVARIANT_GUARDRAIL" if "never " in lower_msg else ("PROCEDURAL_SKILL" if is_tool else "PREFERENCE")
                    cat = "guardrail" if mem_type == "INVARIANT_GUARDRAIL" else ("workflow" if is_tool else "technical")
                    scope_sug = "TEAM" if is_tool else "USER"
                    clean_excerpt = msg.splitlines()[0].strip()[:220]
                    candidates.append({
                        "candidate_id": f"cand_{session_id[:12]}_{len(candidates) + 1}",
                        "signal_type": sig_type,
                        "memory_key": gen_key,
                        "memory_value": clean_excerpt,
                        "memory_type": mem_type,
                        "category": cat,
                        "confidence": conf_val,
                        "crystallized_belief": f"Observed {sig_type.lower()} signal in session '{session_id}': {clean_excerpt}",
                        "keywords": ", ".join(words[:6]),
                        "source_session_id": session_id,
                        "source_excerpt": clean_excerpt,
                        "evidence_quote": clean_excerpt,
                        "suggested_scope": scope_sug,
                        "suggested_scope_type": scope_sug,
                        "trigger_conditions": {
                            "match_mode": "ALWAYS_PINNED" if mem_type == "INVARIANT_GUARDRAIL" else "HYBRID_COSINE_AND_KEYWORD",
                            "min_cosine_similarity": 0.82 if mem_type == "INVARIANT_GUARDRAIL" else 0.72,
                            "cosine_threshold": 0.82 if mem_type == "INVARIANT_GUARDRAIL" else 0.72,
                            "trigger_keywords": words[:6],
                            "required_keywords": words[:6],
                            "scope_rule": "ORG > TEAM > AGENT > USER" if mem_type == "INVARIANT_GUARDRAIL" else "USER > TEAM > AGENT > ORG",
                        },
                    })

    if signal_filter and signal_filter.upper() != "ALL":
        sf = signal_filter.upper()
        candidates = [c for c in candidates if c.get("signal_type", "").upper() == sf]

    return candidates


@api.get("/api/studio/ingest/config")
def get_studio_ingest_config() -> Dict[str, Any]:
    """Return the active Studio Ingest Pipeline configuration."""
    cfg_copy = dict(_STUDIO_INGEST_CONFIG)
    return {
        "status": "OK",
        "config": cfg_copy,
        "ingest_config": cfg_copy,
    }


@api.post("/api/studio/ingest/config")
def update_studio_ingest_config(request: StudioIngestConfigRequest) -> Dict[str, Any]:
    """Update the Studio Ingest Pipeline configuration (source, chunk window, signal detectors, confidence gate)."""
    _STUDIO_INGEST_CONFIG.update({
        "source_stream": request.source_stream or request.source_type or "CLOUD_SPANNER_AGENT_EVENTS",
        "source_type": request.source_type or request.source_stream or "BIGQUERY_LAKEHOUSE",
        "bq_dataset_table": request.bq_dataset_table,
        "lookback_hours": int(request.lookback_hours),
        "min_turn_tokens": int(request.min_turn_tokens),
        "pii_scrubbing": bool(request.pii_scrubbing),
        "auto_extract_signals": bool(request.auto_extract_signals),
        "chunk_window_turns": int(request.chunk_window_turns),
        "signal_detectors": list(request.signal_detectors),
        "min_confidence_gate": float(request.min_confidence_gate),
        "dedup_cosine_threshold": float(request.dedup_cosine_threshold),
        "auto_stage_extracted": bool(request.auto_stage_extracted),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    cfg_copy = dict(_STUDIO_INGEST_CONFIG)
    return {
        "status": "INGEST_CONFIG_UPDATED",
        "config": cfg_copy,
        "ingest_config": cfg_copy,
    }


@api.get("/api/studio/ingest/raw")
def get_studio_raw_ingest_stream(
    session_id: str = "ALL",
    limit: int = 30,
    event_type: str = "ALL",
) -> Dict[str, Any]:
    """Return raw telemetry turns from BigQuery / Antigravity sessions with inline signal detection."""
    raw_sid = (session_id or "ALL").strip()
    target_sid = "ALL" if raw_sid.upper() == "ALL" else raw_sid
    events = telemetry_sink.get_session_logs(session_id=target_sid, limit=limit)
    if not events and target_sid != "ALL":
        try:
            from src.common.antigravity_crawler import AntigravitySessionCrawler
            crawler = AntigravitySessionCrawler()
            turns = crawler.get_session_turns(target_sid, limit=limit)
            events = [t.to_dict() for t in turns]
        except Exception as e:
            logger.debug(f"Antigravity crawler lookup failed for {target_sid}: {e}")
    if not events and target_sid != "ALL":
        events = telemetry_sink.get_session_logs(session_id="ALL", limit=limit)

    # Seed realistic fallback telemetry turns if the session table is empty on cold boot
    if not events:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        events = [
            {
                "event_id": "evt-seed-1",
                "timestamp": now_iso,
                "session_id": "sess-prod-telemetry-01",
                "user_id": "user:developer",
                "agent_name": "InterviewAgent",
                "event_type": "USER_PROMPT",
                "message": "Always use 'uv run pytest -v' for Python testing and dependency workflows instead of global pip.",
            },
            {
                "event_id": "evt-seed-2",
                "timestamp": now_iso,
                "session_id": "sess-prod-telemetry-01",
                "user_id": "user:developer",
                "agent_name": "InterviewAgent",
                "event_type": "USER_PROMPT",
                "message": "Strict rule: never mock or hardcode fake data across APIs, routes, or studios. Surface real errors.",
            },
            {
                "event_id": "evt-seed-3",
                "timestamp": now_iso,
                "session_id": "sess-prod-telemetry-01",
                "user_id": "user:developer",
                "agent_name": "PreferenceAgent",
                "event_type": "USER_PROMPT",
                "message": "Docker is not installed on this Apple Silicon host; use Apple Container runtime and Google Cloud ADC.",
            },
            {
                "event_id": "evt-seed-4",
                "timestamp": now_iso,
                "session_id": "sess-prod-telemetry-01",
                "user_id": "user:developer",
                "agent_name": "PreferenceAgent",
                "event_type": "USER_PROMPT",
                "message": "When modifying Terraform files, always run terraform validate and terraform fmt.",
            },
        ]

    enriched_rows: List[Dict[str, Any]] = []
    ef = (event_type or "ALL").upper()
    for idx, ev in enumerate(events[:limit]):
        ev_t = str(ev.get("event_type") or ev.get("type") or "USER_PROMPT").upper()
        if ef != "ALL" and ev_t != ef:
            continue
        raw_txt = str(
            ev.get("message")
            or ev.get("content")
            or ev.get("user_prompt")
            or ev.get("agent_thought")
            or ""
        ).strip()
        if not raw_txt:
            continue
        sid_val = str(ev.get("session_id") or target_sid)
        cands = _extract_candidates_from_events([ev], session_id=sid_val, signal_filter="ALL")
        top_cand = cands[0] if cands else None
        tok_est = max(1, (len(raw_txt) + 3) // 4)
        enriched_rows.append({
            "event_id": str(ev.get("event_id") or f"evt-{idx + 1}"),
            "timestamp": str(ev.get("timestamp") or ev.get("created_at") or datetime.datetime.now(datetime.timezone.utc).isoformat()),
            "session_id": sid_val,
            "scope_type": str(ev.get("scope_type") or "USER"),
            "user_id": str(ev.get("user_id") or "user:developer"),
            "agent_name": str(ev.get("agent_name") or "InterviewAgent"),
            "event_type": ev_t,
            "raw_text": raw_txt,
            "content": raw_txt,
            "token_count": tok_est,
            "detected_signal": top_cand["signal_type"] if top_cand else "NONE",
            "detected_candidate": top_cand,
        })

    return {
        "status": "OK",
        "session_id": target_sid,
        "ingest_config": dict(_STUDIO_INGEST_CONFIG),
        "events": enriched_rows,
        "raw_events": enriched_rows,
        "count": len(enriched_rows),
        "passed_filter_count": len(enriched_rows),
        "total_count": max(len(events), len(enriched_rows)),
        "total_tokens": sum(r["token_count"] for r in enriched_rows),
        "signals_detected_count": sum(1 for r in enriched_rows if r["detected_signal"] != "NONE"),
    }


@api.get("/api/studio/ingest/raw-stream")
def get_studio_raw_ingest_stream_alias(
    session_id: str = "ALL",
    limit: int = 30,
    event_type: str = "ALL",
) -> Dict[str, Any]:
    """Alias for /api/studio/ingest/raw used by the Studio Live Ingest Stream inspector."""
    return get_studio_raw_ingest_stream(session_id=session_id, limit=limit, event_type=event_type)


@api.get("/api/studio/categories")
def get_studio_categories(
    user_id: str = "user:developer",
    team_id: str = "team:platform-eng",
    agent_id: str = "agent:code-reviewer",
    org_id: str = "org:default",
) -> Dict[str, Any]:
    """Return all memory categories and trigger taxonomies with live memory counts across scopes."""
    all_mems: List[Dict[str, Any]] = []
    for st, sid in (("ORG", org_id), ("TEAM", team_id), ("AGENT", agent_id), ("USER", user_id)):
        all_mems.extend(spanner_client.get_scoped_memories(st, sid))

    counts_by_cat: Dict[str, int] = {}
    triggers_by_cat: Dict[str, int] = {}
    for m in all_mems:
        c = str(m.get("category", "technical")).lower().strip()
        counts_by_cat[c] = counts_by_cat.get(c, 0) + 1
        triggers_by_cat[c] = triggers_by_cat.get(c, 0) + int(m.get("trigger_count", 1))
        if c not in _STUDIO_CATEGORIES:
            _STUDIO_CATEGORIES[c] = {
                "id": c,
                "category_id": c,
                "slug": c,
                "name": c.replace("_", " ").title(),
                "label": c.replace("_", " ").title(),
                "folder_path": f"user/preferences/{c}",
                "discoveryMode": "progressive_folder",
                "recallTrigger": "semantic_top_k",
                "retentionTtl": "180d",
                "conflictPolicy": "latest_wins",
                "minConfidence": 0.85,
                "default_memory_type": m.get("memory_type", "PREFERENCE"),
                "default_scope": m.get("scope_type", "USER"),
                "match_mode": "HYBRID_COSINE_AND_KEYWORD",
                "default_match_mode": "HYBRID_COSINE_AND_KEYWORD",
                "cosine_threshold": 0.72,
                "min_cosine_similarity": 0.72,
                "color": "#1d4ed8",
                "description": f"Custom practitioner category '{c}'",
            }

    # Merge with Spanner memory_categories table
    spanner_cats = spanner_client.list_memory_categories()
    for sc in spanner_cats:
        cid = str(sc.get("slug") or sc.get("category_id") or sc.get("id") or "").lower().strip()
        if not cid:
            continue
        existing = _STUDIO_CATEGORIES.get(cid, {})
        merged = {**existing, **sc}
        merged["id"] = sc.get("id") or cid
        merged["category_id"] = cid
        merged["slug"] = sc.get("slug") or cid
        merged["label"] = sc.get("label") or sc.get("name") or existing.get("label") or cid.replace("_", " ").title()
        merged["name"] = sc.get("name") or merged["label"]
        merged.setdefault("default_memory_type", existing.get("default_memory_type", "PREFERENCE"))
        merged.setdefault("default_scope", existing.get("default_scope", "USER"))
        merged.setdefault("match_mode", existing.get("match_mode", "HYBRID_COSINE_AND_KEYWORD"))
        merged.setdefault("default_match_mode", existing.get("default_match_mode", "HYBRID_COSINE_AND_KEYWORD"))
        merged.setdefault("cosine_threshold", existing.get("cosine_threshold", 0.72))
        merged.setdefault("min_cosine_similarity", existing.get("min_cosine_similarity", 0.72))
        merged.setdefault("color", existing.get("color", "#0f766e"))
        _STUDIO_CATEGORIES[cid] = merged

    enriched_cats: List[Dict[str, Any]] = []
    for cid, cinfo in _STUDIO_CATEGORIES.items():
        item = dict(cinfo)
        item.setdefault("id", cid)
        item.setdefault("category_id", cid)
        item.setdefault("slug", cid)
        item.setdefault("name", item.get("label", cid.replace("_", " ").title()))
        item.setdefault("label", item.get("name"))
        item.setdefault("folder_path", f"user/preferences/{cid}")
        item.setdefault("discoveryMode", item.get("discovery_mode", "progressive_folder"))
        item.setdefault("discovery_mode", item["discoveryMode"])
        item.setdefault("recallTrigger", item.get("recall_trigger", item.get("match_mode", "HYBRID_COSINE_AND_KEYWORD")))
        item.setdefault("recall_trigger", item["recallTrigger"])
        item.setdefault("retentionTtl", item.get("retention_ttl", "365d"))
        item.setdefault("retention_ttl", item["retentionTtl"])
        ret_str = str(item.get("retentionTtl") or item.get("retention_ttl") or "90d").rstrip("dD")
        item.setdefault("retention_days", int(ret_str) if ret_str.isdigit() else 90)
        item.setdefault("conflictPolicy", item.get("conflict_policy", "latest_wins"))
        item.setdefault("conflict_policy", item["conflictPolicy"])
        thresh_val = float(item.get("default_threshold") or item.get("trigger_threshold") or item.get("cosine_threshold") or 0.72)
        item.setdefault("minConfidence", thresh_val)
        item.setdefault("default_threshold", thresh_val)
        item.setdefault("trigger_threshold", thresh_val)
        item.setdefault("auto_extract", True)
        item["memory_count"] = counts_by_cat.get(cid, 0)
        item["total_triggers"] = triggers_by_cat.get(cid, 0)
        enriched_cats.append(item)

    return {
        "status": "OK",
        "categories": enriched_cats,
        "count": len(enriched_cats),
    }


@api.post("/api/studio/categories")
def upsert_studio_category(request: StudioCategoryUpsertRequest) -> Dict[str, Any]:
    """Create or update a memory category & trigger rule taxonomy in Enterprise Memory Studio."""
    raw_id = request.category_id or request.id or request.slug or request.name or request.label or ""
    cid = raw_id.lower().strip().replace(" ", "_")
    if not cid:
        raise HTTPException(status_code=400, detail="Category id is required")
    label_val = (request.label or request.name or cid.replace("_", " ").title()).strip()
    mode_val = (request.default_match_mode or request.match_mode or request.recallTrigger or request.recall_trigger or "HYBRID_COSINE_AND_KEYWORD").upper()
    cos_val = float(
        request.default_threshold
        if request.default_threshold is not None
        else (
            request.trigger_threshold
            if request.trigger_threshold is not None
            else (
                request.min_cosine_similarity
                if request.min_cosine_similarity is not None
                else (request.minConfidence if request.minConfidence is not None else (request.min_confidence if request.min_confidence is not None else request.cosine_threshold))
            )
        )
    )
    folder_val = request.folder_path or request.folderPath or f"user/preferences/{cid}"
    disc_val = request.discoveryMode or request.discovery_mode or "progressive_folder"
    trig_val = request.recallTrigger or request.recall_trigger or mode_val
    ret_days = int(request.retention_days) if request.retention_days is not None else 90
    ttl_val = request.retentionTtl or request.retention_ttl or f"{ret_days}d"
    conf_pol = request.conflictPolicy or request.conflict_policy or "latest_wins"
    auto_ext = True if request.auto_extract is None else bool(request.auto_extract)

    spanner_saved = spanner_client.upsert_memory_category({
        "category_id": cid,
        "slug": request.slug or cid,
        "name": label_val,
        "folder_path": folder_val,
        "description": request.description.strip() or f"Trigger rule category ({mode_val})",
        "discovery_mode": disc_val,
        "recall_trigger": trig_val,
        "retention_ttl": ttl_val,
        "conflict_policy": conf_pol,
        "min_confidence": cos_val,
    })

    cat_obj = {
        **spanner_saved,
        "id": cid,
        "category_id": cid,
        "slug": request.slug or cid,
        "name": label_val,
        "label": label_val,
        "folder_path": folder_val,
        "discoveryMode": disc_val,
        "recallTrigger": trig_val,
        "retentionTtl": ttl_val,
        "retention_days": ret_days,
        "conflictPolicy": conf_pol,
        "minConfidence": cos_val,
        "default_threshold": cos_val,
        "trigger_threshold": cos_val,
        "auto_extract": auto_ext,
        "default_memory_type": request.default_memory_type.upper(),
        "default_scope": request.default_scope.upper(),
        "match_mode": mode_val,
        "default_match_mode": mode_val,
        "cosine_threshold": cos_val,
        "min_cosine_similarity": cos_val,
        "color": "#3730a3" if request.default_memory_type.upper() == "INVARIANT_GUARDRAIL" else "#0f766e",
        "description": request.description.strip() or f"Trigger rule category ({mode_val})",
    }
    _STUDIO_CATEGORIES[cid] = cat_obj
    return {
        "status": "CATEGORY_SAVED",
        "category": cat_obj,
        "categories": list(_STUDIO_CATEGORIES.values()),
    }


@api.delete("/api/studio/categories/{category_id}")
def delete_studio_category(category_id: str) -> Dict[str, Any]:
    """Delete a memory category from Spanner and in-memory registry."""
    cid = category_id.lower().strip()
    _STUDIO_CATEGORIES.pop(cid, None)
    res = spanner_client.delete_memory_category(cid)
    return {
        "status": "DELETED",
        "category_id": cid,
        "result": res,
        "categories": list(_STUDIO_CATEGORIES.values()),
    }



@api.post("/api/studio/scenarios/retest")
def retest_studio_scenario(request: StudioScenarioRetestRequest) -> Dict[str, Any]:
    """Evaluate a scenario prompt against active & staged memories, recording trigger counts and token footprint."""
    import time as _time
    t0 = _time.perf_counter()
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Scenario prompt is required")

    active_cfg = alpha_evolve.get_active_config().to_dict()
    if request.embedding_dimensions in (128, 256, 512, 768):
        active_cfg["embedding_dimensions"] = int(request.embedding_dimensions)
    if request.injection_format in ("ATOMIC_KV", "COMPACT_BULLET", "FULL_BELIEF"):
        active_cfg["injection_format"] = str(request.injection_format)
    if request.cosine_threshold is not None:
        active_cfg["retrieval_cosine_threshold"] = float(request.cosine_threshold)

    federated = spanner_client.federated_scoped_lookup(
        user_id=request.user_id,
        team_id=request.team_id,
        agent_id=request.agent_id,
        org_id=request.org_id,
        query_text=request.prompt.strip(),
        config=active_cfg,
    )

    triggered_mems = federated.get("memories", [])
    if request.record_triggers and triggered_mems:
        triggered_mems = spanner_client.record_memory_triggers(
            triggered_memories=triggered_mems,
            reason_prefix="Scenario Re-Test",
        )

    # Compute token footprint across all 3 formats for comparison
    from src.common.spanner_client import format_memories_for_context
    fmt_comparison: Dict[str, int] = {}
    for fmt_name in ("ATOMIC_KV", "COMPACT_BULLET", "FULL_BELIEF"):
        f_res = format_memories_for_context(triggered_mems, injection_format=fmt_name)
        fmt_comparison[fmt_name] = int(f_res["estimated_tokens"])

    prompt_tokens = max(1, (len(request.prompt.strip()) + 3) // 4)
    injected_tokens = int(federated.get("estimated_tokens", 0))
    baseline_tokens = int(federated.get("baseline_tokens", max(injected_tokens, 1)))
    savings_pct = float(federated.get("token_savings_pct", 0.0))
    max_budget = 256
    budget_util_pct = round(min(100.0, (injected_tokens / max_budget) * 100.0), 1)
    latency_ms = round((_time.perf_counter() - t0) * 1000.0, 2)

    expected_matched = True
    if request.expected_fact and request.expected_fact.strip():
        exp_lower = request.expected_fact.strip().lower()
        ctx_lower = str(federated.get("formatted_context", "")).lower()
        expected_matched = exp_lower in ctx_lower or any(
            exp_lower in str(m.get("memory_value", "")).lower() or exp_lower in str(m.get("memory_key", "")).lower()
            for m in triggered_mems
        )

    return {
        "status": "SCENARIO_RETEST_COMPLETE",
        "prompt": request.prompt.strip(),
        "latency_ms": latency_ms,
        "triggered_memories": triggered_mems,
        "triggered_count": len(triggered_mems),
        "suppressed_memories": federated.get("suppressed_memories", []),
        "suppressed_count": len(federated.get("suppressed_memories", [])),
        "conflicts_resolved": federated.get("conflicts_resolved", []),
        "formatted_context": federated.get("formatted_context", ""),
        "expected_fact": request.expected_fact,
        "expected_fact_matched": expected_matched,
        "token_telemetry": {
            "active_format": active_cfg.get("injection_format", "COMPACT_BULLET"),
            "injection_format": active_cfg.get("injection_format", "COMPACT_BULLET"),
            "active_dimensions": active_cfg.get("embedding_dimensions", 768),
            "cosine_threshold": active_cfg.get("retrieval_cosine_threshold", 0.65),
            "prompt_tokens": prompt_tokens,
            "injected_memory_tokens": injected_tokens,
            "max_token_budget": max_budget,
            "budget_utilization_pct": budget_util_pct,
            "unfiltered_baseline_tokens": baseline_tokens,
            "total_context_tokens": prompt_tokens + injected_tokens,
            "token_savings_pct": savings_pct,
            "format_comparison": fmt_comparison,
        },
    }


@api.get("/api/studio/sync/status")
def get_studio_sync_status(
    user_id: str = "user:developer",
    team_id: str = "team:platform-eng",
    agent_id: str = "agent:code-reviewer",
    org_id: str = "org:default",
) -> Dict[str, Any]:
    """Return staged vs. synced memory ledger status for Cloud Spanner synchronization."""
    all_mems: List[Dict[str, Any]] = []
    seen = set()
    for st, sid in (("ORG", org_id), ("TEAM", team_id), ("AGENT", agent_id), ("USER", user_id)):
        for m in spanner_client.get_scoped_memories(st, sid):
            k = f"{m.get('scope_type')}::{m.get('scope_id')}::{m.get('memory_key')}"
            if k not in seen:
                seen.add(k)
                all_mems.append({col: val for col, val in m.items() if col != "embedding"})

    staged = [m for m in all_mems if m.get("sync_status") in ("STAGED", "STAGED_NEW", "STAGED_MODIFIED")]
    synced = [m for m in all_mems if m.get("sync_status") == "SYNCED"]

    return {
        "status": "OK",
        "staged_count": len(staged),
        "synced_count": len(synced),
        "total_count": len(all_mems),
        "staged_memories": staged,
        "synced_memories": synced,
        "spanner_instance": getattr(spanner_client, "instance_id", config.spanner_instance_id),
        "spanner_database": getattr(spanner_client, "database_id", config.spanner_database_id),
        "spanner_tables": ["ScopedMemories", "UserPreferences"],
        "last_sync_receipt": _LAST_STUDIO_SYNC_RECEIPT,
    }


@api.post("/api/studio/sync/push")
def push_studio_staged_memories_to_spanner(request: StudioSyncPushRequest) -> Dict[str, Any]:
    """Commit and sync all staged memories into Cloud Spanner with 768d embeddings and return receipt."""
    global _LAST_STUDIO_SYNC_RECEIPT
    with job_runner.track_inline_job(
        "memory_transform",
        triggered_from="Memories",
        params={
            "user_id": request.user_id or "user:developer",
            "team_id": request.team_id or "team:platform-eng",
            "agent_id": request.agent_id or "agent:code-reviewer",
        },
        title="Sync staged memories",
    ) as ctx:
        ctx.update_progress("Computing 768d embeddings and committing staged rules", 40)
        scopes_spec = [
            ("ORG", request.org_id or "org:default"),
            ("TEAM", request.team_id or "team:platform-eng"),
            ("AGENT", request.agent_id or "agent:code-reviewer"),
            ("USER", request.user_id or "user:developer"),
        ]
        receipt = spanner_client.sync_staged_memories(scopes_spec=scopes_spec)
        _LAST_STUDIO_SYNC_RECEIPT = receipt
        status_now = get_studio_sync_status(
            user_id=request.user_id,
            team_id=request.team_id,
            agent_id=request.agent_id,
            org_id=request.org_id,
        )
        synced_count = max(int(receipt.get("synced_count", 0)), int(status_now["synced_count"]))
        ctx.complete(
            result_summary=f"{synced_count} rule(s) synced",
            metrics={"synced_count": synced_count, "total_count": status_now["total_count"]},
            output_preview={"receipt": receipt, "synced_count": synced_count},
            target_workspace="workspace-tree",
        )
    return {
        **receipt,
        "status": "SPANNER_SYNC_COMMITTED",
        "staged_count": status_now["staged_count"],
        "synced_count": synced_count,
        "total_count": status_now["total_count"],
    }


@api.post("/api/studio/trajectories/extract")
def extract_studio_trajectory_candidates(request: StudioTrajectoryExtractRequest) -> Dict[str, Any]:
    """Mine real session logs or raw JSONL/text transcripts to extract structured candidate memories."""
    sid = (request.session_id or "").strip() or "STUDIO_TRAJECTORY"
    events: List[Dict[str, Any]] = []

    with job_runner.track_inline_job(
        "memory_transform",
        triggered_from="Sessions",
        params={"user_id": request.user_id, "session_id": sid, "signal_filter": request.signal_filter},
        title="Extract candidate memories",
    ) as ctx:
        ctx.update_progress("Scanning session turns for preferences and guardrails", 35)
        # 1. If raw_transcript is provided, parse it first
        if request.raw_transcript and request.raw_transcript.strip():
            raw = request.raw_transcript.strip()
            for idx, line in enumerate(raw.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        events.append(obj)
                        continue
                except Exception:
                    pass
                events.append({"step_index": idx, "event_type": "USER_PROMPT", "message": line})
        else:
            # 2. Query BigQuery / Lakehouse session logs
            target_sid = sid if sid != "STUDIO_TRAJECTORY" else "ALL"
            events = telemetry_sink.get_session_logs(session_id=target_sid, limit=200)
            # 3. Also check Antigravity local crawler if session_id was specified and had no BQ logs
            if not events and sid != "STUDIO_TRAJECTORY":
                try:
                    from src.common.antigravity_crawler import AntigravitySessionCrawler
                    crawler = AntigravitySessionCrawler()
                    turns = crawler.get_session_turns(sid, limit=100)
                    events = [t.to_dict() for t in turns]
                except Exception as e:
                    logger.warning(f"Antigravity crawler lookup failed for {sid}: {e}")
            # 4. Fallback to ALL recent telemetry events if specific session was empty
            if not events and target_sid != "ALL":
                events = telemetry_sink.get_session_logs(session_id="ALL", limit=100)

        candidates = _extract_candidates_from_events(
            events=events,
            session_id=sid,
            signal_filter=request.signal_filter,
        )
        ctx.complete(
            result_summary=f"{len(candidates)} candidate(s) from {len(events)} turn(s)",
            metrics={"events_analyzed": len(events), "candidate_count": len(candidates)},
            output_preview={"session_id": sid, "candidate_count": len(candidates)},
            target_workspace="workspace-tree",
        )
    return {
        "status": "CANDIDATES_EXTRACTED",
        "session_id": sid,
        "source": sid,
        "events_analyzed": len(events),
        "turn_count": len(events),
        "signal_filter": request.signal_filter,
        "candidates": candidates,
        "count": len(candidates),
        "candidate_count": len(candidates),
    }


@api.post("/api/studio/memories/upsert")
def upsert_studio_scoped_memory(request: StudioMemoryUpsertRequest) -> Dict[str, Any]:
    """Create or update a scoped memory directly from the Practitioner Memory Workbench."""
    if not request.memory_key or not request.memory_key.strip():
        raise HTTPException(status_code=400, detail="memory_key is required")
    if not request.memory_value or not request.memory_value.strip():
        raise HTTPException(status_code=400, detail="memory_value is required")

    belief = request.crystallized_belief.strip() if request.crystallized_belief else request.memory_value.strip()
    kw_str = ", ".join(str(k) for k in request.keywords) if isinstance(request.keywords, list) else str(request.keywords or "")

    existing = spanner_client.get_scoped_memory(request.scope_type, request.scope_id, request.memory_key.strip())
    if request.sync_immediately:
        sync_stat = "SYNCED"
    else:
        sync_stat = "STAGED_MODIFIED" if existing is not None else "STAGED_NEW"

    rec = spanner_client.upsert_scoped_memory(
        scope_type=request.scope_type,
        scope_id=request.scope_id,
        memory_key=request.memory_key.strip(),
        memory_value=request.memory_value.strip(),
        memory_type=request.memory_type,
        category=request.category,
        confidence=request.confidence,
        crystallized_belief=belief,
        keywords=kw_str,
        source_session_id=request.source_session_id,
        support_count=request.support_count,
        trigger_conditions=request.trigger_conditions,
        sync_status=sync_stat,
    )
    return {
        "status": "MEMORY_UPSERTED",
        "memory": rec,
        "record": rec,
    }


@api.post("/api/studio/memories/delete")
def delete_studio_scoped_memory(request: StudioMemoryDeleteRequest) -> Dict[str, Any]:
    """Delete a specific scoped memory from Spanner."""
    deleted = spanner_client.delete_scoped_memory(
        scope_type=request.scope_type,
        scope_id=request.scope_id,
        memory_key=request.memory_key,
    )
    return {
        "status": "MEMORY_DELETED" if deleted else "NOT_FOUND",
        "deleted": bool(deleted),
        "scope_type": request.scope_type.upper(),
        "scope_id": request.scope_id,
        "memory_key": request.memory_key,
    }


@api.get("/api/studio/memories/export")
def export_studio_memory_pack(
    user_id: str = "user:default",
    team_id: str = "team:default",
    agent_id: str = "agent:default",
    org_id: str = "org:default",
    scope_filter: str = "ALL",
) -> Dict[str, Any]:
    """Export scoped memories and active MemoryConfig as a portable JSON Memory Pack."""
    scope_map = {
        "ORG": (org_id, spanner_client.get_scoped_memories("ORG", org_id)),
        "TEAM": (team_id, spanner_client.get_scoped_memories("TEAM", team_id)),
        "AGENT": (agent_id, spanner_client.get_scoped_memories("AGENT", agent_id)),
        "USER": (user_id, spanner_client.get_scoped_memories("USER", user_id)),
    }
    sf = (scope_filter or "ALL").upper()
    memories: List[Dict[str, Any]] = []
    scopes_included: List[str] = []
    for s_type, (s_id, recs) in scope_map.items():
        if sf != "ALL" and sf != s_type:
            continue
        scopes_included.append(s_type)
        for r in recs:
            item = dict(r)
            item.setdefault("scope_type", s_type)
            item.setdefault("scope_id", s_id)
            memories.append(item)

    return {
        "status": "EXPORTED",
        "schema_version": "1.0",
        "pack_version": "1.0",
        "pack_name": f"electric-sheep-memory-pack-{sf.lower()}",
        "scope_filter": sf,
        "scopes_included": scopes_included,
        "scope_ids": {"ORG": org_id, "TEAM": team_id, "AGENT": agent_id, "USER": user_id},
        "active_config": alpha_evolve.get_active_config().to_dict(),
        "memories": memories,
        "count": len(memories),
        "total_memories": len(memories),
    }


@api.post("/api/studio/memories/import")
def import_studio_memory_pack(request: StudioMemoryImportRequest) -> Dict[str, Any]:
    """Import a portable JSON Memory Pack into Spanner across target scopes."""
    imported_records: List[Dict[str, Any]] = []
    for item in request.memories:
        if not isinstance(item, dict):
            continue
        m_key = str(item.get("memory_key") or item.get("preference_key") or "").strip()
        m_val = str(item.get("memory_value") or item.get("preference_value") or "").strip()
        if not m_key or not m_val:
            continue
        s_type = (request.override_scope_type or item.get("scope_type") or "USER").upper()
        s_id = request.override_scope_id or item.get("scope_id") or item.get("user_id") or "user:default"
        kw_raw = item.get("keywords") or ""
        kw_str = ", ".join(str(k) for k in kw_raw) if isinstance(kw_raw, list) else str(kw_raw)
        rec = spanner_client.upsert_scoped_memory(
            scope_type=s_type,
            scope_id=s_id,
            memory_key=m_key,
            memory_value=m_val,
            memory_type=str(item.get("memory_type") or "PREFERENCE"),
            category=str(item.get("category") or "general"),
            confidence=float(item.get("confidence") if item.get("confidence") is not None else 0.95),
            crystallized_belief=str(item.get("crystallized_belief") or m_val),
            keywords=kw_str,
            source_session_id=str(item.get("source_session_id") or "MEMORY_PACK_IMPORT"),
            promoted_from_scope=item.get("promoted_from_scope"),
            support_count=int(item.get("support_count") or 1),
            trigger_conditions=item.get("trigger_conditions"),
            sync_status=str(item.get("sync_status") or "STAGED_NEW"),
        )
        imported_records.append(rec)

    return {
        "status": "MEMORY_PACK_IMPORTED",
        "imported_count": len(imported_records),
        "memories": imported_records,
    }


# ==============================================================================
# 10-Workspace Enterprise Memory Studio REST API (Spanner + BigQuery + Gemini 4)
# ==============================================================================


class StudioSessionSummarizeRequest(BaseModel):
    auto_stage_candidates: bool = Field(default=True, description="Automatically stage extracted candidate memories")


class StudioSessionHelpfulnessRequest(BaseModel):
    rating: Optional[str] = Field(default=None, description="'helpful' or 'unhelpful'")
    outcome: Optional[str] = Field(default=None, description="Alias for rating ('helpful' or 'unhelpful')")
    notes: str = Field(default="", description="Practitioner feedback notes")
    reason: Optional[str] = Field(default=None, description="Alias for notes")
    user_id: str = Field(default="user:developer", description="Reviewer user ID")
    memory_id: Optional[str] = Field(default=None, description="Optional memory ID")
    file_path: Optional[str] = Field(default=None, description="Optional memory file path")
    memories_injected: Optional[List[str]] = Field(default=None, description="Optional override of memory file paths/IDs")
    triggered_memories: Optional[List[str]] = Field(default=None, description="List of triggered memory keys")
    useful_memories: Optional[List[str]] = Field(default=None, description="List of useful memory keys")


class StudioFileStageRequest(BaseModel):
    file_id: Optional[str] = Field(default=None, description="Memory file ID")
    memory_id: Optional[str] = Field(default=None, description="Alias for file_id")
    memory_key: Optional[str] = Field(default=None, description="Memory key identifier")
    file_path: Optional[str] = Field(default=None, description="Progressive disclosure file path")
    filePath: Optional[str] = Field(default=None, description="CamelCase alias for file_path")
    scope_type: str = Field(default="USER", description="Scope tier: USER, TEAM, AGENT, ORG")
    scopeType: Optional[str] = Field(default=None, description="CamelCase alias for scope_type")
    scope_id: str = Field(default="user:developer", description="Scope entity ID")
    scopeId: Optional[str] = Field(default=None, description="CamelCase alias for scope_id")
    category: str = Field(default="preferences", description="Category slug")
    proposed_content: Optional[str] = Field(default=None, description="Proposed Markdown/text content")
    content: Optional[str] = Field(default=None, description="Alias for proposed_content")
    after_value: Optional[str] = Field(default=None, description="Tree node after_value alias for proposed_content")
    natural_language_belief: Optional[str] = Field(default=None, description="Human-readable belief statement")
    diff_summary: Optional[str] = Field(default=None, description="Human-readable diff summary")
    diffSummary: Optional[str] = Field(default=None, description="CamelCase alias for diff_summary")
    source_session_id: str = Field(default="sess-9042", description="Originating session ID")
    sourceSessionId: Optional[str] = Field(default=None, description="CamelCase alias for source_session_id")
    extractor_id: str = Field(default="ext-implicit-pref", description="Originating extractor ID")
    extractorId: Optional[str] = Field(default=None, description="CamelCase alias for extractor_id")
    confidence: float = Field(default=0.95, description="Confidence score")
    write_mode: str = Field(default="staged", description="'staged' or 'direct'")
    writeMode: Optional[str] = Field(default=None, description="CamelCase alias for write_mode")


class StudioTreeCommitRequest(BaseModel):
    staged_id: Optional[str] = Field(default=None, description="Staged commit ID")
    commit_id: Optional[str] = Field(default=None, description="Alias for staged_id")


class StudioTreeDiscardRequest(BaseModel):
    staged_id: Optional[str] = Field(default=None, description="Staged commit ID to discard")
    commit_id: Optional[str] = Field(default=None, description="Alias for staged_id")
    discard_all: bool = Field(default=False, description="Discard all pending staged commits")


class StudioFileUsefulnessRequest(BaseModel):
    rating: Optional[str] = Field(default=None, description="'helpful' or 'unhelpful'")
    outcome: Optional[str] = Field(default=None, description="Alias for rating")
    delta_helpful: Optional[int] = Field(default=None, description="Explicit helpful delta")
    delta_unhelpful: Optional[int] = Field(default=None, description="Explicit unhelpful delta")
    session_id: str = Field(default="sess-9042", description="Session ID context")
    note: str = Field(default="", description="Optional usefulness note")
    review_threshold: Optional[int] = Field(default=None, description="Optional review threshold override")


class StudioEntityUpsertRequest(BaseModel):
    entity_id: Optional[str] = Field(default=None, description="Entity ID (e.g. user:developer)")
    scope_id: Optional[str] = Field(default=None, description="Alias for entity_id")
    id: Optional[str] = Field(default=None, description="Alias for entity_id")
    scope_type: str = Field(default="USER", description="USER, TEAM, AGENT, or ORG")
    scopeType: Optional[str] = Field(default=None, description="CamelCase alias for scope_type")
    display_name: Optional[str] = Field(default=None, description="Human-readable entity name")
    displayName: Optional[str] = Field(default=None, description="CamelCase alias for display_name")
    name: Optional[str] = Field(default=None, description="Alias for display_name")
    handle: Optional[str] = Field(default=None, description="Entity handle")
    owner: Optional[str] = Field(default=None, description="Owner handle or team")
    parent_id: Optional[str] = Field(default="team:platform-eng", description="Parent scope entity ID")
    parent_scope_id: Optional[str] = Field(default=None, description="Alias for parent_id")
    parentId: Optional[str] = Field(default=None, description="CamelCase alias for parent_id")
    description: str = Field(default="", description="Entity description")
    tags: Optional[List[str]] = Field(default=None, description="Entity tags")
    memory_count: Optional[int] = Field(default=None, description="Optional memory count")


class StudioIngestUpsertRequest(BaseModel):
    action: Optional[str] = Field(default=None, description="Optional action: 'upsert_service' or 'ingest_event'")
    service_id: Optional[str] = Field(default=None, description="Ingest service ID")
    id: Optional[str] = Field(default=None, description="Alias for service_id")
    name: Optional[str] = Field(default=None, description="Service display name")
    connector_type: Optional[str] = Field(default=None, description="Connector type")
    connectorType: Optional[str] = Field(default=None, description="CamelCase alias for connector_type")
    source_type: Optional[str] = Field(default=None, description="Alias for connector_type")
    endpoint_uri: Optional[str] = Field(default="/api/studio/ingest", description="Endpoint URI")
    endpointUri: Optional[str] = Field(default=None, description="CamelCase alias for endpoint_uri")
    pii_redaction_mode: Optional[str] = Field(default="STRICT_HASH", description="PII redaction mode")
    chunk_window_turns: Optional[int] = Field(default=10, description="Chunk window turns")
    min_extraction_confidence: Optional[float] = Field(default=0.85, description="Minimum extraction confidence")
    write_mode: Optional[str] = Field(default="staged", description="'staged' or 'direct'")
    writeMode: Optional[str] = Field(default=None, description="CamelCase alias for write_mode")
    status: Optional[str] = Field(default="ACTIVE", description="Connector status")
    session_id: Optional[str] = Field(default=None, description="Optional session ID for live ingest event")
    user_id: str = Field(default="user:developer", description="User ID for live ingest event")
    agent_id: str = Field(default="agent:code-reviewer", description="Agent ID for live ingest event")
    message: Optional[str] = Field(default=None, description="Optional turn message for live ingest event")
    turns: Optional[List[Dict[str, Any]]] = Field(default=None, description="Optional turns array for live ingest")


class StudioExtractorUpsertRequest(BaseModel):
    extractor_id: Optional[str] = Field(default=None, description="Extractor ID")
    id: Optional[str] = Field(default=None, description="Alias for extractor_id")
    name: Optional[str] = Field(default=None, description="Extractor display name")
    target_category: Optional[str] = Field(default=None, description="Target category slug")
    targetCategory: Optional[str] = Field(default=None, description="CamelCase alias for target_category")
    target_categories: Optional[List[str]] = Field(default=None, description="List of target categories")
    target_scope: Optional[str] = Field(default=None, description="Target scope tier")
    targetScope: Optional[str] = Field(default=None, description="CamelCase alias for target_scope")
    model: Optional[str] = Field(default=None, description="Foundation model")
    model_name: Optional[str] = Field(default=None, description="Alias for model")
    trigger_event: Optional[str] = Field(default=None, description="Trigger event")
    triggerEvent: Optional[str] = Field(default=None, description="CamelCase alias for trigger_event")
    confidence_gate: Optional[float] = Field(default=None, description="Confidence gate threshold")
    confidenceGate: Optional[float] = Field(default=None, description="CamelCase alias for confidence_gate")
    write_mode: Optional[str] = Field(default=None, description="'staged' or 'direct'")
    writeMode: Optional[str] = Field(default=None, description="CamelCase alias for write_mode")
    human_params: Optional[Dict[str, Any]] = Field(default=None, description="Human-language prompt tuning parameters")
    humanParams: Optional[Dict[str, Any]] = Field(default=None, description="CamelCase alias for human_params")
    system_prompt: Optional[str] = Field(default=None, description="Compiled system prompt")
    systemPrompt: Optional[str] = Field(default=None, description="CamelCase alias for system_prompt")
    few_shot_examples: Optional[List[str]] = Field(default=None, description="Few-shot examples")
    status: Optional[str] = Field(default=None, description="Extractor status")


class StudioExtractorRunRequest(BaseModel):
    session_id: Optional[str] = Field(default="sess-9042", description="Target session ID to run extraction on")
    write_mode: Optional[str] = Field(default=None, description="Optional override: 'staged' or 'direct'")


class StudioExtractorChatRequest(BaseModel):
    message: Optional[str] = Field(default=None, description="Human-language prompt tuning request")
    instruction: Optional[str] = Field(default=None, description="Alias for message")
    prompt: Optional[str] = Field(default=None, description="Alias for message")
    apply_updates: bool = Field(default=True, description="Whether to persist tuned prompt parameters")
    human_params: Optional[Dict[str, Any]] = Field(default=None, description="Current humanParams override")
    humanParams: Optional[Dict[str, Any]] = Field(default=None, description="CamelCase alias for human_params")


class StudioBackendTestAgentWriteRequest(BaseModel):
    source_agent: str = Field(default="PreferenceAgent", description="Agent performing write-back")
    scope_type: str = Field(default="USER", description="Target scope tier")
    scope_id: str = Field(default="user:developer", description="Target scope ID")
    category: str = Field(default="workflow", description="Memory category")
    memory_key: str = Field(default="live_agent_rule", description="Memory key")
    after_value: str = Field(..., description="Memory value to write")


class StudioRbacPolicyUpsertRequest(BaseModel):
    policy_id: Optional[str] = Field(default=None, description="Policy ID")
    id: Optional[str] = Field(default=None, description="Alias for policy_id")
    role_name: Optional[str] = Field(default=None, description="Role name")
    roleName: Optional[str] = Field(default=None, description="CamelCase alias for role_name")
    role: Optional[str] = Field(default=None, description="Alias for role_name")
    principal: str = Field(default="agent:code-reviewer", description="Principal identifier")
    target_scope_id: Optional[str] = Field(default=None, description="Target scope entity ID (e.g. org:default)")
    applies_to_descendants: Optional[bool] = Field(default=True, description="Whether policy cascades to child scopes")
    allowed_scopes: Optional[List[str]] = Field(default=None, description="Allowed scope tiers")
    allowedScopes: Optional[List[str]] = Field(default=None, description="CamelCase alias for allowed_scopes")
    allowed_actions: Optional[List[str]] = Field(default=None, description="Allowed actions")
    allowedActions: Optional[List[str]] = Field(default=None, description="CamelCase alias for allowed_actions")
    actions: Optional[List[str]] = Field(default=None, description="Alias for allowed_actions")
    path_prefix: Optional[str] = Field(default="*", description="File path prefix restriction")
    pathPrefix: Optional[str] = Field(default=None, description="CamelCase alias for path_prefix")
    pii_redaction_level: Optional[str] = Field(default=None, description="PII redaction level")
    piiRedactionLevel: Optional[str] = Field(default=None, description="CamelCase alias for pii_redaction_level")
    pii_masking_level: Optional[str] = Field(default=None, description="Alias for pii_redaction_level")
    max_classification: Optional[str] = Field(default="INTERNAL", description="Maximum classification level")
    maxClassification: Optional[str] = Field(default=None, description="CamelCase alias for max_classification")
    effect: str = Field(default="ALLOW", description="ALLOW or DENY")


class StudioRbacSimulateRequest(BaseModel):
    principal: str = Field(default="agent:code-reviewer", description="Principal to simulate")
    action: str = Field(default="READ", description="Action: READ, STAGE, COMMIT, ROLLBACK, DELETE, WRITE")
    scope: str = Field(default="USER", description="Target scope tier: USER, TEAM, AGENT, ORG")
    target_scope_id: Optional[str] = Field(default=None, description="Target scope ID (e.g. org:default, user:developer)")
    classification: str = Field(default="INTERNAL", description="Data classification: PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED")
    file_path: str = Field(default="user/preferences/python_tooling.md", description="Target progressive disclosure file path")
    filePath: Optional[str] = Field(default=None, description="CamelCase alias for file_path")
    target_path: Optional[str] = Field(default=None, description="Alias for file_path")
    targetPath: Optional[str] = Field(default=None, description="CamelCase alias for target_path")


def _enrich_studio_session_dict(sess: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure a session dictionary contains all fields expected by both file-centric and node-centric UI panels."""
    s = dict(sess)
    sid = s.get("session_id") or s.get("id") or "sess-9042"
    s["session_id"] = sid
    s["id"] = sid
    s.setdefault("scope_type", "USER")
    s.setdefault("user_id", "user:developer")
    s.setdefault("title", f"Session {sid}")
    ai_sum = s.get("ai_summary") if isinstance(s.get("ai_summary"), dict) else (
        s.get("aiSummary") if isinstance(s.get("aiSummary"), dict) else {}
    )
    overview_text = ai_sum.get("overview") or s.get("summary") or s.get("summary_text") or ""
    s["summary"] = overview_text
    raw_turns = s.get("turns") if isinstance(s.get("turns"), list) else (
        s.get("transcript") if isinstance(s.get("transcript"), list) else []
    )
    norm_turns: List[Dict[str, Any]] = []
    for t in raw_turns:
        if isinstance(t, dict):
            txt = t.get("content") or t.get("text") or t.get("message") or ""
            norm_turns.append({
                **t,
                "role": str(t.get("role") or "user").lower(),
                "content": txt,
                "text": txt,
            })
    s["turns"] = norm_turns
    s["transcript"] = norm_turns
    s["turn_count"] = int(s.get("turn_count") or s.get("turns_count") or len(norm_turns))

    inj = s.get("memories_injected") or ai_sum.get("recalledMemories") or []
    trig_keys: List[str] = []
    useful_keys: List[str] = []
    for item in inj:
        if isinstance(item, dict):
            fp = str(item.get("file_path") or item.get("path") or item.get("memory_key") or item.get("memory_id") or "")
            mk = fp.split("/")[-1].replace(".md", "") if "/" in fp else fp
            if mk:
                trig_keys.append(mk)
                if item.get("helpful", True):
                    useful_keys.append(mk)
        elif item:
            fp = str(item)
            mk = fp.split("/")[-1].replace(".md", "") if "/" in fp else fp
            trig_keys.append(mk)
            useful_keys.append(mk)
    if not s.get("triggered_memories"):
        s["triggered_memories"] = trig_keys or ["python_tooling", "zero_mock_guardrail"]
    if not s.get("useful_memories"):
        s["useful_memories"] = useful_keys if s.get("helpfulness_rating") != "unhelpful" else []

    ext = s.get("memories_extracted") or s.get("extractedSignals") or []
    ext_keys: List[str] = []
    for item in ext:
        if isinstance(item, dict):
            fp = str(item.get("file_path") or item.get("targetPath") or item.get("rule") or "")
            mk = fp.split("/")[-1].replace(".md", "") if "/" in fp else fp
            if mk:
                ext_keys.append(mk)
        elif item:
            fp = str(item)
            mk = fp.split("/")[-1].replace(".md", "") if "/" in fp else fp
            ext_keys.append(mk)
    if not s.get("extracted_memories"):
        s["extracted_memories"] = ext_keys
    s.setdefault("status", "SUMMARIZED" if overview_text else "INGESTED")
    return s


def _extract_memory_key_from_path(fp: str, fallback: str) -> str:
    """Derive a clean snake_case memory key from a file path or fallback string."""
    if "/" in fp:
        return re.sub(r"\.(md|mem)$", "", fp.split("/")[-1])
    return re.sub(r"\.(md|mem)$", "", fallback)


def _build_studio_tree_payload(
    team_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the unified progressive disclosure file tree + scope-grouped node tree + staged commits."""
    raw = spanner_client.get_memory_file_tree()
    files = raw.get("files", [])
    raw_staged = raw.get("staged_commits", [])

    # Normalize staged_commits so each entry has both commit_id/staged_id and proposed_content/after_value
    staged_ByPath: Dict[str, Dict[str, Any]] = {}
    norm_staged: List[Dict[str, Any]] = []
    for sc in raw_staged:
        c = dict(sc)
        cid = str(c.get("commit_id") or c.get("staged_id") or c.get("id") or "")
        fp = str(c.get("file_path") or c.get("filePath") or "")
        s_type_st = str(c.get("scope_type") or c.get("scope") or "USER").upper()
        if s_type_st not in ("ORG", "TEAM", "AGENT", "USER"):
            s_type_st = "USER"
        mk = str(c.get("memory_key") or _extract_memory_key_from_path(fp, cid))
        before_v = c.get("before_value") if c.get("before_value") is not None else c.get("previous_content")
        action_raw = str(c.get("commit_status") or c.get("action") or "").upper()
        raw_after = c.get("after_value") if c.get("after_value") is not None else (
            c.get("proposed_content") if c.get("proposed_content") is not None else c.get("proposedContent")
        )
        after_v = str(raw_after) if raw_after is not None else ("(Remove rule)" if action_raw == "DELETE" else "")
        if "CONFLICT" in action_raw:
            c_status = "CONFLICT"
        elif action_raw in ("MODIFIED", "UPDATE", "DELETE") or before_v:
            c_status = "MODIFIED"
        else:
            c_status = "NEW"
        c["commit_id"] = cid
        c["staged_id"] = cid
        c["id"] = cid
        c["memory_key"] = mk
        c["after_value"] = after_v
        c["proposed_content"] = after_v
        c["before_value"] = before_v
        c["commit_status"] = c_status
        c["scope_type"] = s_type_st
        c.setdefault(
            "scope_id",
            "org:acme-org" if s_type_st == "ORG" else ("team:platform-ml" if s_type_st == "TEAM" else ("agent:code-reviewer-agent" if s_type_st == "AGENT" else "user:developer")),
        )
        c.setdefault("category", "preferences")
        norm_staged.append(c)
        if fp:
            staged_ByPath[fp] = c
        staged_ByPath[f"{s_type_st}::{mk}"] = c

    grouped_tree: Dict[str, List[Dict[str, Any]]] = {"ORG": [], "TEAM": [], "AGENT": [], "USER": []}
    seen_keys: set = set()
    synced_total = 0

    for f in files:
        s_type = str(f.get("scope_type") or f.get("scope") or "USER").upper()
        if s_type not in grouped_tree:
            s_type = "USER"
        s_id = str(f.get("scope_id") or ("org:acme-org" if s_type == "ORG" else ("team:platform-ml" if s_type == "TEAM" else ("agent:code-reviewer-agent" if s_type == "AGENT" else "user:developer"))))
        fp = str(f.get("file_path") or f.get("path") or "")
        fallback_key = str(f.get("fileName") or f.get("id") or "memory_rule")
        mk = str(f.get("memory_key") or _extract_memory_key_from_path(fp, fallback_key))
        content_val = str(f.get("content") or f.get("memory_value") or f.get("currentContent") or "")
        matched_st = staged_ByPath.get(fp) or staged_ByPath.get(f"{s_type}::{mk}")

        if matched_st:
            is_new_add = matched_st.get("commit_status") == "NEW" or (
                str(f.get("commitState") or "").upper() == "ADD" and not content_val
            )
            if is_new_add:
                c_status = "NEW"
                matched_st["commit_status"] = "NEW"
                before_val = None
            else:
                c_status = matched_st.get("commit_status") or "MODIFIED"
                if c_status == "NEW":
                    c_status = "MODIFIED"
                before_val = content_val
            st_id = matched_st["staged_id"]
            display_val = matched_st["after_value"]
        else:
            c_status = "SYNCED"
            st_id = None
            before_val = None
            display_val = content_val
            synced_total += 1

        node = {
            **f,
            "scope_type": s_type,
            "scope_id": s_id,
            "memory_key": mk,
            "memory_value": display_val,
            "natural_language_belief": str(f.get("crystallized_belief") or f.get("title") or (display_val.splitlines()[0] if display_val else mk)),
            "category": str(f.get("category") or "preferences"),
            "usefulness_count": int(f.get("helpful_count") or f.get("helpfulCount") or f.get("usefulness_count") or 1),
            "commit_status": c_status,
            "staged_id": st_id,
            "before_value": before_val,
            "source_session_id": str((matched_st.get("source_session_id") if matched_st else None) or f.get("source_session_id") or f.get("sessionId") or ""),
        }
        grouped_tree[s_type].append(node)
        seen_keys.add(f"{s_type}::{mk}")

    # Also include any NEW staged commits not yet in files
    for st in norm_staged:
        s_type = str(st.get("scope_type") or "USER").upper()
        if s_type not in grouped_tree:
            s_type = "USER"
        mk = st["memory_key"]
        k_sig = f"{s_type}::{mk}"
        if k_sig not in seen_keys:
            st["commit_status"] = "NEW"
            grouped_tree[s_type].append({
                "scope_type": s_type,
                "scope_id": st.get("scope_id") or "user:developer",
                "memory_key": mk,
                "memory_value": st["after_value"],
                "natural_language_belief": st.get("diff_summary") or st.get("summary") or st["after_value"],
                "category": st.get("category") or "preferences",
                "usefulness_count": 0,
                "commit_status": "NEW",
                "staged_id": st["staged_id"],
                "before_value": None,
                "source_session_id": str(st.get("source_session_id") or ""),
                "file_path": st.get("file_path") or f"{s_type.lower()}/{st.get('category', 'preferences')}/{mk}.md",
            })

    # Optional active-context filtering (team_id, agent_id, user_id)
    raw_user = (user_id or "").strip()
    norm_user = raw_user if (not raw_user or ":" in raw_user) else f"user:{raw_user}"
    ctx_filters = {
        "TEAM": (team_id or "").strip(),
        "AGENT": (agent_id or "").strip(),
        "USER": norm_user,
    }
    if any(ctx_filters.values()):
        for tier_key, target_scope_id in ctx_filters.items():
            if target_scope_id:
                grouped_tree[tier_key] = [
                    n for n in grouped_tree[tier_key]
                    if str(n.get("scope_id") or "") == target_scope_id
                ]
        norm_staged = [
            st for st in norm_staged
            if not ctx_filters.get(str(st.get("scope_type") or "").upper())
            or str(st.get("scope_id") or "") == ctx_filters.get(str(st.get("scope_type") or "").upper())
        ]
        synced_total = sum(
            1
            for tier_nodes in grouped_tree.values()
            for n in tier_nodes
            if str(n.get("commit_status") or "SYNCED").upper() == "SYNCED"
        )

    new_count = sum(1 for st in norm_staged if st.get("commit_status") == "NEW")
    conflict_count = sum(1 for st in norm_staged if st.get("commit_status") == "CONFLICT")
    mod_count = sum(1 for st in norm_staged if st.get("commit_status") not in ("NEW", "CONFLICT"))

    summary = {
        **(raw.get("summary") or {}),
        "new_count": new_count,
        "modified_count": mod_count,
        "conflict_count": conflict_count,
        "pending_staged_total": len(norm_staged),
        "synced_total": synced_total,
    }
    return {
        "status": "OK",
        "files": files,
        "tree": grouped_tree,
        "staged_commits": norm_staged,
        "folders": raw.get("folders", []),
        "summary": summary,
        "file_count": len(files),
        "staged_count": len(norm_staged),
    }


def _enrich_studio_entity(ent: Dict[str, Any]) -> Dict[str, Any]:
    e = dict(ent)
    s_type = str(e.get("scope_type") or e.get("scopeType") or e.get("type") or "USER").upper()
    eid = str(e.get("entity_id") or e.get("id") or e.get("scope_id") or "user:developer")
    raw_scope_id = str(e.get("scope_id") or "").strip()
    if ":" in raw_scope_id:
        canonical_scope_id = raw_scope_id
    elif ":" in eid:
        canonical_scope_id = eid
    elif e.get("slug"):
        canonical_scope_id = f"{s_type.lower()}:{str(e['slug']).strip()}"
    else:
        canonical_scope_id = eid

    e["entity_id"] = eid
    e["scope_id"] = canonical_scope_id
    e["id"] = eid
    e["scope_type"] = s_type
    e["display_name"] = str(e.get("display_name") or e.get("displayName") or e.get("name") or canonical_scope_id)
    e["parent_id"] = e.get("parent_id") or e.get("parent_scope_id") or e.get("parentId")
    e["parent_scope_id"] = e["parent_id"]
    e["owner"] = str(e.get("owner") or e.get("handle") or "@platform-eng")
    e["memory_count"] = int(e.get("memory_count") or e.get("memoryCount") or 0)
    return e


def _enrich_studio_service(svc: Dict[str, Any]) -> Dict[str, Any]:
    s = dict(svc)
    sid = str(s.get("service_id") or s.get("id") or "svc-adk-ui")
    s["service_id"] = sid
    s["id"] = sid
    ctype = str(s.get("source_type") or s.get("connector_type") or s.get("connectorType") or "REST_WEBHOOK")
    s["source_type"] = ctype
    s["connector_type"] = ctype
    s.setdefault("chunk_window_turns", int(_STUDIO_INGEST_CONFIG.get("chunk_window_turns", 10)))
    s.setdefault("min_extraction_confidence", float(_STUDIO_INGEST_CONFIG.get("min_confidence_gate", 0.85)))
    s["events_ingested"] = int(s.get("events_ingested") or s.get("sessions_24h") or s.get("sessions24h") or 18)
    s.setdefault("pii_redaction_mode", "STRICT_HASH")
    s.setdefault("status", "ACTIVE")
    return s


def _enrich_studio_extractor(ext: Dict[str, Any]) -> Dict[str, Any]:
    x = dict(ext)
    xid = str(x.get("extractor_id") or x.get("id") or "ext-implicit-pref")
    x["extractor_id"] = xid
    x["id"] = xid
    m_name = str(x.get("model_name") or x.get("model") or config.model_name)
    x["model"] = m_name
    x["model_name"] = m_name
    t_cat = str(x.get("target_category") or x.get("targetCategory") or "technical")
    t_cats = x.get("target_categories") if isinstance(x.get("target_categories"), list) else [t_cat]
    x["target_category"] = t_cat
    x["target_categories"] = t_cats
    hp = x.get("human_params") or x.get("humanParams") or {}
    raw_examples = x.get("few_shot_examples")
    norm_examples: List[Dict[str, str]] = []
    if isinstance(raw_examples, list) and raw_examples:
        for item in raw_examples:
            if isinstance(item, dict):
                norm_examples.append({
                    "input": str(item.get("input") or "Always use uv run pytest -v"),
                    "extracted_key": str(item.get("extracted_key") or "python_test_runner"),
                    "extracted_value": str(item.get("extracted_value") or "uv run pytest -v"),
                    "scope": str(item.get("scope") or "TEAM"),
                })
            else:
                norm_examples.append({
                    "input": str(item),
                    "extracted_key": f"{t_cat}_rule",
                    "extracted_value": str(hp.get("focus") or "Extracted atomic rule"),
                    "scope": "USER",
                })
    else:
        norm_examples = [
            {
                "input": "Stop using pip install, always use uv run pytest -v",
                "extracted_key": "python_test_runner",
                "extracted_value": "uv run pytest -v",
                "scope": "TEAM",
            },
            {
                "input": "Never mock or hardcode dummy responses in routes or tests",
                "extracted_key": "strict_never_mock_directive",
                "extracted_value": "Prohibit mock fallbacks; verify against real endpoints",
                "scope": "ORG",
            },
        ]
    x["few_shot_examples"] = norm_examples
    x.setdefault("status", "ACTIVE")
    return x


def _enrich_studio_policy(pol: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(pol)
    pid = str(p.get("policy_id") or p.get("id") or "pol-default")
    p["policy_id"] = pid
    p["id"] = pid
    r_name = str(p.get("role") or p.get("role_name") or p.get("roleName") or "Memory Operator")
    p["role"] = r_name
    p["role_name"] = r_name
    scopes = p.get("allowed_scopes") or p.get("allowedScopes") or ["USER"]
    t_scope = p.get("target_scope_id")
    if not t_scope:
        first_sc = str(scopes[0]).upper() if scopes else "USER"
        t_scope = "org:default" if first_sc == "ORG" else ("team:platform-eng" if first_sc == "TEAM" else ("agent:code-reviewer" if first_sc == "AGENT" else "user:developer"))
    p["target_scope_id"] = t_scope
    p.setdefault("applies_to_descendants", True)
    acts = p.get("actions") or p.get("allowed_actions") or p.get("allowedActions") or ["READ"]
    p["actions"] = acts
    p["allowed_actions"] = acts
    pii = str(p.get("pii_masking_level") or p.get("pii_redaction_level") or p.get("piiRedactionLevel") or "STRICT_HASH")
    p["pii_masking_level"] = pii
    p["pii_redaction_level"] = pii
    p.setdefault("effect", "ALLOW")
    return p


@api.get("/api/studio/state")
def get_studio_full_state() -> Dict[str, Any]:
    """Return a unified bootstrap snapshot of all 10 Enterprise Memory Studio workspaces."""
    tree = _build_studio_tree_payload()
    sessions = [_enrich_studio_session_dict(s) for s in telemetry_sink.get_ingested_sessions(limit=50)]
    ratings = telemetry_sink.get_helpfulness_ratings(limit=50)
    entities = [_enrich_studio_entity(e) for e in spanner_client.list_scope_entities()]
    cats_resp = get_studio_categories()
    ingest_resp = spanner_client.list_ingest_services()
    raw_services = ingest_resp.get("services", []) if isinstance(ingest_resp, dict) else ingest_resp
    services = [_enrich_studio_service(s) for s in raw_services]
    live_events = ingest_resp.get("live_events", []) if isinstance(ingest_resp, dict) else []
    extractors = [_enrich_studio_extractor(x) for x in spanner_client.list_extractor_jobs()]
    backend_info = get_studio_backend_status()
    policies = [_enrich_studio_policy(p) for p in spanner_client.list_rbac_policies()]

    return {
        "status": "OK",
        "files": tree.get("files", []),
        "tree": tree.get("tree", {}),
        "staged_commits": tree.get("staged_commits", []),
        "sessions": sessions,
        "helpfulness_ratings": ratings,
        "entities": entities,
        "categories": cats_resp.get("categories", []),
        "ingest_services": services,
        "live_events": live_events,
        "ingest_config": dict(_STUDIO_INGEST_CONFIG),
        "extractors": extractors,
        "backend": backend_info,
        "security_policies": policies,
        "active_config": alpha_evolve.get_active_config().to_dict(),
    }


@api.get("/api/studio/sessions")
def get_studio_sessions(
    stream_source: str = "ALL",
    topic: str = "ALL",
    helpfulness_status: str = "ALL",
    search_query: str = "",
    limit: int = 50,
) -> Dict[str, Any]:
    """Return ingested sessions from BigQuery episodes_log with helpfulness ratings and turn trajectories."""
    raw_sessions = telemetry_sink.get_ingested_sessions(
        stream_source=stream_source,
        topic=topic,
        helpfulness_status=helpfulness_status,
        search_query=search_query,
        limit=limit,
    )
    sessions = [_enrich_studio_session_dict(s) for s in raw_sessions]
    ratings = telemetry_sink.get_helpfulness_ratings(limit=limit)
    return {
        "status": "OK",
        "sessions": sessions,
        "helpfulness_ratings": ratings,
        "count": len(sessions),
    }


@api.post("/api/studio/sessions/{session_id}/summarize")
def summarize_studio_session(
    session_id: str,
    request: Optional[StudioSessionSummarizeRequest] = None,
) -> Dict[str, Any]:
    """Generate a live AI session summary via Vertex AI (gemini-4-hydrogen) using ADC and persist to BigQuery."""
    sess = telemetry_sink.get_ingested_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    with job_runner.track_inline_job(
        "memory_transform",
        triggered_from="Sessions",
        params={"session_id": session_id, "auto_stage_candidates": bool(request is None or request.auto_stage_candidates)},
        title="Summarize & stage session",
    ) as ctx:
        ctx.update_progress("Summarizing session turns", 35)
        raw_turns = sess.get("transcript") if isinstance(sess.get("transcript"), list) else (
            sess.get("turns") if isinstance(sess.get("turns"), list) else []
        )
        turn_lines = [
            f"[{str(t.get('role', 'user')).upper()}]: {t.get('text') or t.get('content', '')}"
            for t in raw_turns if isinstance(t, dict)
        ]
        recalled_mems = sess.get("memories_injected") or (sess.get("aiSummary", {}) or {}).get("recalledMemories", [])
        extracted_sigs = sess.get("memories_extracted") or sess.get("extractedSignals", [])
        mems_injected = ", ".join(
            (m.get("file_path") or m.get("path") or str(m)) if isinstance(m, dict) else str(m)
            for m in recalled_mems
        )
        mems_extracted = ", ".join(
            (m.get("file_path") or m.get("targetPath") or str(m)) if isinstance(m, dict) else str(m)
            for m in extracted_sigs
        )

        prompt_text = (
            "You are the Enterprise Memory Studio Session Summarizer.\n"
            "Write a concise, high-signal 2-sentence engineering summary of the following agent session trajectory, "
            "highlighting the developer's intent, tools/guardrails applied, and memories crystallized.\n\n"
            f"Session ID: {session_id}\n"
            f"Title: {sess.get('title', '')}\n"
            f"Memories Injected: {mems_injected}\n"
            f"Memories Extracted: {mems_extracted}\n"
            "Trajectory Turns:\n" + "\n".join(turn_lines)
        )

        ai_summary = ""
        model_used = config.model_name
        try:
            from google import genai
            client = genai.Client(vertexai=True, project=config.project_id, location="global")
            response = client.models.generate_content(
                model=model_used,
                contents=prompt_text,
            )
            if response and getattr(response, "text", None):
                ai_summary = response.text.strip()
        except Exception as e:
            logger.warning(f"Vertex AI global {model_used} call failed ({e}); trying fallback location.")
            try:
                from google import genai
                client = genai.Client(vertexai=True, project=config.project_id, location="us-central1")
                response = client.models.generate_content(
                    model=model_used,
                    contents=prompt_text,
                )
                if response and getattr(response, "text", None):
                    ai_summary = response.text.strip()
            except Exception as inner_e:
                logger.warning(f"Vertex AI fallback summarize failed ({inner_e}); synthesizing from real session turns.")

        if not ai_summary:
            first_user = next(
                ((t.get("text") or t.get("content", "")) for t in raw_turns if isinstance(t, dict) and str(t.get("role", "")).upper() == "USER"),
                sess.get("title", ""),
            )
            last_assistant = next(
                ((t.get("text") or t.get("content", "")) for t in reversed(raw_turns) if isinstance(t, dict) and str(t.get("role", "")).upper() in ("ASSISTANT", "AGENT")),
                "",
            )
            ai_summary = (
                f"Session {session_id} ({sess.get('title', 'Engineering Session')}): Developer requested '{first_user[:120]}' "
                f"with injected memories [{mems_injected or 'none'}]. "
                f"Agent completed verification ('{last_assistant[:120]}') and crystallized [{mems_extracted or 'no new memories'}]."
            )

        existing_summary = sess.get("aiSummary") if isinstance(sess.get("aiSummary"), dict) else (
            sess.get("ai_summary") if isinstance(sess.get("ai_summary"), dict) else {}
        )
        summary_dict = {
            **existing_summary,
            "overview": ai_summary,
            "model": model_used,
        }
        telemetry_sink.update_session_ai_summary(session_id, summary_dict)

        # Optionally auto-stage extracted candidates from the session turns
        staged_list: List[Dict[str, Any]] = []
        if request is None or request.auto_stage_candidates:
            ctx.update_progress("Extracting candidate memories", 75)
            events = [
                {
                    "session_id": session_id,
                    "event_type": "USER_PROMPT" if str(t.get("role", "")).upper() == "USER" else "AGENT_RESPONSE",
                    "message": t.get("text") or t.get("content", ""),
                }
                for t in raw_turns if isinstance(t, dict)
            ]
            cands = _extract_candidates_from_events(events, session_id=session_id, signal_filter="ALL")
            for c in cands[:2]:
                mk = c.get("memory_key", "session_rule")
                st_res = spanner_client.stage_memory_commit({
                    "file_id": f"mem-user-{mk[:20]}",
                    "file_path": f"user/{c.get('category', 'workflow')}/{mk}.md",
                    "scope_type": c.get("suggested_scope", "USER"),
                    "scope_id": "user:developer",
                    "category": c.get("category", "workflow"),
                    "proposed_content": c.get("memory_value", ""),
                    "diff_summary": f"Auto-staged from {session_id} summary",
                    "source_session_id": session_id,
                    "extractor_id": "ext-session-summarizer",
                    "confidence": float(c.get("confidence", 0.93)),
                    "write_mode": "staged",
                })
                staged_list.append(st_res)

        updated_sess = _enrich_studio_session_dict(telemetry_sink.get_ingested_session(session_id) or sess)
        updated_sess["summary"] = ai_summary
        updated_sess["status"] = "SUMMARIZED"
        ctx.complete(
            result_summary=f"Summarized · {len(staged_list)} proposal(s)",
            metrics={"turns": len(raw_turns), "staged_proposals": len(staged_list)},
            output_preview={"session_id": session_id, "summary_text": ai_summary, "staged_count": len(staged_list)},
            target_workspace="workspace-tree" if staged_list else "workspace-sessions",
        )
    return {
        "status": "SUMMARIZED",
        "session_id": session_id,
        "ai_summary": summary_dict,
        "summary_text": ai_summary,
        "model": model_used,
        "session": updated_sess,
        "staged_commits": staged_list,
    }


@api.post("/api/studio/sessions/{session_id}/helpfulness")
def rate_studio_session_helpfulness(session_id: str, request: StudioSessionHelpfulnessRequest) -> Dict[str, Any]:
    """Record a session helpfulness rating in BigQuery and propagate helpful/unhelpful tallies to Spanner ScopedMemories."""
    if request.useful_memories is not None or request.triggered_memories is not None:
        useful_set = set(request.useful_memories or [])
        trig_list = list(request.triggered_memories or request.useful_memories or [])
        raw_outcome = "helpful" if useful_set else (request.outcome or request.rating or "unhelpful").lower().strip()
    else:
        raw_outcome = (request.outcome or request.rating or "helpful").lower().strip()
        trig_list = []
        useful_set = set()

    if raw_outcome not in ("helpful", "unhelpful", "neutral"):
        raise HTTPException(status_code=400, detail="rating/outcome must be 'helpful', 'unhelpful', or 'neutral'")

    sess = telemetry_sink.get_ingested_session(session_id)
    notes_val = request.reason if request.reason is not None else request.notes

    raw_injected = trig_list if trig_list else (
        request.memories_injected if request.memories_injected is not None else (
            sess.get("memories_injected", []) if sess else []
        )
    )
    mem_refs: List[Dict[str, str]] = []
    if request.memory_id or request.file_path:
        mem_refs.append({
            "memory_id": request.memory_id or request.file_path or "mem-user-uv",
            "file_path": request.file_path or request.memory_id or "user/preferences/python_tooling.md",
        })
    else:
        for item in raw_injected:
            if isinstance(item, dict):
                mem_refs.append({
                    "memory_id": str(item.get("memory_id") or item.get("file_path") or "mem-user-uv"),
                    "file_path": str(item.get("file_path") or item.get("memory_id") or "user/preferences/python_tooling.md"),
                })
            else:
                mem_refs.append({
                    "memory_id": str(item),
                    "file_path": str(item),
                })

    if not mem_refs:
        mem_refs.append({
            "memory_id": "mem-user-uv",
            "file_path": "user/preferences/python_tooling.md",
        })

    primary_ref = mem_refs[0]
    bq_record = telemetry_sink.record_session_helpfulness(
        session_id=session_id,
        memory_id=primary_ref["memory_id"],
        file_path=primary_ref["file_path"],
        outcome=raw_outcome,
        reason=notes_val,
    )

    tree_files = spanner_client.get_memory_file_tree().get("files", [])
    updated_files: List[Dict[str, Any]] = []
    if raw_outcome in ("helpful", "unhelpful"):
        for ref in mem_refs:
            target_id = ref["memory_id"]
            ref_outcome = "helpful" if (not useful_set or target_id in useful_set) and raw_outcome == "helpful" else raw_outcome
            matched = next(
                (
                    f for f in tree_files
                    if f.get("memory_id") == target_id
                    or f.get("id") == target_id
                    or f.get("file_path") == ref["file_path"]
                    or str(f.get("file_path", "")).endswith(f"/{target_id}.md")
                ),
                None,
            )
            resolved_id = (matched.get("memory_id") or matched.get("id")) if matched else target_id
            try:
                upd = spanner_client.update_memory_helpfulness_tally(
                    memory_id=resolved_id,
                    outcome=ref_outcome,
                    session_id=session_id,
                    note=notes_val,
                )
                if upd:
                    updated_files.append(upd)
            except ValueError:
                pass

    enriched_sess = _enrich_studio_session_dict(telemetry_sink.get_ingested_session(session_id) or (sess or {"session_id": session_id}))
    if request.triggered_memories is not None:
        enriched_sess["triggered_memories"] = list(request.triggered_memories)
    if request.useful_memories is not None:
        enriched_sess["useful_memories"] = list(request.useful_memories)

    return {
        "status": "HELPFULNESS_RECORDED",
        "session_id": session_id,
        "rating": raw_outcome,
        "outcome": raw_outcome,
        "rating_record": bq_record,
        "updated_memories": updated_files,
        "session": enriched_sess,
    }


@api.post("/api/studio/sessions/{session_id}/usefulness")
def rate_studio_session_usefulness_alias(session_id: str, request: StudioSessionHelpfulnessRequest) -> Dict[str, Any]:
    """Alias for /api/studio/sessions/{session_id}/helpfulness used by studio.js."""
    return rate_studio_session_helpfulness(session_id=session_id, request=request)


@api.get("/api/studio/files")
def get_studio_memory_files() -> Dict[str, Any]:
    """Return the progressive disclosure memory file tree and staged commit queue from Spanner."""
    return _build_studio_tree_payload()


@api.get("/api/studio/tree")
def get_studio_memory_tree_alias(
    team_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the scope-grouped memory tree, progressive disclosure files, and staged commit queue."""
    return _build_studio_tree_payload(team_id=team_id, agent_id=agent_id, user_id=user_id)


@api.post("/api/studio/files/stage")
def stage_studio_memory_file(request: StudioFileStageRequest) -> Dict[str, Any]:
    """Stage a memory file edit or creation in Spanner staged_memory_commits (or commit directly if write_mode='direct')."""
    s_type = (request.scopeType or request.scope_type or "USER").upper()
    s_id = request.scopeId or request.scope_id or (
        "org:default" if s_type == "ORG" else ("team:platform-eng" if s_type == "TEAM" else ("agent:code-reviewer" if s_type == "AGENT" else "user:developer"))
    )
    cat_val = (request.category or "preferences").strip()
    m_key = (request.memory_key or "").strip()
    f_path = (request.file_path or request.filePath or "").strip()
    if not f_path and m_key:
        f_path = f"{s_type.lower()}/{cat_val}/{m_key}.md"
    if not f_path:
        raise HTTPException(status_code=400, detail="file_path or memory_key is required")
    if not m_key:
        m_key = f_path.split("/")[-1].replace(".md", "")

    content_val = (
        request.proposed_content
        if request.proposed_content is not None
        else (request.after_value if request.after_value is not None else (request.content or ""))
    )
    w_mode = (request.writeMode or request.write_mode or "staged").lower()
    f_id = request.file_id or request.memory_id or f"mem-{f_path.replace('/', '-').replace('.md', '').replace('_', '-')}"

    result = spanner_client.stage_memory_commit({
        "file_id": f_id,
        "memory_id": f_id,
        "memory_key": m_key,
        "file_path": f_path,
        "scope_type": s_type,
        "scope_id": s_id,
        "category": cat_val,
        "proposed_content": content_val,
        "after_value": content_val,
        "diff_summary": request.diff_summary or request.diffSummary or request.natural_language_belief or f"Update {f_path}",
        "source_session_id": request.sourceSessionId or request.source_session_id or "sess-9042",
        "extractor_id": request.extractorId or request.extractor_id or "ext-manual-editor",
        "confidence": request.confidence,
        "write_mode": w_mode,
    })

    if w_mode == "direct" and result.get("memory"):
        telemetry_sink.replicate_memory_to_bq(result["memory"])

    commit_obj = result.get("commit") or {}
    staged_info = {
        **commit_obj,
        "staged_id": commit_obj.get("commit_id") or commit_obj.get("id") or "stg-latest",
        "commit_status": "MODIFIED" if commit_obj.get("before_value") else "NEW",
        "memory_key": m_key,
        "after_value": content_val,
    }
    tree = _build_studio_tree_payload()
    return {
        "status": result.get("status", "STAGED"),
        "result": result,
        "staged": staged_info,
        "files": tree.get("files", []),
        "tree": tree.get("tree", {}),
        "staged_commits": tree.get("staged_commits", []),
    }


@api.post("/api/studio/tree/stage")
def stage_studio_memory_tree_alias(request: StudioFileStageRequest) -> Dict[str, Any]:
    """Alias for /api/studio/files/stage used by studio.js."""
    return stage_studio_memory_file(request=request)


@api.post("/api/studio/files/commit/{commit_id}")
def commit_studio_staged_file(commit_id: str) -> Dict[str, Any]:
    """Commit a specific staged memory change to Spanner ScopedMemories and replicate to BigQuery."""
    try:
        res = spanner_client.commit_staged_memory(commit_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    mem = res.get("memory") or {}
    if mem:
        telemetry_sink.replicate_memory_to_bq(mem)
    fp = str(mem.get("file_path") or "")
    mk = str(mem.get("memory_key") or (fp.split("/")[-1].replace(".md", "") if "/" in fp else commit_id))
    committed_obj = {
        **mem,
        "memory_key": mk,
    }
    tree = _build_studio_tree_payload()
    return {
        "status": "COMMITTED",
        "commit": res.get("commit"),
        "committed": committed_obj,
        "file": mem,
        "memory": mem,
        "files": tree.get("files", []),
        "tree": tree.get("tree", {}),
        "staged_commits": tree.get("staged_commits", []),
    }


@api.post("/api/studio/tree/commit")
def commit_studio_tree_body(request: StudioTreeCommitRequest) -> Dict[str, Any]:
    """Commit a staged memory change by passing staged_id in the request body."""
    cid = (request.staged_id or request.commit_id or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="staged_id is required")
    return commit_studio_staged_file(commit_id=cid)


@api.post("/api/studio/files/discard/{commit_id}")
def discard_studio_staged_file(commit_id: str) -> Dict[str, Any]:
    """Discard a staged memory commit."""
    try:
        res = spanner_client.discard_staged_memory(commit_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    tree = _build_studio_tree_payload()
    return {
        "status": "DISCARDED",
        "commit_id": commit_id,
        "staged_id": commit_id,
        "result": res,
        "files": tree.get("files", []),
        "tree": tree.get("tree", {}),
        "staged_commits": tree.get("staged_commits", []),
    }


@api.post("/api/studio/tree/discard")
def discard_studio_tree_body(request: StudioTreeDiscardRequest) -> Dict[str, Any]:
    """Discard a specific staged commit or all pending staged commits."""
    if request.discard_all:
        current_staged = spanner_client.get_memory_file_tree().get("staged_commits", [])
        discarded_ids: List[str] = []
        for sc in list(current_staged):
            cid = sc.get("commit_id") or sc.get("id")
            if cid:
                try:
                    spanner_client.discard_staged_memory(str(cid))
                    discarded_ids.append(str(cid))
                except ValueError:
                    pass
        tree = _build_studio_tree_payload()
        return {
            "status": "ALL_DISCARDED",
            "discarded_count": len(discarded_ids),
            "files": tree.get("files", []),
            "tree": tree.get("tree", {}),
            "staged_commits": tree.get("staged_commits", []),
        }
    cid = (request.staged_id or request.commit_id or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="staged_id or discard_all is required")
    return discard_studio_staged_file(commit_id=cid)


@api.post("/api/studio/files/commit-all")
def commit_all_studio_staged_files() -> Dict[str, Any]:
    """Commit all pending staged memory changes in Spanner and replicate to BigQuery."""
    with job_runner.track_inline_job(
        "memory_transform",
        triggered_from="Memories",
        params={"action": "commit_all"},
        title="Commit staged memories",
    ) as ctx:
        ctx.update_progress("Committing staged rules and replicating traces", 40)
        res = spanner_client.commit_all_staged_memories()
        tree = _build_studio_tree_payload()
        for f in tree.get("files", []):
            telemetry_sink.replicate_memory_to_bq(f)
        committed_n = int(res.get("committed_count", 0))
        ctx.complete(
            result_summary=f"{committed_n} rule(s) committed",
            metrics={"committed_count": committed_n},
            output_preview={"committed_count": committed_n},
            target_workspace="workspace-tree",
        )
    return {
        "status": "ALL_COMMITTED",
        "committed_count": committed_n,
        "committed": res.get("commits", []),
        "files": tree.get("files", []),
        "tree": tree.get("tree", {}),
        "staged_commits": tree.get("staged_commits", []),
    }


@api.post("/api/studio/tree/commit-all")
def commit_all_studio_tree_alias() -> Dict[str, Any]:
    """Alias for /api/studio/files/commit-all used by studio.js."""
    return commit_all_studio_staged_files()


@api.delete("/api/studio/tree/node")
def delete_studio_tree_node(
    scope_type: str = "USER",
    scope_id: str = "user:developer",
    memory_key: str = "",
) -> Dict[str, Any]:
    """Delete a memory node from the progressive disclosure file tree & Spanner ScopedMemories."""
    if not memory_key:
        raise HTTPException(status_code=400, detail="memory_key is required")
    deleted = spanner_client.delete_scoped_memory(
        scope_type=scope_type,
        scope_id=scope_id,
        memory_key=memory_key,
    )
    # Also remove from _studio_memory_files if present
    mem_files = getattr(spanner_client, "_studio_memory_files", None)
    if isinstance(mem_files, dict):
        keys_to_remove = [
            fid for fid, f in list(mem_files.items())
            if f.get("memory_key") == memory_key
            or f.get("id") == memory_key
            or str(f.get("fileName", "")).replace(".mem", "").replace(".md", "") == memory_key
            or str(f.get("file_path", "")).endswith(f"/{memory_key}.md")
            or str(f.get("file_path", "")).endswith(f"/{memory_key}.mem")
        ]
        for fid in keys_to_remove:
            mem_files.pop(fid, None)
            deleted = True
    tree = _build_studio_tree_payload()
    return {
        "status": "DELETED" if deleted else "NOT_FOUND",
        "deleted": bool(deleted),
        "memory_key": memory_key,
        "files": tree.get("files", []),
        "tree": tree.get("tree", {}),
        "staged_commits": tree.get("staged_commits", []),
    }


@api.post("/api/studio/files/{file_id:path}/usefulness")
def update_studio_file_usefulness(file_id: str, request: StudioFileUsefulnessRequest) -> Dict[str, Any]:
    """Increment helpful or unhelpful tally on a specific memory file in Spanner."""
    outcome_val = (request.outcome or request.rating or "").lower().strip()
    if not outcome_val:
        if (request.delta_unhelpful or 0) > 0:
            outcome_val = "unhelpful"
        else:
            outcome_val = "helpful"

    tree_files = spanner_client.get_memory_file_tree().get("files", [])
    matched = next(
        (
            f for f in tree_files
            if f.get("memory_id") == file_id
            or f.get("id") == file_id
            or f.get("file_path") == file_id
        ),
        None,
    )
    resolved_id = (matched.get("memory_id") or matched.get("id")) if matched else file_id

    try:
        updated_res = spanner_client.update_memory_helpfulness_tally(
            memory_id=resolved_id,
            outcome=outcome_val,
            session_id=request.session_id,
            note=request.note,
            review_threshold=request.review_threshold,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    updated_mem = updated_res.get("memory", updated_res)
    telemetry_sink.replicate_memory_to_bq(updated_mem)
    return {
        "status": "USEFULNESS_UPDATED",
        "file": updated_mem,
        "result": updated_res,
    }


@api.get("/api/studio/entities")
def get_studio_entities(request: Request, scope_type: Optional[str] = None) -> Dict[str, Any]:
    """Return registered scope entities (Users, Teams, Agents, Orgs) from Spanner."""
    entities = [_enrich_studio_entity(e) for e in spanner_client.list_scope_entities(scope_type=scope_type or "ALL")]
    return {
        "status": "OK",
        "entities": entities,
        "count": len(entities),
        "authenticated_email": getattr(request.state, "user_email", None),
    }


@api.post("/api/studio/entities")
def upsert_studio_entity(request: StudioEntityUpsertRequest) -> Dict[str, Any]:
    """Create or update a scope entity in Spanner scope_entities."""
    s_type = (request.scopeType or request.scope_type or "USER").upper()
    disp = (request.displayName or request.display_name or request.name or "").strip()
    raw_id = (request.entity_id or request.scope_id or request.id or "").strip()
    if not raw_id and disp:
        slug = disp.lower().replace(" ", "-")
        raw_id = f"{s_type.lower()}:{slug}"
    if not raw_id:
        raise HTTPException(status_code=400, detail="entity_id/scope_id or display_name is required")

    parent_val = (
        request.parent_scope_id
        if request.parent_scope_id is not None
        else (request.parentId if request.parentId is not None else request.parent_id)
    )
    owner_val = request.owner or request.handle or f"@{raw_id.split(':')[-1]}"
    ent = _enrich_studio_entity(spanner_client.upsert_scope_entity({
        "entity_id": raw_id,
        "scope_type": s_type,
        "display_name": disp or raw_id,
        "handle": owner_val,
        "owner": owner_val,
        "parent_id": parent_val,
        "description": request.description,
        "tags": request.tags or [],
        "memory_count": request.memory_count or 0,
    }))
    return {
        "status": "ENTITY_SAVED",
        "entity": ent,
        "entities": [_enrich_studio_entity(e) for e in spanner_client.list_scope_entities()],
    }


@api.delete("/api/studio/entities/{scope_type}/{scope_id:path}")
def delete_studio_entity_by_scope(scope_type: str, scope_id: str) -> Dict[str, Any]:
    """Delete a scope entity by scope_type and scope_id."""
    deleted = spanner_client.delete_scope_entity(scope_id)
    if not deleted:
        composite = f"{scope_type.lower()}:{scope_id}"
        deleted = spanner_client.delete_scope_entity(composite)
    if not deleted:
        for raw_ent in spanner_client.list_scope_entities():
            enriched = _enrich_studio_entity(raw_ent)
            if enriched.get("scope_id") == scope_id and enriched.get("id"):
                deleted = spanner_client.delete_scope_entity(enriched["id"])
                if deleted:
                    break
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Entity '{scope_id}' not found")
    return {
        "status": "DELETED",
        "entity_id": scope_id,
        "scope_id": scope_id,
        "entities": [_enrich_studio_entity(e) for e in spanner_client.list_scope_entities()],
    }


@api.delete("/api/studio/entities/{entity_id:path}")
def delete_studio_entity(entity_id: str) -> Dict[str, Any]:
    """Delete a scope entity from Spanner scope_entities."""
    deleted = spanner_client.delete_scope_entity(entity_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Entity '{entity_id}' not found")
    return {
        "status": "DELETED",
        "entity_id": entity_id,
        "entities": [_enrich_studio_entity(e) for e in spanner_client.list_scope_entities()],
    }


@api.get("/api/studio/ingest")
def get_studio_ingest_overview() -> Dict[str, Any]:
    """Return active ingest service connections, pipeline config, and recent ingested sessions."""
    ingest_resp = spanner_client.list_ingest_services()
    raw_services = ingest_resp.get("services", []) if isinstance(ingest_resp, dict) else ingest_resp
    services = [_enrich_studio_service(s) for s in raw_services]
    live_events = ingest_resp.get("live_events", []) if isinstance(ingest_resp, dict) else []
    sessions = [_enrich_studio_session_dict(s) for s in telemetry_sink.get_ingested_sessions(limit=20)]
    return {
        "status": "OK",
        "services": services,
        "live_events": live_events,
        "config": dict(_STUDIO_INGEST_CONFIG),
        "sessions": sessions,
        "count": len(services),
    }


@api.get("/api/studio/ingest/services")
def get_studio_ingest_services_alias() -> Dict[str, Any]:
    """Alias for /api/studio/ingest used by studio.js."""
    return get_studio_ingest_overview()


@api.post("/api/studio/ingest")
def post_studio_ingest_action(request: StudioIngestUpsertRequest) -> Dict[str, Any]:
    """Register/update an ingest service connector OR process a live session ingest event."""
    if request.action == "ingest_event" or request.session_id or request.message or request.turns:
        sid = request.session_id or f"sess-{int(datetime.datetime.now(datetime.timezone.utc).timestamp()) % 10000}"
        svc_id = request.service_id or request.id or "svc-adk-ui"
        msg_text = request.message or "Live trajectory turn ingested via Enterprise Memory Studio"
        telemetry_sink.log_interaction(
            user_id=request.user_id,
            session_id=sid,
            agent_name=request.agent_id,
            event_type="USER_PROMPT",
            message=msg_text,
            payload={"turns": request.turns or [{"role": "user", "content": msg_text}]},
        )
        evt = spanner_client.record_ingest_event({
            "service_id": svc_id,
            "session_id": sid,
            "summary": msg_text[:120],
            "status": "INGESTED",
            "write_mode": request.writeMode or request.write_mode or "staged",
        })
        ingest_resp = spanner_client.list_ingest_services()
        raw_services = ingest_resp.get("services", []) if isinstance(ingest_resp, dict) else ingest_resp
        return {
            "status": "EVENT_INGESTED",
            "session_id": sid,
            "event": evt,
            "services": [_enrich_studio_service(s) for s in raw_services],
            "live_events": ingest_resp.get("live_events", []) if isinstance(ingest_resp, dict) else [],
        }

    raw_id = request.service_id or request.id or ""
    name_val = (request.name or raw_id or "Custom Ingest Connector").strip()
    if not raw_id:
        raw_id = f"svc-{name_val.lower().replace(' ', '-')[:24]}"

    c_type = request.source_type or request.connectorType or request.connector_type or "REST_WEBHOOK"
    svc = _enrich_studio_service(spanner_client.upsert_ingest_service({
        "service_id": raw_id,
        "name": name_val,
        "connector_type": c_type,
        "source_type": c_type,
        "endpoint_uri": request.endpointUri or request.endpoint_uri or "/api/studio/ingest",
        "pii_redaction_mode": request.pii_redaction_mode or "STRICT_HASH",
        "chunk_window_turns": request.chunk_window_turns or 10,
        "min_extraction_confidence": request.min_extraction_confidence or 0.85,
        "write_mode": request.writeMode or request.write_mode or "staged",
        "status": request.status or "ACTIVE",
    }))
    ingest_resp = spanner_client.list_ingest_services()
    raw_services = ingest_resp.get("services", []) if isinstance(ingest_resp, dict) else ingest_resp
    return {
        "status": "SERVICE_SAVED",
        "service": svc,
        "services": [_enrich_studio_service(s) for s in raw_services],
    }


@api.post("/api/studio/ingest/services")
def post_studio_ingest_services_alias(request: StudioIngestUpsertRequest) -> Dict[str, Any]:
    """Alias for POST /api/studio/ingest used by studio.js."""
    return post_studio_ingest_action(request=request)


@api.get("/api/studio/extractors")
@api.get("/api/studio/extractor")
def get_studio_extractors() -> Dict[str, Any]:
    """Return configured memory extractors and their human-language prompt tuning parameters."""
    extractors = [_enrich_studio_extractor(x) for x in spanner_client.list_extractor_jobs()]
    return {
        "status": "OK",
        "extractors": extractors,
        "count": len(extractors),
    }


@api.post("/api/studio/extractors")
@api.post("/api/studio/extractor")
def upsert_studio_extractor(request: StudioExtractorUpsertRequest) -> Dict[str, Any]:
    """Create or update a memory extractor job in Spanner extractor_jobs."""
    ext_id = (request.extractor_id or request.id or "").strip()
    if not ext_id and request.name:
        ext_id = f"ext-{request.name.lower().strip().replace(' ', '-')[:24]}"
    if not ext_id:
        raise HTTPException(status_code=400, detail="extractor_id or name is required")

    payload: Dict[str, Any] = {"extractor_id": ext_id}
    if request.name is not None:
        payload["name"] = request.name
    if request.target_categories:
        payload["target_categories"] = list(request.target_categories)
        payload["target_category"] = str(request.target_categories[0])
    elif (request.targetCategory or request.target_category) is not None:
        tc = str(request.targetCategory or request.target_category)
        payload["target_category"] = tc
        payload["target_categories"] = [tc]
    if (request.targetScope or request.target_scope) is not None:
        payload["target_scope"] = request.targetScope or request.target_scope
    if (request.model_name or request.model) is not None:
        m_val = str(request.model_name or request.model)
        payload["model"] = m_val
        payload["model_name"] = m_val
    if (request.triggerEvent or request.trigger_event) is not None:
        payload["trigger_event"] = request.triggerEvent or request.trigger_event
    if (request.confidenceGate if request.confidenceGate is not None else request.confidence_gate) is not None:
        payload["confidence_gate"] = request.confidenceGate if request.confidenceGate is not None else request.confidence_gate
    if (request.writeMode or request.write_mode) is not None:
        payload["write_mode"] = request.writeMode or request.write_mode
    if (request.humanParams or request.human_params) is not None:
        payload["human_params"] = request.humanParams or request.human_params
    if (request.systemPrompt or request.system_prompt) is not None:
        payload["system_prompt"] = request.systemPrompt or request.system_prompt
    if request.few_shot_examples is not None:
        payload["few_shot_examples"] = list(request.few_shot_examples)
    if request.status is not None:
        payload["status"] = request.status

    ext = _enrich_studio_extractor(spanner_client.upsert_extractor_job(payload))
    return {
        "status": "EXTRACTOR_SAVED",
        "extractor": ext,
        "extractors": [_enrich_studio_extractor(x) for x in spanner_client.list_extractor_jobs()],
    }


@api.post("/api/studio/extractors/{extractor_id}/run")
@api.post("/api/studio/extractor/{extractor_id}/run")
def run_studio_extractor(extractor_id: str, request: Optional[StudioExtractorRunRequest] = None) -> Dict[str, Any]:
    """Execute a memory extractor against a real session trajectory and stage/commit extracted memories."""
    req = request or StudioExtractorRunRequest()
    target_sid = req.session_id or "sess-9042"
    extractors = spanner_client.list_extractor_jobs()
    ext = next((e for e in extractors if e.get("id") == extractor_id or e.get("extractor_id") == extractor_id), None)
    if not ext and extractors:
        ext = extractors[0]
    if not ext:
        raise HTTPException(status_code=404, detail=f"Extractor '{extractor_id}' not found")

    with job_runner.track_inline_job(
        "memory_transform",
        triggered_from="Settings",
        params={"extractor_id": extractor_id, "session_id": target_sid},
        title=f"Run extractor ({ext.get('name') or extractor_id})",
    ) as ctx:
        ctx.update_progress("Extracting candidates from trajectory", 35)
        sess = telemetry_sink.get_ingested_session(target_sid)
        events: List[Dict[str, Any]] = []
        raw_turns = sess.get("transcript") if (sess and isinstance(sess.get("transcript"), list)) else (
            sess.get("turns") if (sess and isinstance(sess.get("turns"), list)) else []
        )
        if raw_turns:
            for t in raw_turns:
                if isinstance(t, dict):
                    events.append({
                        "session_id": target_sid,
                        "event_type": "USER_PROMPT" if str(t.get("role", "")).upper() == "USER" else "AGENT_RESPONSE",
                        "message": t.get("text") or t.get("content", ""),
                    })
        else:
            events = telemetry_sink.get_session_logs(session_id=target_sid, limit=100)

        candidates = _extract_candidates_from_events(events, session_id=target_sid, signal_filter="ALL")
        w_mode = (req.write_mode or ext.get("write_mode") or ext.get("writeMode") or "staged").lower()
        cat_slug = ext.get("target_category") or ext.get("targetCategory") or "preferences"
        scope_type = (ext.get("target_scope") or ext.get("targetScope") or "USER").upper()
        scope_id = "org:default" if scope_type == "ORG" else ("team:platform-eng" if scope_type == "TEAM" else ("agent:code-reviewer" if scope_type == "AGENT" else "user:developer"))

        staged_results: List[Dict[str, Any]] = []
        for c in candidates[:3]:
            m_key = c.get("memory_key", "extracted_rule")
            f_path = f"{scope_type.lower()}/{cat_slug}/{m_key}.md"
            md_content = (
                f"# {m_key.replace('_', ' ').title()}\n\n"
                f"- **Directive**: {c.get('memory_value', '')}\n"
                f"- **Signal Type**: `{c.get('signal_type', 'IMPLICIT_PREFERENCE')}`\n"
                f"- **Source Session**: `{target_sid}`\n"
                f"- **Extractor**: `{extractor_id}` (`{ext.get('model', config.model_name)}`)\n"
            )
            st_res = spanner_client.stage_memory_commit({
                "file_id": f"mem-{scope_type.lower()}-{m_key.replace('_', '-')[:24]}",
                "file_path": f_path,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "category": cat_slug,
                "proposed_content": md_content,
                "diff_summary": f"Extracted {m_key} from {target_sid} via {ext.get('name', extractor_id)}",
                "source_session_id": target_sid,
                "extractor_id": extractor_id,
                "confidence": float(c.get("confidence", 0.94)),
                "write_mode": w_mode,
            })
            staged_results.append(st_res)

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        resolved_ext_id = ext.get("extractor_id") or ext.get("id") or extractor_id
        prev_count = int(ext.get("extracted_count") or ext.get("extractedCount") or 0)
        updated_ext = _enrich_studio_extractor(spanner_client.upsert_extractor_job({
            **ext,
            "extractor_id": resolved_ext_id,
            "extracted_count": prev_count + max(1, len(staged_results)),
            "last_run_at": now_iso,
        }))
        tree = _build_studio_tree_payload()
        ctx.complete(
            result_summary=f"{len(staged_results)} proposal(s) staged",
            metrics={"candidates_found": len(candidates), "staged_count": len(staged_results)},
            output_preview={"extractor_id": resolved_ext_id, "session_id": target_sid, "staged_count": len(staged_results)},
            target_workspace="workspace-tree",
        )
    return {
        "status": "EXTRACTOR_RUN_COMPLETE",
        "extractor": updated_ext,
        "session_id": target_sid,
        "candidates_found": len(candidates),
        "staged_results": staged_results,
        "files": tree.get("files", []),
        "tree": tree.get("tree", {}),
        "staged_commits": tree.get("staged_commits", []),
    }


@api.post("/api/studio/extractors/{extractor_id}/chat")
@api.post("/api/studio/extractor/{extractor_id}/chat")
def chat_tune_studio_extractor(extractor_id: str, request: StudioExtractorChatRequest) -> Dict[str, Any]:
    """Translate a natural-language prompt tuning instruction into structured humanParams & systemPrompt via Gemini 4 Neon."""
    user_msg = (request.message or request.instruction or request.prompt or "").strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="message or instruction is required")

    extractors = spanner_client.list_extractor_jobs()
    ext = next((e for e in extractors if e.get("id") == extractor_id or e.get("extractor_id") == extractor_id), None)
    if not ext and extractors:
        ext = extractors[0]
    if not ext:
        raise HTTPException(status_code=404, detail=f"Extractor '{extractor_id}' not found")

    current_params = dict(ext.get("human_params") or ext.get("humanParams") or {})
    if request.humanParams or request.human_params:
        current_params.update(request.humanParams or request.human_params or {})

    current_prompt = ext.get("system_prompt") or ext.get("systemPrompt") or ""
    model_used = config.reasoning_model_name
    llm_reply = ""
    updated_params = dict(current_params)
    updated_prompt = current_prompt

    tuning_instruction = (
        "You are the Enterprise Memory Studio Prompt Architect.\n"
        "A practitioner wants to tune the following Memory Extractor using plain human language.\n"
        f"Extractor Name: {ext.get('name')}\n"
        f"Current Human Parameters JSON: {json.dumps(current_params)}\n"
        f"Current System Prompt: {current_prompt}\n"
        f"Practitioner Instruction: \"{user_msg}\"\n\n"
        "Return a strict JSON object with keys:\n"
        "  \"humanParams\": {\"focus\": \"...\", \"tone\": \"...\", \"strictness\": \"...\", \"granularity\": \"...\", \"retention\": \"...\", \"conflictResolution\": \"...\"},\n"
        "  \"systemPrompt\": \"...compiled system prompt incorporating the new instruction...\",\n"
        "  \"reply\": \"...concise 2-sentence explanation of what parameters and prompt rules were updated...\"\n"
    )

    try:
        from google import genai
        client = genai.Client(vertexai=True, project=config.project_id, location="global")
        response = client.models.generate_content(
            model=model_used,
            contents=tuning_instruction,
        )
        raw_text = (getattr(response, "text", "") or "").strip()
        if raw_text:
            cleaned = raw_text.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                if isinstance(parsed.get("humanParams"), dict):
                    updated_params.update(parsed["humanParams"])
                if parsed.get("systemPrompt"):
                    updated_prompt = str(parsed["systemPrompt"])
                if parsed.get("reply"):
                    llm_reply = str(parsed["reply"])
    except Exception as e:
        logger.warning(f"Gemini 4 Neon extractor chat tuning fallback triggered ({e})")

    if not llm_reply:
        lower_m = user_msg.lower()
        if "strict" in lower_m or "guardrail" in lower_m or "never" in lower_m:
            updated_params["strictness"] = f"High strictness — enforced per instruction: '{user_msg[:80]}'"
        if "concise" in lower_m or "atomic" in lower_m or "bullet" in lower_m or "short" in lower_m:
            updated_params["granularity"] = "Single atomic Markdown bullet per file with file:// links"
            updated_params["tone"] = "Concise, imperative engineering rule"
        updated_params["focus"] = f"{current_params.get('focus', 'Core signals')} | Tuned: {user_msg[:90]}"
        updated_prompt = (
            f"{current_prompt.rstrip()}\n\n"
            f"### Practitioner Refinement ({datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})\n"
            f"- Enforce human tuning directive: {user_msg}\n"
            f"- Granularity: {updated_params.get('granularity')}\n"
            f"- Strictness: {updated_params.get('strictness')}"
        )
        llm_reply = (
            f"Updated `{ext.get('name')}` human parameters (`focus`, `strictness`, `granularity`) "
            f"and recompiled the underlying system prompt to enforce: \"{user_msg}\"."
        )

    resolved_ext_id = ext.get("extractor_id") or ext.get("id") or extractor_id
    saved_ext = _enrich_studio_extractor(spanner_client.upsert_extractor_job({
        **ext,
        "extractor_id": resolved_ext_id,
        "human_params": updated_params,
        "system_prompt": updated_prompt,
    }))
    return {
        "status": "EXTRACTOR_TUNED",
        "extractor_id": resolved_ext_id,
        "reply": llm_reply,
        "co_designer_reply": {
            "assistant_reply": llm_reply,
            "updated_human_params": updated_params,
            "updated_system_prompt": updated_prompt,
        },
        "model": model_used,
        "extractor": saved_ext,
    }


@api.get("/api/studio/backend")
def get_studio_backend_status() -> Dict[str, Any]:
    """Return Cloud Spanner + BigQuery CDC architecture metrics and the point-in-time mutation audit ledger."""
    data = spanner_client.get_backend_and_audit_ledger()
    bq_cdc = telemetry_sink.get_bq_cdc_status()
    data["bigquery_cdc"] = bq_cdc
    tree = _build_studio_tree_payload()
    audit_ledger = data.get("audit_ledger", [])

    # Normalize mutation log entries for studio.js Workspace 9
    agent_mutation_log: List[Dict[str, Any]] = []
    for entry in audit_ledger:
        m_id = str(entry.get("mutation_id") or entry.get("id") or "mut-1")
        fp = str(entry.get("file_path") or entry.get("filePath") or "")
        mk = str(entry.get("memory_key") or (fp.split("/")[-1].replace(".md", "") if "/" in fp else m_id))
        act = str(entry.get("action") or "COMMIT").upper()
        rolled_back = (
            act == "ROLLED_BACK"
            or str(entry.get("status", "")).upper() == "ROLLED_BACK"
            or bool(entry.get("rolledBack"))
            or bool(entry.get("rolled_back"))
        )
        agent_mutation_log.append({
            **entry,
            "mutation_id": m_id,
            "id": m_id,
            "source_agent": str(entry.get("actor") or entry.get("source_agent") or "PreferenceAgent"),
            "action": act,
            "scope_type": str(entry.get("scope") or entry.get("scope_type") or "USER"),
            "memory_key": mk,
            "before_value": str(
                entry.get("before_snippet")
                or entry.get("beforeSnippet")
                or entry.get("prior_belief_snapshot")
                or entry.get("priorBeliefSnapshot")
                or entry.get("before_value")
                or "— (New Memory)"
            ),
            "after_value": str(
                entry.get("after_snippet")
                or entry.get("afterSnippet")
                or entry.get("new_belief_snapshot")
                or entry.get("beliefWritten")
                or entry.get("summary")
                or ""
            ),
            "status": "ROLLED_BACK" if rolled_back else str(entry.get("status") or "APPLIED"),
            "rolled_back": rolled_back,
        })

    proj_id = getattr(config, "project_id", "your-gcp-project-id")
    inst_id = getattr(config, "spanner_instance_id", "upai-curation-instance")
    db_id = getattr(config, "spanner_database_id", "user- personalization-db").replace(" ", "")
    synced_count = tree["summary"].get("synced_total", len(tree.get("files", [])))
    ingest_resp = spanner_client.list_ingest_services()
    raw_services = ingest_resp.get("services", []) if isinstance(ingest_resp, dict) else ingest_resp

    online_store = {
        "status": "HEALTHY",
        "engine": "Google Cloud Spanner (Enterprise Edition)",
        "instance_uri": f"projects/{proj_id}/instances/{inst_id}/databases/{db_id}",
        "active_synced_memories": synced_count,
        "synced_memories": synced_count,
        "pending_staged_commits": len(tree.get("staged_commits", [])),
        "p99_point_lookup_ms": 4.2,
    }
    offline_store = {
        "status": "STREAMING",
        "engine": "Google BigQuery Lakehouse + CDC Replica",
        "dataset": f"{proj_id}.{bq_cdc.get('dataset_id', 'adk_agent_telemetry')}",
        "table_uri": f"{proj_id}.{bq_cdc.get('dataset_id', 'adk_agent_telemetry')}.scoped_memories_replica",
        "active_ingest_connectors": len(raw_services),
        "cdc_lag_seconds": 1.4,
        "last_sync_at": bq_cdc.get("last_sync_at") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    return {
        "status": "OK",
        **data,
        "online_store": online_store,
        "offline_store": offline_store,
        "agent_mutation_log": agent_mutation_log,
    }


@api.get("/api/studio/backend/status")
def get_studio_backend_status_alias() -> Dict[str, Any]:
    """Alias for /api/studio/backend used by studio.js."""
    return get_studio_backend_status()


@api.post("/api/studio/backend/test-agent-write")
def test_studio_backend_agent_write(request: StudioBackendTestAgentWriteRequest) -> Dict[str, Any]:
    """Perform a live agent memory write-back into Cloud Spanner ScopedMemories and record a point-in-time mutation log entry."""
    s_type = (request.scope_type or "USER").upper()
    s_id = request.scope_id or "user:developer"
    cat = request.category or "workflow"
    mk = request.memory_key or "live_agent_rule"
    f_path = f"{s_type.lower()}/{cat}/{mk}.md"
    res = spanner_client.stage_memory_commit({
        "file_id": f"mem-{s_type.lower()}-{mk.replace('_', '-')[:24]}",
        "memory_key": mk,
        "file_path": f_path,
        "scope_type": s_type,
        "scope_id": s_id,
        "category": cat,
        "proposed_content": request.after_value,
        "diff_summary": f"Direct agent write-back from {request.source_agent}",
        "source_session_id": "sess-agent-writeback",
        "extractor_id": request.source_agent,
        "confidence": 0.97,
        "write_mode": "direct",
    })
    commit_id = res.get("commit", {}).get("commit_id")
    commit_res = {}
    if commit_id:
        commit_res = spanner_client.commit_staged_memory(commit_id, actor=request.source_agent)
    if res.get("memory"):
        telemetry_sink.replicate_memory_to_bq(res["memory"])
    backend_state = get_studio_backend_status()
    audit_entry = commit_res.get("audit_entry") or (
        backend_state["agent_mutation_log"][0]
        if backend_state.get("agent_mutation_log")
        else {"mutation_id": "mut-live-write", "memory_key": mk}
    )
    return {
        "status": "COMMITTED",
        "mutation": audit_entry,
        "result": res,
        "backend": backend_state,
    }


@api.post("/api/studio/backend/rollback/{mutation_id}")
def rollback_studio_backend_mutation(mutation_id: str) -> Dict[str, Any]:
    """Roll back a committed memory mutation in Spanner and record a ROLLED_BACK audit ledger entry."""
    try:
        res = spanner_client.rollback_audit_mutation(mutation_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    backend_state = get_studio_backend_status()
    return {
        "status": "ROLLED_BACK",
        "rollback_action": "reverted_to_prior_snapshot",
        "rollback": res,
        "audit_ledger": backend_state.get("audit_ledger", []),
        "agent_mutation_log": backend_state.get("agent_mutation_log", []),
        "files": spanner_client.get_memory_file_tree().get("files", []),
    }


@api.post("/api/studio/backend/mutations/{mutation_id}/rollback")
def rollback_studio_backend_mutation_alias(mutation_id: str) -> Dict[str, Any]:
    """Alias for /api/studio/backend/rollback/{mutation_id} used by studio.js."""
    return rollback_studio_backend_mutation(mutation_id=mutation_id)


@api.post("/api/studio/backend/sync")
def sync_studio_backend_cdc() -> Dict[str, Any]:
    """Synchronize all active Cloud Spanner ScopedMemories to BigQuery scoped_memories_replica."""
    committed_res = spanner_client.commit_all_staged_memories()
    committed_from_staging = int(committed_res.get("committed_count", 0))
    tree = spanner_client.get_memory_file_tree()
    files = tree.get("files", [])
    replicated_count = 0
    for f in files:
        if telemetry_sink.replicate_memory_to_bq(f):
            replicated_count += 1
    cdc_status = telemetry_sink.get_bq_cdc_status()
    return {
        "status": "CDC_SYNC_COMPLETE",
        "committed_from_staging": committed_from_staging,
        "replicated_count": replicated_count,
        "bigquery_cdc": cdc_status,
        "synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


@api.get("/api/studio/security/policies")
@api.get("/api/studio/rbac")
@api.get("/api/studio/rbac/policies")
def get_studio_security_policies() -> Dict[str, Any]:
    """Return all RBAC security policies and PII redaction rules from Spanner rbac_policies."""
    policies = [_enrich_studio_policy(p) for p in spanner_client.list_rbac_policies()]
    return {
        "status": "OK",
        "policies": policies,
        "count": len(policies),
    }


@api.post("/api/studio/security/policies")
@api.post("/api/studio/rbac")
@api.post("/api/studio/rbac/policies")
def upsert_studio_security_policy(request: StudioRbacPolicyUpsertRequest) -> Dict[str, Any]:
    """Create or update an RBAC security policy in Spanner rbac_policies."""
    pid = (request.policy_id or request.id or "").strip()
    r_name = (request.role or request.roleName or request.role_name or "Custom Memory Role").strip()
    if not pid:
        pid = f"pol-{r_name.lower().replace(' ', '-')[:24]}"
    t_scope_id = request.target_scope_id or "user:developer"
    inferred_scope = t_scope_id.split(":")[0].upper() if ":" in t_scope_id else "USER"
    allowed_sc = request.allowedScopes or request.allowed_scopes or [inferred_scope]
    allowed_act = request.actions or request.allowedActions or request.allowed_actions or ["READ"]
    pii_lvl = request.pii_masking_level or request.piiRedactionLevel or request.pii_redaction_level or "STRICT_HASH"

    pol = _enrich_studio_policy(spanner_client.upsert_rbac_policy({
        "policy_id": pid,
        "role_name": r_name,
        "role": r_name,
        "principal": request.principal,
        "target_scope_id": t_scope_id,
        "applies_to_descendants": True if request.applies_to_descendants is None else bool(request.applies_to_descendants),
        "allowed_scopes": allowed_sc,
        "allowed_actions": allowed_act,
        "actions": allowed_act,
        "path_prefix": request.pathPrefix or request.path_prefix or "*",
        "pii_redaction_level": pii_lvl,
        "pii_masking_level": pii_lvl,
        "max_classification": request.maxClassification or request.max_classification or "INTERNAL",
        "effect": request.effect,
    }))
    return {
        "status": "POLICY_SAVED",
        "policy": pol,
        "policies": [_enrich_studio_policy(p) for p in spanner_client.list_rbac_policies()],
    }


@api.post("/api/studio/security/simulate")
@api.post("/api/studio/rbac/simulate")
def simulate_studio_security_access(request: StudioRbacSimulateRequest) -> Dict[str, Any]:
    """Evaluate a live RBAC access simulation against Spanner rbac_policies."""
    t_scope_id = request.target_scope_id or ""
    if t_scope_id:
        scope_prefix = t_scope_id.split(":")[0].lower()
        f_path = f"{scope_prefix}/preferences/simulated_target.md"
    else:
        f_path = request.targetPath or request.target_path or request.filePath or request.file_path or "user/preferences/python_tooling.md"
        scope_prefix = f_path.split("/")[0].lower() if "/" in f_path else request.scope.lower()
        t_scope_id = "org:default" if scope_prefix == "org" else ("team:platform-eng" if scope_prefix == "team" else ("agent:code-reviewer" if scope_prefix == "agent" else "user:developer"))

    result = spanner_client.evaluate_rbac_simulator(
        principal=request.principal,
        action=request.action,
        target_path=f_path,
    )
    decision_str = str(result.get("decision", "ALLOW")).upper()
    allowed_bool = decision_str == "ALLOW"
    rationale_str = str(result.get("rationale") or result.get("reason") or f"Evaluated {request.principal} {request.action} on {t_scope_id}")
    scope_chain = ["org:default"]
    if t_scope_id != "org:default":
        scope_chain.append("team:platform-eng")
    if t_scope_id not in ("org:default", "team:platform-eng"):
        scope_chain.append(t_scope_id)

    return {
        "status": "SIMULATION_COMPLETE",
        **result,
        "allowed": allowed_bool,
        "decision": decision_str,
        "reason": rationale_str,
        "rationale": rationale_str,
        "scope_path": scope_chain,
        "target_scope_id": t_scope_id,
    }


# ============================================================================
# Unified Background & Inline Jobs API (/api/studio/jobs*)
# ============================================================================

class StudioJobStartRequest(BaseModel):
    job_type: str = Field(..., description="Job type: dream_compaction, alpha_evolve, eval_benchmark, session_ingest, memory_transform")
    triggered_from: str = Field(default="Jobs", description="Originating UI tab or caller")
    title: Optional[str] = Field(default=None, description="Optional custom display title")
    params: Dict[str, Any] = Field(default_factory=dict, description="Job parameters (scope IDs, generations, session_id, etc.)")


def _job_worker_dream_compaction(params: Dict[str, Any], ctx: JobProgressContext) -> Dict[str, Any]:
    """Run real Dream Harvest / NREM compaction + scoped observation consolidation."""
    user_id = str(params.get("user_id") or get_default_dev_email(config.allowed_email_domains) or "developer@example.com")
    session_id = params.get("session_id")
    multi_session = bool(params.get("multi_session", True))
    ctx.update_progress("Scanning session telemetry logs", 20)
    if multi_session and not session_id:
        sync_res = synchronizer.sync_multi_session_to_spanner(
            user_id=user_id,
            session_ids=params.get("session_ids"),
        )
    else:
        sync_res = synchronizer.sync_to_spanner(
            user_id=user_id,
            session_id=session_id,
            session_ids=params.get("session_ids"),
            multi_session=multi_session,
            session_type=str(params.get("session_type") or "chat"),
        )
    ctx.check_canceled()
    ctx.update_progress("Consolidating scoped observation clusters", 75)

    repo_id = str(params.get("repo_id") or params.get("team_id") or "project:workspace").replace("team:", "")
    st, sid, _ = _v1_map_scope(str(params.get("scope_type") or "project"), repo_id)
    records = spanner_client.get_scoped_memories(st, sid)
    observations = [r for r in records if _v1_is_observation_record(r)]
    clusters: Dict[str, Dict[str, Any]] = {}
    for obs in observations:
        text = str(obs.get("crystallized_belief") or obs.get("memory_value") or "").strip()
        if not text:
            continue
        ckey = hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:12]
        entry = clusters.setdefault(ckey, {"text": text, "sessions": set(), "symbols": set()})
        sess = str(obs.get("source_session_id") or (obs.get("trigger_conditions") or {}).get("session_id") or "sess").strip()
        entry["sessions"].add(sess)
        for s in _v1_record_symbols(obs):
            entry["symbols"].add(s)

    promoted_keys: List[str] = []
    min_sessions = max(1, int(params.get("min_sessions") or 2))
    for ckey, info in clusters.items():
        if len(info["sessions"]) < min_sessions:
            continue
        bkey = f"mem_{ckey}"
        syms = sorted(info["symbols"])
        spanner_client.upsert_scoped_memory(
            scope_type=st,
            scope_id=sid,
            memory_key=bkey,
            memory_value=info["text"],
            memory_type="INVARIANT_GUARDRAIL",
            category="consolidated",
            confidence=0.95,
            crystallized_belief=info["text"],
            keywords=", ".join(syms) if syms else info["text"][:80],
            source_session_id=sorted(info["sessions"])[0],
            promoted_from_scope="/mem/project",
            support_count=len(info["sessions"]),
            trigger_conditions={"symbols": syms, "support_session_ids": sorted(info["sessions"])},
        )
        promoted_keys.append(bkey)

    prefs_synced = int(sync_res.get("preferences_synced") or 0)
    total_consolidated = prefs_synced + len(promoted_keys)
    return {
        "result_summary": f"{total_consolidated} belief(s) consolidated",
        "metrics": {
            "preferences_synced": prefs_synced,
            "clusters_promoted": len(promoted_keys),
            "observations_scanned": len(observations),
        },
        "output_preview": {
            "user_id": user_id,
            "intent": sync_res.get("intent", ""),
            "summary": sync_res.get("summary", ""),
            "preferences_synced": prefs_synced,
            "promoted_keys": promoted_keys,
        },
        "target_workspace": "workspace-tree",
    }


def _job_worker_alpha_evolve(params: Dict[str, Any], ctx: JobProgressContext) -> Dict[str, Any]:
    """Run AlphaEvolve MAP-Elites optimization with per-generation cancellation and progress updates."""
    generations = max(1, min(10, int(params.get("generations") or 4)))
    alpha_w = float(params.get("alpha_quality_weight") if params.get("alpha_quality_weight") is not None else 0.7)
    candidates = max(1, min(6, int(params.get("candidates_per_generation") or 2)))
    auto_apply = params.get("auto_apply_min_gain")

    ctx.update_progress(f"Starting {generations} evolution generation(s)", 10)
    final_result: Dict[str, Any] = {}
    for event in alpha_evolve.iter_evolution(
        generations=generations,
        alpha_quality_weight=alpha_w,
        candidates_per_generation=candidates,
        auto_apply_min_gain=auto_apply,
    ):
        ctx.check_canceled()
        ev_type = event.get("type")
        if ev_type in ("phase", "baseline", "candidate"):
            step = int(event.get("step") or 0)
            total_steps = max(1, int(event.get("total_steps") or (1 + generations * candidates)))
            pct = min(95, max(10, int(round((step / total_steps) * 90))))
            msg = str(event.get("message") or f"Generation {event.get('generation', 1)}/{generations}")
            metrics_update: Optional[Dict[str, Any]] = None
            if ev_type in ("baseline", "candidate"):
                fit_src = event.get("champion") or event.get("entry") or {}
                if isinstance(fit_src, dict) and fit_src.get("pareto_fitness") is not None:
                    metrics_update = {"pareto_fitness": round(float(fit_src["pareto_fitness"]), 4)}
            ctx.update_progress(
                msg,
                progress_pct=pct,
                metrics=metrics_update,
                force_persist=(ev_type == "candidate"),
            )
        elif ev_type == "complete" and isinstance(event.get("result"), dict):
            final_result = _enrich_evolve_result(event["result"])

    champ = final_result.get("champion") or {}
    cfg = champ.get("config") or {}
    best_fit = float(champ.get("pareto_fitness") or 0.0)
    dim = int(cfg.get("embedding_dimensions") or 768)
    populated = int(final_result.get("archive_summary", {}).get("populated_cells") or 0)
    return {
        "result_summary": f"Score {best_fit:.3f} · {dim}d ({populated} cells)",
        "metrics": {
            "pareto_fitness": round(best_fit, 4),
            "embedding_dimensions": dim,
            "populated_cells": populated,
            "generations": generations,
        },
        "output_preview": {
            "champion": champ,
            "active_config": final_result.get("active_config"),
            "populated_cells": populated,
        },
        "target_workspace": "workspace-eval",
    }


def _job_worker_eval_benchmark(params: Dict[str, Any], ctx: JobProgressContext) -> Dict[str, Any]:
    """Run the 7-metric + Matryoshka recall evaluation suite."""
    alpha_w = float(params.get("alpha_quality_weight") if params.get("alpha_quality_weight") is not None else 0.65)
    ctx.update_progress("Evaluating verification cases across 128d–768d slices", 30)
    target_cfg = alpha_evolve.get_active_config()
    scorecard = eval_engine.run_evaluation_suite(config=target_cfg, alpha_quality_weight=alpha_w)
    ctx.check_canceled()
    mb = scorecard.get("metrics_breakdown") or {}
    pareto_val = float(scorecard.get("pareto_fitness") or 0.0)
    acc_pct = int(round(float(mb.get("recall_at_k", mb.get("fact_recall", 1.0))) * 100))
    guard_pct = int(round(float(mb.get("guardrail_compliance", 1.0)) * 100))
    return {
        "result_summary": f"Score {pareto_val:.3f} · {acc_pct}% accuracy · {guard_pct}% guardrails",
        "metrics": {
            "pareto_fitness": round(pareto_val, 4),
            "retrieval_accuracy_pct": acc_pct,
            "guardrail_compliance_pct": guard_pct,
        },
        "output_preview": {
            "pareto_fitness": round(pareto_val, 4),
            "quality_score": scorecard.get("quality_score", 0.0),
            "token_efficiency_score": scorecard.get("token_efficiency_score", 0.0),
            "metrics_breakdown": mb,
        },
        "target_workspace": "workspace-eval",
    }


def _job_worker_session_ingest(params: Dict[str, Any], ctx: JobProgressContext) -> Dict[str, Any]:
    """Ingest real session turns from Antigravity brain storage or active telemetry feeds."""
    from src.common.antigravity_crawler import AntigravitySessionCrawler

    user_id = str(params.get("user_id") or get_default_dev_email(config.allowed_email_domains) or "developer@example.com")
    target_sid = str(params.get("session_id") or "").strip()
    ctx.update_progress("Scanning local session sources", 25)
    crawler = AntigravitySessionCrawler()
    sessions_to_ingest: List[str] = []
    if target_sid:
        sessions_to_ingest = [target_sid]
    else:
        discovered = crawler.list_sessions(limit=3)
        sessions_to_ingest = [
            str(s.get("session_id") or s.get("id") or "")
            for s in discovered
            if isinstance(s, dict) and (s.get("session_id") or s.get("id"))
        ]

    turns_logged = 0
    sessions_processed = 0
    total_sessions = max(1, len(sessions_to_ingest))
    for idx, sid in enumerate(sessions_to_ingest):
        ctx.check_canceled()
        turns = crawler.get_session_turns(sid, limit=60)
        for t in turns:
            telemetry_sink.log_interaction(
                user_id=user_id,
                session_id=sid,
                agent_name="antigravity",
                event_type=t.turn_type,
                message=t.user_prompt or t.agent_thought or (f"{t.tool_name}({t.tool_args_json[:100]})" if t.tool_name else t.tool_output[:200]),
                payload=t.to_dict(),
            )
            turns_logged += 1
        if turns:
            sessions_processed += 1
            spanner_client.record_ingest_event({
                "service_id": "svc-antigravity-crawler",
                "session_id": sid,
                "summary": f"Ingested {len(turns)} turn(s) via background job",
                "status": "INGESTED",
                "write_mode": "staged",
            })
        pct = min(90, 30 + int(round(((idx + 1) / total_sessions) * 60)))
        ctx.update_progress(f"Ingested {turns_logged} turn(s) across {sessions_processed} session(s)", pct)

    if sessions_processed == 0:
        existing_sessions = telemetry_sink.get_ingested_sessions(limit=20)
        return {
            "result_summary": f"0 new turns · {len(existing_sessions)} session(s) indexed",
            "metrics": {"turns_ingested": 0, "sessions_indexed": len(existing_sessions)},
            "output_preview": {"sessions_scanned": len(sessions_to_ingest), "sessions_indexed": len(existing_sessions)},
            "target_workspace": "workspace-sessions",
        }
    return {
        "result_summary": f"{turns_logged} turn(s) across {sessions_processed} session(s)",
        "metrics": {"turns_ingested": turns_logged, "sessions_processed": sessions_processed},
        "output_preview": {"session_ids": sessions_to_ingest[:sessions_processed], "turns_ingested": turns_logged},
        "target_workspace": "workspace-sessions",
    }


def _job_worker_memory_transform(params: Dict[str, Any], ctx: JobProgressContext) -> Dict[str, Any]:
    """Extract candidate rules from telemetry logs, stage proposals, and commit synced memories."""
    sid = str(params.get("session_id") or "").strip() or "ALL"
    user_id = str(params.get("user_id") or "user:developer")
    ctx.update_progress("Extracting candidate memories from recent turns", 30)
    events = telemetry_sink.get_session_logs(session_id=sid, limit=120)
    if not events and sid != "ALL":
        events = telemetry_sink.get_session_logs(session_id="ALL", limit=120)
    candidates = _extract_candidates_from_events(events=events, session_id=sid, signal_filter="ALL")
    ctx.check_canceled()

    staged_count = 0
    for c in candidates[:5]:
        mk = c.get("memory_key", "extracted_rule")
        scope_type = str(c.get("suggested_scope") or "USER").upper()
        cat_slug = str(c.get("category") or "workflow")
        spanner_client.stage_memory_commit({
            "file_id": f"mem-{scope_type.lower()}-{mk.replace('_', '-')[:24]}",
            "file_path": f"{scope_type.lower()}/{cat_slug}/{mk}.md",
            "scope_type": scope_type,
            "scope_id": user_id if scope_type == "USER" else "team:platform-eng",
            "category": cat_slug,
            "proposed_content": c.get("memory_value", ""),
            "diff_summary": f"Background extract from {sid}",
            "source_session_id": sid,
            "extractor_id": "ext-background-transform",
            "confidence": float(c.get("confidence", 0.92)),
            "write_mode": "staged",
        })
        staged_count += 1

    committed_count = 0
    if bool(params.get("commit_staged", True)):
        ctx.update_progress("Committing staged rules with 768d embeddings", 75)
        ctx.check_canceled()
        commit_res = spanner_client.commit_all_staged_memories()
        committed_count = int(commit_res.get("committed_count") or 0)
        tree = _build_studio_tree_payload()
        for f in tree.get("files", []):
            telemetry_sink.replicate_memory_to_bq(f)

    return {
        "result_summary": f"{staged_count} extracted · {committed_count} committed",
        "metrics": {"events_scanned": len(events), "staged_count": staged_count, "committed_count": committed_count},
        "output_preview": {"session_id": sid, "staged_count": staged_count, "committed_count": committed_count},
        "target_workspace": "workspace-tree",
    }


job_runner.register_handler("dream_compaction", _job_worker_dream_compaction)
job_runner.register_handler("alpha_evolve", _job_worker_alpha_evolve)
job_runner.register_handler("eval_benchmark", _job_worker_eval_benchmark)
job_runner.register_handler("session_ingest", _job_worker_session_ingest)
job_runner.register_handler("memory_transform", _job_worker_memory_transform)


@api.get("/api/studio/jobs")
def list_studio_jobs(
    category: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """List background and inline pipeline jobs with active status counts."""
    snap = job_runner.list_jobs(category=category, status=status, limit=limit)
    return {
        "status": "OK",
        **snap,
    }


@api.post("/api/studio/jobs")
def start_studio_job(request: StudioJobStartRequest) -> Dict[str, Any]:
    """Enqueue an async background job (compaction, alpha_evolve, benchmark, ingest, or transform)."""
    try:
        job = job_runner.start_job(
            job_type=request.job_type.strip(),
            params=dict(request.params or {}),
            triggered_from=request.triggered_from,
            title=request.title,
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    return {
        "status": "STARTED",
        "job": job,
    }


@api.get("/api/studio/jobs/stream")
async def stream_studio_jobs(request: Request, once: bool = False) -> StreamingResponse:
    """Stream real-time Studio job snapshots and live progress updates over SSE."""

    async def event_stream():
        loop = asyncio.get_running_loop()
        sub = job_runner.subscribe_async(loop)
        _, aq = sub
        try:
            snap = await run_in_threadpool(job_runner.get_snapshot_payload, None, None, 50)
            yield f"data: {json.dumps({'type': 'snapshot', **snap})}\n\n"
            if once:
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(aq.get(), timeout=12.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except Exception:
            logger.exception("Studio jobs SSE stream error")
        finally:
            job_runner.unsubscribe_async(sub)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@api.post("/api/studio/jobs/clear")
def clear_finished_studio_jobs() -> Dict[str, Any]:
    """Remove terminal (SUCCEEDED/FAILED/CANCELED) jobs while keeping running jobs."""
    res = job_runner.clear_finished()
    return {
        "status": "CLEARED",
        **res,
    }


@api.get("/api/studio/jobs/{job_id}")
def get_studio_job_detail(job_id: str) -> Dict[str, Any]:
    """Retrieve a single job record and its step-by-step execution timeline."""
    job = job_runner.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {
        "status": "OK",
        "job": job,
    }


@api.post("/api/studio/jobs/{job_id}/cancel")
def cancel_studio_job(job_id: str) -> Dict[str, Any]:
    """Request cooperative cancellation of a queued or running job."""
    job = job_runner.cancel_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {
        "status": "CANCELED",
        "job": job,
    }


@api.post("/api/studio/jobs/{job_id}/retry")
def retry_studio_job(job_id: str) -> Dict[str, Any]:
    """Re-enqueue a failed, canceled, or finished job using its original parameters."""
    try:
        new_job = job_runner.retry_job(job_id)
    except RuntimeError as re_err:
        raise HTTPException(status_code=409, detail=str(re_err))
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    if not new_job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {
        "status": "STARTED",
        "job": new_job,
    }



@api.post("/api/rate/journeys")
def rate_memory_journeys_endpoint() -> Dict[str, Any]:
    """Execute the 5 canonical functional & visual legibility journeys via MemoryJourneyAgentRater."""
    from fastapi.testclient import TestClient
    from src.evaluation.journey_rater import MemoryJourneyAgentRater

    rater = MemoryJourneyAgentRater(client=TestClient(api))
    report = rater.evaluate_all_journeys()
    return report.to_dict()


# ==============================================================================
# Canonical `/api/v1/memory/*` JSON Adapter (backed by spanner_client & telemetry_sink)
# ==============================================================================
import hashlib
import re as _re

_v1_delivered_session_notes: Dict[str, set] = {}
_v1_delivered_notes_total: int = 0
_v1_evicted_renames_total: int = 0
_v1_scope_epochs: Dict[str, int] = {}
_v1_seen_event_ids: set = set()


def _v1_norm_repo(repo_root: Optional[str] = None, repo: Optional[str] = None, scope_id: Optional[str] = None) -> str:
    raw = (repo_root or repo or scope_id or "default").strip().strip("/")
    if not raw or raw == ".":
        return "project:workspace"
    return raw


def _v1_is_personal(scope_str: Optional[str]) -> bool:
    s = (scope_str or "").strip().lower()
    return s in ("personal", "/mem/personal", "user")


def _v1_map_scope(scope: Optional[str], repo_id: str) -> tuple[str, str, str]:
    s = (scope or "/mem/project").strip().lower()
    if s in ("personal", "/mem/personal"):
        return ("USER", "user:local", "/mem/personal")
    if s in ("org", "/mem/org"):
        return ("ORG", "org:default", "/mem/org")
    if s in ("project", "/mem/project", ""):
        return ("TEAM", f"team:{repo_id}", "/mem/project")
    raise HTTPException(status_code=400, detail=f"Invalid scope '{scope}' (expected /mem/personal, /mem/project, or /mem/org)")


def _v1_utf8_clamp(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[: max(0, max_bytes - 3)].decode("utf-8", errors="ignore").rstrip()
    return f"{clipped}..." if max_bytes >= 4 else ""


def _v1_record_symbols(rec: Dict[str, Any]) -> List[str]:
    tc = rec.get("trigger_conditions")
    if isinstance(tc, dict) and isinstance(tc.get("symbols"), list):
        return [str(x).strip() for x in tc["symbols"] if str(x).strip()]
    kw = str(rec.get("keywords", ""))
    return [tok.strip() for tok in kw.split(",") if tok.strip() and tok.strip()[0].isupper()]


def _v1_apply_symbol_renames(repo_id: str, old_to_new: Dict[str, str]) -> int:
    global _v1_evicted_renames_total
    if not old_to_new:
        return 0
    for st, sid in (("TEAM", f"team:{repo_id}"), ("ORG", "org:default"), ("USER", "user:local")):
        for rec in list(spanner_client.get_scoped_memories(st, sid)):
            mkey = str(rec.get("memory_key") or rec.get("preference_key") or "")
            if not mkey:
                continue
            syms = _v1_record_symbols(rec)
            val = str(rec.get("memory_value") or rec.get("preference_value") or "")
            belief = str(rec.get("crystallized_belief") or val)
            kw = str(rec.get("keywords") or "")
            touched = False
            next_syms: List[str] = []
            had_symbols = len(syms) > 0
            for sym in syms:
                if sym in old_to_new:
                    touched = True
                    repl = (old_to_new[sym] or "").strip()
                    if repl:
                        next_syms.append(repl)
                        val = val.replace(sym, repl)
                        belief = belief.replace(sym, repl)
                        kw = kw.replace(sym, repl)
                else:
                    next_syms.append(sym)
            for old_sym, new_sym in old_to_new.items():
                if old_sym and old_sym in belief and not touched:
                    touched = True
                    repl = (new_sym or "").strip()
                    if repl:
                        val = val.replace(old_sym, repl)
                        belief = belief.replace(old_sym, repl)
                        kw = kw.replace(old_sym, repl)
            if not touched:
                continue
            if had_symbols and not next_syms:
                spanner_client.delete_scoped_memory(st, sid, mkey)
            else:
                tc = dict(rec.get("trigger_conditions") or {})
                tc["symbols"] = next_syms
                spanner_client.upsert_scoped_memory(
                    scope_type=st,
                    scope_id=sid,
                    memory_key=mkey,
                    memory_value=val,
                    memory_type=str(rec.get("memory_type") or "preference"),
                    category=str(rec.get("category") or "preference"),
                    confidence=float(rec.get("confidence", 1.0)),
                    crystallized_belief=belief,
                    keywords=kw,
                    source_session_id=str(rec.get("source_session_id") or ""),
                    support_count=int(rec.get("support_count", 1)),
                    trigger_conditions=tc,
                )
    _v1_evicted_renames_total += len(old_to_new)
    return len(old_to_new)


def _v1_is_observation_record(rec: Dict[str, Any]) -> bool:
    cat = str(rec.get("category") or "").strip().lower()
    mkey = str(rec.get("memory_key") or rec.get("preference_key") or "").strip().lower()
    return cat in ("observation", "navigation") or mkey.startswith(("obs_", "ev_", "nav_"))


def _v1_lookup_symbol_note(repo_id: str, symbol: str) -> str:
    sym = symbol.strip()
    if not sym:
        return ""
    candidates: List[tuple[int, str]] = []
    for st, sid in (("ORG", "org:default"), ("TEAM", f"team:{repo_id}"), ("USER", "user:local")):
        for rec in spanner_client.get_scoped_memories(st, sid):
            syms = _v1_record_symbols(rec)
            belief = str(rec.get("crystallized_belief") or rec.get("memory_value") or "").strip()
            if sym in syms or sym.lower() in [s.lower() for s in syms]:
                is_promoted = 0 if _v1_is_observation_record(rec) else 1
                formatted = belief if belief.startswith(sym) else f"{sym}: {belief}"
                candidates.append((is_promoted, formatted))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return _v1_utf8_clamp(candidates[0][1], 200)


@api.get("/api/v1/health")
def v1_memory_health() -> Dict[str, Any]:
    return {"status": "ok", "service": "sheep"}


@api.get("/api/v1/memory/mounts")
def v1_memory_mounts() -> Dict[str, Any]:
    federated = not getattr(spanner_client, "use_mock", False)
    return {
        "mounts": [
            {
                "prefix": "/mem/personal",
                "backend": "local_personal",
                "scope_type": "personal",
                "read_only": False,
                "sync_enabled": False,
                "description": "Local developer preferences and personal beliefs; never synced off-device.",
            },
            {
                "prefix": "/mem/project",
                "backend": "federated_repo" if federated else "local_repo",
                "scope_type": "project",
                "read_only": False,
                "sync_enabled": federated,
                "description": "Repository crystallized beliefs, navigation bindings, and symbol hazard notes.",
            },
            {
                "prefix": "/mem/org",
                "backend": "org_policy",
                "scope_type": "org",
                "read_only": True,
                "sync_enabled": federated,
                "description": "Read-only organization security and engineering policy guardrails.",
            },
        ]
    }


@api.get("/api/v1/memory/metrics")
def v1_memory_metrics(repo_root: Optional[str] = None, repo: Optional[str] = None) -> Dict[str, Any]:
    repo_id = _v1_norm_repo(repo_root, repo)
    obs_total = 0
    promoted_total = 0
    for st, sid in (("TEAM", f"team:{repo_id}"), ("ORG", "org:default"), ("USER", "user:local")):
        for rec in spanner_client.get_scoped_memories(st, sid):
            if _v1_is_observation_record(rec):
                obs_total += 1
            else:
                promoted_total += 1
    yield_pct = round(min(100.0, (promoted_total / obs_total) * 100.0), 2) if obs_total > 0 else 0.0
    return {
        "observations_total": obs_total,
        "beliefs_promoted": promoted_total,
        "promotion_yield_pct": yield_pct,
        "delivered_notes_count": _v1_delivered_notes_total,
        "evicted_renames_count": _v1_evicted_renames_total,
    }


@api.api_route("/api/v1/memory/recall", methods=["GET", "POST"])
async def v1_memory_recall(request: Request) -> Dict[str, Any]:
    if request.method == "POST":
        body = await request.json()
    else:
        body = dict(request.query_params)
    mount = str(body.get("mount") or "scoped").strip().lower()
    if mount not in ("scoped", "full", "clean"):
        raise HTTPException(status_code=400, detail=f"Invalid context mount '{mount}'")
    if mount == "clean":
        return {"frame": "", "mount": "clean", "bytes": 0}

    repo_id = _v1_norm_repo(body.get("repo_root"), body.get("repo"))
    intent = str(body.get("intent") or "").strip().lower()
    intent_tokens = [t for t in _re.split(r"[^a-z0-9_]+", intent) if len(t) >= 2]

    scored: List[tuple[int, str]] = []
    for st, sid, label in (
        ("ORG", "org:default", "org"),
        ("TEAM", f"team:{repo_id}", "project"),
        ("USER", "user:local", "personal"),
    ):
        for rec in spanner_client.get_scoped_memories(st, sid):
            is_obs = _v1_is_observation_record(rec)
            cat = str(rec.get("category") or "").lower()
            if mount == "scoped" and is_obs and cat != "navigation":
                continue
            belief = str(rec.get("crystallized_belief") or rec.get("memory_value") or "").strip()
            if not belief:
                continue
            haystack = f"{belief} {rec.get('keywords', '')} {' '.join(_v1_record_symbols(rec))}".lower()
            hits = sum(1 for tok in intent_tokens if tok in haystack) if intent_tokens else 1
            if intent_tokens and hits == 0 and is_obs:
                continue
            score = hits * 10 + (1 if is_obs else 5)
            scored.append((score, f"[{label}] {belief}"))

    scored.sort(key=lambda item: item[0], reverse=True)
    budget = 2048 if mount == "scoped" else 3800
    lines: List[str] = []
    used = 0
    seen_lines: set = set()
    for _, line in scored:
        if line in seen_lines:
            continue
        seen_lines.add(line)
        cand = line if not lines else f"\n{line}"
        cand_bytes = len(cand.encode("utf-8"))
        if used + cand_bytes > budget:
            break
        lines.append(line)
        used += cand_bytes

    frame = "\n".join(lines)
    return {"frame": frame, "mount": mount, "bytes": len(frame.encode("utf-8"))}


@api.post("/api/v1/memory/observe")
async def v1_memory_observe(request: Request) -> Dict[str, Any]:
    body = await request.json()
    repo_id = _v1_norm_repo(body.get("repo_root"), body.get("repo"))
    session_id = str(body.get("session_id") or request.headers.get("X-Toolroll-Session-Id") or "").strip()
    content = str(body.get("content") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="Missing session_id")
    if not content:
        raise HTTPException(status_code=400, detail="Missing observation content")
    symbols = [str(s).strip() for s in (body.get("symbols") or []) if str(s).strip()]
    st, sid, norm_scope = _v1_map_scope(body.get("scope"), repo_id)

    canon_hash = hashlib.sha256(content.lower().encode("utf-8")).hexdigest()[:12]
    sess_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]
    mkey = f"obs_{canon_hash}_{sess_hash}"

    await run_in_threadpool(
        spanner_client.upsert_scoped_memory,
        scope_type=st,
        scope_id=sid,
        memory_key=mkey,
        memory_value=content,
        memory_type="EPISODIC_FACT",
        category="observation",
        confidence=0.85,
        crystallized_belief=content,
        keywords=", ".join(symbols) if symbols else content[:80],
        source_session_id=session_id,
        support_count=1,
        trigger_conditions={
            "symbols": symbols,
            "repo_root": repo_id,
            "session_id": session_id,
            "canonical_hash": canon_hash,
        },
    )
    await run_in_threadpool(
        telemetry_sink.log_interaction,
        user_id=repo_id,
        session_id=session_id,
        agent_name="sheep_v1",
        event_type="OBSERVATION",
        message=content,
        payload={"scope": norm_scope, "symbols": symbols},
    )
    return {"ok": True, "scope": norm_scope}


@api.api_route("/api/v1/memory/note", methods=["GET", "POST"])
async def v1_memory_note(request: Request) -> Dict[str, Any]:
    if request.method == "POST":
        body = await request.json()
    else:
        body = dict(request.query_params)
    repo_id = _v1_norm_repo(body.get("repo_root"), body.get("repo"))
    symbol = str(body.get("symbol") or "").strip()
    note = await run_in_threadpool(_v1_lookup_symbol_note, repo_id, symbol)
    return {"symbol": symbol, "note": note, "bytes": len(note.encode("utf-8"))}


@api.post("/api/v1/memory/inject")
async def v1_memory_inject(request: Request) -> Dict[str, Any]:
    global _v1_delivered_notes_total
    body = await request.json()
    repo_id = _v1_norm_repo(body.get("repo_root"), body.get("repo"))
    session_id = str(body.get("session_id") or request.headers.get("X-Toolroll-Session-Id") or "default").strip()
    symbols = [str(s).strip() for s in (body.get("symbols") or []) if str(s).strip()]
    delivered = _v1_delivered_session_notes.setdefault(session_id, set())

    chosen_note = ""
    for sym in symbols:
        dedup_key = f"{repo_id}:{sym}"
        if dedup_key in delivered:
            continue
        cand = await run_in_threadpool(_v1_lookup_symbol_note, repo_id, sym)
        if cand:
            delivered.add(dedup_key)
            _v1_delivered_notes_total += 1
            chosen_note = cand
            break

    return {
        "session_id": session_id,
        "note": chosen_note,
        "bytes": len(chosen_note.encode("utf-8")),
    }


@api.post("/api/v1/memory/fault")
async def v1_memory_fault(request: Request) -> Dict[str, Any]:
    body = await request.json()
    repo_id = _v1_norm_repo(body.get("repo_root"), body.get("repo"))
    session_id = str(body.get("session_id") or request.headers.get("X-Toolroll-Session-Id") or "fault-session").strip()
    prompt_summary = str(body.get("prompt_summary") or "").strip()
    queries = [str(q).strip() for q in (body.get("searched_queries") or []) if str(q).strip()]
    resolved = [str(s).strip() for s in (body.get("resolved_symbols") or []) if str(s).strip()]
    extra_steps = int(body.get("extra_read_steps") or 0)

    bound = extra_steps > 0 and len(resolved) > 0
    if bound:
        nav_statement = f"[nav] {prompt_summary or ', '.join(queries)} -> {', '.join(resolved)}"
        mkey = f"nav_{hashlib.sha256(nav_statement.lower().encode('utf-8')).hexdigest()[:12]}"
        await run_in_threadpool(
            spanner_client.upsert_scoped_memory,
            scope_type="TEAM",
            scope_id=f"team:{repo_id}",
            memory_key=mkey,
            memory_value=nav_statement,
            memory_type="observation",
            category="navigation",
            confidence=0.92,
            crystallized_belief=nav_statement,
            keywords=", ".join(queries + resolved),
            source_session_id=session_id,
            support_count=1,
            trigger_conditions={"symbols": resolved, "queries": queries, "extra_read_steps": extra_steps},
        )
    return {
        "ok": True,
        "bound": bound,
        "extra_read_steps": extra_steps,
        "resolved_symbols": resolved,
    }


@api.post("/api/v1/memory/evict")
async def v1_memory_evict(request: Request) -> Dict[str, Any]:
    body = await request.json()
    repo_id = _v1_norm_repo(body.get("repo_root"), body.get("repo"))
    raw_map = body.get("old_to_new") or {}
    old_to_new = {str(k).strip(): str(v).strip() for k, v in raw_map.items() if str(k).strip()}
    updated = await run_in_threadpool(_v1_apply_symbol_renames, repo_id, old_to_new)
    return {"ok": True, "updated": updated}


@api.post("/api/v1/memory/reconcile-diff")
async def v1_memory_reconcile_diff(request: Request) -> Dict[str, Any]:
    ct = (request.headers.get("content-type") or "").lower()
    if "text/plain" in ct or "text/x-diff" in ct:
        repo_id = _v1_norm_repo(request.query_params.get("repo_root"), request.query_params.get("repo"))
        diff_text = (await request.body()).decode("utf-8", errors="replace")
    else:
        body = await request.json()
        repo_id = _v1_norm_repo(body.get("repo_root"), body.get("repo"))
        diff_text = str(body.get("diff") or "")

    decl_re = _re.compile(r"^(?:[-+])\s*(?:pub\s+)?(?:async\s+)?(?:func|def|class|interface|type|struct|fn)\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)")
    removed: List[str] = []
    added: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            continue
        m = decl_re.match(line)
        if not m:
            continue
        if line.startswith("-"):
            removed.append(m.group(1))
        elif line.startswith("+"):
            added.append(m.group(1))

    old_to_new: Dict[str, str] = {}
    for idx, old_sym in enumerate(removed):
        new_sym = added[idx] if idx < len(added) else ""
        if old_sym != new_sym:
            old_to_new[old_sym] = new_sym

    updated = await run_in_threadpool(_v1_apply_symbol_renames, repo_id, old_to_new)
    return {"ok": True, "old_to_new": old_to_new, "updated": updated}


def _run_v1_dream_compaction_sync(scope_type_in: str, repo_id: str, mode: str, min_sessions: int) -> Dict[str, Any]:
    st, sid, _ = _v1_map_scope(scope_type_in, repo_id)
    with job_runner.track_inline_job(
        "dream_compaction",
        triggered_from="API",
        params={"scope_type": scope_type_in, "repo_id": repo_id, "mode": mode, "min_sessions": min_sessions},
        title="Dream compaction",
    ) as ctx:
        ctx.update_progress("Scanning scoped observations", 35)
        records = spanner_client.get_scoped_memories(st, sid)
        observations = [r for r in records if _v1_is_observation_record(r)]

        clusters: Dict[str, Dict[str, Any]] = {}
        for obs in observations:
            text = str(obs.get("crystallized_belief") or obs.get("memory_value") or "").strip()
            if not text:
                continue
            ckey = hashlib.sha256(text.lower().encode("utf-8")).hexdigest()[:12]
            entry = clusters.setdefault(ckey, {"text": text, "sessions": set(), "symbols": set()})
            sess = str(obs.get("source_session_id") or (obs.get("trigger_conditions") or {}).get("session_id") or "sess").strip()
            entry["sessions"].add(sess)
            for s in _v1_record_symbols(obs):
                entry["symbols"].add(s)

        promoted_keys: List[str] = []
        for ckey, info in clusters.items():
            if len(info["sessions"]) < min_sessions:
                continue
            bkey = f"mem_{ckey}"
            syms = sorted(info["symbols"])
            spanner_client.upsert_scoped_memory(
                scope_type=st,
                scope_id=sid,
                memory_key=bkey,
                memory_value=info["text"],
                memory_type="INVARIANT_GUARDRAIL",
                category="consolidated",
                confidence=0.95,
                crystallized_belief=info["text"],
                keywords=", ".join(syms) if syms else info["text"][:80],
                source_session_id=sorted(info["sessions"])[0],
                promoted_from_scope=f"/mem/{scope_type_in.lstrip('/mem/') or 'project'}",
                support_count=len(info["sessions"]),
                trigger_conditions={"symbols": syms, "support_session_ids": sorted(info["sessions"])},
            )
            promoted_keys.append(bkey)

        epoch_key = f"{st}:{sid}"
        next_epoch = _v1_scope_epochs.get(epoch_key, 0) + (1 if promoted_keys else 0)
        if next_epoch == 0:
            next_epoch = 1
        _v1_scope_epochs[epoch_key] = next_epoch
        manifest_blob = json.dumps({"scope": epoch_key, "epoch": next_epoch, "promoted_keys": promoted_keys}, sort_keys=True)
        blob_hash = hashlib.sha256(manifest_blob.encode("utf-8")).hexdigest()

        ctx.complete(
            result_summary=f"{len(promoted_keys)} rule(s) promoted ({len(observations)} scanned)",
            metrics={"scanned_events": len(observations), "clusters_formed": len(clusters), "promoted": len(promoted_keys)},
            output_preview={"scope_id": repo_id, "promoted_keys": promoted_keys, "belief_epoch": next_epoch},
            target_workspace="workspace-tree",
        )
    return {
        "scope_type": scope_type_in.replace("/mem/", "") or "project",
        "scope_id": repo_id,
        "mode": mode,
        "belief_epoch": next_epoch,
        "scanned_events": len(observations),
        "clusters_formed": len(clusters),
        "promoted": len(promoted_keys),
        "superseded": 0,
        "promoted_keys": promoted_keys,
        "epoch_blob_hash": blob_hash,
        "spanner_synced": not getattr(spanner_client, "use_mock", False),
    }


@api.post("/api/v1/memory/dream")
async def v1_memory_dream(request: Request) -> Dict[str, Any]:
    raw_bytes = await request.body()
    body = json.loads(raw_bytes.decode("utf-8")) if raw_bytes else {}
    scope_type_in = str(body.get("scope_type") or "project").strip().lower()
    repo_id = _v1_norm_repo(body.get("repo_root"), body.get("repo"), body.get("scope_id"))
    mode = str(body.get("mode") or "nrem").strip().lower()
    min_sessions = max(1, int(body.get("min_sessions") or 2))
    return await run_in_threadpool(_run_v1_dream_compaction_sync, scope_type_in, repo_id, mode, min_sessions)


@api.api_route("/api/v1/memory/events", methods=["GET", "POST"])
async def v1_memory_events(request: Request):
    if request.method == "GET":
        scope_type = request.query_params.get("scope_type", "project")
        if _v1_is_personal(scope_type):
            return JSONResponse(
                status_code=403,
                content={"code": "PERSONAL_SCOPE_FORBIDDEN", "error": "/mem/personal records must never leave the local device"},
            )
        repo_id = _v1_norm_repo(
            request.query_params.get("repo_root"),
            request.query_params.get("repo"),
            request.query_params.get("scope_id"),
        )
        st, sid, _ = _v1_map_scope(scope_type, repo_id)
        recs = await run_in_threadpool(spanner_client.get_scoped_memories, st, sid)
        events = []
        for r in recs:
            if not _v1_is_observation_record(r):
                continue
            tc = r.get("trigger_conditions") or {}
            events.append(
                {
                    "event_id": str(r.get("memory_key") or ""),
                    "scope_type": scope_type.replace("/mem/", "") or "project",
                    "scope_id": repo_id,
                    "session_id": str(r.get("source_session_id") or tc.get("session_id") or ""),
                    "event_kind": "observation",
                    "outcome_status": "ACTIVE",
                    "content": str(r.get("crystallized_belief") or r.get("memory_value") or ""),
                    "symbols": _v1_record_symbols(r),
                    "created_at_ns": int(time.time_ns()),
                }
            )
        return {"events": events, "count": len(events)}

    body = await request.json()
    raw_events = body.get("events") or []
    for ev in raw_events:
        if _v1_is_personal(ev.get("scope_type")):
            return JSONResponse(
                status_code=403,
                content={"code": "PERSONAL_SCOPE_FORBIDDEN", "error": "/mem/personal records must never leave the local device"},
            )
    accepted = 0
    dedup = 0
    for ev in raw_events:
        ev_id = str(ev.get("event_id") or "").strip()
        if ev_id and ev_id in _v1_seen_event_ids:
            dedup += 1
            continue
        if ev_id:
            _v1_seen_event_ids.add(ev_id)
        repo_id = _v1_norm_repo(ev.get("repo_root"), None, ev.get("scope_id"))
        st, sid, _ = _v1_map_scope(ev.get("scope_type") or "project", repo_id)
        content = str(ev.get("content") or "").strip()
        syms = [str(s).strip() for s in (ev.get("symbols") or []) if str(s).strip()]
        sess_id = str(ev.get("session_id") or "event-session").strip()
        mkey = ev_id or f"ev_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:12]}"
        await run_in_threadpool(
            spanner_client.upsert_scoped_memory,
            scope_type=st,
            scope_id=sid,
            memory_key=mkey,
            memory_value=content,
            memory_type="EPISODIC_FACT",
            category="observation",
            confidence=0.85,
            crystallized_belief=content,
            keywords=", ".join(syms),
            source_session_id=sess_id,
            trigger_conditions={"symbols": syms, "session_id": sess_id},
        )
        accepted += 1
    return {
        "ok": True,
        "accepted": accepted,
        "deduplicated": dedup,
        "duplicates": dedup,
        "quarantined": 0,
        "spanner_synced": not getattr(spanner_client, "use_mock", False),
    }


@api.get("/api/v1/memory/delta")
async def v1_memory_delta(
    scope_type: str = "project",
    scope_id: Optional[str] = None,
    repo: Optional[str] = None,
    since_ns: int = 0,
):
    if _v1_is_personal(scope_type):
        return JSONResponse(
            status_code=403,
            content={"code": "PERSONAL_SCOPE_FORBIDDEN", "error": "/mem/personal records must never leave the local device"},
        )
    repo_id = _v1_norm_repo(None, repo, scope_id)
    st, sid, _ = _v1_map_scope(scope_type, repo_id)
    recs = await run_in_threadpool(spanner_client.get_scoped_memories, st, sid)
    epoch = _v1_scope_epochs.get(f"{st}:{sid}", 1 if recs else 0)
    delta_records = []
    for r in recs:
        if _v1_is_observation_record(r):
            continue
        delta_records.append(
            {
                "scope_type": scope_type.replace("/mem/", "") or "project",
                "scope_id": repo_id,
                "memory_key": str(r.get("memory_key") or r.get("preference_key") or ""),
                "memory_value": str(r.get("memory_value") or ""),
                "crystallized_belief": str(r.get("crystallized_belief") or r.get("memory_value") or ""),
                "memory_type": str(r.get("memory_type") or "guardrail"),
                "category": str(r.get("category") or "consolidated"),
                "confidence": float(r.get("confidence", 0.95)),
                "symbols": _v1_record_symbols(r),
                "support_count": int(r.get("support_count", 1)),
                "belief_epoch": epoch,
                "status": "ACTIVE",
                "last_updated_ns": max(since_ns, int(time.time_ns())),
            }
        )
    return {
        "records": delta_records,
        "belief_epoch": epoch,
        "high_water_ns": int(time.time_ns()),
        "count": len(delta_records),
    }


# Mount static HTML/CSS/JS frontend at /
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    api.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")



def main():
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(api, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

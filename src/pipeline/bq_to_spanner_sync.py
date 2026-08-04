"""Pipeline for summarizing BigQuery telemetry session logs and storing learnings in Google Cloud Spanner."""

import json
import re
import logging
from typing import Any, Dict, List, Optional
from src.common.config import get_config
from src.common.spanner_client import SpannerPreferenceClient
from src.common.telemetry import BigQueryTelemetrySink

from google import genai
from google.genai import types

logger = logging.getLogger("adk.pipeline.bq_to_spanner")


class BqToSpannerSynchronizer:
    """Synchronizes user session learnings from BigQuery logs into Google Cloud Spanner."""

    def __init__(
        self,
        telemetry_sink: Optional[BigQueryTelemetrySink] = None,
        spanner_client: Optional[SpannerPreferenceClient] = None,
        use_mock: Optional[bool] = None,
    ):
        self.config = get_config()
        self.use_mock = self.config.use_mock_clients if use_mock is None else use_mock
        self.telemetry_sink = telemetry_sink or BigQueryTelemetrySink(use_mock=self.use_mock)
        self.spanner_client = spanner_client or SpannerPreferenceClient(use_mock=self.use_mock)

    def summarize_session_logs(
        self,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        session_ids: Optional[List[str]] = None,
        multi_session: bool = False,
    ) -> Dict[str, Any]:
        """Perform offline REM Memory Consolidation (Agent Dreaming) over waking logs from BigQuery.

        Supports both single-session intent capture and multi-session cross-session consolidation.
        """
        if multi_session or session_ids or session_id in ("ALL", None, ""):
            if not user_id:
                raise ValueError("user_id is required for multi-session dream compaction")
            return self.summarize_multi_session_logs(user_id=user_id, session_ids=session_ids)

        assert session_id is not None
        assert user_id is not None
        logs = self.telemetry_sink.get_session_logs(session_id=session_id)
        logger.info(f"Retrieved {len(logs)} logs for session {session_id} (user={user_id})")

        # Sort logs chronologically (oldest to newest) for conversation transcript construction
        sorted_logs = sorted(logs, key=lambda x: str(x.get("timestamp", "")))
        transcript_lines = []
        for l in sorted_logs:
            evt = l.get("event_type", "MSG")
            msg = l.get("message", "")
            if msg:
                transcript_lines.append(f"{evt}: {msg}")
        full_transcript = "\n".join(transcript_lines)

        if not full_transcript.strip():
            logger.warning(f"Session {session_id} has no transcript lines; returning empty dream.")
            return {
                "user_id": user_id,
                "session_id": session_id,
                "summary": f"Empty session {session_id}",
                "session_intent": "",
                "intent_category": "general",
                "intent_confidence": 0.0,
                "preferences": [],
                "dream_prompt": "No waking logs found for session; dream harvest skipped.",
            }

        dream_prompt = f"""
You are an AI REM Memory Synthesizer ("Dream Engine") performing offline memory consolidation over waking conversation logs for user '{user_id}' in session '{session_id}'.
Your goal is to crystallize short-term episodic interactions into clean, accurate, high-confidence long-term semantic beliefs and capture user intent.

Analyze the conversation transcript below:
{full_transcript}

Rules:
1. Extract the user's primary "session_intent": what is the user's underlying goal, motivation, or project focus in this session? (e.g., "The user is establishing their technical identity as a Principal Cloud Architect to explore Python and infrastructure tools.").
2. Extract the "intent_category": choose one of "onboarding", "exploratory", "architecture_design", "troubleshooting", "profile", or "general".
3. Provide an "intent_confidence" float score between 0.0 and 1.0.
4. Extract every distinct preference, technical skill, job role, favorite language, preferred tool, or identity attribute.
5. NEVER confuse or merge the user's name with their job role. For example, if the transcript mentions "My name is Jason, my favorite programming language is Python, and my job role is Principal Cloud Architect", extract:
   - preference_key: "name", preference_value: "Jason", category: "profile"
   - preference_key: "favorite_language", preference_value: "Python", category: "technical"
   - preference_key: "job_role", preference_value: "Principal Cloud Architect", category: "role"
6. For each preference record, provide:
   - "preference_key": canonical snake_case key (e.g., "favorite_language", "job_role", "name", "preferred_tools", "work_style").
   - "preference_value": concise exact value (e.g., "Python", "Principal Cloud Architect", "Jason").
   - "category": "profile", "technical", "role", "infrastructure", or "preference".
   - "confidence": float between 0.0 and 1.0.
   - "crystallized_belief": A complete natural language semantic belief statement (e.g., "The user Jason works as a Principal Cloud Architect specializing in cloud infrastructure." or "The user's favorite programming language is Python.").
   - "keywords": A comma-separated list of semantic search keywords and synonyms (e.g., "python, language, programming, code, coding", or "architect, principal, cloud, role, job, title, career", or "jason, name, identity").
7. Also provide a 2-sentence overall profile summary.

Return a valid JSON object matching this schema:
{{
  "user_id": "{user_id}",
  "session_id": "{session_id}",
  "summary": "Overall profile summary text...",
  "session_intent": "The user's primary intent in this session...",
  "intent_category": "profile",
  "intent_confidence": 0.95,
  "preferences": [
    {{
      "preference_key": "favorite_language",
      "preference_value": "Python",
      "category": "technical",
      "confidence": 0.98,
      "crystallized_belief": "The user's favorite programming language is Python.",
      "keywords": "python, language, programming, code, coding"
    }}
  ]
}}
"""

        try:
            client = genai.Client()
            response = client.models.generate_content(
                model=self.config.model_name,
                contents=dream_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            return self._parse_summary_json(response.text, user_id, session_id, dream_prompt=dream_prompt)
        except Exception as e:
            logger.error(f"Live REM Memory Consolidation (Gemini via Vertex AI) failed: {e}")
            raise e

    def summarize_multi_session_logs(
        self,
        user_id: str,
        session_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Perform Cross-Session REM Memory Compaction over multiple waking sessions for a user.

        Synthesizes overarching user intent, intent trajectory across sessions, and deduplicates/resolves
        preference conflicts across multiple sessions.
        """
        logs = self.telemetry_sink.get_user_logs(user_id=user_id, session_ids=session_ids)
        logger.info(f"Retrieved {len(logs)} total logs across sessions for user={user_id}")

        # Sort logs chronologically (oldest to newest) for multi-session transcript construction
        sorted_logs = sorted(logs, key=lambda x: str(x.get("timestamp", "")))
        sessions_seen = []
        grouped_lines = []
        current_session = None
        for l in sorted_logs:
            sid = l.get("session_id", "default_session")
            if sid not in sessions_seen:
                sessions_seen.append(sid)
            if sid != current_session:
                current_session = sid
                grouped_lines.append(f"\n=== SESSION: {sid} ===")
            evt = l.get("event_type", "MSG")
            msg = l.get("message", "")
            if msg:
                grouped_lines.append(f"[{l.get('timestamp', '')}] {evt}: {msg}")
        multi_session_transcript = "\n".join(grouped_lines)

        if not multi_session_transcript.strip():
            logger.warning(f"User {user_id} has no transcript lines across sessions; returning empty multi-session dream.")
            return {
                "user_id": user_id,
                "session_id": "MULTI_SESSION_SYNTHESIS",
                "sessions_analyzed": [],
                "session_count": 0,
                "summary": f"No logs found for user {user_id}",
                "cross_session_intent": "",
                "intent_category": "general",
                "intent_trajectory": "No session trajectory available.",
                "preferences": [],
                "dream_prompt": "No waking logs found across sessions; multi-session dream harvest skipped.",
            }

        multi_session_dream_prompt = f"""
You are an AI REM Multi-Session Memory Synthesizer ("Dream Engine") performing cross-session memory consolidation and trajectory analysis over multiple waking conversation sessions for user '{user_id}'.
Your goal is to crystallize information over multiple sessions, track how the user's intent evolved across sessions, and synthesize a single unified, deduplicated set of long-term semantic beliefs.

Analyze the multi-session conversation transcript below:
{multi_session_transcript}

Rules:
1. Extract "cross_session_intent": an overarching statement capturing the user's primary goals, intent, and focus across all sessions.
2. Extract "intent_trajectory": a 2-3 sentence narrative describing how the user's questions, goals, and focus evolved from earlier sessions to later sessions (e.g., "In initial sessions, the user focused on establishing their profile and favorite languages. In subsequent sessions, they progressed to architecture discussions around low-latency Spanner caching and multi-agent orchestration.").
3. Extract "intent_category": one of "onboarding", "exploratory", "architecture_design", "troubleshooting", "profile", or "general".
4. Extract every distinct user preference across ALL sessions (favorite_language, job_role, name, preferred_tools, work_style, deploy_target, etc.).
5. CROSS-SESSION DEDUPLICATION & CONFLICT RESOLUTION:
   - If a preference appears in multiple sessions with the same value, merge it and reinforce its confidence (up to 1.0).
   - If a preference changed over time across sessions (e.g., language preference changed in a later session), let the latest session win and record the current belief.
   - For each preference, include "source_session_id" indicating which session established it (or "multi_session" if reinforced across multiple sessions).
6. Provide an overall 2-3 sentence executive profile summary ("summary").

Return a valid JSON object matching this schema:
{{
  "user_id": "{user_id}",
  "session_id": "MULTI_SESSION_SYNTHESIS",
  "summary": "Overall cross-session executive profile summary...",
  "cross_session_intent": "The user's overarching intent across all sessions...",
  "intent_trajectory": "In session 1 the user explored X, and in session 2 progressed to Y...",
  "intent_category": "architecture_design",
  "preferences": [
    {{
      "preference_key": "favorite_language",
      "preference_value": "Python",
      "category": "technical",
      "confidence": 0.99,
      "source_session_id": "session-1",
      "crystallized_belief": "The user's favorite programming language is Python.",
      "keywords": "python, language, programming, code, coding"
    }}
  ]
}}
"""

        try:
            client = genai.Client()
            response = client.models.generate_content(
                model=self.config.model_name,
                contents=multi_session_dream_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            parsed = self._parse_summary_json(response.text, user_id, "MULTI_SESSION_SYNTHESIS", dream_prompt=multi_session_dream_prompt)
            parsed["sessions_analyzed"] = sessions_seen
            parsed["session_count"] = len(sessions_seen)
            return parsed
        except Exception as e:
            logger.error(f"Live Multi-Session REM Memory Consolidation failed: {e}")
            raise e

    def _parse_summary_json(self, raw_json: str, user_id: str, session_id: str, dream_prompt: Optional[str] = None) -> Dict[str, Any]:
        """Parse JSON output from Gemini REM Synthesis for single or multiple sessions."""
        try:
            cleaned = raw_json.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            data = json.loads(cleaned)
            key_alias = {
                "favorite_programming_language": "favorite_language",
                "preferred_database_and_analytics_tools": "preferred_tools",
                "preferred_database_tools": "preferred_tools",
                "preferred_infrastructure_tools": "preferred_tools",
                "preferred_databases": "preferred_tools",
                "database_tools": "preferred_tools",
                "role": "job_role",
                "user_name": "name",
                "intent": "session_intent",
                "goal": "session_intent",
                "overall_intent": "cross_session_intent",
            }
            for pref in data.get("preferences", []):
                pk = pref.get("preference_key", "")
                if pk in key_alias:
                    pref["preference_key"] = key_alias[pk]

            intent_text = data.get("session_intent") or data.get("cross_session_intent") or data.get("intent") or ""
            intent_cat = data.get("intent_category", "general")
            intent_conf = float(data.get("intent_confidence", 1.0))
            intent_traj = data.get("intent_trajectory", "")

            # Automatically synthesize preference records for intent and trajectory so Spanner can serve them via cache
            prefs = data.get("preferences", [])
            if intent_text and not any(p.get("preference_key") in ("session_intent", "cross_session_intent") for p in prefs):
                prefs.append({
                    "preference_key": "session_intent" if session_id != "MULTI_SESSION_SYNTHESIS" else "cross_session_intent",
                    "preference_value": intent_text,
                    "category": "intent",
                    "confidence": intent_conf,
                    "source_session_id": session_id,
                    "crystallized_belief": intent_text,
                    "keywords": "intent, goal, objective, purpose, focus, session, cross session, multi session",
                })
            if intent_traj and not any(p.get("preference_key") == "intent_trajectory" for p in prefs):
                prefs.append({
                    "preference_key": "intent_trajectory",
                    "preference_value": intent_traj,
                    "category": "intent",
                    "confidence": 1.0,
                    "source_session_id": session_id,
                    "crystallized_belief": intent_traj,
                    "keywords": "trajectory, evolution, progress, sessions, history, timeline, multi session",
                })

            return {
                "user_id": data.get("user_id", user_id),
                "session_id": data.get("session_id", session_id),
                "summary": data.get("summary", ""),
                "session_intent": intent_text,
                "intent": intent_text,
                "intent_category": intent_cat,
                "intent_confidence": intent_conf,
                "intent_trajectory": intent_traj,
                "preferences": prefs,
                "dream_prompt": dream_prompt or "",
            }
        except Exception as e:
            logger.error(f"Error parsing Gemini summary JSON ({e}): {raw_json}")
            raise ValueError(f"Invalid JSON returned by Dream Engine: {e}")

    def sync_to_spanner(
        self,
        user_id: str,
        session_id: Optional[str] = None,
        session_ids: Optional[List[str]] = None,
        multi_session: bool = False,
    ) -> Dict[str, Any]:
        """Run the full sync: summarize BQ logs for a session (or multiple sessions) and upsert into Google Cloud Spanner."""
        if multi_session or session_ids or session_id in ("ALL", None, ""):
            return self.sync_multi_session_to_spanner(user_id=user_id, session_ids=session_ids)

        assert session_id is not None
        summary = self.summarize_session_logs(session_id=session_id, user_id=user_id)

        # 1. Store session summary and intent
        summary_text = summary.get("summary", "")
        if summary.get("session_intent"):
            summary_text = f"[Intent: {summary.get('session_intent')}] {summary_text}"

        self.spanner_client.upsert_session_summary(
            user_id=user_id,
            session_id=session_id,
            summary_text=summary_text,
        )

        # 2. Store individual preferences with crystallized beliefs and search tags
        upserted_prefs = []
        for pref in summary.get("preferences", []):
            rec = self.spanner_client.upsert_user_preference(
                user_id=user_id,
                preference_key=pref["preference_key"],
                preference_value=str(pref["preference_value"]),
                category=pref.get("category", "general"),
                confidence=float(pref.get("confidence", 1.0)),
                source_session_id=pref.get("source_session_id", session_id),
                crystallized_belief=pref.get("crystallized_belief", ""),
                keywords=pref.get("keywords", ""),
            )
            upserted_prefs.append(rec)

        project_id = self.config.project_id
        dataset_id = self.config.bq_dataset_id
        table_id = self.config.bq_table_id

        bq_select_query = f"""-- Waking Telemetry Extraction (Google BigQuery)
SELECT session_id, user_id, event_type, message, timestamp
FROM `{project_id}.{dataset_id}.{table_id}`
WHERE session_id = '{session_id}'
ORDER BY timestamp ASC;"""

        bq_ai_query = f"""-- BigQuery AI Batch Synthesizer (ML.GENERATE_TEXT / AI.GENERATE)
SELECT ml_generate_text_result['content'] AS distilled_preferences
FROM ML.GENERATE_TEXT(
  MODEL `{project_id}.{dataset_id}.gemini_3_5_flash_lite`,
  (
    SELECT CONCAT('Analyze these waking logs and distill JSON beliefs:\\n', STRING_AGG(message, '\\n')) AS prompt
    FROM `{project_id}.{dataset_id}.{table_id}`
    WHERE session_id = '{session_id}'
  )
);"""

        spanner_queries = [
            f"""-- Crystalline Memory SessionSummary Upsert (Google Cloud Spanner)\nINSERT OR UPDATE SessionSummary (user_id, session_id, summary_text, updated_at)\nVALUES ('{user_id}', '{session_id}', '{str(summary_text).replace("'", "''")}', PENDING_COMMIT_TIMESTAMP());"""
        ]
        for pref in summary.get("preferences", []):
            pk = pref.get("preference_key", "")
            pv = str(pref.get("preference_value", "")).replace("'", "''")
            cat = pref.get("category", "general")
            conf = float(pref.get("confidence", 1.0))
            src_sid = pref.get("source_session_id", session_id)
            belief = str(pref.get("crystallized_belief", "")).replace("'", "''")
            kw = str(pref.get("keywords", "")).replace("'", "''")
            spanner_queries.append(
                f"""-- Upsert preference '{pk}'\nINSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)\nVALUES ('{user_id}', '{pk}', '{pv}', '{cat}', {conf}, '{src_sid}', '{belief}', '{kw}', PENDING_COMMIT_TIMESTAMP());"""
            )

        queries_run = {
            "bigquery_select": bq_select_query,
            "bigquery_ai_generate": bq_ai_query,
            "spanner_upserts": spanner_queries,
        }

        logger.info(f"Successfully synced {len(upserted_prefs)} preferences to Spanner for user={user_id}")
        return {
            "user_id": user_id,
            "session_id": session_id,
            "summary": summary.get("summary", ""),
            "intent": summary.get("session_intent") or summary.get("intent", ""),
            "intent_category": summary.get("intent_category", "general"),
            "intent_confidence": summary.get("intent_confidence", 1.0),
            "preferences_synced": len(upserted_prefs),
            "records": upserted_prefs,
            "prompt_used": summary.get("dream_prompt", ""),
            "queries_run": queries_run,
        }

    def sync_multi_session_to_spanner(
        self,
        user_id: str,
        session_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Run multi-session cross-session dream compaction and upsert consolidated beliefs and intent trajectory to Spanner."""
        summary = self.summarize_multi_session_logs(user_id=user_id, session_ids=session_ids)

        # 1. Store cross-session summary and intent trajectory
        summary_text = summary.get("summary", "")
        if summary.get("intent_trajectory"):
            summary_text = f"[Intent Trajectory: {summary.get('intent_trajectory')}] {summary_text}"
        elif summary.get("intent"):
            summary_text = f"[Cross-Session Intent: {summary.get('intent')}] {summary_text}"

        self.spanner_client.upsert_session_summary(
            user_id=user_id,
            session_id="MULTI_SESSION_SYNTHESIS",
            summary_text=summary_text,
        )

        # 2. Store consolidated preferences
        upserted_prefs = []
        for pref in summary.get("preferences", []):
            rec = self.spanner_client.upsert_user_preference(
                user_id=user_id,
                preference_key=pref["preference_key"],
                preference_value=str(pref["preference_value"]),
                category=pref.get("category", "general"),
                confidence=float(pref.get("confidence", 1.0)),
                source_session_id=pref.get("source_session_id", "MULTI_SESSION_SYNTHESIS"),
                crystallized_belief=pref.get("crystallized_belief", ""),
                keywords=pref.get("keywords", ""),
            )
            upserted_prefs.append(rec)

        project_id = self.config.project_id
        dataset_id = self.config.bq_dataset_id
        table_id = self.config.bq_table_id

        where_clause = f"user_id = '{user_id}'"
        if session_ids:
            sids_str = ", ".join(f"'{s}'" for s in session_ids)
            where_clause += f" AND session_id IN ({sids_str})"

        bq_select_query = f"""-- Multi-Session Waking Telemetry Extraction (Google BigQuery)
SELECT session_id, user_id, event_type, message, timestamp
FROM `{project_id}.{dataset_id}.{table_id}`
WHERE {where_clause}
ORDER BY timestamp ASC;"""

        bq_ai_query = f"""-- BigQuery AI Cross-Session Synthesizer (ML.GENERATE_TEXT / AI.GENERATE)
SELECT ml_generate_text_result['content'] AS distilled_preferences
FROM ML.GENERATE_TEXT(
  MODEL `{project_id}.{dataset_id}.gemini_3_5_flash_lite`,
  (
    SELECT CONCAT('Analyze these waking logs across sessions and distill JSON beliefs:\\n', STRING_AGG(message, '\\n')) AS prompt
    FROM `{project_id}.{dataset_id}.{table_id}`
    WHERE {where_clause}
  )
);"""

        spanner_queries = [
            f"""-- Crystalline Memory Multi-Session Summary Upsert (Google Cloud Spanner)\nINSERT OR UPDATE SessionSummary (user_id, session_id, summary_text, updated_at)\nVALUES ('{user_id}', 'MULTI_SESSION_SYNTHESIS', '{str(summary_text).replace("'", "''")}', PENDING_COMMIT_TIMESTAMP());"""
        ]
        for pref in summary.get("preferences", []):
            pk = pref.get("preference_key", "")
            pv = str(pref.get("preference_value", "")).replace("'", "''")
            cat = pref.get("category", "general")
            conf = float(pref.get("confidence", 1.0))
            src_sid = pref.get("source_session_id", "MULTI_SESSION_SYNTHESIS")
            belief = str(pref.get("crystallized_belief", "")).replace("'", "''")
            kw = str(pref.get("keywords", "")).replace("'", "''")
            spanner_queries.append(
                f"""-- Upsert preference '{pk}'\nINSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)\nVALUES ('{user_id}', '{pk}', '{pv}', '{cat}', {conf}, '{src_sid}', '{belief}', '{kw}', PENDING_COMMIT_TIMESTAMP());"""
            )

        queries_run = {
            "bigquery_select": bq_select_query,
            "bigquery_ai_generate": bq_ai_query,
            "spanner_upserts": spanner_queries,
        }

        logger.info(f"Successfully synced {len(upserted_prefs)} cross-session preferences to Spanner for user={user_id}")
        return {
            "user_id": user_id,
            "session_id": "MULTI_SESSION_SYNTHESIS",
            "summary": summary.get("summary", ""),
            "intent": summary.get("intent", ""),
            "intent_category": summary.get("intent_category", "general"),
            "intent_trajectory": summary.get("intent_trajectory", ""),
            "sessions_analyzed": summary.get("sessions_analyzed", []),
            "preferences_synced": len(upserted_prefs),
            "records": upserted_prefs,
            "prompt_used": summary.get("dream_prompt", ""),
            "queries_run": queries_run,
        }


def run_sync_pipeline(
    user_id: str,
    session_id: Optional[str] = None,
    session_ids: Optional[List[str]] = None,
    multi_session: bool = False,
) -> Dict[str, Any]:
    """Helper entry point for running the BQ-to-Spanner sync (single-session or multi-session)."""
    synchronizer = BqToSpannerSynchronizer()
    return synchronizer.sync_to_spanner(
        user_id=user_id,
        session_id=session_id,
        session_ids=session_ids,
        multi_session=multi_session,
    )


def run_multi_session_sync_pipeline(user_id: str, session_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Helper entry point for running multi-session cross-session dream compaction."""
    synchronizer = BqToSpannerSynchronizer()
    return synchronizer.sync_multi_session_to_spanner(user_id=user_id, session_ids=session_ids)

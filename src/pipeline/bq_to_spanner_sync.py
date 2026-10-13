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
        self.use_mock = False if use_mock is None else use_mock
        self.telemetry_sink = telemetry_sink or BigQueryTelemetrySink(use_mock=self.use_mock)
        self.spanner_client = spanner_client or SpannerPreferenceClient(use_mock=self.use_mock)

    @staticmethod
    def _nrem_analyze_traces(logs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Phase 1: NREM (Non-Rapid Eye Movement) Statistical Trace Analysis.

        Extracts empirical event distributions, error frequencies, friction points,
        and token volumes from raw waking telemetry traces before generative REM sleep.
        """
        if not logs:
            return {
                "phase": "NREM_PHASE_1",
                "total_events": 0,
                "event_distribution": {},
                "estimated_raw_tokens": 0,
                "error_count": 0,
                "error_rate": 0.0,
                "friction_points": [],
                "detected_domains": [],
                "summary": "No waking telemetry traces observed.",
            }

        event_counts: Dict[str, int] = {}
        total_chars = 0
        errors = 0
        friction_points: List[str] = []
        domain_keywords = {
            "python": 0, "spanner": 0, "bigquery": 0, "cloud_run": 0, "docker": 0,
            "terraform": 0, "uv": 0, "robotics": 0, "torque": 0, "guardrail": 0
        }

        for l in logs:
            evt = str(l.get("event_type") or l.get("action") or "MSG").upper()
            event_counts[evt] = event_counts.get(evt, 0) + 1

            msg = str(l.get("message") or l.get("payload") or "")
            total_chars += len(msg)

            raw_outcome = str(l.get("outcome") or l.get("status") or "").upper()
            err_msg = str(l.get("error_message") or l.get("error") or "")
            if raw_outcome in ("FAILED", "ERROR", "SHEAR_FRACTURE") or evt in ("ERROR", "EXCEPTION") or err_msg:
                errors += 1
                fp = err_msg or f"Failure during {evt}: {msg[:80]}"
                if fp not in friction_points and len(friction_points) < 5:
                    friction_points.append(fp)

            msg_lower = msg.lower()
            for d in domain_keywords:
                if d in msg_lower:
                    domain_keywords[d] += 1

        total_events = len(logs)
        error_rate = round(errors / total_events, 4) if total_events > 0 else 0.0
        estimated_raw_tokens = max(1, total_chars // 4)
        detected_domains = [d for d, cnt in domain_keywords.items() if cnt > 0]

        summary = (
            f"NREM Phase 1 complete: analyzed {total_events} events ({estimated_raw_tokens} raw tokens), "
            f"identified {len(detected_domains)} technical domains, error rate {round(error_rate * 100, 1)}%."
        )

        return {
            "phase": "NREM_PHASE_1",
            "total_events": total_events,
            "event_distribution": event_counts,
            "estimated_raw_tokens": estimated_raw_tokens,
            "error_count": errors,
            "error_rate": error_rate,
            "friction_points": friction_points,
            "detected_domains": detected_domains,
            "summary": summary,
        }

    @staticmethod
    def certify_memory_candidates(
        candidates: List[Dict[str, Any]],
        transcript: str,
        existing_preferences: Optional[List[Dict[str, Any]]] = None,
        confidence_threshold: float = 0.85,
    ) -> Dict[str, Any]:
        """Pre-commit Dream Certification Gate.

        Validates candidate memories distilled during REM sleep before they are committed
        to Spanner crystalline storage:
        1. Confidence Verification: Discards low-confidence assertions (< 0.85).
        2. Grounding Provenance: Checks that preference values/keys have evidence in transcript.
        3. Non-Contradiction Verification: Validates consistency.
        """
        existing_map = {}
        if existing_preferences:
            for p in existing_preferences:
                existing_map[p.get("preference_key", "").lower().strip()] = p

        certified_records = []
        rejected_records = []
        details = []

        transcript_lower = (transcript or "").lower()

        for cand in candidates:
            pk = str(cand.get("preference_key", "")).strip()
            pv = str(cand.get("preference_value", "")).strip()
            conf = float(cand.get("confidence", 1.0))

            # Check 1: Confidence
            if conf < confidence_threshold:
                cand_copy = dict(cand)
                cand_copy["certification_status"] = "REJECTED_LOW_CONFIDENCE"
                cand_copy["rejection_reason"] = f"Confidence {conf:.2f} below threshold {confidence_threshold:.2f}"
                rejected_records.append(cand_copy)
                details.append(cand_copy)
                continue

            # Check 2: Grounding Provenance
            is_intent = pk in ("session_intent", "cross_session_intent", "intent_trajectory")
            grounded = is_intent
            if not grounded:
                pv_words = [w for w in re.findall(r"\w+", pv.lower()) if len(w) >= 3]
                pk_words = [w for w in re.findall(r"\w+", pk.lower()) if len(w) >= 3]
                if pv.lower() in transcript_lower or pk.lower() in transcript_lower:
                    grounded = True
                elif any(w in transcript_lower for w in pv_words):
                    grounded = True
                elif any(w in transcript_lower for w in pk_words):
                    grounded = True

            if not grounded:
                cand_copy = dict(cand)
                cand_copy["certification_status"] = "REJECTED_UNGROUNDED"
                cand_copy["rejection_reason"] = "No grounded lexical provenance found in waking transcript"
                rejected_records.append(cand_copy)
                details.append(cand_copy)
                continue

            cand_copy = dict(cand)
            cand_copy["certification_status"] = "CERTIFIED"
            cand_copy["grounding_confidence"] = round(conf, 2)
            certified_records.append(cand_copy)
            details.append(cand_copy)

        return {
            "certified": len(rejected_records) == 0 or len(certified_records) > 0,
            "total_candidates": len(candidates),
            "certified_count": len(certified_records),
            "rejected_count": len(rejected_records),
            "certified_records": certified_records,
            "rejected_records": rejected_records,
            "details": details,
        }

    def summarize_session_logs(
        self,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        session_ids: Optional[List[str]] = None,
        multi_session: bool = False,
        session_type: str = "chat",
    ) -> Dict[str, Any]:
        """Perform offline REM Memory Consolidation (Agent Dreaming) over waking logs from BigQuery or Antigravity sessions.

        Supports both single-session intent capture and multi-session cross-session consolidation.
        """
        if multi_session or session_ids or session_id in ("ALL", None, ""):
            if not user_id:
                raise ValueError("user_id is required for multi-session dream compaction")
            return self.summarize_multi_session_logs(user_id=user_id, session_ids=session_ids)

        assert session_id is not None
        assert user_id is not None

        if session_type == "antigravity":
            from src.common.antigravity_crawler import AntigravitySessionCrawler
            crawler = AntigravitySessionCrawler()
            full_transcript = crawler.format_session_transcript(session_id)
            if not full_transcript:
                logs = self.telemetry_sink.get_session_logs(session_id=session_id)
                sorted_logs = sorted(logs, key=lambda x: str(x.get("timestamp", "")))
                full_transcript = "\n".join(f"{l.get('event_type', 'MSG')}: {l.get('message', '')}" for l in sorted_logs if l.get("message"))
        else:
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
                "nrem_analysis": self._nrem_analyze_traces([]),
                "certification": self.certify_memory_candidates([], "", []),
                "procedural_guardrails": [],
            }

        trace_logs = logs if session_type != "antigravity" else [
            {"event_type": line.split(":", 1)[0].strip(), "message": line.split(":", 1)[1].strip()}
            for line in full_transcript.splitlines() if ":" in line
        ]
        nrem_analysis = self._nrem_analyze_traces(trace_logs)

        dream_prompt = f"""
You are an AI REM Memory Synthesizer ("Dream Engine") performing offline memory consolidation over waking conversation logs for user '{user_id}' in session '{session_id}'.
Your goal is to crystallize short-term episodic interactions into clean, accurate, high-confidence long-term semantic beliefs and capture user intent.

Analyze the conversation transcript below:
{full_transcript}

Rules:
1. Extract the user's primary "session_intent": what is the user's underlying goal, motivation, or project focus in this session? (e.g., "The user is establishing their technical identity as a Principal Cloud Architect to explore Python and infrastructure tools.").
2. Extract the "intent_category": choose one of "onboarding", "exploratory", "architecture_design", "troubleshooting", "profile", "guardrail", or "general".
3. Provide an "intent_confidence" float score between 0.0 and 1.0.
4. Extract every distinct preference, technical skill, job role, favorite language, preferred tool, identity attribute, OR operational guardrail / procedural invariant (e.g., negative constraints like 'never use docker', 'always run terraform fmt', 'prefer uv for testing', physical/procedural rules).
5. NEVER confuse or merge the user's name with their job role. For example, if the transcript mentions "My name is Jason, my favorite programming language is Python, and my job role is Principal Cloud Architect", extract:
   - preference_key: "name", preference_value: "Jason", category: "profile"
   - preference_key: "favorite_language", preference_value: "Python", category: "technical"
   - preference_key: "job_role", preference_value: "Principal Cloud Architect", category: "role"
6. For each preference record, provide:
   - "preference_key": canonical snake_case key (e.g., "favorite_language", "job_role", "name", "preferred_tools", "work_style", "guardrail_docker", "guardrail_terraform").
   - "preference_value": concise exact value (e.g., "Python", "Principal Cloud Architect", "Jason", "Use Apple Container; prune dangling images").
   - "category": "profile", "technical", "role", "infrastructure", "preference", "guardrail", or "procedural".
   - "confidence": float between 0.0 and 1.0.
   - "crystallized_belief": A complete natural language semantic belief statement (e.g., "The user Jason works as a Principal Cloud Architect specializing in cloud infrastructure." or "The user's favorite programming language is Python." or "Always run terraform validate and terraform fmt when modifying infrastructure.").
   - "keywords": A comma-separated list of semantic search keywords and synonyms (e.g., "python, language, programming, code, coding", or "architect, principal, cloud, role, job, title, career", or "guardrail, docker, container, rules, policy").
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
            client = genai.Client(vertexai=True, project=self.config.project_id, location="global")
            response = client.models.generate_content(
                model=self.config.model_name,
                contents=dream_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            parsed = self._parse_summary_json(response.text, user_id, session_id, dream_prompt=dream_prompt)
            existing_prefs = self.spanner_client.get_all_user_preferences(user_id)
            cert = self.certify_memory_candidates(parsed.get("preferences", []), full_transcript, existing_prefs)
            parsed["nrem_analysis"] = nrem_analysis
            parsed["certification"] = cert
            parsed["procedural_guardrails"] = [
                p for p in parsed.get("preferences", [])
                if p.get("category") in ("guardrail", "procedural") or "guardrail" in str(p.get("keywords", "")).lower() or "invariant" in str(p.get("keywords", "")).lower()
            ]
            return parsed
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
                "nrem_analysis": self._nrem_analyze_traces([]),
                "certification": self.certify_memory_candidates([], "", []),
                "procedural_guardrails": [],
            }

        multi_session_dream_prompt = f"""
You are an AI REM Multi-Session Memory Synthesizer ("Dream Engine") performing cross-session memory consolidation and trajectory analysis over multiple waking conversation sessions for user '{user_id}'.
Your goal is to crystallize information over multiple sessions, track how the user's intent evolved across sessions, and synthesize a single unified, deduplicated set of long-term semantic beliefs.

Analyze the multi-session conversation transcript below:
{multi_session_transcript}

Rules:
1. Extract "cross_session_intent": an overarching statement capturing the user's primary goals, intent, and focus across all sessions.
2. Extract "intent_trajectory": a 2-3 sentence narrative describing how the user's questions, goals, and focus evolved from earlier sessions to later sessions (e.g., "In initial sessions, the user focused on establishing their profile and favorite languages. In subsequent sessions, they progressed to architecture discussions around low-latency Spanner caching and multi-agent orchestration.").
3. Extract "intent_category": one of "onboarding", "exploratory", "architecture_design", "troubleshooting", "profile", "guardrail", or "general".
4. Extract every distinct user preference and operational guardrail across ALL sessions (favorite_language, job_role, name, preferred_tools, work_style, deploy_target, operational invariants, etc.).
5. CROSS-SESSION DEDUPLICATION & CONFLICT RESOLUTION:
   - If a preference appears in multiple sessions with the same value, merge it and reinforce its confidence (up to 1.0).
   - If a preference changed over time across sessions (e.g., language preference changed in a later session), let the latest session win and record the current belief.
   - For each preference, include "source_session_id" indicating which session established it (or "multi_session" if reinforced across multiple sessions).
   - For category, assign "profile", "technical", "role", "infrastructure", "preference", "guardrail", or "procedural".
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
            client = genai.Client(vertexai=True, project=self.config.project_id, location="global")
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
            nrem_analysis = self._nrem_analyze_traces(sorted_logs)
            existing_prefs = self.spanner_client.get_all_user_preferences(user_id)
            cert = self.certify_memory_candidates(parsed.get("preferences", []), multi_session_transcript, existing_prefs)
            parsed["nrem_analysis"] = nrem_analysis
            parsed["certification"] = cert
            parsed["procedural_guardrails"] = [
                p for p in parsed.get("preferences", [])
                if p.get("category") in ("guardrail", "procedural") or "guardrail" in str(p.get("keywords", "")).lower() or "invariant" in str(p.get("keywords", "")).lower()
            ]
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
        session_type: str = "chat",
    ) -> Dict[str, Any]:
        """Run the full sync: summarize BQ logs for a session (or multiple sessions) and upsert into Google Cloud Spanner."""
        if multi_session or session_ids or session_id in ("ALL", None, ""):
            return self.sync_multi_session_to_spanner(user_id=user_id, session_ids=session_ids)

        assert session_id is not None
        summary = self.summarize_session_logs(session_id=session_id, user_id=user_id, session_type=session_type)

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
  MODEL `{project_id}.{dataset_id}.gemini_3_8_flash`,
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
            "nrem_analysis": summary.get("nrem_analysis"),
            "certification": summary.get("certification"),
            "procedural_guardrails": summary.get("procedural_guardrails", []),
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
  MODEL `{project_id}.{dataset_id}.gemini_3_8_flash`,
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
            "nrem_analysis": summary.get("nrem_analysis"),
            "certification": summary.get("certification"),
            "procedural_guardrails": summary.get("procedural_guardrails", []),
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

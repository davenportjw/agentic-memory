"""OpenTelemetry and BigQuery telemetry sink for logging ADK agent prompts and responses."""

import datetime
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional
from src.common.config import get_config

logger = logging.getLogger("adk.telemetry")

# Global in-memory logs so mock mode works across multiple sink instances
_GLOBAL_IN_MEMORY_LOGS: List[Dict[str, Any]] = []


def extract_5_word_summary(raw_text: str) -> str:
    """Generate a clean, readable ~5-word summary from a raw message, JSON turn, or summary text."""
    if not raw_text:
        return "General session interaction"
    text = raw_text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
            for k in ("content", "message", "prompt", "user_prompt", "summary", "intent", "text"):
                if k in data and isinstance(data[k], str) and data[k].strip():
                    text = data[k].strip()
                    break
        except Exception:
            pass
    intent_match = re.search(r"\[(?:Intent|Intent Trajectory|Cross-Session Intent):\s*([^\]]+)\]", text)
    if intent_match:
        text = intent_match.group(1).strip()
    else:
        text = re.sub(r"\[(Intent|Intent Trajectory|Cross-Session Intent):[^\]]*\]", "", text)
    text = re.sub(r"\[Spanner Vector Memory[^\]]*\]", "", text)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    match_first_clause = re.split(r"[.!?]\s+|(?:\sand\s)", text)
    if match_first_clause and match_first_clause[0].strip():
        clause_words = [w for w in match_first_clause[0].split() if w]
        if 3 <= len(clause_words) <= 7:
            return " ".join(clause_words)

    words = [w for w in text.split() if w]
    if not words:
        return "General session interaction"
    if len(words) <= 6:
        return " ".join(words)
    return " ".join(words[:5])


class BigQueryTelemetrySink:
    """Sink that exports agent interactions (prompts, responses, tool calls) to BigQuery."""

    def __init__(self, use_mock: Optional[bool] = None):
        self.config = get_config()
        self.use_mock = False if use_mock is None else use_mock
        self.in_memory_logs = _GLOBAL_IN_MEMORY_LOGS
        self._bq_client = None

        if not self.use_mock:
            try:
                from google.cloud import bigquery
                self._bq_client = bigquery.Client(project=self.config.project_id)
            except Exception as e:
                logger.warning(
                    f"Could not initialize BigQuery client ({e}). Falling back to in-memory telemetry sink."
                )
                self.use_mock = True

    def log_interaction(
        self,
        user_id: str,
        session_id: str,
        agent_name: str,
        event_type: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Log an agent interaction event (e.g. USER_PROMPT or AGENT_RESPONSE) to BigQuery."""
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record = {
            "timestamp": timestamp,
            "user_id": user_id,
            "session_id": session_id,
            "agent_name": agent_name,
            "event_type": event_type,
            "message": message,
            "json_payload": json.dumps(payload or {}),
        }

        self.in_memory_logs.append(record)

        if not self.use_mock and self._bq_client:
            try:
                table_ref = f"{self.config.project_id}.{self.config.bq_dataset_id}.{self.config.bq_table_id}"
                errors = self._bq_client.insert_rows_json(table_ref, [record])
                if errors:
                    logger.warning(f"Failed to insert row into BigQuery telemetry table {table_ref}: {errors}")
            except Exception as e:
                logger.error(f"Exception writing to BigQuery ({e})")

        return record

    def get_session_logs(self, session_id: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        """Retrieve logs from memory or BigQuery for a given session, ordered from newest to oldest.

        If session_id is None, empty, or 'ALL', logs across all sessions are returned (up to limit).
        """
        is_all = not session_id or session_id.upper() in ("ALL", "*")
        memory_logs = [
            r for r in self.in_memory_logs
            if is_all or r.get("session_id") == session_id
        ]
        if self.use_mock or not self._bq_client:
            return sorted(memory_logs, key=lambda x: str(x.get("timestamp", "")), reverse=True)[:limit]

        try:
            where_clause = "" if is_all else "WHERE session_id = @session_id"
            query = f"""
                SELECT *
                FROM `{self.config.project_id}.{self.config.bq_dataset_id}.{self.config.bq_table_id}`
                {where_clause}
                ORDER BY timestamp DESC
                LIMIT @limit
            """
            from google.cloud import bigquery
            params = [bigquery.ScalarQueryParameter("limit", "INT64", limit)]
            if not is_all:
                params.append(bigquery.ScalarQueryParameter("session_id", "STRING", session_id))
            job_config = bigquery.QueryJobConfig(query_parameters=params)
            rows = self._bq_client.query(query, job_config=job_config).result()
            bq_logs = [dict(row) for row in rows]

            def _log_key(r: Dict[str, Any]) -> tuple:
                ts = r.get("timestamp", "")
                if hasattr(ts, "isoformat"):
                    ts = ts.isoformat()
                return (str(ts)[:19], str(r.get("event_type", "")), str(r.get("message", ""))[:50])

            seen = {_log_key(r) for r in bq_logs}
            for ml in memory_logs:
                key = _log_key(ml)
                if key not in seen:
                    bq_logs.append(ml)
                    seen.add(key)
            return sorted(bq_logs, key=lambda x: str(x.get("timestamp", "")), reverse=True)[:limit]
        except Exception as e:
            logger.error(f"Failed to query BigQuery logs ({e})")
            return sorted(memory_logs, key=lambda x: str(x.get("timestamp", "")), reverse=True)[:limit]

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List distinct sessions with primary source agent and ~5-word summary."""
        sessions_map: Dict[str, Dict[str, Any]] = {}

        # 1. From BigQuery if available
        if not self.use_mock and self._bq_client:
            try:
                from google.cloud import bigquery
                query = f"""
                    SELECT session_id,
                           ARRAY_AGG(agent_name IGNORE NULLS ORDER BY timestamp ASC LIMIT 1)[OFFSET(0)] AS source,
                           ARRAY_AGG(message IGNORE NULLS ORDER BY timestamp ASC LIMIT 1)[OFFSET(0)] AS first_message,
                           COUNT(1) AS event_count,
                           MAX(timestamp) AS last_timestamp
                    FROM `{self.config.project_id}.{self.config.bq_dataset_id}.{self.config.bq_table_id}`
                    WHERE session_id IS NOT NULL AND session_id != ''
                    GROUP BY session_id
                    ORDER BY last_timestamp DESC
                    LIMIT @limit
                """
                job_config = bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("limit", "INT64", limit)]
                )
                rows = self._bq_client.query(query, job_config=job_config).result()
                for row in rows:
                    sid = row["session_id"]
                    first_msg = row["first_message"] or ""
                    last_ts = row["last_timestamp"]
                    sessions_map[sid] = {
                        "session_id": sid,
                        "source": row["source"] or "BigQuery Telemetry",
                        "summary": extract_5_word_summary(first_msg),
                        "event_count": row["event_count"] or 0,
                        "last_timestamp": last_ts.isoformat() if hasattr(last_ts, "isoformat") else str(last_ts or ""),
                    }
            except Exception as e:
                logger.error(f"Failed to query BigQuery distinct sessions: {e}")

        # 2. In-memory logs aggregation (for mock mode and local dev)
        mem_by_session: Dict[str, List[Dict[str, Any]]] = {}
        for r in self.in_memory_logs:
            sid = r.get("session_id")
            if sid:
                mem_by_session.setdefault(sid, []).append(r)

        for sid, logs in mem_by_session.items():
            if sid in sessions_map:
                sessions_map[sid]["event_count"] = max(sessions_map[sid]["event_count"], len(logs))
                continue
            first_log = logs[0]
            last_ts = max((str(l.get("timestamp", "")) for l in logs), default="")
            source = next((l.get("agent_name") for l in logs if l.get("agent_name")), "in-memory")
            first_msg = first_log.get("message", "")
            sessions_map[sid] = {
                "session_id": sid,
                "source": source,
                "summary": extract_5_word_summary(first_msg),
                "event_count": len(logs),
                "last_timestamp": last_ts,
            }

        sorted_sessions = sorted(
            sessions_map.values(),
            key=lambda s: str(s.get("last_timestamp", "")),
            reverse=True,
        )
        return sorted_sessions[:limit]

    def get_user_logs(
        self,
        user_id: str,
        session_id: Optional[str] = None,
        session_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve logs from memory or BigQuery for a given user across all or specified sessions, ordered from newest to oldest."""
        memory_logs = [
            r for r in self.in_memory_logs
            if r.get("user_id") == user_id
            and (not session_id or r.get("session_id") == session_id)
            and (not session_ids or r.get("session_id") in session_ids)
        ]
        if self.use_mock or not self._bq_client:
            return sorted(memory_logs, key=lambda x: str(x.get("timestamp", "")), reverse=True)

        try:
            from google.cloud import bigquery
            conditions = ["user_id = @user_id"]
            params = [bigquery.ScalarQueryParameter("user_id", "STRING", user_id)]
            if session_id:
                conditions.append("session_id = @session_id")
                params.append(bigquery.ScalarQueryParameter("session_id", "STRING", session_id))
            elif session_ids:
                conditions.append("session_id IN UNNEST(@session_ids)")
                params.append(bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids))

            where_clause = " AND ".join(conditions)
            query = f"""
                SELECT *
                FROM `{self.config.project_id}.{self.config.bq_dataset_id}.{self.config.bq_table_id}`
                WHERE {where_clause}
                ORDER BY timestamp DESC
            """
            job_config = bigquery.QueryJobConfig(query_parameters=params)
            rows = self._bq_client.query(query, job_config=job_config).result()
            bq_logs = [dict(row) for row in rows]

            def _log_key(r: Dict[str, Any]) -> tuple:
                ts = r.get("timestamp", "")
                if hasattr(ts, "isoformat"):
                    ts = ts.isoformat()
                return (str(ts)[:19], str(r.get("event_type", "")), str(r.get("message", ""))[:50])

            seen = {_log_key(r) for r in bq_logs}
            for ml in memory_logs:
                key = _log_key(ml)
                if key not in seen:
                    bq_logs.append(ml)
                    seen.add(key)
            return sorted(bq_logs, key=lambda x: str(x.get("timestamp", "")), reverse=True)
        except Exception as e:
            logger.error(f"Failed to query BigQuery user logs ({e})")
            return sorted(memory_logs, key=lambda x: str(x.get("timestamp", "")), reverse=True)

    # =========================================================================
    # Enterprise Memory Studio Telemetry & CDC Lakehouse (`electric_sheep_telemetry`)
    # =========================================================================

    def _default_seed_episodes(self) -> List[Dict[str, Any]]:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return [
            {
                "id": "sess-9042",
                "session_id": "sess-9042",
                "title": "Enforce uv over pip & Cloud Run Live Endpoint Verification",
                "user": "developer@example.com",
                "user_id": "user:developer",
                "agent": "antigravity-coding-agent",
                "agent_id": "agent:antigravity-coding-agent",
                "team_id": "team:platform-ml",
                "scopeTarget": "/team/platform-ml & /org/acme-org",
                "stream": "MCP_LIVE_STREAM",
                "stream_source": "MCP_LIVE_STREAM",
                "topic": "PYTHON_TOOLING",
                "turns": 6,
                "turn_count": 6,
                "status": "PENDING_EXTRACTION",
                "timestamp": "2 mins ago",
                "ingested_at": now_iso,
                "helpfulness_status": "MIXED",
                "summary": "Developer halted global pip install and mandated uv for all Python testing; also enforced live Cloud Run deployment verification.",
                "aiSummary": {
                    "overview": "Pair-programming session where the developer corrected the agent for attempting `pip install pytest` and finalized the deployment verification workflow for `your-gcp-project-id`.",
                    "intent": "Establish non-negotiable Python package management (`uv`) and production Cloud Run endpoint verification gates.",
                    "resolution": "Replaced `pip` workflow with `uv run pytest`, deployed to Cloud Run, and staged 2 memory commits (+1 ADD, ~1 UPDATE).",
                    "recalledMemories": [
                        {
                            "fileId": "team_python_uv",
                            "path": "/team/platform-ml/python_tooling/python_uv_runner.mem",
                            "version": "v3 (Prior)",
                            "outcome": "HELPED",
                            "reason": "Correctly guided agent to use `uv run pytest`, though missing explicit ban on `pip install`.",
                        },
                        {
                            "fileId": "user_legacy_pip_override",
                            "path": "/user/developer/python_tooling/legacy_pip_fallback.mem",
                            "version": "v1 (Stale)",
                            "outcome": "NOT_USEFUL",
                            "reason": "4th not-useful session recall (crossed >=3 threshold): stale fallback caused agent to attempt `pip install` before user halted execution. Now flagged for eviction.",
                        },
                    ],
                },
                "transcript": [
                    {
                        "turn": 1,
                        "role": "AGENT",
                        "time": "14:20:04",
                        "text": "I'll install pytest and run the test suite using `pip install pytest && pytest tests/`.",
                    },
                    {
                        "turn": 2,
                        "role": "USER",
                        "time": "14:20:19",
                        "text": "Stop! Never use global `pip install` or bare `pytest`. Always use `uv run pytest` for Python testing in this repo. Also remember: a task involving services is NOT complete until deployed to Cloud Run (`your-gcp-project-id`) and verified via live HTTP probe.",
                        "signal": {
                            "type": "EXPLICIT_CORRECTION",
                            "confidence": 0.98,
                            "summary": "Mandated `uv run pytest` over `pip` and required Cloud Run live HTTP probe verification.",
                            "stagedCommitIds": ["commit_8801", "commit_8802"],
                        },
                    },
                    {
                        "turn": 3,
                        "role": "AGENT",
                        "time": "14:20:31",
                        "text": "Understood. Switching to `uv run pytest` (~2.4s local execution) and queuing `./scripts/deploy_cloud_run_source.sh` followed by `python3 -m scripts.validate_live_endpoints`.",
                    },
                ],
                "extractedSignals": [
                    {
                        "id": "sig-9042-a",
                        "type": "EXPLICIT_CORRECTION",
                        "confidence": 0.99,
                        "targetPath": "/team/platform-ml/python_tooling/python_uv_runner.mem",
                        "action": "UPDATE",
                        "extractorId": "ext_dev_corrections",
                        "preview": "Always use `uv run pytest` for Python testing; never invoke `pip install`.",
                    },
                    {
                        "id": "sig-9042-b",
                        "type": "ARCHITECTURAL_INVARIANT",
                        "confidence": 0.97,
                        "targetPath": "/org/acme-org/cloud_run_deploy/live_endpoint_verification.mem",
                        "action": "ADD",
                        "extractorId": "ext_arch_invariants",
                        "preview": "Services must be deployed to Cloud Run (`your-gcp-project-id`) and verified via live HTTP probe before task completion.",
                    },
                    {
                        "id": "sig-9042-c",
                        "type": "CONTRADICTION_EVICTION",
                        "confidence": 0.96,
                        "targetPath": "/user/developer/python_tooling/legacy_pip_fallback.mem",
                        "action": "DELETE",
                        "extractorId": "ext_dev_corrections",
                        "preview": "Evict stale `pip install --user` fallback rule after crossing 4/3 Not-Useful session threshold.",
                    },
                ],
            },
            {
                "id": "sess-9019",
                "session_id": "sess-9019",
                "title": "Apple Silicon Guardrails & Vertex AI L4 Offloading",
                "user": "developer@example.com",
                "user_id": "user:developer",
                "agent": "antigravity-coding-agent",
                "agent_id": "agent:antigravity-coding-agent",
                "team_id": "team:platform-ml",
                "scopeTarget": "/org/acme-org & /user/developer",
                "stream": "BIGQUERY_REALTIME_SINK",
                "stream_source": "BIGQUERY_REALTIME_SINK",
                "topic": "APPLE_SILICON",
                "turns": 8,
                "turn_count": 8,
                "status": "EXTRACTED_AND_SYNCED",
                "timestamp": "1 hour ago",
                "ingested_at": now_iso,
                "helpfulness_status": "HELPED",
                "summary": "Configured Apple Silicon memory watchdog protection; offloaded heavy Gemma 4 fine-tuning to Vertex AI Custom Training L4 GPUs.",
                "aiSummary": {
                    "overview": "Hardware configuration session preventing local macOS kernel panics during LLM evaluation.",
                    "intent": "Restrict local Apple Silicon execution to fast unit tests (~3s) and route heavy training to Vertex AI.",
                    "resolution": "Committed `/org/acme-org/apple_silicon_guardrails/hardware_offload.mem` to Cloud Spanner.",
                    "recalledMemories": [
                        {
                            "fileId": "org_apple_silicon",
                            "path": "/org/acme-org/apple_silicon_guardrails/hardware_offload.mem",
                            "version": "v2 (Synced)",
                            "outcome": "HELPED",
                            "reason": "Prevented local autoregressive loop execution on macOS host and routed job to Vertex AI L4 GPU.",
                        },
                        {
                            "fileId": "user_developer_theme",
                            "path": "/user/developer/ui_design_system/academic_light_sepia.mem",
                            "version": "v1 (Synced)",
                            "outcome": "HELPED",
                            "reason": "Applied 3-panel layout and high-contrast light styling without user prompting.",
                        },
                    ],
                },
                "transcript": [
                    {
                        "turn": 1,
                        "role": "USER",
                        "time": "13:10:12",
                        "text": "Host machine is Apple Silicon (macOS). Never execute heavy model training or unrolled autoregressive loops locally. Keep local runs to `uv run pytest` (~3s) and offload training to Vertex AI L4 GPUs using `gs://your-vertex-staging-bucket/`.",
                        "signal": {
                            "type": "HARDWARE_GUARDRAIL",
                            "confidence": 0.99,
                            "summary": "Synced hardware guardrail to `/org/acme-org/apple_silicon_guardrails/`.",
                            "stagedCommitIds": [],
                        },
                    },
                    {
                        "turn": 2,
                        "role": "AGENT",
                        "time": "13:10:28",
                        "text": "Crystallized and committed to Spanner `/org/acme-org/apple_silicon_guardrails/hardware_offload.mem`.",
                    },
                ],
                "extractedSignals": [
                    {
                        "id": "sig-9019-a",
                        "type": "HARDWARE_GUARDRAIL",
                        "confidence": 0.99,
                        "targetPath": "/org/acme-org/apple_silicon_guardrails/hardware_offload.mem",
                        "action": "SYNCED",
                        "extractorId": "ext_arch_invariants",
                        "preview": "Never run heavy training on local Apple Silicon; offload to Vertex AI L4 GPUs.",
                    }
                ],
            },
            {
                "id": "sess-8984",
                "session_id": "sess-8984",
                "title": "Workcell B Kinematics J5 Wrist Torque Clamp Audit",
                "user": "ops-lead@example.com",
                "user_id": "user:ops-lead",
                "agent": "workcell-agent",
                "agent_id": "agent:workcell-agent",
                "team_id": "team:robotics-cleanroom",
                "scopeTarget": "/agent/workcell-agent",
                "stream": "VERTEX_AGENT_ENGINE",
                "stream_source": "VERTEX_AGENT_ENGINE",
                "topic": "WORKCELL_KINEMATICS",
                "turns": 5,
                "turn_count": 5,
                "status": "EXTRACTED_AND_SYNCED",
                "timestamp": "4 hours ago",
                "ingested_at": now_iso,
                "helpfulness_status": "HELPED",
                "summary": "Verified 14.0 Nm J5 wrist fracture torque limit and Cascade Andon Halt state transition.",
                "aiSummary": {
                    "overview": "Adversarial stress-test of Workcell B kinematics under high-payload wafer transfer.",
                    "intent": "Ensure J5 wrist torque never exceeds 14.0 Nm before triggering Cascade Andon Halt.",
                    "resolution": "Verified pinned memory `/agent/workcell-agent/kinematics_constraints/max_wrist_torque.mem`.",
                    "recalledMemories": [
                        {
                            "fileId": "agent_workcell_kinematics",
                            "path": "/agent/workcell-agent/kinematics_constraints/max_wrist_torque.mem",
                            "version": "v4 (Pinned)",
                            "outcome": "HELPED",
                            "reason": "Successfully clamped J5 torque at 13.8 Nm and triggered Andon halt before fracture limit.",
                        }
                    ],
                },
                "transcript": [
                    {
                        "turn": 1,
                        "role": "USER",
                        "time": "10:04:11",
                        "text": "Clamp Workcell B J5 wrist torque to 14.0 Nm max; trigger immediate Cascade Andon Halt if exceeded.",
                        "signal": {
                            "type": "SAFETY_INVARIANT",
                            "confidence": 1.0,
                            "summary": "Pinned to `/agent/workcell-agent/kinematics_constraints/max_wrist_torque.mem`.",
                            "stagedCommitIds": [],
                        },
                    }
                ],
                "extractedSignals": [],
            },
        ]

    def ensure_telemetry_tables(self) -> Dict[str, Any]:
        """Ensure BigQuery dataset `electric_sheep_telemetry` and its 3 tables exist and are seeded."""
        if not hasattr(self, "_episodes_cache"):
            self._episodes_cache: Dict[str, Dict[str, Any]] = {
                ep["session_id"]: ep for ep in self._default_seed_episodes()
            }
        if not hasattr(self, "_ratings_cache"):
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._ratings_cache: List[Dict[str, Any]] = [
                {
                    "rating_id": "rat-9042-1",
                    "session_id": "sess-9042",
                    "memory_id": "team_python_uv",
                    "file_path": "/team/platform-ml/python_tooling/python_uv_runner.mem",
                    "outcome": "HELPED",
                    "reason": "Correctly guided agent to use `uv run pytest`.",
                    "rated_at": now_iso,
                },
                {
                    "rating_id": "rat-9042-2",
                    "session_id": "sess-9042",
                    "memory_id": "user_legacy_pip_override",
                    "file_path": "/user/developer/python_tooling/legacy_pip_fallback.mem",
                    "outcome": "NOT_USEFUL",
                    "reason": "4th not-useful recall (crossed >=3 threshold): triggered global pip install; user halted & enforced uv.",
                    "rated_at": now_iso,
                },
                {
                    "rating_id": "rat-9019-1",
                    "session_id": "sess-9019",
                    "memory_id": "org_apple_silicon",
                    "file_path": "/org/acme-org/apple_silicon_guardrails/hardware_offload.mem",
                    "outcome": "HELPED",
                    "reason": "Prevented local autoregressive loop execution on macOS host and routed job to Vertex AI L4 GPU.",
                    "rated_at": now_iso,
                },
                {
                    "rating_id": "rat-8984-1",
                    "session_id": "sess-8984",
                    "memory_id": "agent_workcell_kinematics",
                    "file_path": "/agent/workcell-agent/kinematics_constraints/max_wrist_torque.mem",
                    "outcome": "HELPED",
                    "reason": "Successfully clamped J5 torque at 13.8 Nm and triggered Andon halt before fracture limit.",
                    "rated_at": now_iso,
                },
            ]

        if getattr(self, "_studio_bq_ensured", False):
            return {"status": "READY", "dataset": "electric_sheep_telemetry"}

        if self.use_mock or not self._bq_client:
            self._studio_bq_ensured = True
            return {"status": "MOCK_READY", "dataset": "electric_sheep_telemetry"}

        try:
            from google.cloud import bigquery

            dataset_id = f"{self.config.project_id}.electric_sheep_telemetry"
            ds = bigquery.Dataset(dataset_id)
            ds.location = "US"
            self._bq_client.create_dataset(ds, exists_ok=True)

            # 1. episodes_log
            episodes_schema = [
                bigquery.SchemaField("session_id", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("user_id", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("agent_id", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("team_id", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("stream_source", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("topic", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("turn_count", "INTEGER", mode="NULLABLE"),
                bigquery.SchemaField("transcript_json", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("extracted_signals_json", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("ai_summary", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("helpfulness_status", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED"),
            ]
            t_episodes = bigquery.Table(f"{dataset_id}.episodes_log", schema=episodes_schema)
            self._bq_client.create_table(t_episodes, exists_ok=True)

            # 2. scoped_memories_replica
            replica_schema = [
                bigquery.SchemaField("memory_id", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("scope_type", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("scope_id", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("category", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("file_path", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("memory_key", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("memory_value", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("crystallized_belief", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("confidence", "FLOAT", mode="NULLABLE"),
                bigquery.SchemaField("helpful_count", "INTEGER", mode="NULLABLE"),
                bigquery.SchemaField("unhelpful_count", "INTEGER", mode="NULLABLE"),
                bigquery.SchemaField("write_mode", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("updated_at", "TIMESTAMP", mode="REQUIRED"),
                bigquery.SchemaField("replicated_at", "TIMESTAMP", mode="NULLABLE"),
            ]
            t_replica = bigquery.Table(f"{dataset_id}.scoped_memories_replica", schema=replica_schema)
            self._bq_client.create_table(t_replica, exists_ok=True)

            # 3. session_helpfulness_ratings
            ratings_schema = [
                bigquery.SchemaField("rating_id", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("session_id", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("memory_id", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("file_path", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("outcome", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("reason", "STRING", mode="NULLABLE"),
                bigquery.SchemaField("rated_at", "TIMESTAMP", mode="REQUIRED"),
            ]
            t_ratings = bigquery.Table(f"{dataset_id}.session_helpfulness_ratings", schema=ratings_schema)
            self._bq_client.create_table(t_ratings, exists_ok=True)

            # Seed episodes_log if empty
            cnt_q = f"SELECT COUNT(1) AS cnt FROM `{dataset_id}.episodes_log`"
            cnt_rows = list(self._bq_client.query(cnt_q).result())
            if not cnt_rows or int(cnt_rows[0]["cnt"]) == 0:
                rows_to_insert = []
                for ep in self._default_seed_episodes():
                    rows_to_insert.append({
                        "session_id": ep["session_id"],
                        "user_id": ep["user_id"],
                        "agent_id": ep["agent_id"],
                        "team_id": ep["team_id"],
                        "stream_source": ep["stream_source"],
                        "topic": ep["topic"],
                        "turn_count": ep["turn_count"],
                        "transcript_json": json.dumps(ep["transcript"]),
                        "extracted_signals_json": json.dumps(ep["extractedSignals"]),
                        "ai_summary": json.dumps({
                            "title": ep["title"],
                            "summary": ep["summary"],
                            "status": ep["status"],
                            "scopeTarget": ep["scopeTarget"],
                            "timestamp": ep["timestamp"],
                            **ep["aiSummary"],
                        }),
                        "helpfulness_status": ep["helpfulness_status"],
                        "ingested_at": ep["ingested_at"],
                    })
                self._bq_client.insert_rows_json(f"{dataset_id}.episodes_log", rows_to_insert)

            # Seed session_helpfulness_ratings if empty
            r_cnt_q = f"SELECT COUNT(1) AS cnt FROM `{dataset_id}.session_helpfulness_ratings`"
            r_cnt_rows = list(self._bq_client.query(r_cnt_q).result())
            if not r_cnt_rows or int(r_cnt_rows[0]["cnt"]) == 0:
                self._bq_client.insert_rows_json(
                    f"{dataset_id}.session_helpfulness_ratings",
                    self._ratings_cache,
                )

            self._studio_bq_ensured = True
            return {
                "status": "READY",
                "dataset": "electric_sheep_telemetry",
                "tables": ["episodes_log", "scoped_memories_replica", "session_helpfulness_ratings"],
            }
        except Exception as e:
            logger.warning(f"BigQuery Studio table initialization notice: {e}")
            self._studio_bq_ensured = True
            return {"status": "DEGRADED_CACHE", "error": str(e)}

    def _hydrate_episode_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a raw BigQuery episodes_log row into the rich Studio Session schema."""
        sid = str(row.get("session_id") or row.get("id") or "sess-unknown")
        default_match = getattr(self, "_episodes_cache", {}).get(sid, {})
        transcript = row.get("transcript")
        if transcript is None and row.get("transcript_json"):
            try:
                transcript = json.loads(row["transcript_json"])
            except Exception:
                transcript = []
        if not transcript:
            transcript = default_match.get("transcript", [])

        extracted_signals = row.get("extractedSignals") or row.get("extracted_signals")
        if extracted_signals is None and row.get("extracted_signals_json"):
            try:
                extracted_signals = json.loads(row["extracted_signals_json"])
            except Exception:
                extracted_signals = []
        if extracted_signals is None:
            extracted_signals = default_match.get("extractedSignals", [])

        ai_sum_raw = row.get("aiSummary") or row.get("ai_summary")
        ai_sum_obj: Dict[str, Any] = {}
        if isinstance(ai_sum_raw, dict):
            ai_sum_obj = dict(ai_sum_raw)
        elif isinstance(ai_sum_raw, str) and ai_sum_raw.strip():
            try:
                parsed = json.loads(ai_sum_raw)
                if isinstance(parsed, dict):
                    ai_sum_obj = parsed
                else:
                    ai_sum_obj = {"overview": ai_sum_raw}
            except Exception:
                ai_sum_obj = {"overview": ai_sum_raw}
        if not ai_sum_obj and default_match.get("aiSummary"):
            ai_sum_obj = dict(default_match["aiSummary"])

        title = (
            row.get("title")
            or ai_sum_obj.get("title")
            or default_match.get("title")
            or f"Ingested Session {sid}"
        )
        summary_txt = (
            row.get("summary")
            or ai_sum_obj.get("summary")
            or ai_sum_obj.get("overview")
            or default_match.get("summary")
            or "Ingested agent trajectory episode."
        )
        status_val = (
            row.get("status")
            or ai_sum_obj.get("status")
            or default_match.get("status")
            or ("PENDING_EXTRACTION" if extracted_signals else "EXTRACTED_AND_SYNCED")
        )
        ingested_at = row.get("ingested_at") or default_match.get("ingested_at") or datetime.datetime.now(datetime.timezone.utc).isoformat()
        if hasattr(ingested_at, "isoformat"):
            ingested_at = ingested_at.isoformat()

        stream_src = str(row.get("stream_source") or row.get("stream") or default_match.get("stream") or "MCP_LIVE_STREAM")
        user_id = str(row.get("user_id") or default_match.get("user_id") or "user:developer")
        agent_id = str(row.get("agent_id") or default_match.get("agent_id") or "agent:antigravity-coding-agent")
        team_id = str(row.get("team_id") or default_match.get("team_id") or "team:platform-ml")
        topic = str(row.get("topic") or default_match.get("topic") or "PYTHON_TOOLING")
        turns = int(row.get("turn_count") or row.get("turns") or len(transcript) or 1)
        helpfulness_status = str(row.get("helpfulness_status") or default_match.get("helpfulness_status") or "HELPED")

        return {
            "id": sid,
            "session_id": sid,
            "title": title,
            "user": row.get("user") or default_match.get("user") or user_id.replace("user:", "") + "@example.com",
            "user_id": user_id,
            "agent": row.get("agent") or default_match.get("agent") or agent_id.replace("agent:", ""),
            "agent_id": agent_id,
            "team_id": team_id,
            "scopeTarget": row.get("scopeTarget") or ai_sum_obj.get("scopeTarget") or default_match.get("scopeTarget") or f"/{team_id.replace(':', '/')}",
            "stream": stream_src,
            "stream_source": stream_src,
            "topic": topic,
            "turns": turns,
            "turn_count": turns,
            "status": status_val,
            "timestamp": row.get("timestamp") or ai_sum_obj.get("timestamp") or default_match.get("timestamp") or "Recent",
            "ingested_at": str(ingested_at),
            "helpfulness_status": helpfulness_status,
            "summary": summary_txt,
            "aiSummary": {
                "overview": ai_sum_obj.get("overview") or summary_txt,
                "intent": ai_sum_obj.get("intent") or "Extract durable developer rules and domain guardrails.",
                "resolution": ai_sum_obj.get("resolution") or "Processed trajectory signals and verified memory recall.",
                "recalledMemories": ai_sum_obj.get("recalledMemories") or default_match.get("aiSummary", {}).get("recalledMemories", []),
            },
            "transcript": transcript,
            "extractedSignals": extracted_signals,
        }

    def get_ingested_sessions(
        self,
        stream_source: str = "ALL",
        topic: str = "ALL",
        helpfulness_status: str = "ALL",
        search_query: str = "",
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query ingested episodes from BigQuery `electric_sheep_telemetry.episodes_log` with filtering."""
        self.ensure_telemetry_tables()
        sessions_by_id: Dict[str, Dict[str, Any]] = {}

        if not self.use_mock and self._bq_client:
            try:
                dataset_id = f"{self.config.project_id}.electric_sheep_telemetry"
                query = f"""
                    SELECT
                        session_id,
                        user_id,
                        agent_id,
                        team_id,
                        stream_source,
                        topic,
                        turn_count,
                        transcript_json,
                        extracted_signals_json,
                        ai_summary,
                        helpfulness_status,
                        ingested_at
                    FROM `{dataset_id}.episodes_log`
                    ORDER BY ingested_at DESC
                    LIMIT {int(limit)}
                """
                for r in self._bq_client.query(query).result():
                    hydrated = self._hydrate_episode_row(dict(r))
                    if hydrated["session_id"] not in sessions_by_id:
                        sessions_by_id[hydrated["session_id"]] = hydrated
            except Exception as e:
                logger.warning(f"Failed to query episodes_log from BigQuery ({e}), using cache")

        # Merge cached/seeded episodes so recent mutations in streaming buffer are immediately visible
        for sid, cached_ep in getattr(self, "_episodes_cache", {}).items():
            if sid not in sessions_by_id:
                sessions_by_id[sid] = self._hydrate_episode_row(cached_ep)
            else:
                # Prefer newer cached AI summary or helpfulness updates if mutated in-session
                if cached_ep.get("_updated_in_session"):
                    sessions_by_id[sid] = self._hydrate_episode_row(cached_ep)

        results = list(sessions_by_id.values())

        # Apply filters
        if stream_source and stream_source.upper() != "ALL":
            sf = stream_source.upper()
            results = [s for s in results if str(s.get("stream_source", "")).upper() == sf]
        if topic and topic.upper() != "ALL":
            tf = topic.upper()
            results = [s for s in results if str(s.get("topic", "")).upper() == tf]
        if helpfulness_status and helpfulness_status.upper() != "ALL":
            hf = helpfulness_status.upper()
            if hf == "NOT_USEFUL":
                results = [
                    s for s in results
                    if str(s.get("helpfulness_status", "")).upper() in ("NOT_USEFUL", "MIXED")
                    or any(m.get("outcome") == "NOT_USEFUL" for m in s.get("aiSummary", {}).get("recalledMemories", []))
                ]
            elif hf == "HELPED":
                results = [
                    s for s in results
                    if str(s.get("helpfulness_status", "")).upper() in ("HELPED", "MIXED")
                    or any(m.get("outcome") == "HELPED" for m in s.get("aiSummary", {}).get("recalledMemories", []))
                ]
            else:
                results = [s for s in results if str(s.get("helpfulness_status", "")).upper() == hf]

        if search_query and search_query.strip():
            q = search_query.strip().lower()
            results = [
                s for s in results
                if q in str(s.get("session_id", "")).lower()
                or q in str(s.get("title", "")).lower()
                or q in str(s.get("summary", "")).lower()
                or q in str(s.get("topic", "")).lower()
            ]

        return results[:limit]

    def get_ingested_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single ingested session by session_id."""
        self.ensure_telemetry_tables()
        all_sess = self.get_ingested_sessions(limit=100)
        for s in all_sess:
            if s.get("session_id") == session_id or s.get("id") == session_id:
                return s
        return None

    def update_session_ai_summary(
        self,
        session_id: str,
        ai_summary_dict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist a newly generated AI summary for a session into BigQuery `episodes_log`."""
        self.ensure_telemetry_tables()
        existing = self.get_ingested_session(session_id)
        if not existing:
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            existing = {
                "id": session_id,
                "session_id": session_id,
                "title": f"Session {session_id}",
                "user_id": "user:developer",
                "agent_id": "agent:antigravity-coding-agent",
                "team_id": "team:platform-ml",
                "stream_source": "MCP_LIVE_STREAM",
                "topic": "GENERAL",
                "turn_count": 1,
                "status": "EXTRACTED_AND_SYNCED",
                "ingested_at": now_iso,
                "helpfulness_status": "HELPED",
                "transcript": [],
                "extractedSignals": [],
                "aiSummary": {},
            }

        merged_ai = dict(existing.get("aiSummary") or {})
        merged_ai.update(ai_summary_dict)
        existing["aiSummary"] = merged_ai
        if ai_summary_dict.get("overview"):
            existing["summary"] = ai_summary_dict["overview"]
        existing["_updated_in_session"] = True
        self._episodes_cache[session_id] = existing

        if not self.use_mock and self._bq_client:
            try:
                dataset_id = f"{self.config.project_id}.electric_sheep_telemetry"
                row_payload = {
                    "session_id": session_id,
                    "user_id": existing.get("user_id", "user:developer"),
                    "agent_id": existing.get("agent_id", "agent:antigravity-coding-agent"),
                    "team_id": existing.get("team_id", "team:platform-ml"),
                    "stream_source": existing.get("stream_source", "MCP_LIVE_STREAM"),
                    "topic": existing.get("topic", "PYTHON_TOOLING"),
                    "turn_count": int(existing.get("turn_count", 1)),
                    "transcript_json": json.dumps(existing.get("transcript", [])),
                    "extracted_signals_json": json.dumps(existing.get("extractedSignals", [])),
                    "ai_summary": json.dumps({
                        "title": existing.get("title"),
                        "summary": existing.get("summary"),
                        "status": existing.get("status"),
                        "scopeTarget": existing.get("scopeTarget"),
                        "timestamp": existing.get("timestamp"),
                        **merged_ai,
                    }),
                    "helpfulness_status": existing.get("helpfulness_status", "HELPED"),
                    "ingested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
                self._bq_client.insert_rows_json(f"{dataset_id}.episodes_log", [row_payload])
            except Exception as e:
                logger.warning(f"Failed to write updated AI summary to BigQuery episodes_log: {e}")

        return existing

    def record_session_helpfulness(
        self,
        session_id: str,
        memory_id: str,
        file_path: str,
        outcome: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Insert a session helpfulness rating into BigQuery `session_helpfulness_ratings` and update episode recall log."""
        self.ensure_telemetry_tables()
        norm_outcome = "HELPED" if str(outcome).upper() in ("HELPED", "USEFUL", "HELPFUL", "TRUE", "1") else "NOT_USEFUL"
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rating_row = {
            "rating_id": f"rat-{uuid.uuid4().hex[:10]}",
            "session_id": session_id,
            "memory_id": memory_id,
            "file_path": file_path,
            "outcome": norm_outcome,
            "reason": reason or f"Recorded {norm_outcome} signal during session {session_id}",
            "rated_at": now_iso,
        }
        self._ratings_cache.insert(0, rating_row)

        # Update cached episode's recalledMemories if session exists
        ep = self.get_ingested_session(session_id)
        if ep:
            recalled = list(ep.get("aiSummary", {}).get("recalledMemories", []))
            updated_match = False
            for rm in recalled:
                if rm.get("fileId") == memory_id or rm.get("path") == file_path:
                    rm["outcome"] = norm_outcome
                    if reason:
                        rm["reason"] = reason
                    updated_match = True
            if not updated_match:
                recalled.append({
                    "fileId": memory_id,
                    "path": file_path,
                    "version": "v1 (Live)",
                    "outcome": norm_outcome,
                    "reason": rating_row["reason"],
                })
            ep.setdefault("aiSummary", {})["recalledMemories"] = recalled
            outcomes = {r.get("outcome") for r in recalled}
            if "HELPED" in outcomes and "NOT_USEFUL" in outcomes:
                ep["helpfulness_status"] = "MIXED"
            elif "NOT_USEFUL" in outcomes:
                ep["helpfulness_status"] = "NOT_USEFUL"
            else:
                ep["helpfulness_status"] = "HELPED"
            ep["_updated_in_session"] = True
            self._episodes_cache[session_id] = ep

        if not self.use_mock and self._bq_client:
            try:
                dataset_id = f"{self.config.project_id}.electric_sheep_telemetry"
                self._bq_client.insert_rows_json(
                    f"{dataset_id}.session_helpfulness_ratings",
                    [rating_row],
                )
            except Exception as e:
                logger.warning(f"Failed to insert helpfulness rating into BigQuery: {e}")

        return rating_row

    def get_helpfulness_ratings(
        self,
        memory_id: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query session helpfulness ratings from BigQuery `session_helpfulness_ratings`."""
        self.ensure_telemetry_tables()
        results: List[Dict[str, Any]] = []
        if not self.use_mock and self._bq_client:
            try:
                from google.cloud import bigquery

                dataset_id = f"{self.config.project_id}.electric_sheep_telemetry"
                conds = ["1=1"]
                params = []
                if memory_id:
                    conds.append("memory_id = @memory_id")
                    params.append(bigquery.ScalarQueryParameter("memory_id", "STRING", memory_id))
                if session_id:
                    conds.append("session_id = @session_id")
                    params.append(bigquery.ScalarQueryParameter("session_id", "STRING", session_id))
                q = f"""
                    SELECT rating_id, session_id, memory_id, file_path, outcome, reason, rated_at
                    FROM `{dataset_id}.session_helpfulness_ratings`
                    WHERE {" AND ".join(conds)}
                    ORDER BY rated_at DESC
                    LIMIT {int(limit)}
                """
                jc = bigquery.QueryJobConfig(query_parameters=params)
                for r in self._bq_client.query(q, job_config=jc).result():
                    d = dict(r)
                    if hasattr(d.get("rated_at"), "isoformat"):
                        d["rated_at"] = d["rated_at"].isoformat()
                    results.append(d)
            except Exception as e:
                logger.warning(f"Failed to query session_helpfulness_ratings from BigQuery: {e}")

        seen_ids = {r.get("rating_id") for r in results}
        for c in getattr(self, "_ratings_cache", []):
            if c.get("rating_id") not in seen_ids:
                if memory_id and c.get("memory_id") != memory_id:
                    continue
                if session_id and c.get("session_id") != session_id:
                    continue
                results.append(dict(c))
        return results[:limit]

    def replicate_memory_to_bq(self, memory_row: Dict[str, Any]) -> bool:
        """Replicate a Cloud Spanner `scoped_memories` row into BigQuery `scoped_memories_replica` (CDC sync)."""
        self.ensure_telemetry_tables()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if not hasattr(self, "_replica_cache"):
            self._replica_cache: Dict[str, Dict[str, Any]] = {}

        mem_id = str(memory_row.get("memory_id") or memory_row.get("id") or memory_row.get("memory_key") or "mem-unknown")
        payload = {
            "memory_id": mem_id,
            "scope_type": str(memory_row.get("scope_type") or memory_row.get("scope") or "USER").upper(),
            "scope_id": str(memory_row.get("scope_id") or "user:developer"),
            "category": str(memory_row.get("category") or "general"),
            "file_path": str(memory_row.get("file_path") or memory_row.get("path") or f"/user/developer/general/{mem_id}.mem"),
            "memory_key": str(memory_row.get("memory_key") or mem_id),
            "memory_value": str(memory_row.get("memory_value") or memory_row.get("content") or ""),
            "crystallized_belief": str(memory_row.get("crystallized_belief") or memory_row.get("memory_value") or ""),
            "confidence": float(memory_row.get("confidence") if memory_row.get("confidence") is not None else 0.95),
            "helpful_count": int(memory_row.get("helpful_count") if memory_row.get("helpful_count") is not None else 1),
            "unhelpful_count": int(memory_row.get("unhelpful_count") if memory_row.get("unhelpful_count") is not None else 0),
            "write_mode": str(memory_row.get("write_mode") or "WRITE_THROUGH_LIVE"),
            "updated_at": str(memory_row.get("updated_at") or now_iso),
            "replicated_at": now_iso,
        }
        self._replica_cache[mem_id] = payload

        if self.use_mock or not self._bq_client:
            return True

        try:
            dataset_id = f"{self.config.project_id}.electric_sheep_telemetry"
            errors = self._bq_client.insert_rows_json(f"{dataset_id}.scoped_memories_replica", [payload])
            return len(errors) == 0
        except Exception as e:
            logger.warning(f"Failed to replicate memory {mem_id} to BigQuery CDC table: {e}")
            return False

    def get_bq_cdc_status(self) -> Dict[str, Any]:
        """Return live BigQuery analytical plane and Spanner-to-BQ CDC replication metrics."""
        self.ensure_telemetry_tables()
        episodes_count = len(getattr(self, "_episodes_cache", {}))
        replica_count = len(getattr(self, "_replica_cache", {}))
        ratings_count = len(getattr(self, "_ratings_cache", []))
        if not self.use_mock and self._bq_client:
            try:
                dataset_id = f"{self.config.project_id}.electric_sheep_telemetry"
                q = f"""
                    SELECT
                        (SELECT COUNT(1) FROM `{dataset_id}.episodes_log`) AS ep_cnt,
                        (SELECT COUNT(1) FROM `{dataset_id}.scoped_memories_replica`) AS rep_cnt,
                        (SELECT COUNT(1) FROM `{dataset_id}.session_helpfulness_ratings`) AS rat_cnt
                """
                rows = list(self._bq_client.query(q).result())
                if rows:
                    episodes_count = max(episodes_count, int(rows[0]["ep_cnt"] or 0))
                    replica_count = max(replica_count, int(rows[0]["rep_cnt"] or 0))
                    ratings_count = max(ratings_count, int(rows[0]["rat_cnt"] or 0))
            except Exception as e:
                logger.debug(f"BigQuery CDC count query fallback: {e}")
        return {
            "dataset": f"{self.config.project_id}.electric_sheep_telemetry",
            "episodes_log_count": episodes_count,
            "scoped_memories_replica_count": replica_count,
            "session_helpfulness_ratings_count": ratings_count,
            "sync_state": "IN_SYNC",
            "last_watermark": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }


def setup_otel_tracing(service_name: str) -> None:
    """Initialize OpenTelemetry tracing to Google Cloud Trace if enabled."""
    config = get_config()
    if not config.otel_to_cloud:
        logger.info(f"OpenTelemetry cloud export disabled for {service_name} (OTEL_TO_CLOUD=false)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider()
        exporter = CloudTraceSpanExporter(project_id=config.project_id)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        logger.info(f"OpenTelemetry CloudTraceSpanExporter initialized for project {config.project_id}")
    except Exception as e:
        logger.warning(f"Could not initialize OpenTelemetry Cloud Trace exporter: {e}")


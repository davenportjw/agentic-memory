"""OpenTelemetry and BigQuery telemetry sink for logging ADK agent prompts and responses."""

import datetime
import json
import logging
from typing import Any, Dict, List, Optional
from src.common.config import get_config

logger = logging.getLogger("adk.telemetry")

# Global in-memory logs so mock mode works across multiple sink instances
_GLOBAL_IN_MEMORY_LOGS: List[Dict[str, Any]] = []


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

    def get_session_logs(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve logs from memory or BigQuery for a given session, ordered from newest to oldest."""
        memory_logs = [
            r for r in self.in_memory_logs
            if not session_id or r.get("session_id") == session_id
        ]
        if self.use_mock or not self._bq_client:
            return sorted(memory_logs, key=lambda x: str(x.get("timestamp", "")), reverse=True)

        try:
            query = f"""
                SELECT *
                FROM `{self.config.project_id}.{self.config.bq_dataset_id}.{self.config.bq_table_id}`
                {"WHERE session_id = @session_id" if session_id else ""}
                ORDER BY timestamp DESC
            """
            job_config = None
            if session_id:
                from google.cloud import bigquery
                job_config = bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("session_id", "STRING", session_id)
                    ]
                )
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
            logger.error(f"Failed to query BigQuery logs ({e})")
            return sorted(memory_logs, key=lambda x: str(x.get("timestamp", "")), reverse=True)

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

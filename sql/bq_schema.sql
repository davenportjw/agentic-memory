-- BigQuery Table and Remote Model Schema for ADK Agent Telemetry & Session Summarization
-- Replace ${PROJECT_ID} and ${BQ_DATASET_ID} with your GCP project and BigQuery dataset names.

-- 1. Create Dataset (if not exists)
-- CREATE SCHEMA IF NOT EXISTS `${PROJECT_ID}.${BQ_DATASET_ID}`;

-- 2. Create Telemetry Logs Table
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${BQ_DATASET_ID}.agent_logs` (
  timestamp TIMESTAMP NOT NULL OPTIONS(description="UTC timestamp of the interaction"),
  user_id STRING NOT NULL OPTIONS(description="User identifier"),
  session_id STRING NOT NULL OPTIONS(description="Session identifier"),
  agent_name STRING NOT NULL OPTIONS(description="Name of the ADK agent"),
  event_type STRING NOT NULL OPTIONS(description="Event type: USER_PROMPT or AGENT_RESPONSE"),
  message STRING OPTIONS(description="Prompt or response text content"),
  json_payload JSON OPTIONS(description="Structured metadata and telemetry payload")
)
PARTITION BY DATE(timestamp)
CLUSTER BY user_id, session_id, agent_name;

-- 3. Create Session Summaries Table (for caching BQ AI.GENERATE results before Spanner export)
CREATE TABLE IF NOT EXISTS `${PROJECT_ID}.${BQ_DATASET_ID}.session_summaries` (
  summary_id STRING NOT NULL,
  user_id STRING NOT NULL,
  session_id STRING NOT NULL,
  generated_summary_json STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY user_id, session_id;

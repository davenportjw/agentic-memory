# BigQuery Dataset for ADK agent telemetry and AI summarization models
resource "google_bigquery_dataset" "agent_telemetry" {
  dataset_id                 = var.bq_dataset_id
  project                    = var.project_id
  location                   = "US"
  description                = "Stores ADK agent prompt/response telemetry and Gemini BQ remote models"
  delete_contents_on_destroy = false

  depends_on = [google_project_service.required_apis]
}

# Telemetry interaction logs table
resource "google_bigquery_table" "agent_logs" {
  dataset_id          = google_bigquery_dataset.agent_telemetry.dataset_id
  table_id            = var.bq_table_id
  project             = var.project_id
  deletion_protection = false

  schema = <<EOF
[
  {
    "name": "timestamp",
    "type": "TIMESTAMP",
    "mode": "NULLABLE",
    "description": "UTC timestamp of the interaction"
  },
  {
    "name": "user_id",
    "type": "STRING",
    "mode": "NULLABLE",
    "description": "User identifier"
  },
  {
    "name": "session_id",
    "type": "STRING",
    "mode": "NULLABLE",
    "description": "Session identifier"
  },
  {
    "name": "agent_name",
    "type": "STRING",
    "mode": "NULLABLE",
    "description": "Name of the ADK agent"
  },
  {
    "name": "event_type",
    "type": "STRING",
    "mode": "NULLABLE",
    "description": "Event type: USER_PROMPT or AGENT_RESPONSE"
  },
  {
    "name": "message",
    "type": "STRING",
    "mode": "NULLABLE",
    "description": "Prompt or response text content"
  },
  {
    "name": "json_payload",
    "type": "JSON",
    "mode": "NULLABLE",
    "description": "Structured metadata and telemetry payload"
  }
]
EOF
}

# Session summaries table
resource "google_bigquery_table" "session_summaries" {
  dataset_id          = google_bigquery_dataset.agent_telemetry.dataset_id
  table_id            = "session_summaries"
  project             = var.project_id
  deletion_protection = false

  schema = <<EOF
[
  {
    "name": "summary_id",
    "type": "STRING",
    "mode": "REQUIRED"
  },
  {
    "name": "user_id",
    "type": "STRING",
    "mode": "REQUIRED"
  },
  {
    "name": "session_id",
    "type": "STRING",
    "mode": "REQUIRED"
  },
  {
    "name": "generated_summary_json",
    "type": "STRING",
    "mode": "REQUIRED"
  },
  {
    "name": "created_at",
    "type": "TIMESTAMP",
    "mode": "REQUIRED"
  }
]
EOF
}

# BigQuery Cloud Resource connection for Vertex AI ML.GENERATE_TEXT
resource "google_bigquery_connection" "vertex_ai" {
  connection_id = "vertex_ai_conn"
  project       = var.project_id
  location      = "US"
  cloud_resource {}

  depends_on = [google_project_service.required_apis]
}

# Grant Vertex AI User role to the BigQuery connection's service account
resource "google_project_iam_member" "bq_connection_aiplatform" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_bigquery_connection.vertex_ai.cloud_resource[0].service_account_id}"
}

# Create BigQuery Remote Model for AI.GENERATE_TEXT / ML.GENERATE_TEXT
locals {
  gemini_model_ddl = <<EOT
CREATE OR REPLACE MODEL `${var.project_id}.${var.bq_dataset_id}.gemini_summarizer`
REMOTE WITH CONNECTION `${var.project_id}.us.vertex_ai_conn`
OPTIONS (ENDPOINT = '${var.model_name}');
EOT
}

resource "google_bigquery_job" "create_gemini_model" {
  # BigQuery jobs are immutable and job IDs can't be reused, so the ID is derived from the DDL:
  # changing the model/endpoint yields a new job (replace) that re-runs CREATE OR REPLACE MODEL.
  job_id   = "create_gemini_model_${substr(sha1(local.gemini_model_ddl), 0, 12)}"
  project  = var.project_id
  location = "US"

  query {
    query          = local.gemini_model_ddl
    use_legacy_sql = false
    # ML DDL rejects dispositions ("Cannot set create disposition in jobs with ML DDL statements");
    # the provider otherwise defaults to CREATE_IF_NEEDED / WRITE_EMPTY.
    create_disposition = ""
    write_disposition  = ""
  }

  depends_on = [
    google_bigquery_dataset.agent_telemetry,
    google_bigquery_connection.vertex_ai,
    google_project_iam_member.bq_connection_aiplatform
  ]
}

# Enterprise Memory Studio Analytical Lakehouse Dataset (`electric_sheep_telemetry`)
resource "google_bigquery_dataset" "electric_sheep_telemetry" {
  dataset_id                 = "electric_sheep_telemetry"
  project                    = var.project_id
  location                   = "US"
  description                = "Stores ingested session episodes, Spanner CDC memory replicas, and multi-session helpfulness ratings"
  delete_contents_on_destroy = false

  depends_on = [google_project_service.required_apis]
}

# Episodes Log Table (`episodes_log`)
resource "google_bigquery_table" "episodes_log" {
  dataset_id          = google_bigquery_dataset.electric_sheep_telemetry.dataset_id
  table_id            = "episodes_log"
  project             = var.project_id
  deletion_protection = true


  schema = <<EOF
[
  {"name": "session_id", "type": "STRING", "mode": "REQUIRED"},
  {"name": "user_id", "type": "STRING", "mode": "NULLABLE"},
  {"name": "agent_id", "type": "STRING", "mode": "NULLABLE"},
  {"name": "team_id", "type": "STRING", "mode": "NULLABLE"},
  {"name": "stream_source", "type": "STRING", "mode": "NULLABLE"},
  {"name": "topic", "type": "STRING", "mode": "NULLABLE"},
  {"name": "turn_count", "type": "INTEGER", "mode": "NULLABLE"},
  {"name": "transcript_json", "type": "STRING", "mode": "NULLABLE"},
  {"name": "extracted_signals_json", "type": "STRING", "mode": "NULLABLE"},
  {"name": "ai_summary", "type": "STRING", "mode": "NULLABLE"},
  {"name": "helpfulness_status", "type": "STRING", "mode": "NULLABLE"},
  {"name": "ingested_at", "type": "TIMESTAMP", "mode": "REQUIRED"}
]
EOF
}

# Scoped Memories CDC Replica Table (`scoped_memories_replica`)
resource "google_bigquery_table" "scoped_memories_replica" {
  dataset_id          = google_bigquery_dataset.electric_sheep_telemetry.dataset_id
  table_id            = "scoped_memories_replica"
  project             = var.project_id
  deletion_protection = true


  schema = <<EOF
[
  {"name": "memory_id", "type": "STRING", "mode": "REQUIRED"},
  {"name": "scope_type", "type": "STRING", "mode": "REQUIRED"},
  {"name": "scope_id", "type": "STRING", "mode": "REQUIRED"},
  {"name": "category", "type": "STRING", "mode": "NULLABLE"},
  {"name": "file_path", "type": "STRING", "mode": "REQUIRED"},
  {"name": "memory_key", "type": "STRING", "mode": "REQUIRED"},
  {"name": "memory_value", "type": "STRING", "mode": "REQUIRED"},
  {"name": "crystallized_belief", "type": "STRING", "mode": "NULLABLE"},
  {"name": "confidence", "type": "FLOAT", "mode": "NULLABLE"},
  {"name": "helpful_count", "type": "INTEGER", "mode": "NULLABLE"},
  {"name": "unhelpful_count", "type": "INTEGER", "mode": "NULLABLE"},
  {"name": "write_mode", "type": "STRING", "mode": "NULLABLE"},
  {"name": "updated_at", "type": "TIMESTAMP", "mode": "REQUIRED"},
  {"name": "replicated_at", "type": "TIMESTAMP", "mode": "NULLABLE"}
]
EOF
}

# Session Helpfulness Ratings Table (`session_helpfulness_ratings`)
resource "google_bigquery_table" "session_helpfulness_ratings" {
  dataset_id          = google_bigquery_dataset.electric_sheep_telemetry.dataset_id
  table_id            = "session_helpfulness_ratings"
  project             = var.project_id
  deletion_protection = true


  schema = <<EOF
[
  {"name": "rating_id", "type": "STRING", "mode": "REQUIRED"},
  {"name": "session_id", "type": "STRING", "mode": "REQUIRED"},
  {"name": "memory_id", "type": "STRING", "mode": "REQUIRED"},
  {"name": "file_path", "type": "STRING", "mode": "NULLABLE"},
  {"name": "outcome", "type": "STRING", "mode": "REQUIRED"},
  {"name": "reason", "type": "STRING", "mode": "NULLABLE"},
  {"name": "rated_at", "type": "TIMESTAMP", "mode": "REQUIRED"}
]
EOF
}


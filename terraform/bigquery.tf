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
    "mode": "REQUIRED",
    "description": "UTC timestamp of the interaction"
  },
  {
    "name": "user_id",
    "type": "STRING",
    "mode": "REQUIRED",
    "description": "User identifier"
  },
  {
    "name": "session_id",
    "type": "STRING",
    "mode": "REQUIRED",
    "description": "Session identifier"
  },
  {
    "name": "agent_name",
    "type": "STRING",
    "mode": "REQUIRED",
    "description": "Name of the ADK agent"
  },
  {
    "name": "event_type",
    "type": "STRING",
    "mode": "REQUIRED",
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
resource "google_bigquery_job" "create_gemini_model" {
  job_id   = "create_gemini_model_job"
  project  = var.project_id
  location = "US"

  query {
    query          = <<EOT
      CREATE OR REPLACE MODEL `${var.project_id}.${var.bq_dataset_id}.gemini_summarizer`
      REMOTE WITH CONNECTION `${var.project_id}.us.vertex_ai_conn`
      OPTIONS (ENDPOINT = '${var.model_name}');
    EOT
    use_legacy_sql = false
  }

  depends_on = [
    google_bigquery_dataset.agent_telemetry,
    google_bigquery_connection.vertex_ai,
    google_project_iam_member.bq_connection_aiplatform
  ]
}

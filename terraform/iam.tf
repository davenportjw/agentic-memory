# Service Account for Cloud Run ADK Agent Services
resource "google_service_account" "agent_sa" {
  account_id   = "adk-agent-sa"
  display_name = "Google ADK 2.0 Cloud Run Agent Service Account"
  project      = var.project_id

  depends_on = [google_project_service.required_apis]
}

# Grant Vertex AI User role for Gemini calls
resource "google_project_iam_member" "aiplatform_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Grant BigQuery Data Editor for telemetry logs and session summaries
resource "google_project_iam_member" "bigquery_data_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Grant BigQuery Job User for executing ML.GENERATE_TEXT queries
resource "google_project_iam_member" "bigquery_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Grant Spanner Database User for reading/writing profile preferences
resource "google_project_iam_member" "spanner_database_user" {
  project = var.project_id
  role    = "roles/spanner.databaseUser"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Grant Cloud Trace Agent for OpenTelemetry trace exporting
resource "google_project_iam_member" "cloudtrace_agent" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

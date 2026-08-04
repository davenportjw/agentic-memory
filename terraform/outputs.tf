output "project_id" {
  description = "Google Cloud Project ID"
  value       = var.project_id
}

output "model_name" {
  description = "Gemini model configured for ADK agents and BigQuery AI"
  value       = var.model_name
}

output "interview_agent_url" {
  description = "Cloud Run service URL for InterviewAgent"
  value       = google_cloud_run_v2_service.interview_agent.uri
}

output "preference_agent_url" {
  description = "Cloud Run service URL for PreferenceAgent"
  value       = google_cloud_run_v2_service.preference_agent.uri
}

output "ui_service_url" {
  description = "Cloud Run service URL for Material Light UI Service"
  value       = google_cloud_run_v2_service.ui_service.uri
}

output "bigquery_dataset_id" {
  description = "BigQuery dataset ID containing agent logs and remote model"
  value       = google_bigquery_dataset.agent_telemetry.dataset_id
}

output "spanner_database_path" {
  description = "Google Cloud Spanner database resource path"
  value       = google_spanner_database.adk_database.name
}

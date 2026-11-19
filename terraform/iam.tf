# Legacy shared Service Account for Cloud Run ADK Agent Services.
# No Cloud Run service runs as this account any more: each service has its own dedicated,
# least-privilege account below. Kept (never deleted) so that existing references and audit history
# stay valid; it holds no project-level roles.
resource "google_service_account" "agent_sa" {
  account_id   = "adk-agent-sa"
  display_name = "Google ADK 2.0 Cloud Run Agent Service Account"
  project      = var.project_id

  depends_on = [google_project_service.required_apis]
}

# Grant Service Account Token Creator on itself so ADC IAM Credentials API can mint OIDC tokens
resource "google_service_account_iam_member" "agent_sa_token_creator" {
  service_account_id = google_service_account.agent_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.agent_sa.email}"
}

# ---------------------------------------------------------------------------------------------
# Dedicated per-service accounts (one runtime identity per Cloud Run service).
# On Cloud Run, OIDC tokens for service-to-service calls come from the metadata server
# (google.oauth2.id_token.fetch_id_token), so no Token Creator grant is needed.
# ---------------------------------------------------------------------------------------------

resource "google_service_account" "ui_sa" {
  account_id   = "es-ui-sa"
  display_name = "Electric Sheep ui-service runtime"
  project      = var.project_id

  depends_on = [google_project_service.required_apis]
}

resource "google_service_account" "interview_sa" {
  account_id   = "es-interview-sa"
  display_name = "Electric Sheep interview-agent runtime"
  project      = var.project_id

  depends_on = [google_project_service.required_apis]
}

resource "google_service_account" "preference_sa" {
  account_id   = "es-preference-sa"
  display_name = "Electric Sheep preference-agent runtime"
  project      = var.project_id

  depends_on = [google_project_service.required_apis]
}

locals {
  # Project-level roles that have no narrower resource scope (Vertex AI, Cloud Trace, BigQuery jobs),
  # plus ui-service extras:
  # - bigquery.dataEditor (project): ui-service's studio telemetry calls create_dataset(exists_ok=True)
  #   for `electric_sheep_telemetry`, which needs bigquery.datasets.create at project level.
  # - firebaseauth.admin: ui-service mints Firebase session cookies with Identity Toolkit
  #   projects.createSessionCookie (firebaseauth.users.createSession) and checks disabled/revoked
  #   accounts with accounts:lookup (firebaseauth.users.get), both via ADC.
  es_project_roles = {
    "ui-aiplatform"         = { sa = google_service_account.ui_sa.email, role = "roles/aiplatform.user" }
    "ui-cloudtrace"         = { sa = google_service_account.ui_sa.email, role = "roles/cloudtrace.agent" }
    "ui-bq-jobuser"         = { sa = google_service_account.ui_sa.email, role = "roles/bigquery.jobUser" }
    "ui-bq-dataeditor"      = { sa = google_service_account.ui_sa.email, role = "roles/bigquery.dataEditor" }
    "ui-firebaseauth"       = { sa = google_service_account.ui_sa.email, role = "roles/firebaseauth.admin" }
    "interview-aiplatform"  = { sa = google_service_account.interview_sa.email, role = "roles/aiplatform.user" }
    "interview-cloudtrace"  = { sa = google_service_account.interview_sa.email, role = "roles/cloudtrace.agent" }
    "interview-bq-jobuser"  = { sa = google_service_account.interview_sa.email, role = "roles/bigquery.jobUser" }
    "preference-aiplatform" = { sa = google_service_account.preference_sa.email, role = "roles/aiplatform.user" }
    "preference-cloudtrace" = { sa = google_service_account.preference_sa.email, role = "roles/cloudtrace.agent" }
    "preference-bq-jobuser" = { sa = google_service_account.preference_sa.email, role = "roles/bigquery.jobUser" }
    # Spanner client-side metrics (google-cloud-spanner) export to Cloud Monitoring and fail with
    # `monitoring.timeSeries.create denied` without this role.
    "ui-metricwriter"         = { sa = google_service_account.ui_sa.email, role = "roles/monitoring.metricWriter" }
    "preference-metricwriter" = { sa = google_service_account.preference_sa.email, role = "roles/monitoring.metricWriter" }
  }
}

resource "google_project_iam_member" "es_project_roles" {
  for_each = local.es_project_roles

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${each.value.sa}"
}

# The agents only read/write the ADK telemetry dataset (agent_logs / session_summaries).
resource "google_bigquery_dataset_iam_member" "agent_telemetry_editors" {
  for_each = {
    interview  = google_service_account.interview_sa.email
    preference = google_service_account.preference_sa.email
  }

  project    = var.project_id
  dataset_id = google_bigquery_dataset.agent_telemetry.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${each.value}"
}

# Spanner memory store: database-scoped, only for the services that use SpannerPreferenceClient.
resource "google_spanner_database_iam_member" "memory_db_users" {
  for_each = {
    ui         = google_service_account.ui_sa.email
    preference = google_service_account.preference_sa.email
  }

  project  = var.project_id
  instance = google_spanner_instance.adk_instance.name
  database = google_spanner_database.adk_database.name
  role     = "roles/spanner.databaseUser"
  member   = "serviceAccount:${each.value}"
}

# Google Cloud Run Service: InterviewAgent
resource "google_cloud_run_v2_service" "interview_agent" {
  name     = "interview-agent"
  location = var.region
  project  = var.project_id

  template {
    service_account = google_service_account.agent_sa.email

    containers {
      image = var.container_image
      ports {
        container_port = 8080
      }
      env {
        name  = "SERVICE_MODULE"
        value = "src.interview_agent.server"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_REGION"
        value = var.region
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "true"
      }
      env {
        name  = "MODEL_NAME"
        value = var.model_name
      }
      env {
        name  = "BQ_DATASET_ID"
        value = var.bq_dataset_id
      }
      env {
        name  = "BQ_TABLE_ID"
        value = var.bq_table_id
      }
      env {
        name  = "SPANNER_INSTANCE_ID"
        value = var.spanner_instance_id
      }
      env {
        name  = "SPANNER_DATABASE_ID"
        value = var.spanner_database_id
      }
      env {
        name  = "OTEL_TO_CLOUD"
        value = tostring(var.otel_to_cloud)
      }
      env {
        name  = "ENABLE_TELEMETRY"
        value = "true"
      }
      env {
        name  = "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"
        value = "true"
      }
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "true"
      }
      env {
        name  = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"
        value = "true"
      }
      env {
        name  = "USE_MOCK_CLIENTS"
        value = "false"
      }
      env {
        name  = "IAP_ENABLED"
        value = tostring(var.iap_enabled)
      }
      env {
        name  = "IAP_EXPECTED_AUDIENCE"
        value = var.iap_expected_audience
      }
      env {
        name  = "IAP_HEADER_NAME"
        value = var.iap_header_name
      }
      env {
        name  = "IAP_PROXY_URL"
        value = var.iap_proxy_url
      }
    }
  }

  depends_on = [
    google_project_service.required_apis,
    google_bigquery_table.agent_logs,
    google_spanner_database.adk_database
  ]
}

# Google Cloud Run Service: PreferenceAgent
resource "google_cloud_run_v2_service" "preference_agent" {
  name     = "preference-agent"
  location = var.region
  project  = var.project_id

  template {
    service_account = google_service_account.agent_sa.email

    containers {
      image = var.container_image
      ports {
        container_port = 8080
      }
      env {
        name  = "SERVICE_MODULE"
        value = "src.preference_agent.server"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_REGION"
        value = var.region
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "true"
      }
      env {
        name  = "MODEL_NAME"
        value = var.model_name
      }
      env {
        name  = "BQ_DATASET_ID"
        value = var.bq_dataset_id
      }
      env {
        name  = "BQ_TABLE_ID"
        value = var.bq_table_id
      }
      env {
        name  = "SPANNER_INSTANCE_ID"
        value = var.spanner_instance_id
      }
      env {
        name  = "SPANNER_DATABASE_ID"
        value = var.spanner_database_id
      }
      env {
        name  = "OTEL_TO_CLOUD"
        value = tostring(var.otel_to_cloud)
      }
      env {
        name  = "ENABLE_TELEMETRY"
        value = "true"
      }
      env {
        name  = "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"
        value = "true"
      }
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "true"
      }
      env {
        name  = "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"
        value = "true"
      }
      env {
        name  = "USE_MOCK_CLIENTS"
        value = "false"
      }
      env {
        name  = "IAP_ENABLED"
        value = tostring(var.iap_enabled)
      }
      env {
        name  = "IAP_EXPECTED_AUDIENCE"
        value = var.iap_expected_audience
      }
      env {
        name  = "IAP_HEADER_NAME"
        value = var.iap_header_name
      }
      env {
        name  = "IAP_PROXY_URL"
        value = var.iap_proxy_url
      }
    }
  }

  depends_on = [
    google_project_service.required_apis,
    google_bigquery_table.agent_logs,
    google_spanner_database.adk_database
  ]
}

# Google Cloud Run Service: Material Light UI Service
resource "google_cloud_run_v2_service" "ui_service" {
  name     = "ui-service"
  location = var.region
  project  = var.project_id

  template {
    service_account = google_service_account.agent_sa.email

    containers {
      image = var.container_image
      ports {
        container_port = 8080
      }
      env {
        name  = "SERVICE_MODULE"
        value = "src.ui_service.server"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_REGION"
        value = var.region
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "true"
      }
      env {
        name  = "MODEL_NAME"
        value = var.model_name
      }
      env {
        name  = "BQ_DATASET_ID"
        value = var.bq_dataset_id
      }
      env {
        name  = "BQ_TABLE_ID"
        value = var.bq_table_id
      }
      env {
        name  = "SPANNER_INSTANCE_ID"
        value = var.spanner_instance_id
      }
      env {
        name  = "SPANNER_DATABASE_ID"
        value = var.spanner_database_id
      }
      env {
        name  = "INTERVIEW_AGENT_URL"
        value = google_cloud_run_v2_service.interview_agent.uri
      }
      env {
        name  = "PREFERENCE_AGENT_URL"
        value = google_cloud_run_v2_service.preference_agent.uri
      }
      env {
        name  = "USE_MOCK_CLIENTS"
        value = "false"
      }
      env {
        name  = "ALLOWED_EMAIL_DOMAINS"
        value = var.allowed_email_domains
      }
      env {
        name  = "FIREBASE_API_KEY"
        value = var.firebase_api_key
      }
      env {
        name  = "FIREBASE_AUTH_DOMAIN"
        value = var.firebase_auth_domain
      }
      env {
        name  = "FIREBASE_PROJECT_ID"
        value = var.firebase_project_id
      }
      env {
        name  = "FIREBASE_STORAGE_BUCKET"
        value = var.firebase_storage_bucket
      }
      env {
        name  = "FIREBASE_MESSAGING_SENDER_ID"
        value = var.firebase_messaging_sender_id
      }
      env {
        name  = "FIREBASE_APP_ID"
        value = var.firebase_app_id
      }
      env {
        name  = "IAP_ENABLED"
        value = tostring(var.iap_enabled)
      }
      env {
        name  = "IAP_EXPECTED_AUDIENCE"
        value = var.iap_expected_audience
      }
      env {
        name  = "IAP_HEADER_NAME"
        value = var.iap_header_name
      }
      env {
        name  = "IAP_PROXY_URL"
        value = var.iap_proxy_url
      }
    }
  }

  depends_on = [
    google_cloud_run_v2_service.interview_agent,
    google_cloud_run_v2_service.preference_agent
  ]
}

# Internal access binding: backends are only accessible by the UI/frontend service account and developer validator
resource "google_cloud_run_service_iam_binding" "interview_agent_invoker" {
  location = google_cloud_run_v2_service.interview_agent.location
  project  = google_cloud_run_v2_service.interview_agent.project
  service  = google_cloud_run_v2_service.interview_agent.name
  role     = "roles/run.invoker"
  members = concat(
    ["serviceAccount:${google_service_account.agent_sa.email}"],
    var.authorized_invoker_members
  )
}

resource "google_cloud_run_service_iam_binding" "preference_agent_invoker" {
  location = google_cloud_run_v2_service.preference_agent.location
  project  = google_cloud_run_v2_service.preference_agent.project
  service  = google_cloud_run_v2_service.preference_agent.name
  role     = "roles/run.invoker"
  members = concat(
    ["serviceAccount:${google_service_account.agent_sa.email}"],
    var.authorized_invoker_members
  )
}

# Public access binding for UI Service (Control Center login dashboard)
resource "google_cloud_run_service_iam_binding" "ui_service_public" {
  location = google_cloud_run_v2_service.ui_service.location
  project  = google_cloud_run_v2_service.ui_service.project
  service  = google_cloud_run_v2_service.ui_service.name
  role     = "roles/run.invoker"
  members  = ["allUsers"]
}

# Manage Authorized Domains for Google Identity Platform / Firebase Auth
resource "google_identity_platform_config" "auth_config" {
  project = var.project_id

  authorized_domains = [
    "localhost",
    "google.com",
    "cloudadvocacyorg.joonix.net",
    "ui-service-txgsracloq-uc.a.run.app",
    "ui-service-834476222725.us-central1.run.app",
    "interview-agent-txgsracloq-uc.a.run.app",
    "interview-agent-834476222725.us-central1.run.app",
    "preference-agent-txgsracloq-uc.a.run.app",
    "preference-agent-834476222725.us-central1.run.app",
    "${var.project_id}.firebaseapp.com",
    "${var.project_id}.web.app",
    replace(replace(google_cloud_run_v2_service.ui_service.uri, "https://", ""), "/", ""),
    replace(replace(google_cloud_run_v2_service.interview_agent.uri, "https://", ""), "/", ""),
    replace(replace(google_cloud_run_v2_service.preference_agent.uri, "https://", ""), "/", "")
  ]

  lifecycle {
    ignore_changes = [
      autodelete_anonymous_users,
      blocking_functions,
      client,
      mfa,
      monitoring,
      multi_tenant,
      quota,
      sign_in,
      sms_region_config
    ]
  }

  depends_on = [
    google_project_service.required_apis
  ]
}

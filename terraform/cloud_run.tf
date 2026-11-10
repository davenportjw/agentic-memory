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

  # Rollouts (image, env, source build) are owned by scripts/deploy_cloud_run_source.sh via
  # `gcloud run deploy`; Terraform owns the service shell, scaling, IAM and identity.
  lifecycle {
    ignore_changes = [
      client,
      client_version,
      build_config,
      template[0].containers[0].image,
      template[0].containers[0].env,
      template[0].containers[0].name,
    ]
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

  # Rollouts (image, env, source build) are owned by scripts/deploy_cloud_run_source.sh via
  # `gcloud run deploy`; Terraform owns the service shell, scaling, IAM and identity.
  lifecycle {
    ignore_changes = [
      client,
      client_version,
      build_config,
      template[0].containers[0].image,
      template[0].containers[0].env,
      template[0].containers[0].name,
    ]
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

  # Rollouts (image, env, source build) are owned by scripts/deploy_cloud_run_source.sh via
  # `gcloud run deploy`; Terraform owns the service shell, scaling, IAM and identity.
  lifecycle {
    ignore_changes = [
      client,
      client_version,
      build_config,
      template[0].containers[0].image,
      template[0].containers[0].env,
      template[0].containers[0].name,
    ]
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

# Sign-in redirect hosts for Google Identity Platform / Firebase Auth.
# The project's authorized-domains list is shared by every app in the project, so this repo no
# longer manages the whole list (google_identity_platform_config is owned by keynote/infra/step0).
# It adds only its own Cloud Run hosts on create and removes only those on destroy, one resource per
# service (scripts/authorized-domains.sh).
locals {
  sign_in_hosts = {
    ui_service       = [for u in google_cloud_run_v2_service.ui_service.urls : trimsuffix(trimprefix(u, "https://"), "/")]
    interview_agent  = [for u in google_cloud_run_v2_service.interview_agent.urls : trimsuffix(trimprefix(u, "https://"), "/")]
    preference_agent = [for u in google_cloud_run_v2_service.preference_agent.urls : trimsuffix(trimprefix(u, "https://"), "/")]
  }
}

resource "terraform_data" "authorized_domains" {
  for_each = local.sign_in_hosts

  triggers_replace = {
    project = var.project_id
    hosts   = join(" ", sort(each.value))
  }

  provisioner "local-exec" {
    command = "${path.module}/scripts/authorized-domains.sh add ${self.triggers_replace.project} ${self.triggers_replace.hosts}"
  }

  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "[ -x ${path.module}/scripts/authorized-domains.sh ] && ${path.module}/scripts/authorized-domains.sh remove ${self.triggers_replace.project} ${self.triggers_replace.hosts}"
  }

  depends_on = [google_project_service.required_apis]
}

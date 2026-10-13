"""Configuration and environment variables for the ADK agents and pipelines."""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class AppConfig:
    project_id: str
    region: str
    bq_dataset_id: str
    bq_table_id: str
    spanner_instance_id: str
    spanner_database_id: str
    otel_to_cloud: bool
    enable_telemetry: bool
    model_name: str
    antigravity_brain_path: str
    ui_service_url: str
    interview_agent_url: str
    preference_agent_url: str
    allowed_email_domains: str
    firebase_api_key: str
    firebase_auth_domain: str
    firebase_project_id: str
    firebase_storage_bucket: str
    firebase_messaging_sender_id: str
    firebase_app_id: str
    iap_enabled: bool
    iap_expected_audience: str
    iap_header_name: str
    iap_proxy_url: str


def get_config() -> AppConfig:
    """Load configuration from environment variables with sensible defaults."""
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", os.environ.get("PROJECT_ID", "davenport-boutique"))
    region = os.environ.get("GOOGLE_CLOUD_REGION", os.environ.get("REGION", "us-central1"))
    bq_dataset_id = os.environ.get("BQ_DATASET_ID", "adk_agent_telemetry")
    bq_table_id = os.environ.get("BQ_TABLE_ID", "agent_logs")
    spanner_instance_id = os.environ.get("SPANNER_INSTANCE_ID", "adk-spanner-instance")
    spanner_database_id = os.environ.get("SPANNER_DATABASE_ID", "agent_memory_db")
    otel_to_cloud = os.environ.get("OTEL_TO_CLOUD", "false").lower() in ("true", "1", "yes")
    enable_telemetry = os.environ.get("ENABLE_TELEMETRY", "true").lower() in ("true", "1", "yes")
    model_name = os.environ.get("MODEL_NAME", "gemini-3.8-flash")
    antigravity_brain_path = os.environ.get("ANTIGRAVITY_BRAIN_PATH", os.path.expanduser("~/.gemini/antigravity/brain"))
    ui_service_url = os.environ.get("UI_SERVICE_URL", "http://localhost:8080")
    interview_agent_url = os.environ.get("INTERVIEW_AGENT_URL", "http://localhost:8081")
    preference_agent_url = os.environ.get("PREFERENCE_AGENT_URL", "http://localhost:8082")

    # Authentication & Firebase configuration
    allowed_email_domains = os.environ.get("ALLOWED_EMAIL_DOMAINS", "example.com,user@example.com")
    firebase_api_key = os.environ.get("FIREBASE_API_KEY", "your_firebase_api_key_here")
    firebase_auth_domain = os.environ.get("FIREBASE_AUTH_DOMAIN", "your-project-id.firebaseapp.com")
    firebase_project_id = os.environ.get("FIREBASE_PROJECT_ID", "your-project-id")
    firebase_storage_bucket = os.environ.get("FIREBASE_STORAGE_BUCKET", "your-project-id.firebasestorage.app")
    firebase_messaging_sender_id = os.environ.get("FIREBASE_MESSAGING_SENDER_ID", "123456789012")
    firebase_app_id = os.environ.get("FIREBASE_APP_ID", "1:123456789012:web:your_app_id_here")

    # Identity-Aware Proxy (IAP) & reverse proxy configuration
    iap_enabled = os.environ.get("IAP_ENABLED", "false").lower() in ("true", "1", "yes")
    iap_expected_audience = os.environ.get("IAP_EXPECTED_AUDIENCE", "")
    iap_header_name = os.environ.get("IAP_HEADER_NAME", "x-goog-iap-jwt-assertion")
    iap_proxy_url = os.environ.get("IAP_PROXY_URL", "")

    # Explicitly set Vertex AI environment variables so ADK/google.genai.Client always uses Vertex AI
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
    os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"

    return AppConfig(
        project_id=project_id,
        region=region,
        bq_dataset_id=bq_dataset_id,
        bq_table_id=bq_table_id,
        spanner_instance_id=spanner_instance_id,
        spanner_database_id=spanner_database_id,
        otel_to_cloud=otel_to_cloud,
        enable_telemetry=enable_telemetry,
        model_name=model_name,
        antigravity_brain_path=antigravity_brain_path,
        ui_service_url=ui_service_url,
        interview_agent_url=interview_agent_url,
        preference_agent_url=preference_agent_url,
        allowed_email_domains=allowed_email_domains,
        firebase_api_key=firebase_api_key,
        firebase_auth_domain=firebase_auth_domain,
        firebase_project_id=firebase_project_id,
        firebase_storage_bucket=firebase_storage_bucket,
        firebase_messaging_sender_id=firebase_messaging_sender_id,
        firebase_app_id=firebase_app_id,
        iap_enabled=iap_enabled,
        iap_expected_audience=iap_expected_audience,
        iap_header_name=iap_header_name,
        iap_proxy_url=iap_proxy_url,
    )


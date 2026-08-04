variable "project_id" {
  description = "The Google Cloud Project ID where ADK agents and services are deployed."
  type        = string
  default     = "your-gcp-project-id"
}

variable "region" {
  description = "The Google Cloud region for deploying Cloud Run and Spanner."
  type        = string
  default     = "us-central1"
}

variable "model_name" {
  description = "The Gemini model name to use across ADK agents and BigQuery AI summarization."
  type        = string
  default     = "gemini-3.5-flash-lite"
}

variable "bq_dataset_id" {
  description = "The BigQuery dataset ID for storing agent telemetry and ML models."
  type        = string
  default     = "adk_agent_telemetry"
}

variable "bq_table_id" {
  description = "The BigQuery table ID for storing interaction logs and traces."
  type        = string
  default     = "agent_logs"
}

variable "spanner_instance_id" {
  description = "The Google Cloud Spanner instance ID for agent memory."
  type        = string
  default     = "adk-spanner-instance"
}

variable "spanner_database_id" {
  description = "The Google Cloud Spanner database ID for user preferences and summaries."
  type        = string
  default     = "agent_memory_db"
}

variable "otel_to_cloud" {
  description = "Enable OpenTelemetry exporting to Google Cloud Trace."
  type        = bool
  default     = true
}

variable "container_image" {
  description = "Optional prebuilt container image URI. Defaults to Python slim test image if not set."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "allowed_email_domains" {
  description = "Comma-separated list of email addresses or domains authorized to sign in to the frontend Control Center."
  type        = string
  default     = "example.com,user@example.com"
}

variable "firebase_api_key" {
  description = "Firebase Web SDK API Key for Google Sign-In popup in browser."
  type        = string
  default     = "your_firebase_api_key_here"
}

variable "firebase_auth_domain" {
  description = "Firebase Web SDK Auth Domain."
  type        = string
  default     = "your-project-id.firebaseapp.com"
}

variable "firebase_project_id" {
  description = "Firebase Web SDK Project ID."
  type        = string
  default     = "your-project-id"
}

variable "firebase_storage_bucket" {
  description = "Firebase Web SDK Storage Bucket."
  type        = string
  default     = "your-project-id.firebasestorage.app"
}

variable "firebase_messaging_sender_id" {
  description = "Firebase Web SDK Messaging Sender ID."
  type        = string
  default     = "123456789012"
}

variable "firebase_app_id" {
  description = "Firebase Web SDK App ID."
  type        = string
  default     = "1:123456789012:web:your_app_id_here"
}

variable "authorized_invoker_members" {
  description = "List of additional IAM members authorized to invoke backend Cloud Run services (e.g. user:developer@example.com or service accounts)."
  type        = list(string)
  default     = []
}

variable "iap_enabled" {
  description = "Enable Identity-Aware Proxy (IAP) or behind-IAP-proxy mode on Cloud Run services."
  type        = bool
  default     = false
}

variable "iap_expected_audience" {
  description = "Expected audience (aud) claim for signed IAP JWT assertions from Google Cloud IAP."
  type        = string
  default     = ""
}

variable "iap_header_name" {
  description = "HTTP header name for IAP JWT assertion or authenticated email."
  type        = string
  default     = "x-goog-iap-jwt-assertion"
}

variable "iap_proxy_url" {
  description = "The external URL of the IAP proxy or Application Load Balancer."
  type        = string
  default     = ""
}

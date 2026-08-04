#!/usr/bin/env bash
# Deploy all 3 Google ADK 2.0 services to Google Cloud Run from source using root Dockerfile
set -euo pipefail

# Automatically load local .env file if present (ignored by git)
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    if [[ ! "$key" =~ ^# && -n "$key" ]]; then
      value=$(echo "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'\''"]//' -e 's/["'\''"]$//')
      export "$key=$value"
    fi
  done < .env
fi

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-your-gcp-project-id}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
MODEL_NAME="${MODEL_NAME:-gemini-3.5-flash-lite}"
BQ_DATASET_ID="${BQ_DATASET_ID:-adk_agent_telemetry}"
BQ_TABLE_ID="${BQ_TABLE_ID:-agent_logs}"
SPANNER_INSTANCE_ID="${SPANNER_INSTANCE_ID:-adk-spanner-instance}"
SPANNER_DATABASE_ID="${SPANNER_DATABASE_ID:-agent_memory_db}"
UI_SERVICE_URL="${UI_SERVICE_URL:-http://localhost:8080}"
INTERVIEW_AGENT_URL="${INTERVIEW_AGENT_URL:-http://localhost:8081}"
PREFERENCE_AGENT_URL="${PREFERENCE_AGENT_URL:-http://localhost:8082}"
ALLOWED_EMAIL_DOMAINS="${ALLOWED_EMAIL_DOMAINS:-example.com,user@example.com}"
FIREBASE_API_KEY="${FIREBASE_API_KEY:-your_firebase_api_key_here}"
FIREBASE_AUTH_DOMAIN="${FIREBASE_AUTH_DOMAIN:-your-project-id.firebaseapp.com}"
FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-your-project-id}"
FIREBASE_STORAGE_BUCKET="${FIREBASE_STORAGE_BUCKET:-your-project-id.firebasestorage.app}"
FIREBASE_MESSAGING_SENDER_ID="${FIREBASE_MESSAGING_SENDER_ID:-123456789012}"
FIREBASE_APP_ID="${FIREBASE_APP_ID:-1:123456789012:web:your_app_id_here}"

# Identity-Aware Proxy (IAP) & Reverse Proxy Configuration
IAP_ENABLED="${IAP_ENABLED:-false}"
IAP_EXPECTED_AUDIENCE="${IAP_EXPECTED_AUDIENCE:-}"
IAP_HEADER_NAME="${IAP_HEADER_NAME:-x-goog-iap-jwt-assertion}"
IAP_PROXY_URL="${IAP_PROXY_URL:-}"

echo "================================================================================"
echo "  Deploying Google ADK 2.0 Multi-Agent Demo to Google Cloud Run"
echo "  Project:  ${PROJECT_ID}"
echo "  Region:   ${REGION}"
echo "  Model:    ${MODEL_NAME}"
echo "================================================================================"

echo ""
echo "[1/3] Deploying interview-agent..."
gcloud run deploy interview-agent \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --source=. \
  --clear-base-image \
  --no-allow-unauthenticated \
  --set-env-vars="SERVICE_MODULE=src.interview_agent.server,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=true,MODEL_NAME=${MODEL_NAME},BQ_DATASET_ID=${BQ_DATASET_ID},BQ_TABLE_ID=${BQ_TABLE_ID},SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID},SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID},USE_MOCK_CLIENTS=false,IAP_ENABLED=${IAP_ENABLED},IAP_EXPECTED_AUDIENCE=${IAP_EXPECTED_AUDIENCE},IAP_HEADER_NAME=${IAP_HEADER_NAME},IAP_PROXY_URL=${IAP_PROXY_URL}" \
  --quiet

echo ""
echo "[2/3] Deploying preference-agent..."
gcloud run deploy preference-agent \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --source=. \
  --clear-base-image \
  --no-allow-unauthenticated \
  --set-env-vars="SERVICE_MODULE=src.preference_agent.server,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=true,MODEL_NAME=${MODEL_NAME},BQ_DATASET_ID=${BQ_DATASET_ID},BQ_TABLE_ID=${BQ_TABLE_ID},SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID},SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID},USE_MOCK_CLIENTS=false,IAP_ENABLED=${IAP_ENABLED},IAP_EXPECTED_AUDIENCE=${IAP_EXPECTED_AUDIENCE},IAP_HEADER_NAME=${IAP_HEADER_NAME},IAP_PROXY_URL=${IAP_PROXY_URL}" \
  --quiet

echo ""
echo "[3/3] Deploying ui-service..."
gcloud run deploy ui-service \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --source=. \
  --clear-base-image \
  --allow-unauthenticated \
  --set-env-vars="SERVICE_MODULE=src.ui_service.server,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=true,MODEL_NAME=${MODEL_NAME},BQ_DATASET_ID=${BQ_DATASET_ID},BQ_TABLE_ID=${BQ_TABLE_ID},SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID},SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID},USE_MOCK_CLIENTS=false,UI_SERVICE_URL=${UI_SERVICE_URL},INTERVIEW_AGENT_URL=${INTERVIEW_AGENT_URL},PREFERENCE_AGENT_URL=${PREFERENCE_AGENT_URL},ALLOWED_EMAIL_DOMAINS=${ALLOWED_EMAIL_DOMAINS},FIREBASE_API_KEY=${FIREBASE_API_KEY},FIREBASE_AUTH_DOMAIN=${FIREBASE_AUTH_DOMAIN},FIREBASE_PROJECT_ID=${FIREBASE_PROJECT_ID},FIREBASE_STORAGE_BUCKET=${FIREBASE_STORAGE_BUCKET},FIREBASE_MESSAGING_SENDER_ID=${FIREBASE_MESSAGING_SENDER_ID},FIREBASE_APP_ID=${FIREBASE_APP_ID},IAP_ENABLED=${IAP_ENABLED},IAP_EXPECTED_AUDIENCE=${IAP_EXPECTED_AUDIENCE},IAP_HEADER_NAME=${IAP_HEADER_NAME},IAP_PROXY_URL=${IAP_PROXY_URL}" \
  --quiet

echo ""
echo "================================================================================"
echo "  All services deployed successfully!"
echo "================================================================================"

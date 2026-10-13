#!/usr/bin/env bash
# Example wrapper script demonstrating how to override environment variables
# when deploying electric-sheep to a custom Google Cloud project.
set -euo pipefail

# 1. Set Google Cloud Project & Region
export GOOGLE_CLOUD_PROJECT="my-custom-gcp-project"
export GOOGLE_CLOUD_REGION="us-central1"

# 2. Configure AI Model & Infrastructure IDs
export MODEL_NAME="gemini-3.8-flash"
export BQ_DATASET_ID="adk_agent_telemetry"
export BQ_TABLE_ID="agent_logs"
export SPANNER_INSTANCE_ID="adk-spanner-instance"
export SPANNER_DATABASE_ID="agent_memory_db"

# 3. Configure Firebase Auth variables for web Control Center login
export ALLOWED_EMAIL_DOMAINS="your-domain.com"
export FIREBASE_API_KEY="your-firebase-api-key"
export FIREBASE_AUTH_DOMAIN="my-custom-gcp-project.firebaseapp.com"
export FIREBASE_PROJECT_ID="my-custom-gcp-project"
export FIREBASE_STORAGE_BUCKET="my-custom-gcp-project.firebasestorage.app"
export FIREBASE_MESSAGING_SENDER_ID="your-sender-id"
export FIREBASE_APP_ID="your-app-id"

echo "Deploying electric-sheep to Google Cloud project: ${GOOGLE_CLOUD_PROJECT}"
./scripts/deploy_cloud_run_source.sh

echo ""
echo "Running mandatory live endpoint validation..."
python3 -m scripts.validate_live_endpoints

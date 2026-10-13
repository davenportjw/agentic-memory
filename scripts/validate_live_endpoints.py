"""Script to validate live Google Cloud Run endpoints programmatically."""

import json
import logging
import os
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("adk.validate_live")

# Automatically load local .env file if present
dotenv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
if os.path.exists(dotenv_path):
    with open(dotenv_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

UI_SERVICE_URL = os.environ.get("UI_SERVICE_URL", "http://localhost:8080")
INTERVIEW_AGENT_URL = os.environ.get("INTERVIEW_AGENT_URL", "http://localhost:8081")
PREFERENCE_AGENT_URL = os.environ.get("PREFERENCE_AGENT_URL", "http://localhost:8082")


def get_auth_headers(url: str) -> dict:
    """Retrieve an identity token so developer validation scripts can authenticate against secure endpoints."""
    headers = {"User-Agent": "ADK-Validator"}
    try:
        import subprocess

        result = subprocess.run(
            ["gcloud", "auth", "print-identity-token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            headers["Authorization"] = f"Bearer {result.stdout.strip()}"
            return headers
    except Exception:
        pass
    try:
        from google.auth.transport.requests import Request as AuthRequest
        from google.oauth2 import id_token

        token = id_token.fetch_id_token(AuthRequest(), url)
        if token:
            headers["Authorization"] = f"Bearer {token}"
            return headers
    except Exception:
        pass
    return headers


def test_get(url: str, name: str):
    logger.info(f"Testing GET {name}: {url}")
    try:
        req = urllib.request.Request(url, headers=get_auth_headers(url))
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
            logger.info(f" -> [{status}] OK: {json.dumps(body)}")
            return True, body
    except Exception as e:
        logger.error(f" -> FAILED GET {url}: {e}")
        return False, None


def test_html_get(url: str, name: str):
    logger.info(f"Testing GET HTML {name}: {url}")
    try:
        req = urllib.request.Request(url, headers=get_auth_headers(url))
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            content = resp.read().decode("utf-8", errors="ignore")
            logger.info(f" -> [{status}] OK: HTML length={len(content)} bytes")
            return True, content
    except Exception as e:
        logger.error(f" -> FAILED GET HTML {url}: {e}")
        return False, None


def test_post(url: str, name: str, payload: dict):
    logger.info(f"Testing POST {name}: {url}")
    try:
        data = json.dumps(payload).encode("utf-8")
        headers = get_auth_headers(url)
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
            logger.info(f" -> [{status}] OK: {json.dumps(body)}")
            return True, body
    except Exception as e:
        logger.error(f" -> FAILED POST {url}: {e}")
        return False, None


def main():
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", os.environ.get("PROJECT_ID", "configured-project"))
    print("=" * 80)
    print(f"  VALIDATING LIVE GOOGLE CLOUD RUN ENDPOINTS ({project_id})  ")
    print("=" * 80)

    # 1. Health checks (Unauthenticated/public readiness checks)
    test_get(f"{UI_SERVICE_URL}/health", "UI Service Health")
    test_get(f"{INTERVIEW_AGENT_URL}/health", "InterviewAgent Health")
    test_get(f"{PREFERENCE_AGENT_URL}/health", "PreferenceAgent Health")

    # 1a. Authentication and Login page checks
    test_html_get(f"{UI_SERVICE_URL}/login.html", "UI Service Control Center Login Page")
    test_get(f"{UI_SERVICE_URL}/api/auth/config", "UI Service Auth Config")

    # 1b. Static HTML pages validation
    test_html_get(f"{UI_SERVICE_URL}/", "UI Service Control Center Page")
    test_html_get(f"{UI_SERVICE_URL}/how-it-works.html", "UI Service How It Works Architecture Page")

    # 2. Programmatic API interaction with UI Service
    user_id = "test-live-user-42"
    session_id = "session-live-validator-01"

    test_post(
        f"{UI_SERVICE_URL}/api/interview/query",
        "InterviewAgent Query",
        {
            "user_id": user_id,
            "session_id": session_id,
            "prompt": "My favorite programming language is Python and I am a Principal Cloud Architect",
        },
    )

    test_get(
        f"{UI_SERVICE_URL}/api/logs/{session_id}",
        "BigQuery Telemetry Logs",
    )

    test_post(
        f"{UI_SERVICE_URL}/api/harvest",
        "Dream Harvest (BQ AI Summarization -> Spanner Sync)",
        {"user_id": user_id, "session_id": session_id},
    )

    test_post(
        f"{UI_SERVICE_URL}/api/harvest/multi",
        "Multi-Session Cross-Session Dream Harvest",
        {"user_id": user_id},
    )

    test_get(
        f"{UI_SERVICE_URL}/api/spanner/preferences/{user_id}",
        "Google Cloud Spanner Memory",
    )

    test_get(
        f"{UI_SERVICE_URL}/api/spanner/guardrails/{user_id}",
        "Google Cloud Spanner Guardrail Memory",
    )

    test_post(
        f"{UI_SERVICE_URL}/api/telemetry/ingest_transcript",
        "BYOT Transcript Ingestion",
        {
            "user_id": user_id,
            "session_id": "session-live-byot-01",
            "format": "jsonl",
            "content": '{"type": "USER_INPUT", "content": "Never mock data in production"}\n{"type": "PLANNER_RESPONSE", "content": "Understood, using real services."}',
        },
    )

    test_post(
        f"{UI_SERVICE_URL}/api/preference/query",
        "PreferenceAgent Query (Fast Spanner Cache Hit)",
        {
            "user_id": user_id,
            "session_id": session_id,
            "prompt": "What is my favorite programming language?",
        },
    )

    test_post(
        f"{UI_SERVICE_URL}/api/preference/inject",
        "PreferenceAgent Memory Injection (/api/preference/inject)",
        {
            "user_id": user_id,
            "preference_key": "user_intent",
            "preference_value": "Deploy autonomous agents to Cloud Run",
            "category": "intent",
            "confidence": 1.0,
            "crystallized_belief": "The user's intent is to deploy autonomous agents to Cloud Run.",
            "keywords": "intent, deploy, cloud run, agent",
        },
    )

    test_post(
        f"{UI_SERVICE_URL}/api/preference/extract_memories",
        "PreferenceAgent Memory Extraction (/api/preference/extract_memories)",
        {
            "user_id": user_id,
            "intent_or_question": "user_intent",
            "limit": 5,
            "include_random": True,
        },
    )

    test_post(
        f"{UI_SERVICE_URL}/api/mcp/execute",
        "MCP Live Tool Execution (/api/mcp/execute)",
        {
            "tool_name": "get_user_crystallized_beliefs",
            "arguments": {"user_id": user_id},
        },
    )

    print("=" * 80)
    print("  LIVE GCP PROGRAMMATIC ENDPOINT VALIDATION COMPLETED!  ")
    print("=" * 80)


if __name__ == "__main__":
    main()

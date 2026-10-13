"""End-to-End integration tests for the Material Light UI Service and agent lifecycle."""

from unittest.mock import patch
from fastapi.testclient import TestClient
from src.ui_service.server import api
from src.common.config import get_config
from src.common.auth import get_default_dev_email


def test_ui_service_health_check():
    """Verify UI service readiness and default model gemini-3.8-flash."""
    client = TestClient(api)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "gemini" in data["model"]


def test_ui_service_e2e_lifecycle():
    """Test the complete multi-agent lifecycle through UI service endpoints."""
    client = TestClient(api)
    user_id = "test-ui-user-77"
    session_id = "session-ui-e2e-01"

    # 1. Chat with InterviewAgent
    interview_res = client.post(
        "/api/interview/query",
        json={
            "user_id": user_id,
            "session_id": session_id,
            "prompt": "My favorite programming language is Python and I am a Principal Cloud Architect",
        },
    )
    assert interview_res.status_code == 200
    int_data = interview_res.json()
    assert "gemini" in int_data["model"]
    assert int_data["user_id"] == user_id

    # 2. Check telemetry logs
    logs_res = client.get(f"/api/logs/{session_id}")
    assert logs_res.status_code == 200
    logs_data = logs_res.json()
    assert logs_data["count"] >= 2  # USER_PROMPT + AGENT_RESPONSE

    # 3. Run Dream Harvest
    harvest_res = client.post(
        "/api/harvest",
        json={"user_id": user_id, "session_id": session_id},
    )
    assert harvest_res.status_code == 200
    harvest_data = harvest_res.json()
    assert harvest_data["status"] == "success"
    assert harvest_data["preferences_synced"] >= 1
    assert "prompt_used" in harvest_data
    assert "queries_run" in harvest_data
    assert "bigquery_select" in harvest_data["queries_run"]
    assert "spanner_upserts" in harvest_data["queries_run"]

    # 4. Verify Google Cloud Spanner memory records
    spanner_res = client.get(f"/api/spanner/preferences/{user_id}")
    assert spanner_res.status_code == 200
    spanner_data = spanner_res.json()
    assert spanner_data["count"] >= 1
    pref_keys = [p["preference_key"] for p in spanner_data["preferences"]]
    assert "favorite_language" in pref_keys or "job_role" in pref_keys

    # 5. Query PreferenceAgent and observe fast-path Spanner cache hit
    pref_res = client.post(
        "/api/preference/query",
        json={
            "user_id": user_id,
            "session_id": session_id,
            "prompt": "What is my favorite programming language?",
        },
    )
    assert pref_res.status_code == 200
    pref_data = pref_res.json()
    assert pref_data["source"] in ("SPANNER_CACHE", "LLM_FALLBACK")
    assert "Python" in pref_data["response"] or "Spanner" in pref_data["response"]


def test_ui_service_preference_inject_and_extract_endpoints():
    """Verify UI service /api/preference/inject and /api/preference/extract_memories endpoints."""
    client = TestClient(api)
    user_id = "test-ui-memory-user-77"

    inject_res = client.post(
        "/api/preference/inject",
        json={
            "user_id": user_id,
            "preference_key": "user_intent",
            "preference_value": "Deploy autonomous agents to Cloud Run",
            "category": "intent",
            "confidence": 1.0,
            "crystallized_belief": "The user's intent is to deploy autonomous agents to Cloud Run.",
            "keywords": "intent, deploy, cloud run, agent",
        },
    )
    assert inject_res.status_code == 200
    assert inject_res.json()["status"] == "success"

    extract_res = client.post(
        "/api/preference/extract_memories",
        json={
            "user_id": user_id,
            "intent_or_question": "user_intent",
            "limit": 5,
            "include_random": True,
        },
    )
    assert extract_res.status_code == 200
    data = extract_res.json()
    assert data["status"] == "extracted_memories"
    assert any(
        m["preference_key"] == "user_intent" for m in data["relevant_memories"] + data["random_memories"]
    )


def test_ui_service_how_it_works_page():
    """Verify that the How It Works architecture page and assets are served correctly."""
    client = TestClient(api)
    response = client.get("/how-it-works.html")
    assert response.status_code == 200
    assert "Do AI Agents Dream of Binary Sheep?" in response.text
    assert "Google ADK 2.0" in response.text
    assert 'id="sandboxJsonBq"' in response.text
    assert 'id="sandboxJsonDream"' in response.text
    assert 'id="sandboxDreamQueries"' in response.text
    assert 'id="sandboxDreamPrompt"' in response.text
    assert 'id="sandboxJsonSpanner"' in response.text
    assert 'id="section-architecture-diagram"' in response.text
    assert 'id="githubPopoutBtn"' in response.text
    assert 'id="githubModal"' in response.text

    js_response = client.get("/how-it-works.js")
    assert js_response.status_code == 200
    assert "crystallized_belief" in js_response.text
    assert "PENDING_COMMIT_TIMESTAMP()" in js_response.text
    assert "dreamQueries" in js_response.text
    assert "dreamPrompt" in js_response.text
    assert "ARCH_DIAGRAM_DATA" in js_response.text
    img_response = client.get("/agent_dream_hero.jpg")
    assert img_response.status_code == 200
    qr_response = client.get("/qrcode_github.svg")
    assert qr_response.status_code == 200
    assert "<svg" in qr_response.text


def test_ui_service_topbar_and_layout_integrity():
    """Verify top app-bar 2-child layout and page-to-page structural consistency per DESIGN.md."""
    client = TestClient(api)
    for path in ("/", "/how-it-works.html"):
        res = client.get(path)
        assert res.status_code == 200
        html = res.text

        # 1. Verify app-nav is inside app-title-group (2-child standard in app-bar-content)
        assert 'class="app-title-group"' in html
        assert 'class="app-nav"' in html
        title_idx = html.find('class="app-title-group"')
        nav_idx = html.find('class="app-nav"')
        session_idx = html.find('class="session-info"')
        assert title_idx < nav_idx < session_idx, f"Layout order violated on {path}"
        assert 'id="githubPopoutBtn"' in html, f"Missing githubPopoutBtn on {path}"
        assert '<dialog id="githubModal"' in html, f"Missing native <dialog> githubModal on {path}"
        assert 'class="signout-btn"' in html, f"Missing signout-btn on {path}"
        assert 'id="sessionInfoBtn"' in html, f"Missing sessionInfoBtn on {path}"
        assert 'id="sessionInfoTooltip"' in html, f"Missing sessionInfoTooltip on {path}"
        assert "no-cache" in res.headers.get("Cache-Control", ""), f"Missing Cache-Control on {path}"

        # 2. Verify page-to-page structural consistency (.app-body-layout + .left-nav + .single-pane-container)
        assert 'class="app-body-layout"' in html, f"Missing app-body-layout on {path}"
        assert 'class="left-nav"' in html, f"Missing left-nav sidebar on {path}"
        assert 'class="single-pane-container"' in html, f"Missing single-pane-container on {path}"
        assert 'id="leftNavToggleBtn"' in html, f"Missing leftNavToggleBtn collapse button on {path}"
        assert 'class="left-nav-collapse-btn"' in html, f"Missing left-nav-collapse-btn on {path}"

    # 3. Verify collapsible sidebar styling & topbar nowrap in style.css
    css_res = client.get("/style.css")
    assert css_res.status_code == 200
    assert ".left-nav.collapsed" in css_res.text
    assert ".app-body-layout.nav-collapsed" in css_res.text
    assert "flex-wrap: nowrap;" in css_res.text
    assert ".info-tooltip" in css_res.text
    assert ".signout-btn" in css_res.text
    assert "no-cache" in css_res.headers.get("Cache-Control", "")

    # 4. Verify How It Works page target anchors exist for sidebar links
    how_html = client.get("/how-it-works.html").text
    for sec in ("section-hero", "section-flowchart", "section-philosophy", "section-sandbox", "section-code"):
        assert f'id="{sec}"' in how_html, f"Missing section id {sec} in how-it-works.html"
    assert "arch-pipeline-grid" in how_html, "Missing 4-column arch-pipeline-grid in how-it-works.html per Section 3.3"


def test_ui_service_auth_endpoints_and_login_protection():
    """Verify login screen, Firebase config endpoint, dev session bypass, and logout."""
    client = TestClient(api)
    config = get_config()
    expected_domain = config.allowed_email_domains
    expected_email = get_default_dev_email(expected_domain)

    # 1. Login page should be accessible without authentication
    login_res = client.get("/login.html")
    assert login_res.status_code == 200
    assert "Control Center Login" in login_res.text
    assert '<strong id="allowed-domain-text">' in login_res.text

    # 2. Auth config endpoint should return allowlist and Firebase settings
    cfg_res = client.get("/api/auth/config")
    assert cfg_res.status_code == 200
    cfg_data = cfg_res.json()
    assert cfg_data["allowedEmailDomains"] == expected_domain

    # 3. Create dev session (bypass auth in dev/mock mode)
    sess_res = client.post("/api/auth/session", json={"idToken": "dev-admin-token"})
    assert sess_res.status_code == 200
    assert sess_res.json()["email"] == expected_email

    # 4. Logout should delete session cookie and redirect to login.html
    logout_res = client.get("/logout", follow_redirects=False)
    assert logout_res.status_code == 302
    assert "/login.html" in logout_res.headers.get("location", "")

    # 5. Verify strict auth enforcement rejects unauthenticated requests when X-Test-Enforce-Auth is set
    unauth_api = client.get("/api/spanner/preferences/demo-user-42", headers={"X-Test-Enforce-Auth": "true"})
    assert unauth_api.status_code == 401

    unauth_html = client.get("/", headers={"X-Test-Enforce-Auth": "true"}, follow_redirects=False)
    assert unauth_html.status_code == 302
    assert "/login.html" in unauth_html.headers.get("location", "")


def test_ui_service_byot_and_cognitive_sleep_features():
    """Verify BYOT transcript ingestion, procedural guardrails endpoint, and sleep model artifacts."""
    client = TestClient(api)
    user_id = "test-byot-user-42"
    session_id = "test-byot-session-42"

    # 1. Ingest JSONL transcript
    sample_jsonl = (
        '{"role": "user", "content": "Rule: Always run uv run pytest for unit tests."}\n'
        '{"role": "assistant", "content": "Understood. Docker is not installed so use apple container."}\n'
    )
    ingest_res = client.post(
        "/api/telemetry/ingest_transcript",
        json={
            "user_id": user_id,
            "session_id": session_id,
            "raw_transcript": sample_jsonl,
            "format": "jsonl",
        },
    )
    assert ingest_res.status_code == 200
    ingest_data = ingest_res.json()
    assert ingest_data["status"] == "success"
    assert ingest_data["events_ingested"] >= 2
    assert ingest_data["session_id"] == session_id

    # 2. Check telemetry logs recorded the ingested events
    logs_res = client.get(f"/api/logs/{session_id}")
    assert logs_res.status_code == 200
    logs_data = logs_res.json()
    assert logs_data["count"] >= 2

    # 3. Test procedural guardrails query endpoint
    guardrails_res = client.get(f"/api/spanner/guardrails/{user_id}")
    assert guardrails_res.status_code == 200
    g_data = guardrails_res.json()
    assert g_data["status"] == "success"
    assert "guardrails" in g_data
    assert isinstance(g_data["guardrails"], list)

    # 4. Verify How It Works includes Section Math Visualizer
    how_html = client.get("/how-it-works.html").text
    assert 'id="section-math-visualizer"' in how_html
    assert 'id="mathTurnsSlider"' in how_html
    assert 'id="mathTokensVal"' in how_html
    assert 'id="mathTtftVal"' in how_html

    how_js = client.get("/how-it-works.js").text
    assert "initMathVisualizer" in how_js


def test_ui_service_refresh_buttons_and_spanner_matrix_integration():
    """Verify that Refresh Spanner and Refresh Logs buttons and endpoints are fully wired."""
    client = TestClient(api)
    
    # 1. Assert buttons exist in index.html DOM
    index_res = client.get("/")
    assert index_res.status_code == 200
    html = index_res.text
    assert 'id="refreshSpannerBtn"' in html
    assert 'id="refreshLogsBtn"' in html
    assert 'id="spannerTableBody"' in html
    assert 'id="spannerGuardrailsList"' in html
    assert 'id="spannerSummariesList"' in html

    # 2. Assert app.js wires refresh listeners with visual feedback and coordinated fetches
    js_res = client.get("/app.js")
    assert js_res.status_code == 200
    js = js_res.text
    assert "refreshSpannerBtn" in js
    assert "Refreshing..." in js
    assert "Refreshed ✓" in js
    assert "fetchSpannerPrefs" in js
    assert "fetchSpannerGuardrails" in js
    assert "fetchSpannerSummaries" in js
    assert "refreshLogsBtn" in js

    # 3. Assert all three Spanner endpoints invoked by Refresh Spanner return 200
    user_id = "test-refresh-user"
    prefs_res = client.get(f"/api/spanner/preferences/{user_id}")
    assert prefs_res.status_code == 200
    assert "preferences" in prefs_res.json()

    guardrails_res = client.get(f"/api/spanner/guardrails/{user_id}")
    assert guardrails_res.status_code == 200
    assert "guardrails" in guardrails_res.json()

    summaries_res = client.get(f"/api/spanner/summaries/{user_id}")
    assert summaries_res.status_code == 200
    assert "summaries" in summaries_res.json()


def test_ui_service_mcp_execute_endpoint():
    """Verify that /api/mcp/execute routes correctly to MCP tools and returns SLA latency."""
    client = TestClient(api)
    with patch("src.mcp_server.server.get_user_crystallized_beliefs", return_value="### Mock Beliefs"):
        res = client.post(
            "/api/mcp/execute",
            json={
                "tool_name": "get_user_crystallized_beliefs",
                "arguments": {"user_id": "test-user"},
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["tool_name"] == "get_user_crystallized_beliefs"
        assert data["result"] == "### Mock Beliefs"
        assert "latency_ms" in data
        assert "sla" in data






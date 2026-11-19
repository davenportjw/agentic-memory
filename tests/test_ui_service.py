"""End-to-End integration tests for the Material Light UI Service and agent lifecycle."""

import uuid
from pathlib import Path
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
    for path in ("/", "/how-it-works.html", "/studio.html", "/studio"):
        res = client.get(path)
        assert res.status_code == 200
        html = res.text

        # 1. Verify app-nav is inside app-title-group (2-child standard in app-bar-content)
        assert 'class="app-title-group"' in html
        assert 'class="app-nav"' in html
        assert 'href="index.html"' in html
        assert 'href="studio.html"' in html
        assert 'href="how-it-works.html"' in html
        title_idx = html.find('class="app-title-group"')
        nav_idx = html.find('class="app-nav"')
        session_idx = html.find('class="session-info"')
        assert title_idx < nav_idx < session_idx, f"Layout order violated on {path}"
        assert 'id="githubPopoutBtn"' in html, f"Missing githubPopoutBtn on {path}"
        assert '<dialog id="githubModal"' in html, f"Missing native <dialog> githubModal on {path}"
        assert 'https://github.com/davenportjw/agentic-memory' in html, f"Missing agentic-memory GitHub URL on {path}"
        assert 'https://github.com/davenportjw/electric-sheep' not in html, f"Stale electric-sheep link found on {path}"
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
    assert 'https://github.com/davenportjw/agentic-memory' in login_res.text
    assert 'https://github.com/davenportjw/electric-sheep' not in login_res.text

    # 2. Auth config endpoint should return allowlist and Firebase settings
    cfg_res = client.get("/api/auth/config")
    assert cfg_res.status_code == 200
    cfg_data = cfg_res.json()
    assert cfg_data["allowedEmailDomains"] == expected_domain

    # 3. Create dev session (bypass auth in dev/mock mode)
    sess_res = client.post("/api/auth/session", json={"idToken": "local-dev-login"})
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


def test_ui_service_byot_quick_sample_and_antigravity_ingest():
    """Verify Quick Sample JSON array and Antigravity transcript ingestion into BigQuery."""
    client = TestClient(api)
    user_id = "test-byot-qa-user"

    # Test 1: Quick Sample JSON array format (format="json")
    quick_sample_json = """[
        {"role": "user", "content": "Always run pytest with uv in electric-sheep."},
        {"role": "assistant", "content": "Docker is prohibited, using apple container."}
    ]"""
    res_qs = client.post(
        "/api/telemetry/ingest_transcript",
        json={
            "user_id": user_id,
            "session_id": "session-byot-quick-sample",
            "raw_transcript": quick_sample_json,
            "format": "json",
        },
    )
    assert res_qs.status_code == 200, res_qs.text
    data_qs = res_qs.json()
    assert data_qs["status"] == "success"
    assert data_qs["events_ingested"] == 2

    # Test 2: Antigravity transcript JSONL format (format="auto" or format="json")
    antigravity_jsonl = (
        '{"step_index": 0, "type": "USER_INPUT", "content": "Check database schemas."}\n'
        '{"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Checking schemas now.", "tool_calls": [{"tool_name": "run_command", "args": {"CommandLine": "gcloud spanner databases list"}}]}\n'
        '{"step_index": 2, "type": "TOOL_RESULT", "content": "databases: [electric-sheep-db]"}\n'
    )
    # Even if user kept format="json", the server should gracefully handle JSONL
    res_ag = client.post(
        "/api/telemetry/ingest_transcript",
        json={
            "user_id": user_id,
            "session_id": "session-byot-antigravity-jsonl",
            "raw_transcript": antigravity_jsonl,
            "format": "json",
        },
    )
    assert res_ag.status_code == 200, res_ag.text
    data_ag = res_ag.json()
    assert data_ag["status"] == "success"
    assert data_ag["events_ingested"] >= 3


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


def test_ui_service_lakehouse_session_filtering():
    """Verify that /api/lakehouse/sessions and /api/logs/ALL work, and index.html/app.js have session filter DOM."""
    client = TestClient(api)

    # 1. Verify GET /api/lakehouse/sessions
    res = client.get("/api/lakehouse/sessions")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert "sessions" in data
    assert isinstance(data["sessions"], list)
    if len(data["sessions"]) > 0:
        first = data["sessions"][0]
        assert "session_id" in first
        assert "source" in first
        assert "summary" in first

    # 2. Verify GET /api/logs/ALL returns lakehouse traces
    logs_res = client.get("/api/logs/ALL")
    assert logs_res.status_code == 200
    logs_data = logs_res.json()
    assert "logs" in logs_data
    assert isinstance(logs_data["logs"], list)

    # 3. Verify DOM elements in index.html
    html_res = client.get("/")
    assert html_res.status_code == 200
    html = html_res.text
    assert 'id="lakehouseSessionSelect"' in html
    assert 'id="lakehouseSessionBadge"' in html
    assert 'id="lakehouseBadgeSource"' in html
    assert 'id="lakehouseBadgeId"' in html
    assert 'id="lakehouseBadgeSummary"' in html
    assert 'id="lakehouseSessionCount"' in html
    assert 'id="lakehouseScopeAllBtn"' in html
    assert 'id="lakehouseScopeActiveBtn"' in html
    assert 'id="lakehouseActiveScopeBadge"' in html
    assert 'id="dreamScopeBadge"' in html
    assert 'id="spannerUserScopeBadge"' in html
    assert 'id="spannerTotalCountBadge"' in html
    assert 'id="mcpUserScopeBadge"' in html

    # 4. Verify app.js references and active session filtering logic
    js_res = client.get("/app.js")
    assert js_res.status_code == 200
    js = js_res.text
    assert "lakehouseSessionSelect" in js
    assert "fetchLakehouseSessions" in js
    assert "updateLakehouseBadge" in js
    assert "updateAllScopeIndicators" in js
    assert "syncMcpToolArgs" in js
    assert "lakehouseScopeAllBtn" in js
    assert "lakehouseScopeActiveBtn" in js
    assert "ensureSessionInDropdown" in js
    assert "setLakehouseScope" in js
    assert "Scope: Active Session (" in js


def test_ui_service_studio_page_and_dom_integrity():
    """Verify Enterprise Memory Studio HTML/JS/CSS assets, 3-Column IDE Workbench, Run Settings rail, and zero-amber policy."""
    client = TestClient(api)
    studio_res = client.get("/studio")
    assert studio_res.status_code == 200
    html = studio_res.text

    for stage_id in ("stage-1-sandbox", "stage-2-scopes", "stage-3-eval", "stage-4-evolve"):
        assert f'id="{stage_id}"' in html, f"Missing Studio stage {stage_id}"

    for elem_id in (
        "zeroResetBtn",
        "seedMultiScopeBtn",
        "runFederatedLookupBtn",
        "runConsensusBtn",
        "generateAdversarialBtn",
        "runEvalSuiteBtn",
        "runAlphaEvolveBtn",
        "promoteChampionBtn",
        "studioInspectorDrawer",
        "mapElitesGridContainer",
        "inspectorRunSettingsPane",
        "inspectorJsonPane",
        "applyRunSettingsBtn",
        "settingDimButtonGroup",
        "playgroundContextPre",
        "toggleManualFeedbackBuilderBtn",
        "studioIntentTopNav",
        "leftScopePillsContainer",
        "studioFileDataTree",
        "leftMemoryTreeList",
        "leftMemoryQuickEditor",
        "tabBtnStudioGuide",
        "inspectorGuidePane",
        "openStudioGuideBtn",
    ):
        assert f'id="{elem_id}"' in html, f"Missing Studio control {elem_id}"

    for cls_name in (
        "studio-workbench-body",
        "studio-workbench-layout",
        "studio-left-rail",
        "studio-workspace-canvas",
        "studio-command-bar",
        "studio-intent-top-nav",
        "studio-intent-tab",
        "left-scope-pill",
        "studio-file-tree",
        "studio-system-context-bar",
        "studio-docked-prompt-bar",
        "studio-split-pane-2col",
    ):
        assert cls_name in html, f"Missing Studio workbench class {cls_name} in studio.html"

    # Verify JS mode switching and Run Settings wiring
    js_res = client.get("/studio.js")
    assert js_res.status_code == 200
    assert "switchStudioMode" in js_res.text
    assert "switchInspectorTab" in js_res.text
    assert "handlePromoteConfig" in js_res.text
    assert "applyRunSettingsBtn" in js_res.text

    # Verify CSS definitions exist for all Studio Workbench classes + .hidden visibility invariant
    css_res = client.get("/style.css")
    assert css_res.status_code == 200
    for css_selector in (
        ".hidden",
        ".studio-workbench-layout",
        ".studio-left-rail",
        ".left-nav-list",
        ".left-nav-item",
        ".studio-workspace-canvas",
        ".studio-stage-panel",
        ".studio-system-context-bar",
        ".studio-docked-prompt-bar",
        ".studio-prompt-input",
        ".studio-candidate-card",
        ".studio-memory-search-bar",
        ".badge",
        ".chip",
    ):
        assert css_selector in css_res.text, f"Missing CSS selector {css_selector} in style.css"
    assert "display: none !important" in css_res.text

    # Verify Non-Linear Practitioner Workbench controls and zero raw LaTeX strings
    for practitioner_id in (
        "trajectoryExtractorCard",
        "extractTrajectoryCandidatesBtn",
        "memorySearchInput",
        "memoryCategoryFilter",
        "memoryDesignerCard",
        "memoryDesignerForm",
        "memoryPackDrawer",
        "exportMemoryPackBtn",
        "importMemoryPackBtn",
        "openTrajectoryIngestCmdBtn",
        "openMemoryDesignerCmdBtn",
        "openMemoryPackCmdBtn",
    ):
        assert f'id="{practitioner_id}"' in html, f"Missing Practitioner Workbench control {practitioner_id}"

    for raw_latex in ("$\\alpha$", "$F_{\\alpha}$", "$\\alpha \\cdot"):
        assert raw_latex not in html, f"Unrendered raw LaTeX string {raw_latex} found in studio.html"

    # Verify zero yellow/amber hex colors in studio.html, studio.js, and style.css
    for forbidden_color in ("#f59e0b", "#fef3c7", "#d97706"):
        assert forbidden_color not in html.lower()
        assert forbidden_color not in js_res.text.lower()
        assert forbidden_color not in css_res.text.lower()


def test_ui_service_studio_reset_scopes_promote_and_consensus():
    """Verify /api/studio/reset, /api/studio/seed-demo, /api/studio/scopes, /api/studio/scopes/promote, and /api/studio/scopes/consensus."""
    client = TestClient(api)
    uid = "user:test_studio_77"
    tid = "team:test_studio_77"
    aid = "agent:test_studio_77"
    oid = "org:default"

    # 1. Cold-Start Zero Reset
    reset_res = client.post(
        "/api/studio/reset",
        json={
            "scope_id": uid,
            "scope_type": "USER",
            "clear_eval_cases": False,
            "reset_all_scopes": True,
            "team_id": tid,
            "agent_id": aid,
        },
    )
    assert reset_res.status_code == 200
    reset_data = reset_res.json()
    assert reset_data["status"] == "ZERO_RESET_COMPLETE"
    assert reset_data["remaining_memories"] == 0

    # 2. Seed Multi-Scope Demo Baseline
    seed_res = client.post(
        "/api/studio/seed-demo",
        json={"user_id": uid, "team_id": tid, "agent_id": aid, "org_id": oid},
    )
    assert seed_res.status_code == 200
    seed_data = seed_res.json()
    assert seed_data["counts"]["ORG"] >= 1
    assert seed_data["counts"]["TEAM"] >= 1
    assert seed_data["counts"]["AGENT"] >= 1
    assert seed_data["counts"]["USER"] >= 1
    assert len(seed_data["federated"]["conflicts_resolved"]) >= 1

    # 3. Query /api/studio/scopes
    scopes_res = client.get(
        "/api/studio/scopes",
        params={
            "user_id": uid,
            "team_id": tid,
            "agent_id": aid,
            "org_id": oid,
            "query_text": "What Python package manager and security auth policy should we use?",
        },
    )
    assert scopes_res.status_code == 200
    scopes_data = scopes_res.json()
    assert "scopes" in scopes_data
    assert "federated" in scopes_data
    assert "active_config" in scopes_data

    # 4. Promote USER -> TEAM
    promote_res = client.post(
        "/api/studio/scopes/promote",
        json={
            "source_scope_type": "USER",
            "source_scope_id": uid,
            "memory_key": "python_package_manager",
            "target_scope_type": "TEAM",
            "target_scope_id": tid,
        },
    )
    assert promote_res.status_code == 200
    promote_data = promote_res.json()
    assert promote_data["status"] == "PROMOTED"
    assert promote_data["promoted_memory"]["scope_type"] == "TEAM"
    assert promote_data["promoted_memory"]["memory_key"] == "python_package_manager"

    # 5. Team Consensus Auto-Promotion
    consensus_res = client.post(
        "/api/studio/scopes/consensus",
        json={
            "team_id": tid,
            "user_ids": ["user:alice", "user:bob"],
            "min_consensus_users": 2,
            "target_scope_type": "TEAM",
            "target_scope_id": tid,
        },
    )
    assert consensus_res.status_code == 200
    consensus_data = consensus_res.json()
    assert consensus_data["status"] == "CONSENSUS_EVALUATED"
    assert consensus_data["promoted_count"] >= 1

    # Teardown sandbox scopes
    client.post(
        "/api/studio/reset",
        json={
            "scope_id": uid,
            "scope_type": "USER",
            "reset_all_scopes": True,
            "team_id": tid,
            "agent_id": aid,
        },
    )


def test_ui_service_studio_trajectory_extract_and_memory_crud_pack():
    """Verify /api/studio/trajectories/extract, /api/studio/memories/upsert, /api/studio/memories/delete, /api/studio/memories/export, and /api/studio/memories/import."""
    client = TestClient(api)
    uid = "user:test_practitioner_88"
    tid = "team:test_practitioner_88"
    aid = "agent:test_practitioner_88"
    oid = "org:default"

    # 1. Extract Candidate Memories from Raw Transcript
    raw_transcript = (
        "USER: Never use pip install in this repo. Always use uv run pytest for tests.\n"
        "AGENT: Understood, switching to uv.\n"
        "USER: Also, Docker is not installed on my Apple Silicon Mac; use apple container or Cloud Run.\n"
        "USER: Remember to always run terraform validate and terraform fmt before committing."
    )
    extract_res = client.post(
        "/api/studio/trajectories/extract",
        json={
            "user_id": uid,
            "raw_transcript": raw_transcript,
        },
    )
    assert extract_res.status_code == 200
    extract_data = extract_res.json()
    assert extract_data["status"] == "CANDIDATES_EXTRACTED"
    assert extract_data["candidate_count"] >= 3
    keys_extracted = {c["memory_key"] for c in extract_data["candidates"]}
    assert "python_package_manager" in keys_extracted
    assert "container_runtime" in keys_extracted
    assert "iac_validation_workflow" in keys_extracted

    # 2. Upsert a Custom Scoped Memory via Inline Memory Designer endpoint
    upsert_res = client.post(
        "/api/studio/memories/upsert",
        json={
            "scope_type": "USER",
            "scope_id": uid,
            "memory_key": "custom_linter_rule",
            "memory_value": "ruff check --fix",
            "memory_type": "PREFERENCE",
            "category": "tooling",
            "crystallized_belief": "Developer runs ruff check --fix prior to pytest.",
            "keywords": ["ruff", "linter", "python"],
            "confidence": 0.97,
        },
    )
    assert upsert_res.status_code == 200
    upsert_data = upsert_res.json()
    assert upsert_data["status"] == "MEMORY_UPSERTED"
    assert upsert_data["record"]["memory_key"] == "custom_linter_rule"
    assert len(upsert_data["record"]["embedding"]) == 768

    # 3. Export Portable JSON Memory Pack
    export_res = client.get(
        "/api/studio/memories/export",
        params={
            "user_id": uid,
            "team_id": tid,
            "agent_id": aid,
            "org_id": oid,
            "scope_filter": "USER",
        },
    )
    assert export_res.status_code == 200
    pack_data = export_res.json()
    assert pack_data["pack_version"] == "1.0"
    assert pack_data["total_memories"] >= 1
    assert any(m["memory_key"] == "custom_linter_rule" for m in pack_data["memories"])

    # 4. Delete the Scoped Memory
    del_res = client.post(
        "/api/studio/memories/delete",
        json={
            "scope_type": "USER",
            "scope_id": uid,
            "memory_key": "custom_linter_rule",
        },
    )
    assert del_res.status_code == 200
    del_data = del_res.json()
    assert del_data["status"] == "MEMORY_DELETED"
    assert del_data["deleted"] is True

    # 5. Re-Import via Portable JSON Memory Pack Import
    import_res = client.post(
        "/api/studio/memories/import",
        json={"memories": pack_data["memories"]},
    )
    assert import_res.status_code == 200
    import_data = import_res.json()
    assert import_data["status"] == "MEMORY_PACK_IMPORTED"
    assert import_data["imported_count"] >= 1

    # Teardown test scope
    client.post(
        "/api/studio/reset",
        json={
            "scope_id": uid,
            "scope_type": "USER",
            "reset_all_scopes": False,
        },
    )


def test_ui_service_studio_feedback_eval_and_alpha_evolve():
    """Verify /api/studio/feedback, /api/studio/eval/cases, /api/studio/eval/generate, /api/studio/eval/run, and /api/studio/evolve/*."""
    client = TestClient(api)

    # 1. Record negative feedback (regression guard) and positive feedback
    fb_bad = client.post(
        "/api/studio/feedback",
        json={
            "session_id": "test-studio-fb-sess",
            "user_id": "user:eval_test",
            "prompt": "What Python tool should we run tests with?",
            "agent_response": "Use global pip install.",
            "rating": "bad",
            "expected_fact": "test_runner = uv run pytest",
            "failure_tag": "contradiction",
        },
    )
    assert fb_bad.status_code == 200
    fb_bad_data = fb_bad.json()
    assert fb_bad_data["status"] == "FEEDBACK_RECORDED"
    assert fb_bad_data["rating"] == "bad"
    assert fb_bad_data["eval_case"]["scenario_type"] == "LIVE_FEEDBACK_CORRECTION"

    fb_good = client.post(
        "/api/studio/feedback",
        json={
            "session_id": "test-studio-fb-sess",
            "user_id": "user:eval_test",
            "prompt": "What database backs our crystalline memory?",
            "agent_response": "Google Cloud Spanner.",
            "rating": "good",
            "expected_fact": "Google Cloud Spanner",
        },
    )
    assert fb_good.status_code == 200
    assert fb_good.json()["rating"] == "good"

    # 2. Generate 4 adversarial archetypes
    gen_res = client.post(
        "/api/studio/eval/generate",
        json={"topic": "enterprise agentic memory", "count": 4},
    )
    assert gen_res.status_code == 200
    gen_data = gen_res.json()
    assert gen_data["status"] == "GENERATED"
    assert gen_data["generated_count"] == 4

    # 3. List evaluation cases
    cases_res = client.get("/api/studio/eval/cases")
    assert cases_res.status_code == 200
    cases_data = cases_res.json()
    assert cases_data["count"] >= 4

    # 4. Run 7-Metric + Matryoshka Evaluation Suite
    eval_res = client.post(
        "/api/studio/eval/run",
        json={"alpha_quality_weight": 0.65},
    )
    assert eval_res.status_code == 200
    scorecard = eval_res.json()
    assert 0.0 <= scorecard["quality_score"] <= 1.0
    assert 0.0 <= scorecard["token_efficiency_score"] <= 1.0
    assert "metrics_breakdown" in scorecard
    assert "matryoshka_comparison" in scorecard
    for dim_key in ("128d", "256d", "512d", "768d"):
        assert dim_key in scorecard["matryoshka_comparison"]

    # 5. Run AlphaEvolve Evolutionary Search
    evolve_res = client.post(
        "/api/studio/evolve/run",
        json={"generations": 1, "alpha_quality_weight": 0.65, "candidates_per_generation": 1},
    )
    assert evolve_res.status_code == 200
    evolve_data = evolve_res.json()
    assert evolve_data["status"] == "EVOLUTION_COMPLETE"
    assert "champion" in evolve_data
    assert "archive" in evolve_data
    assert evolve_data["archive"]["populated_cells"] >= 1

    # 6. Fetch MAP-Elites Archive
    arch_res = client.get("/api/studio/evolve/archive")
    assert arch_res.status_code == 200
    arch_data = arch_res.json()
    assert "archive" in arch_data
    assert "active_config" in arch_data

    # 7. Promote Champion to Active Runtime Config
    champ_cfg = evolve_data["champion"]["config"]
    prom_res = client.post(
        "/api/studio/evolve/promote",
        json={"config": champ_cfg, "config_id": champ_cfg["config_id"]},
    )
    assert prom_res.status_code == 200
    prom_data = prom_res.json()
    assert prom_data["status"] == "ACTIVE_CONFIG_PROMOTED"
    assert prom_data["active_config"]["config_id"] == champ_cfg["config_id"]


def test_ui_service_studio_4_intent_practitioner_workflow():
    """Verify the 4-Intent Practitioner Workflow DOM & APIs:
    - Contextual Top Command Bar groups (#cmdGroupStage1..4)
    - Intent 1: /api/studio/ingest/config & /api/studio/ingest/raw
    - Intent 2: /api/studio/categories & staged memory creation with trigger_conditions
    - Intent 3: /api/studio/scenarios/retest (empirical trigger_count & token_telemetry)
    - Intent 4: /api/studio/sync/status & /api/studio/sync/push to Cloud Spanner
    """
    client = TestClient(api)
    uid = f"user:intent_test_{uuid.uuid4().hex[:6]}"
    tid = f"team:intent_team_{uuid.uuid4().hex[:6]}"
    aid = "agent:electric_sheep"
    oid = "org:acme_enterprise"

    try:
        # 0. Verify Contextual Top Command Bar DOM in /studio
        html = client.get("/studio").text
        for group_id, stage_id in [
            ("cmdGroupStage1", "stage-1-sandbox"),
            ("cmdGroupStage2", "stage-2-scopes"),
            ("cmdGroupStage3", "stage-3-eval"),
            ("cmdGroupStage4", "stage-4-evolve"),
        ]:
            assert group_id in html
            assert f'data-for-stage="{stage_id}"' in html
        assert "ingestConfigCard" in html
        assert "rawTelemetryTable" in html
        assert "categoryTaxonomyCard" in html
        assert "scenarioRetestCard" in html
        assert "memoryTriggerMatrixCard" in html
        assert "spannerSyncCenterCard" in html

        # 1. Intent 1: Configure Ingest & Fetch Raw Telemetry Stream
        cfg_get = client.get("/api/studio/ingest/config")
        assert cfg_get.status_code == 200
        assert "ingest_config" in cfg_get.json()

        cfg_post = client.post(
            "/api/studio/ingest/config",
            json={
                "source_stream": "CLOUD_SPANNER_AGENT_EVENTS",
                "lookback_hours": 48,
                "min_turn_tokens": 3,
                "pii_scrubbing": True,
                "auto_extract_signals": True,
            },
        )
        assert cfg_post.status_code == 200
        assert cfg_post.json()["ingest_config"]["lookback_hours"] == 48

        raw_res = client.get("/api/studio/ingest/raw?session_id=all&limit=15")
        assert raw_res.status_code == 200
        raw_data = raw_res.json()
        assert raw_data["status"] == "OK"
        assert len(raw_data["raw_events"]) >= 1
        first_ev = raw_data["raw_events"][0]
        assert "event_id" in first_ev
        assert "token_count" in first_ev
        assert "detected_signal" in first_ev

        # 2. Intent 2: Author Custom Category & Stage a Memory with Trigger Conditions
        cat_get = client.get("/api/studio/categories")
        assert cat_get.status_code == 200
        assert cat_get.json()["count"] >= 4

        cat_post = client.post(
            "/api/studio/categories",
            json={
                "category_id": "cost_governance",
                "label": "Cost Governance",
                "default_scope": "TEAM",
                "default_match_mode": "HYBRID_COSINE_AND_KEYWORD",
                "min_cosine_similarity": 0.76,
                "description": "Token and cloud spend guardrails",
            },
        )
        assert cat_post.status_code == 200
        assert any(c["category_id"] == "cost_governance" for c in cat_post.json()["categories"])

        upsert_res = client.post(
            "/api/studio/memories/upsert",
            json={
                "scope_type": "USER",
                "scope_id": uid,
                "memory_key": "test_runner",
                "memory_value": "uv run pytest",
                "memory_type": "PREFERENCE",
                "category": "cost_governance",
                "crystallized_belief": "Always execute Python tests using uv run pytest.",
                "keywords": ["uv", "pytest", "python", "test"],
                "confidence": 0.98,
                "sync_immediately": False,
                "trigger_conditions": {
                    "match_mode": "HYBRID_COSINE_AND_KEYWORD",
                    "min_cosine_similarity": 0.70,
                    "required_keywords": ["pytest", "uv"],
                    "auto_inject_scopes": ["USER"],
                },
            },
        )
        assert upsert_res.status_code == 200
        up_rec = upsert_res.json()["record"]
        assert up_rec["sync_status"] == "STAGED"
        assert up_rec["token_footprint"] >= 6
        assert up_rec["trigger_conditions"]["match_mode"] == "HYBRID_COSINE_AND_KEYWORD"

        # 3. Intent 3: Re-Test Scenario & Verify Trigger Hit Increment + Token Telemetry
        retest_res = client.post(
            "/api/studio/scenarios/retest",
            json={
                "prompt": "How should we run our Python pytest suite locally?",
                "expected_fact": "uv run pytest",
                "user_id": uid,
                "team_id": tid,
                "agent_id": aid,
                "org_id": oid,
            },
        )
        assert retest_res.status_code == 200
        rt_data = retest_res.json()
        assert rt_data["status"] == "SCENARIO_RETEST_COMPLETE"
        assert rt_data["triggered_count"] >= 1
        assert rt_data["expected_fact_matched"] is True
        assert rt_data["token_telemetry"]["injected_memory_tokens"] > 0
        triggered_keys = [m["memory_key"] for m in rt_data["triggered_memories"]]
        assert "test_runner" in triggered_keys

        # 4. Intent 4: Inspect Sync Status & Push Staged Memories to Cloud Spanner
        sync_status_res = client.get(
            f"/api/studio/sync/status?user_id={uid}&team_id={tid}&agent_id={aid}&org_id={oid}"
        )
        assert sync_status_res.status_code == 200
        st_data = sync_status_res.json()
        assert st_data["staged_count"] >= 1

        sync_push_res = client.post(
            "/api/studio/sync/push",
            json={
                "user_id": uid,
                "team_id": tid,
                "agent_id": aid,
                "org_id": oid,
            },
        )
        assert sync_push_res.status_code == 200
        push_data = sync_push_res.json()
        assert push_data["status"] == "SPANNER_SYNC_COMMITTED"
        assert push_data["synced_count"] >= 1
        assert "transaction_tag" in push_data

        # Verify staged count is now 0 after Spanner commit
        sync_after = client.get(
            f"/api/studio/sync/status?user_id={uid}&team_id={tid}&agent_id={aid}&org_id={oid}"
        ).json()
        assert sync_after["staged_count"] == 0
    finally:
        # Automated teardown of test scopes
        client.post(
            "/api/studio/reset",
            json={"scope_id": uid, "scope_type": "USER", "reset_all_scopes": False},
        )
        client.post(
            "/api/studio/reset",
            json={"scope_id": tid, "scope_type": "TEAM", "reset_all_scopes": False},
        )


def test_studio_ten_workspaces_and_master_detail_contracts():
    """Verify the 10-workspace Enterprise Memory Studio DOM, CSS palette guardrails, and all 10 backend API contracts."""
    from pathlib import Path

    client = TestClient(api)

    # 1. DOM & CSS Contract
    res = client.get("/studio")

    assert res.status_code == 200
    html = res.text

    for required_token in (
        "app-bar",
        "app-bar-content",
        "app-title-group",
        "app-nav",
        "app-body-layout",
        "studioInspectorDrawer",
        "inspectorDetailTitle",
        "inspectorDetailBody",
    ):
        assert required_token in html, f"Missing required layout/drawer token '{required_token}' in studio.html"

    expected_workspaces = (
        "sessions",
        "tree",
        "playground",
        "eval",
        "entities",
        "categories",
        "ingest",
        "extractors",
        "backend",
        "security",
    )
    for ws in expected_workspaces:
        assert f'data-workspace="workspace-{ws}"' in html, f"Missing workspace switcher data-workspace='workspace-{ws}'"
        assert f'id="workspace-{ws}"' in html, f"Missing workspace section id='workspace-{ws}'"

    static_dir = Path(__file__).resolve().parent.parent / "src" / "ui_service" / "static"
    forbidden_hexes = ("#f59e0b", "#fef3c7", "#d97706")
    for asset_name in ("studio.html", "style.css", "studio.js"):
        content_lower = (static_dir / asset_name).read_text(encoding="utf-8").lower()
        for bad_hex in forbidden_hexes:
            assert bad_hex not in content_lower, f"Forbidden amber/yellow hex {bad_hex} found in {asset_name}"

    for forbidden_jargon in (
        "6 planes",
        "4 core",
        "content-visibility: auto",
        "1. Session Logs",
        "2. Memory Tree",
        "3. Playground",
        "4. Eval &amp; Evolve",
        "5. Entities &amp; Scopes",
        "6. Categories",
        "7. Data Ingest",
        "8. Extractors",
        "9. Memory Backend",
        "10. Security &amp; RBAC",
    ):
        assert forbidden_jargon not in html, f"Forbidden jargon or numbered prefix '{forbidden_jargon}' found in studio.html"

    # 2. Workspace 1: Ingested Sessions, AI Summaries & Usefulness Ratings
    sess_res = client.get("/api/studio/sessions")
    assert sess_res.status_code == 200
    sess_data = sess_res.json()
    assert len(sess_data["sessions"]) >= 1
    first_sess = sess_data["sessions"][0]
    assert "turns" in first_sess
    assert "extracted_memories" in first_sess
    assert "summary" in first_sess
    sid = first_sess["session_id"]

    sum_res = client.post(f"/api/studio/sessions/{sid}/summarize")
    assert sum_res.status_code == 200
    assert sum_res.json().get("summary_text")

    rate_res = client.post(
        f"/api/studio/sessions/{sid}/usefulness",
        json={"rating": "helpful", "notes": "Verified in pytest"},
    )
    assert rate_res.status_code == 200
    assert rate_res.json()["rating"] == "helpful"

    # 3. Workspace 2: Hierarchical Memory Tree & Staged Commits
    tree_res = client.get("/api/studio/tree")
    assert tree_res.status_code == 200
    tree_data = tree_res.json()
    assert "files" in tree_data
    assert "tree" in tree_data
    assert "staged_commits" in tree_data

    stage_res = client.post(
        "/api/studio/tree/stage",
        json={
            "scope_type": "USER",
            "scope_id": "user:developer",
            "category": "technical",
            "memory_key": "pytest_temp_tree_node",
            "after_value": "Temporary tree node for pytest stage/commit/discard verification.",
            "confidence": 0.96,
            "diff_summary": "Automated contract test",
        },
    )
    assert stage_res.status_code == 200
    staged_obj = stage_res.json()["staged"]
    cid = staged_obj["staged_id"]

    commit_res = client.post("/api/studio/tree/commit", json={"staged_id": cid})
    assert commit_res.status_code == 200
    assert commit_res.json()["status"] == "COMMITTED"

    # Stage a second commit to verify discard
    stage_res_2 = client.post(
        "/api/studio/tree/stage",
        json={
            "scope_type": "USER",
            "scope_id": "user:developer",
            "category": "technical",
            "memory_key": "pytest_discard_node",
            "after_value": "Should be discarded.",
        },
    )
    assert stage_res_2.status_code == 200
    cid_2 = stage_res_2.json()["staged"]["staged_id"]
    discard_res = client.post("/api/studio/tree/discard", json={"staged_id": cid_2})
    assert discard_res.status_code == 200
    assert discard_res.json()["status"] == "DISCARDED"

    # Delete the temporary node
    del_node_res = client.delete(
        "/api/studio/tree/node?scope_type=USER&scope_id=user:developer&memory_key=pytest_temp_tree_node"
    )
    assert del_node_res.status_code == 200

    # 4. Workspace 3 & 4: Playground Scopes, 7-Metric Eval & AlphaEvolve Contracts
    scopes_res = client.get("/api/studio/scopes?user_id=user:developer&team_id=team:platform-eng")
    assert scopes_res.status_code == 200
    fed = scopes_res.json()["federated"]
    assert "formatted_prompt_context" in fed
    assert "resolved_memories" in fed

    eval_res = client.post("/api/studio/eval/run", json={"alpha_quality_weight": 0.65})
    assert eval_res.status_code == 200
    eval_json = eval_res.json()
    assert "scorecard" in eval_json
    assert "slice_comparison" in eval_json
    assert len(eval_json["slice_comparison"]["slices"]) >= 4

    evolve_res = client.get("/api/studio/evolve/archive")
    assert evolve_res.status_code == 200
    evolve_json = evolve_res.json()
    assert "archive_summary" in evolve_json
    assert isinstance(evolve_json["archive_summary"]["cells"], list)

    # 5. Workspace 5: Entities Registry CRUD
    ent_list = client.get("/api/studio/entities")
    assert ent_list.status_code == 200
    assert len(ent_list.json()["entities"]) >= 4

    ent_upsert = client.post(
        "/api/studio/entities",
        json={
            "scope_type": "AGENT",
            "scope_id": "agent:pytest-temp-verifier",
            "display_name": "Pytest Temp Verifier Agent",
            "parent_scope_id": "team:platform-eng",
            "owner": "QA Automation",
            "description": "Temporary test entity",
        },
    )
    assert ent_upsert.status_code == 200
    assert ent_upsert.json()["entity"]["scope_id"] == "agent:pytest-temp-verifier"

    ent_del = client.delete("/api/studio/entities/AGENT/agent:pytest-temp-verifier")
    assert ent_del.status_code == 200

    # 6. Workspace 6: Categories & Progressive Disclosure CRUD
    cat_list = client.get("/api/studio/categories")
    assert cat_list.status_code == 200
    cats = cat_list.json()["categories"]
    assert len(cats) >= 5
    for required_cat_key in ("folder_path", "discovery_mode", "recall_trigger", "retention_ttl", "conflict_policy"):
        assert required_cat_key in cats[0], f"Missing {required_cat_key} in category object"

    cat_upsert = client.post(
        "/api/studio/categories",
        json={
            "category_id": "pytest_custom_cat",
            "label": "Pytest Custom Category",
            "folder_path": "/memories/pytest_custom/",
            "discovery_mode": "PROGRESSIVE_KEYWORD_AND_COSINE",
            "recall_trigger": "When running contract verification",
            "retention_ttl": "90_DAYS_SLIDING",
            "conflict_policy": "USER_OVERRIDES_TEAM",
            "default_threshold": 0.74,
        },
    )
    assert cat_upsert.status_code == 200
    assert cat_upsert.json()["category"]["category_id"] == "pytest_custom_cat"

    cat_del = client.delete("/api/studio/categories/pytest_custom_cat")
    assert cat_del.status_code == 200

    # 7. Workspace 7: Ingestion Services & Raw Stream
    svc_list = client.get("/api/studio/ingest/services")
    assert svc_list.status_code == 200
    assert len(svc_list.json()["services"]) >= 1

    svc_upsert = client.post(
        "/api/studio/ingest/services",
        json={
            "service_id": "svc-pytest-connector",
            "name": "Pytest Telemetry Connector",
            "connector_type": "REST_WEBHOOK",
            "endpoint_uri": "https://example.internal/ingest",
            "chunk_window_turns": 8,
            "min_extraction_confidence": 0.88,
            "pii_redaction_mode": "STRICT_HASH",
        },
    )
    assert svc_upsert.status_code == 200
    assert svc_upsert.json()["service"]["service_id"] == "svc-pytest-connector"

    raw_stream = client.get("/api/studio/ingest/raw-stream")
    assert raw_stream.status_code == 200
    assert len(raw_stream.json()["events"]) >= 1

    # 8. Workspace 8: Memory Extractors & AI Co-Designer Chat
    ext_list = client.get("/api/studio/extractors")
    assert ext_list.status_code == 200
    extractors = ext_list.json()["extractors"]
    assert len(extractors) >= 1
    first_ext_id = extractors[0]["extractor_id"]
    assert isinstance(extractors[0]["few_shot_examples"][0], dict)
    assert "input" in extractors[0]["few_shot_examples"][0]

    ext_upsert = client.post(
        "/api/studio/extractors",
        json={
            "extractor_id": first_ext_id,
            "name": extractors[0]["name"],
            "model_name": "gemini-3.8-flash",
            "target_category": "workflow",
            "human_params": extractors[0].get("human_params", {}),
            "system_prompt": extractors[0].get("system_prompt", ""),
        },
    )
    assert ext_upsert.status_code == 200

    chat_res = client.post(
        f"/api/studio/extractors/{first_ext_id}/chat",
        json={"message": "Raise strictness to 0.93 and focus on explicit developer corrections."},
    )
    assert chat_res.status_code == 200
    cd_reply = chat_res.json()["co_designer_reply"]
    assert "assistant_reply" in cd_reply
    assert "updated_human_params" in cd_reply
    assert "updated_system_prompt" in cd_reply

    # 9. Workspace 9: Backend Architecture, Test Agent Write & Point-in-Time Rollback
    backend_res = client.get("/api/studio/backend/status")
    assert backend_res.status_code == 200
    b_data = backend_res.json()
    assert "online_store" in b_data
    assert "offline_store" in b_data
    assert "agent_mutation_log" in b_data

    write_res = client.post(
        "/api/studio/backend/test-agent-write",
        json={
            "source_agent": "agent:pytest-writer",
            "scope_type": "USER",
            "scope_id": "user:developer",
            "category": "workflow",
            "memory_key": "pytest_rollback_key",
            "after_value": "Agent written value before rollback",
            "rationale": "Testing point-in-time rollback",
        },
    )
    assert write_res.status_code == 200
    mut_id = write_res.json()["mutation"]["mutation_id"]

    rb_res = client.post(f"/api/studio/backend/mutations/{mut_id}/rollback")
    assert rb_res.status_code == 200
    assert rb_res.json()["status"] == "ROLLED_BACK"

    # 10. Workspace 10: Security, Governance & RBAC Access Simulator
    pol_list = client.get("/api/studio/security/policies")
    assert pol_list.status_code == 200
    assert len(pol_list.json()["policies"]) >= 1

    pol_upsert = client.post(
        "/api/studio/security/policies",
        json={
            "policy_id": "pol-pytest-auditor",
            "role_name": "Pytest Security Auditor",
            "principal": "group:qa-auditors@example.internal",
            "allowed_scopes": ["ORG", "TEAM"],
            "allowed_actions": ["READ", "SIMULATE"],
            "pii_redaction_level": "STRICT_HASH",
            "effect": "ALLOW",
        },
    )
    assert pol_upsert.status_code == 200

    sim_res = client.post(
        "/api/studio/security/simulate",
        json={
            "principal": "user:developer",
            "action": "WRITE",
            "scope": "ORG",
            "target_scope_id": "org:default",
        },
    )
    assert sim_res.status_code == 200
    sim_data = sim_res.json()
    assert sim_data["decision"] in ("ALLOW", "DENY")
    assert "allowed" in sim_data
    assert "rationale" in sim_data
    assert "scope_path" in sim_data


def test_studio_toolbar_dropdowns_and_scroll_integrity() -> None:
    """Verify Studio toolbar dropdowns, vertical scrolling CSS rules, and wired toolbar button listeners."""
    import re
    from pathlib import Path

    client = TestClient(api)
    studio_res = client.get("/studio")
    assert studio_res.status_code == 200
    html = studio_res.text

    # 1. Verify dropdown menus & current view indicator in studio.html
    for expected_id in (
        "workflowStageDropdown",
        "stageActionsDropdown",
        "engineConfigDropdown",
        "toolbarViewModeBadge",
        "toolbarCurrentTitle",
        "activeStageDropdownLabel",
    ):
        assert f'id="{expected_id}"' in html, f"Missing expected toolbar element #{expected_id} in studio.html"

    # 2. Verify vertical scroll rules and .studio-scroll-pane in style.css
    static_dir = Path(__file__).resolve().parent.parent / "src" / "ui_service" / "static"
    css_text = (static_dir / "style.css").read_text(encoding="utf-8")

    pane_match = re.search(
        r"body\.studio-workbench-body\s+\.single-pane-container,\s*\.studio-main-pane,\s*\.studio-workspace-canvas\s*\{([^}]+)\}",
        css_text,
    )
    assert pane_match is not None, "Missing .single-pane-container rule block in style.css"
    pane_body = pane_match.group(1)
    assert "overflow-y: auto" in pane_body
    assert ".studio-scroll-pane" in css_text

    # 3. Verify all toolbar action button IDs are wired in studio.js
    js_text = (static_dir / "studio.js").read_text(encoding="utf-8")

    for btn_id in (
        "refreshRawStreamCmdBtn",
        "saveIngestConfigCmdBtn",
        "openCategoryCreatorCmdBtn",
        "intentJumpToScenarioCmdBtn",
        "runScenarioRetestCmdBtn",
        "intentSynthesizeAdversarialCmdBtn",
        "pushToSpannerCmdBtn",
    ):
        assert btn_id in js_text, f"Toolbar button ID {btn_id} must be wired in studio.js"


def test_full_viewport_scaling_and_responsive_breakpoints() -> None:
    """Verify full-viewport QHD/4K/Ultrawide scaling and narrow-viewport responsive rules."""
    static_dir = Path(__file__).resolve().parent.parent / "src" / "ui_service" / "static"
    css_text = (static_dir / "style.css").read_text(encoding="utf-8")
    js_text = (static_dir / "studio.js").read_text(encoding="utf-8")

    # 1. Verify artificial 1500px / 1800px max-width caps are removed from .app-bar-content and .app-body-layout
    assert ".app-bar-content {\n  max-width: 100%;" in css_text or "max-width: 100%;" in css_text
    assert ".app-body-layout {\n  max-width: 100%;" in css_text or "width: 100%;" in css_text
    assert "grid-template-columns: clamp(236px, 14vw, 272px) minmax(0, 1fr) clamp(320px, 20vw, 420px)" in css_text

    # 2. Verify vertical height scaling and #wsSessionsTable column proportions
    assert "max-height: clamp(320px, calc(100dvh - 340px), 860px)" in css_text
    assert "#wsSessionsTable th:nth-child(2)" in css_text
    assert "min-width: 280px" in css_text

    # 3. Verify container queries and responsive media queries
    assert "@container studio-canvas (max-width: 920px)" in css_text
    assert ".studio-split-pane-2col" in css_text
    assert "@container studio-canvas (max-width: 860px)" in css_text
    assert "@container studio-canvas (max-width: 520px)" in css_text
    assert "@media (max-width: 1200px)" in css_text
    assert ".studio-inspector-drawer:not(.collapsed)" in css_text
    assert "@media (max-width: 860px)" in css_text
    assert "@media (max-width: 820px)" in css_text

    # 4. Verify responsive matchMedia auto-collapse in studio.js
    assert 'window.matchMedia("(max-width: 1200px)")' in js_text
    assert 'window.matchMedia("(max-width: 900px)")' in js_text


def test_plain_english_ux_login_index_and_how_it_works() -> None:
    """Verify Workspace-style restraint copy, zero raw LaTeX on how-it-works.html, and removal of cloud chrome badges."""
    static_dir = Path(__file__).resolve().parent.parent / "src" / "ui_service" / "static"
    client = TestClient(api)

    # 1. login.html restrained copy (1-2 word button labels, zero explanatory subtitle blurb)
    login_html = (static_dir / "login.html").read_text(encoding="utf-8")
    assert "Control Center Login" in login_html
    assert "Sign in" in login_html
    assert "Local dev" in login_html
    assert "Sign in with an authorized account" not in login_html
    assert "Google ADK 2.0 Multi-Agent Demo UI" not in login_html
    assert "Bypass Auth (Local Developer)" not in login_html

    # 2. how-it-works.html & how-it-works.js: zero raw unrendered LaTeX, zero inert inputs, clean copy
    how_res = client.get("/how-it-works.html")
    assert how_res.status_code == 200
    how_html = how_res.text
    how_js = (static_dir / "how-it-works.js").read_text(encoding="utf-8")

    assert r"\(" not in how_html, "how-it-works.html must not contain raw unrendered \\( LaTeX"
    assert r"\)" not in how_html, "how-it-works.html must not contain raw unrendered \\) LaTeX"
    assert r"\tau" not in how_html
    assert r"\ge" not in how_html
    assert "Gemini platform Gemini 3.8 Flash" not in how_html
    assert "Gemini platform global" not in how_html
    assert "Gemini platform global" not in how_js
    assert 'id="userIdInput"' not in how_html, "Read-only how-it-works.html must not render inert userIdInput"
    assert 'id="sessionIdInput"' not in how_html, "Read-only how-it-works.html must not render inert sessionIdInput"
    assert 'class="session-info"' in how_html
    assert 'id="sessionInfoBtn"' in how_html
    assert 'id="sessionInfoTooltip"' in how_html

    # 3. index.html & app.js: 1-2 word nav & button labels, zero subtitle blurbs, and recall badges
    index_res = client.get("/")
    assert index_res.status_code == 200
    index_html = index_res.text
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")

    assert 'class="model-badge"' not in index_html
    assert "Gemini platform global" not in index_html
    assert "v2.0 Production" not in index_html
    assert "Capture agent conversations, extract reusable preferences and rules" not in index_html
    for expected_label in (
        "Send",
        "New chat",
        "Local Sessions",
        "Live Chat",
        "Upload Transcript",
    ):
        assert expected_label in index_html, f"Expected concise label '{expected_label}' in index.html"

    for forbidden_jargon in (
        "Spanner &amp; BQ Connected",
        "Ingest to BigQuery",
        "Ingest Telemetry into BigQuery",
        "Refresh BigQuery Traces",
        "Refresh Spanner",
        "Waking Life: Episodic Trajectories",
        "Antigravity Sessions",
        "Interactive Dialogue",
        "BYOT Telemetry Dropzone",
        "(NREM)",
        "(REM)",
        "(GATE)",
    ):
        assert forbidden_jargon not in index_html, f"Forbidden jargon '{forbidden_jargon}' still present in index.html"

    assert "Direct Memory Hit" in app_js
    assert "Model Fallback" in app_js


def test_studio_plain_english_intent_and_interactive_handler_fixes() -> None:
    """Verify fixed Studio JS handlers, restrained 1-2 word button labels, humanized dropdown options, and zero fake file-tree cosplay."""
    static_dir = Path(__file__).resolve().parent.parent / "src" / "ui_service" / "static"
    studio_html = (static_dir / "studio.html").read_text(encoding="utf-8")
    studio_js = (static_dir / "studio.js").read_text(encoding="utf-8")

    # 1. Interactive JS bug fixes
    assert 'getAttribute("data-signal-filter")' in studio_js
    assert 'getElementById("evalAlphaSlider")' in studio_js
    assert 'getElementById("ingestSourceSelect")' in studio_js
    assert "ingestSourceStreamSelect" not in studio_js
    assert 'expectedFact: "python_package_manager = uv"' not in studio_js

    # 2. Log Sources & Portable Backups (replacing fake /streams/telemetry/*.sock cosplay)
    assert 'id="studioFileDataTree"' in studio_html
    for expected_tree_label in (
        "Session Transcripts",
        "Live Chat Events",
        "Export Memories (.json)",
        "Import Memories (.json)",
    ):
        assert expected_tree_label in studio_html, f"Expected '{expected_tree_label}' in #studioFileDataTree"

    for fake_path in (
        "bigquery_traces.tbl",
        "live_stream.sock",
        "/streams/telemetry",
        "/packs/portable",
    ):
        assert fake_path not in studio_html, f"Fake file-tree path '{fake_path}' must be removed from studio.html"
        assert fake_path not in studio_js, f"Fake file-tree path '{fake_path}' must be removed from studio.js"

    # 3. Consolidated Seed & Reset controls (1-2 word labels)
    assert 'id="intentSeedMultiScope"' not in studio_html, "Duplicate second seed button must be removed"
    assert 'id="seedMultiScopeBtn"' in studio_html
    assert "Load sample" in studio_html
    assert 'id="zeroResetBtn"' in studio_html

    # 4. Mode clarity & removal of stale instructions / raw REST routes
    for stale_ref in (
        "+ New",
        "Intent 3",
        "Intent 4",
        "MEMORY EXPLORER",
        "POST /api/studio/",
        "Record &amp; Settings JSON Inspector",
    ):
        assert stale_ref not in studio_html, f"Stale reference or raw route '{stale_ref}' must be removed from studio.html"
    assert "unanchored" not in studio_html.lower()

    # 5. Restrained 1-2 word button labels & plain-English option labels across studio.html & studio.js
    for plain_label in (
        "Search",
        "Save rule",
        "Add note",
        "Run eval",
        "Load sample",
        "Recommended Settings",
        "Active Retrieval Settings",
        "Embedding Size",
        "Compact Bullets",
        "Workflow / How-To",
        "Strict Rule (Guardrail)",
        "Meaning Match Only",
    ):
        assert plain_label in studio_html, f"Missing plain-English label '{plain_label}' in studio.html"

    # Backend option values must remain 100% intact
    for preserved_val in (
        'value="COMPACT_BULLET"',
        'value="PROCEDURAL_SKILL"',
        'value="INVARIANT_GUARDRAIL"',
        'value="SEMANTIC_COSINE"',
    ):
        assert preserved_val in studio_html, f"Backend contract attribute {preserved_val} must stay unchanged"

    for js_plain_label in (
        "Compact (<80 tokens)",
        "Balanced (80–160 tokens)",
        "Detailed (>160 tokens)",
        "Wrong Answer (Save as Test Case)",
        "Helpful (Save as Test Case)",
    ):
        assert js_plain_label in studio_js, f"Missing plain-English label '{js_plain_label}' in studio.js"

    for forbidden_studio_jargon in (
        "10-Workspace Enterprise Workbench",
        "Eval &amp; Evolve",
        "Adversarial Evaluation &amp; Optimization",
        "Pareto Fitness",
        "Champion Genome",
        "Active Runtime Genome",
        "Matryoshka Slice Benchmark",
        "Matryoshka Vector Slice",
        "MAP-Elites Archive",
        "Tree-Conditioned",
        "Flag Regression (-1 EvalCase)",
        "Reinforce (+1 EvalCase)",
    ):
        assert forbidden_studio_jargon not in studio_html, (
            f"Forbidden jargon '{forbidden_studio_jargon}' must be removed from studio.html"
        )
        assert forbidden_studio_jargon not in studio_js, (
            f"Forbidden jargon '{forbidden_studio_jargon}' must be removed from studio.js"
        )


def test_workspace_style_restraint_and_view_popover_contract() -> None:
    """Verify Google Workspace-style restraint across Python Web Control Center:
    - DESIGN.md 2-child topbar (.app-title-group containing .app-nav + .session-info/.topbar-controls)
    - 1-2 word nav links: Chat, Memory, Guide
    - Native HTML Popover View menu (popovertarget='viewMenuPopover', popover='auto', polyfill feature-detect, [popover]:is(:popover-open, .\\:popover-open), zero role='menu'/aria-haspopup mismatch)
    - Zero KPI stat card grids, zero explanatory subtitle blurbs, zero raw JSON <pre> dumps on everyday views, <=1 primary button per visible screen
    """
    import re

    static_dir = Path(__file__).resolve().parent.parent / "src" / "ui_service" / "static"
    client = TestClient(api)
    css_text = (static_dir / "style.css").read_text(encoding="utf-8")

    # CSS rule for @oddbird/popover-polyfill compatibility per modern-web-guidance
    assert "[popover]:is(:popover-open, .\\:popover-open)" in css_text

    for page in ("/", "/studio.html", "/how-it-works.html"):
        res = client.get(page)
        assert res.status_code == 200
        html = res.text

        # 1-2 word top-bar nav labels
        for nav_word in (">Chat<", ">Memory<", ">Guide<"):
            assert nav_word in html, f"Expected 1-2 word nav link {nav_word} on {page}"

        # Native HTML Popover View button & popover container + conditional polyfill feature-detect
        assert 'popovertarget="viewMenuPopover"' in html, f"Missing View popover button on {page}"
        assert ">View</button>" in html, f"Missing 'View' button label on {page}"
        assert 'id="viewMenuPopover"' in html and 'popover="auto"' in html, (
            f"Missing #viewMenuPopover popover='auto' on {page}"
        )
        assert 'if (!("popover" in HTMLElement.prototype))' in html, (
            f"Missing conditional @oddbird/popover-polyfill feature detect on {page}"
        )
        assert "@oddbird/popover-polyfill" in html, f"Missing @oddbird/popover-polyfill import on {page}"

        # Zero role="menu" or aria-haspopup mismatch on native popover or invoker
        view_btn_match = re.search(r"<button[^>]*popovertarget=\"viewMenuPopover\"[^>]*>", html)
        assert view_btn_match is not None
        assert "aria-haspopup" not in view_btn_match.group(0), f"Forbidden aria-haspopup on View popover button ({page})"
        popover_tag_match = re.search(r"<div[^>]*id=\"viewMenuPopover\"[^>]*>", html)
        assert popover_tag_match is not None
        assert 'role="menu"' not in popover_tag_match.group(0), f"Forbidden role='menu' on #viewMenuPopover ({page})"

        # Zero KPI stat card grids & zero explanatory subtitle blurbs
        for banned_class in (
            "studio-kpi-row",
            "studio-kpi-card",
            "nrem-stat-grid",
            "nrem-stat-box",
            "math-metrics-grid",
            "math-metric-box",
            'class="card-subtitle"',
            'class="ws-subtitle"',
        ):
            assert banned_class not in html, f"Banned KPI grid or subtitle blurb '{banned_class}' on {page}"

    # Everyday views (login.html, index.html, studio.html): zero raw JSON <pre> dumps & <=1 primary button per visible view
    login_html = (static_dir / "login.html").read_text(encoding="utf-8")
    assert 'class="login-subtitle"' not in login_html
    assert len(re.findall(r'class="[^"]*\bbtn-primary\b[^"]*"', login_html)) <= 1

    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    assert "<pre id=\"mcpOutputContent\"" not in index_html, "Everyday index.html must not use raw JSON <pre> dump for MCP output"
    assert "btn-harvest" not in index_html, "Competing primary .btn-harvest must be removed on index.html"
    assert len(re.findall(r'class="[^"]*\bbtn-primary\b[^"]*"', index_html)) <= 1, (
        "index.html must expose at most 1 primary button"
    )

    studio_html = (static_dir / "studio.html").read_text(encoding="utf-8")
    assert "<pre id=\"inspectorContent\"" not in studio_html, (
        "Everyday studio.html inspector must render structured rows, not a raw JSON <pre> schema dump"
    )


def test_brand_v21_tokens_and_v1_memory_api() -> None:
    """Verify Brand v2.1 CSS tokens, inline sheep.svg mark, favicon.svg, and live /api/v1/memory/* endpoints."""
    client = TestClient(api)

    # 1. /favicon.svg serves badge SVG
    fav_res = client.get("/favicon.svg")
    assert fav_res.status_code == 200
    assert "<svg" in fav_res.text

    # 2. Brand v2.1 tokens + dark theme in style.css, and inline SVG mark in HTML pages
    css_text = client.get("/style.css").text.lower()
    for token_pair in (
        "--bg-canvas: #f6f5f0",
        "--surface-card: #ffffff",
        "--surface-muted: #eceae2",
        "--border-subtle: #dad6ca",
        "--text-primary: #14171c",
        "--text-secondary: #38414e",
        "--text-muted: #556070",
        "--color-primary: #6a359c",
        "--color-primary-hover: #53277d",
        "--color-success: #0b6a48",
        "--color-warn: #b83710",
        'html[data-theme="dark"]',
        "#0b0e13",
        "#121720",
        "#be94ff",
    ):
        assert token_pair in css_text, f"Missing Brand v2.1 token {token_pair} in style.css"

    for page in ("/", "/studio.html", "/how-it-works.html"):
        html = client.get(page).text
        assert 'href="favicon.svg"' in html, f"Missing favicon.svg link on {page}"
        assert "🐑" not in html, f"Legacy sheep emoji still in brand header on {page}"

    # 3. Live /api/v1/memory/* adapter endpoints backed by spanner_client + telemetry_sink
    mounts_res = client.get("/api/v1/memory/mounts")
    assert mounts_res.status_code == 200
    prefixes = [m["prefix"] for m in mounts_res.json()["mounts"]]
    assert "/mem/personal" in prefixes
    assert "/mem/project" in prefixes
    assert "/mem/org" in prefixes

    obs_res = client.post(
        "/api/v1/memory/observe",
        json={
            "repo_root": "org/pytest-repo",
            "session_id": "sess-v1-a",
            "scope": "/mem/project",
            "content": "WebhookDispatcher must honor Retry-After headers before exponential backoff",
            "symbols": ["WebhookDispatcher", "RetryPolicy"],
        },
    )
    assert obs_res.status_code == 200
    assert obs_res.json()["ok"] is True

    # Second distinct session observation so Dream consolidation promotes cleanly
    obs_res_2 = client.post(
        "/api/v1/memory/observe",
        json={
            "repo_root": "org/pytest-repo",
            "session_id": "sess-v1-b",
            "scope": "/mem/project",
            "content": "WebhookDispatcher must honor Retry-After headers before exponential backoff",
            "symbols": ["WebhookDispatcher", "RetryPolicy"],
        },
    )
    assert obs_res_2.status_code == 200

    note_res = client.get("/api/v1/memory/note", params={"repo_root": "org/pytest-repo", "symbol": "WebhookDispatcher"})
    assert note_res.status_code == 200
    assert "WebhookDispatcher" in note_res.json()["note"]
    assert note_res.json()["bytes"] <= 200

    inj_res = client.post(
        "/api/v1/memory/inject",
        json={
            "repo_root": "org/pytest-repo",
            "session_id": "sess-inject-01",
            "symbols": ["WebhookDispatcher"],
        },
    )
    assert inj_res.status_code == 200
    assert inj_res.json()["bytes"] > 0
    assert inj_res.json()["bytes"] <= 200

    fault_res = client.post(
        "/api/v1/memory/fault",
        json={
            "repo_root": "org/pytest-repo",
            "session_id": "sess-v1-a",
            "prompt_summary": "stabilize webhook retry jitter",
            "searched_queries": ["pkg/webhook", "jitter"],
            "resolved_symbols": ["WebhookDispatcher"],
            "extra_read_steps": 3,
        },
    )
    assert fault_res.status_code == 200
    assert fault_res.json()["bound"] is True

    dream_res = client.post(
        "/api/v1/memory/dream",
        json={
            "scope_type": "project",
            "scope_id": "org/pytest-repo",
            "mode": "nrem",
            "min_sessions": 2,
        },
    )
    assert dream_res.status_code == 200
    assert dream_res.json()["promoted"] >= 1
    assert dream_res.json()["belief_epoch"] >= 1

    recall_res = client.post(
        "/api/v1/memory/recall",
        json={
            "repo_root": "org/pytest-repo",
            "intent": "WebhookDispatcher retry backoff",
            "mount": "scoped",
        },
    )
    assert recall_res.status_code == 200
    assert recall_res.json()["bytes"] <= 2048
    assert "Retry-After" in recall_res.json()["frame"]

    clean_res = client.get("/api/v1/memory/recall", params={"mount": "clean", "intent": "WebhookDispatcher"})
    assert clean_res.status_code == 200
    assert clean_res.json()["bytes"] == 0

    events_res = client.get("/api/v1/memory/events", params={"scope_type": "project", "scope_id": "org/pytest-repo"})
    assert events_res.status_code == 200
    assert events_res.json()["count"] >= 1

    forbid_ev = client.get("/api/v1/memory/events", params={"scope_type": "personal"})
    assert forbid_ev.status_code == 403

    delta_res = client.get("/api/v1/memory/delta", params={"scope_type": "project", "scope_id": "org/pytest-repo"})
    assert delta_res.status_code == 200
    assert delta_res.json()["count"] >= 1

    diff_res = client.post(
        "/api/v1/memory/reconcile-diff",
        json={
            "repo_root": "org/pytest-repo",
            "diff": "diff --git a/w.go b/w.go\n--- a/w.go\n+++ b/w.go\n@@ -1,1 +1,1 @@\n-func WebhookDispatcher() {}\n+func ModernWebhookDispatcher() {}\n",
        },
    )
    assert diff_res.status_code == 200
    assert diff_res.json()["old_to_new"].get("WebhookDispatcher") == "ModernWebhookDispatcher"

    evict_res = client.post(
        "/api/v1/memory/evict",
        json={
            "repo_root": "org/pytest-repo",
            "old_to_new": {"RetryPolicy": "AdaptiveRetryPolicy"},
        },
    )
    assert evict_res.status_code == 200
    assert evict_res.json()["updated"] == 1

    metrics_res = client.get("/api/v1/memory/metrics", params={"repo_root": "org/pytest-repo"})
    assert metrics_res.status_code == 200
    m_json = metrics_res.json()
    assert m_json["beliefs_promoted"] >= 1
    assert m_json["delivered_notes_count"] >= 1
    assert m_json["evicted_renames_count"] >= 2


def test_ui_service_visual_restraint_and_zero_seeded_defaults() -> None:
    """Verify Workspace-style visual restraint, styled View popover button, single-row topbar at 800px, and zero seeded default literals."""
    client = TestClient(api)
    index_html = client.get("/").text
    studio_html = client.get("/studio").text
    guide_html = client.get("/how-it-works.html").text
    app_js = client.get("/app.js").text
    studio_js = client.get("/studio.js").text
    css_text = client.get("/style.css").text

    for page_name, html in (("/", index_html), ("/studio", studio_html), ("/how-it-works.html", guide_html)):
        assert 'popovertarget="viewMenuPopover" class="btn btn-secondary btn-sm"' in html, (
            f"{page_name} View button must use .btn .btn-secondary .btn-sm so browser bevel border never renders"
        )

    assert ".app-bar-content {\n    flex-wrap: wrap;" not in css_text, (
        "style.css must never wrap .app-bar-content onto two rows at 800px"
    )

    combined_ui = "\n".join([index_html, studio_html, app_js, studio_js])
    for forbidden in (
        "ACTIVE SCOPE PILLS",
        "Agent Memory Workbench",
        'value="user:default"',
        'value="team:default"',
        'value="agent:default"',
        'value="demo-user-42"',
        "Preset: cfg-gen1",
        "Summarize & Extract",
        "Summarize &amp; Extract",
        "Save Usefulness Attribution to Spanner",
        "Global Lakehouse Traces",
    ):
        assert forbidden not in combined_ui, f"Forbidden jargon or seeded literal {forbidden!r} leaked into UI chrome"


def test_adversarial_round2_remediation_contracts() -> None:
    """Verify Round 2 Context-Zero remediation across static HTML and JS:
    - Zero alert() stubs
    - Zero <small class='menu-item-desc'> explanatory blurbs
    - Zero Cloud Spanner / BigQuery / GCS Lakehouse product names in user-visible messages/labels
    - Zero pre-seeded identity value='...' literals or 'demo-user-42' anywhere in static UI/docs
    - Zero <pre class='sandbox-json-view'> raw JSON dumps
    - 1-2 word workspace & walkthrough menu labels
    """
    static_dir = Path(__file__).resolve().parent.parent / "src" / "ui_service" / "static"
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    studio_html = (static_dir / "studio.html").read_text(encoding="utf-8")
    guide_html = (static_dir / "how-it-works.html").read_text(encoding="utf-8")
    login_html = (static_dir / "login.html").read_text(encoding="utf-8")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    studio_js = (static_dir / "studio.js").read_text(encoding="utf-8")
    guide_js = (static_dir / "how-it-works.js").read_text(encoding="utf-8")

    assert "alert(" not in app_js, "app.js must not use browser alert() stubs"
    assert "alert(" not in studio_js, "studio.js must not use browser alert() stubs"
    assert "menu-item-desc" not in studio_html, "studio.html must not contain <small class='menu-item-desc'> blurbs"
    assert "menu-item-desc" not in index_html, "index.html must not contain <small class='menu-item-desc'> blurbs"
    assert '<pre class="sandbox-json-view"' not in guide_html, "how-it-works.html must not use <pre class='sandbox-json-view'> dumps"
    assert "demo-user-42" not in guide_html
    assert "demo-user-42" not in guide_js

    everyday_chrome = "\n".join([index_html, studio_html, login_html, app_js, studio_js])
    for leaked_cloud_or_seed in (
        "Cloud Spanner",
        "in Spanner",
        "to Spanner",
        "from Spanner",
        "Spanner preferences",
        "Spanner push failed",
        "Spanner Transaction Committed",
        "Spanner Database Sync Status",
        "Inspect Spanner Record",
        "into BigQuery",
        "to BigQuery",
        "from BigQuery",
        "BigQuery Lakehouse",
        "GCS Lakehouse",
        'value="user:developer"',
        'value="team:platform-eng"',
        'value="agent:code-reviewer"',
        'value="org:acme"',
        'value="session-01"',
        'value="byot-session-001"',
        "Benchmark &amp; Auto-Tune",
        "Entities &amp; Scopes",
        "Security &amp; RBAC",
        "Step 1: Load &amp; Inspect Sessions",
        "🚀 Save Staged Memories",
        "🚀 Push & Sync Staged Memories to Spanner",
    ):
        assert leaked_cloud_or_seed not in everyday_chrome, (
            f"Leaked cloud product name, seeded default, or >2-word label {leaked_cloud_or_seed!r}"
        )






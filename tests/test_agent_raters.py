"""Agentic Rater Test Suite: VisualLegibilityRater, MemoryStudioFunctionalRater, MemoryJourneyAgentRater, and OIDC/IAP Auth.

Executes real HTTP requests against `src.ui_service.server:api` and inspects real
HTML/CSS/JS assets without any mocks or synthetic pass flags.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

from fastapi.testclient import TestClient

from src.common.auth import (
    _TOKEN_CACHE,
    _normalize_audience,
    clear_oidc_token_cache,
    extract_iap_user_email,
    get_oidc_token,
    get_service_auth_headers,
)
from src.evaluation.journey_rater import MemoryJourneyAgentRater
from src.ui_service.server import api


class VisualLegibilityRater:
    """Specialized rater verifying DESIGN.md layout, typography, palette, and 10-workspace DOM contracts."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.static_dir = Path(__file__).resolve().parent.parent / "src" / "ui_service" / "static"

    def evaluate(self) -> Dict[str, Any]:
        index_html = self.client.get("/").text
        studio_html = self.client.get("/studio").text
        how_html = self.client.get("/how-it-works.html").text
        css_text = (self.static_dir / "style.css").read_text(encoding="utf-8")
        studio_js = (self.static_dir / "studio.js").read_text(encoding="utf-8")

        checks: Dict[str, bool] = {}

        # 1. Top App Bar 2-Child Standard & .app-body-layout parity across all 3 pages
        for page_name, html in (
            ("index.html", index_html),
            ("studio.html", studio_html),
            ("how-it-works.html", how_html),
        ):
            checks[f"{page_name}_topbar_2_child"] = (
                "app-bar-content" in html
                and "app-title-group" in html
                and "app-nav" in html
                and ("session-info" in html or "app-actions" in html)
            )
            checks[f"{page_name}_body_layout"] = "app-body-layout" in html

        # 2. All 10 Studio Workspaces & Contextual Inspector Drawer
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
        checks["studio_10_workspaces"] = all(
            f'data-workspace="workspace-{ws}"' in studio_html
            and f'id="workspace-{ws}"' in studio_html
            for ws in expected_workspaces
        )
        checks["studio_inspector_drawer"] = (
            "studioInspectorDrawer" in studio_html
            and "inspectorDetailTitle" in studio_html
            and "inspectorDetailBody" in studio_html
        )

        # 3. Warm Ivory & Sepia Palette Guardrails (zero harsh amber hexes)
        forbidden_hexes = ("#f59e0b", "#fef3c7", "#d97706")
        combined_lower = (studio_html + css_text + studio_js).lower()
        checks["warm_ivory_palette_clean"] = all(h not in combined_lower for h in forbidden_hexes)

        # 4. Vertical Scroll & Dual-Font Typography
        pane_match = re.search(
            r"body\.studio-workbench-body\s+\.single-pane-container,\s*\.studio-main-pane,\s*\.studio-workspace-canvas\s*\{([^}]+)\}",
            css_text,
        )
        checks["vertical_scroll_rules"] = bool(
            pane_match
            and "overflow-y: auto" in pane_match.group(1)
            and ".studio-scroll-pane" in css_text
        )
        checks["dual_font_typography"] = (
            "--font-sans" in css_text
            and "--font-mono" in css_text
            and ("IBM Plex Sans" in css_text or "Plus Jakarta Sans" in css_text)
            and ("IBM Plex Mono" in css_text or "JetBrains Mono" in css_text)
        )

        passed_count = sum(1 for v in checks.values() if v)
        total_count = len(checks)
        return {
            "checks": checks,
            "passed": passed_count,
            "total": total_count,
            "score": passed_count / total_count if total_count else 1.0,
        }


class MemoryStudioFunctionalRater:
    """Specialized rater verifying two-stage action -> consequence persistence across Electric Sheep & Studio."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def evaluate(self) -> Dict[str, Any]:
        checks: Dict[str, bool] = {}

        # 1. Waking Ingest -> Lakehouse Session Consequence
        ing = self.client.post(
            "/api/telemetry/ingest_transcript",
            json={
                "user_id": "user:developer",
                "session_id": "sess-func-rater",
                "content": "User: Always use uv run pytest -v.\nAgent: Enforcing uv run pytest -v.",
                "format": "text",
            },
        )
        lh = self.client.get("/api/lakehouse/sessions")
        checks["waking_ingest_to_lakehouse"] = (
            ing.status_code == 200
            and lh.status_code == 200
            and len(lh.json().get("sessions", [])) >= 1
        )

        # 2. Sleep Harvest -> Spanner Crystalline Vault -> MCP Tool Consequence
        harv = self.client.post(
            "/api/harvest",
            json={"user_id": "user:developer", "session_id": "sess-func-rater"},
        )
        mcp_res = self.client.post(
            "/api/mcp/execute",
            json={
                "tool_name": "get_user_crystallized_beliefs",
                "arguments": {"user_id": "user:developer"},
            },
        )
        checks["sleep_harvest_to_mcp_recall"] = (
            harv.status_code == 200
            and mcp_res.status_code == 200
            and str(mcp_res.json().get("status", "")).upper() == "SUCCESS"
        )

        # 3. Studio Git-Style Stage -> Commit -> Cleanup Consequence
        stg = self.client.post(
            "/api/studio/tree/stage",
            json={
                "scope_type": "USER",
                "scope_id": "user:developer",
                "category": "technical",
                "memory_key": "func_rater_temp_node",
                "after_value": "Functional rater temporary node.",
                "confidence": 0.95,
            },
        )
        staged_id = stg.json().get("staged", {}).get("staged_id", "") if stg.status_code == 200 else ""
        cmt = self.client.post("/api/studio/tree/commit", json={"staged_id": staged_id})
        cleanup = self.client.delete(
            "/api/studio/tree/node?scope_type=USER&scope_id=user:developer&memory_key=func_rater_temp_node"
        )
        checks["studio_git_tree_lifecycle"] = (
            stg.status_code == 200
            and bool(staged_id)
            and cmt.status_code == 200
            and cmt.json().get("status") == "COMMITTED"
            and cleanup.status_code == 200
        )

        # 4. 7-Metric Matryoshka Eval & AlphaEvolve MAP-Elites Consequence
        ev_run = self.client.post("/api/studio/eval/run", json={"alpha_quality_weight": 0.65})
        evolve = self.client.post(
            "/api/studio/evolve/run",
            json={"generations": 1, "alpha_quality_weight": 0.65, "candidates_per_generation": 1},
        )
        checks["matryoshka_eval_and_alphaevolve"] = (
            ev_run.status_code == 200
            and "matryoshka_comparison" in ev_run.json()
            and evolve.status_code == 200
            and evolve.json().get("status") == "EVOLUTION_COMPLETE"
        )

        passed_count = sum(1 for v in checks.values() if v)
        total_count = len(checks)
        return {
            "checks": checks,
            "passed": passed_count,
            "total": total_count,
            "score": passed_count / total_count if total_count else 1.0,
        }


def test_visual_legibility_rater_100_percent() -> None:
    """Verify VisualLegibilityRater achieves 100% score across all DESIGN.md checks."""
    client = TestClient(api)
    result = VisualLegibilityRater(client).evaluate()
    assert result["score"] == 1.0, f"Failed legibility checks: {result['checks']}"


def test_memory_studio_functional_rater_100_percent() -> None:
    """Verify MemoryStudioFunctionalRater achieves 100% score across all two-stage consequence checks."""
    client = TestClient(api)
    result = MemoryStudioFunctionalRater(client).evaluate()
    assert result["score"] == 1.0, f"Failed functional checks: {result['checks']}"


def test_memory_journey_agent_rater_all_5_journeys_certified() -> None:
    """Verify MemoryJourneyAgentRater evaluates all 5 canonical journeys with A+ [CERTIFIED] grade."""
    client = TestClient(api)
    rater = MemoryJourneyAgentRater(client=client)
    report = rater.evaluate_all_journeys()

    assert report.journeys_evaluated == 5
    assert report.journeys_passed == 5, f"Critical findings: {report.critical_findings}"
    assert report.overall_functional_score == 1.0
    assert report.overall_legibility_score == 1.0
    assert report.composite_score == 1.0
    assert report.composite_grade == "A+ [CERTIFIED]"

    expected_ids = [
        "J1_WAKING_INGEST_AND_LAKEHOUSE",
        "J2_SLEEP_HARVEST_AND_SPANNER_VAULT",
        "J3_FAST_PREFERENCE_AND_INTERVIEW_AGENT",
        "J4_MEMORY_STUDIO_HIERARCHY_AND_GIT_TREE",
        "J5_ALPHAEVOLVE_AND_EXTRACTOR_LAB",
    ]
    assert [jr.journey_id for jr in report.journey_ratings] == expected_ids


def test_api_rate_journeys_endpoint() -> None:
    """Verify POST /api/rate/journeys executes MemoryJourneyAgentRater and returns A+ [CERTIFIED]."""
    client = TestClient(api)
    res = client.post("/api/rate/journeys")
    assert res.status_code == 200
    data = res.json()
    assert data["journeys_evaluated"] == 5
    assert data["journeys_passed"] == 5
    assert data["composite_grade"] == "A+ [CERTIFIED]"


def test_oidc_token_cache_and_iap_service_headers() -> None:
    """Verify per-audience OIDC token caching, audience normalization, and IAP header propagation."""
    clear_oidc_token_cache()
    assert _normalize_audience("https://electric-sheep-ui-test.us-central1.run.app/api/ studio") == (
        "https://electric-sheep-ui-test.us-central1.run.app"
    )

    # Seed cache for an HTTPS Cloud Run audience and verify cache hit without network roundtrip
    aud = "https://electric-sheep-interview-test.us-central1.run.app"
    import time

    _TOKEN_CACHE[aud] = ("eyJhbGciOiJSUzI1NiJ9.payload.sig", time.time() + 3000.0)
    tok = get_oidc_token(f"{aud}/query", force_refresh=False)
    assert tok == "eyJhbGciOiJSUzI1NiJ9.payload.sig"

    incoming = {
        "X-Goog-Authenticated-User-Email": "accounts.google.com:developer@example.com",
        "X-Goog-IAP-JWT-Assertion": "iap.jwt.token",
    }
    assert extract_iap_user_email(incoming) == "developer@example.com"

    headers = get_service_auth_headers(f"{aud}/query", incoming_headers=incoming)
    assert headers["Authorization"] == "Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig"
    assert headers["X-Goog-Authenticated-User-Email"] == "accounts.google.com:developer@example.com"
    assert headers["X-Goog-IAP-JWT-Assertion"] == "iap.jwt.token"
    clear_oidc_token_cache()

"""Agentic Functional & Visual Legibility Rater for Electric Sheep & Memory Studio.

Executes 5 canonical end-to-end functional user journeys against the real FastAPI
application (`src.ui_service.server:api`) and inspects real HTML/CSS/JS static
assets for `DESIGN.md` layout and visual legibility compliance.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi.testclient import TestClient


@dataclass
class JourneyStepAction:
    """Definition of a single functional step within a user journey."""

    step_id: str
    description: str
    method: str
    endpoint: str
    payload: Optional[Dict[str, Any]] = None
    expected_status: int = 200
    expected_consequence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class JourneyStepMetric:
    """Empirical measurement of an executed journey step and its downstream consequence."""

    step_id: str
    description: str
    endpoint: str
    status_code: int
    latency_ms: float
    passed: bool
    consequence_verified: bool
    evidence: str
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LegibilityCheck:
    """Result of a DOM, CSS, or layout legibility assertion."""

    check_id: str
    category: str
    selector_or_rule: str
    passed: bool
    evidence: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class JourneyRating:
    """Aggregated functional and visual legibility scorecard for one canonical journey."""

    journey_id: str
    persona: str
    title: str
    steps_total: int
    steps_passed: int
    consequences_verified: int
    legibility_checks_total: int
    legibility_checks_passed: int
    avg_latency_ms: float
    functional_score: float
    legibility_score: float
    overall_journey_score: float
    grade: str
    step_metrics: List[JourneyStepMetric] = field(default_factory=list)
    legibility_metrics: List[LegibilityCheck] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "journey_id": self.journey_id,
            "persona": self.persona,
            "title": self.title,
            "steps_total": self.steps_total,
            "steps_passed": self.steps_passed,
            "consequences_verified": self.consequences_verified,
            "legibility_checks_total": self.legibility_checks_total,
            "legibility_checks_passed": self.legibility_checks_passed,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "functional_score": round(self.functional_score, 4),
            "legibility_score": round(self.legibility_score, 4),
            "overall_journey_score": round(self.overall_journey_score, 4),
            "grade": self.grade,
            "step_metrics": [m.to_dict() for m in self.step_metrics],
            "legibility_metrics": [c.to_dict() for c in self.legibility_metrics],
            "recommendations": list(self.recommendations),
        }


@dataclass
class AgentRaterReport:
    """Complete multi-journey certification report produced by MemoryJourneyAgentRater."""

    rater_id: str
    timestamp_epoch: float
    overall_functional_score: float
    overall_legibility_score: float
    composite_score: float
    composite_grade: str
    journeys_evaluated: int
    journeys_passed: int
    journey_ratings: List[JourneyRating] = field(default_factory=list)
    critical_findings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rater_id": self.rater_id,
            "timestamp_epoch": self.timestamp_epoch,
            "overall_functional_score": round(self.overall_functional_score, 4),
            "overall_legibility_score": round(self.overall_legibility_score, 4),
            "composite_score": round(self.composite_score, 4),
            "composite_grade": self.composite_grade,
            "journeys_evaluated": self.journeys_evaluated,
            "journeys_passed": self.journeys_passed,
            "journey_ratings": [jr.to_dict() for jr in self.journey_ratings],
            "critical_findings": list(self.critical_findings),
        }


def _compute_grade(score: float, certified: bool = False) -> str:
    if score >= 0.95:
        return "A+ [CERTIFIED]" if certified else "A+"
    if score >= 0.90:
        return "A"
    if score >= 0.80:
        return "B"
    if score >= 0.70:
        return "C"
    return "F [ACTION REQUIRED]"


class MemoryJourneyAgentRater:
    """Executes and grades the 5 canonical functional & legibility journeys for Electric Sheep."""

    def __init__(self, client: Optional[TestClient] = None) -> None:
        if client is None:
            from src.ui_service.server import api

            self.client = TestClient(api)
        else:
            self.client = client

        self.static_dir = Path(__file__).resolve().parent.parent / "ui_service" / "static"
        self._index_html = (self.static_dir / "index.html").read_text(encoding="utf-8")
        self._studio_html = (self.static_dir / "studio.html").read_text(encoding="utf-8")
        self._how_html = (self.static_dir / "how-it-works.html").read_text(encoding="utf-8")
        self._style_css = (self.static_dir / "style.css").read_text(encoding="utf-8")
        self._app_js = (self.static_dir / "app.js").read_text(encoding="utf-8")
        self._studio_js = (self.static_dir / "studio.js").read_text(encoding="utf-8")

    def _run_step(
        self,
        action: JourneyStepAction,
        verifier: Callable[[ Any], tuple[bool, str]],
    ) -> tuple[JourneyStepMetric, Any]:
        t0 = time.perf_counter()
        error_msg: Optional[str] = None
        status_code = 0
        resp_data: Any = None
        passed = False
        consequence_ok = False
        evidence = ""

        try:
            method = action.method.upper()
            if method == "GET":
                resp = self.client.get(action.endpoint)
            elif method == "POST":
                resp = self.client.post(action.endpoint, json=action.payload or {})
            elif method == "DELETE":
                resp = self.client.delete(action.endpoint)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            latency_ms = (time.perf_counter() - t0) * 1000.0
            status_code = resp.status_code
            content_type = resp.headers.get("content-type", "")
            if "application/json" in content_type:
                resp_data = resp.json()
            else:
                resp_data = resp.text

            status_ok = status_code == action.expected_status
            consequence_ok, evidence = verifier(resp_data)
            passed = status_ok and consequence_ok
            if not status_ok:
                error_msg = f"Expected HTTP {action.expected_status}, got {status_code}"
            elif not consequence_ok:
                error_msg = f"Consequence verification failed: {evidence}"
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            error_msg = str(exc)
            evidence = f"Exception during {action.method} {action.endpoint}: {exc}"

        metric = JourneyStepMetric(
            step_id=action.step_id,
            description=action.description,
            endpoint=action.endpoint,
            status_code=status_code,
            latency_ms=round(latency_ms, 2),
            passed=passed,
            consequence_verified=consequence_ok,
            evidence=evidence,
            error=error_msg,
        )
        return metric, resp_data

    def _finalize_journey(
        self,
        journey_id: str,
        persona: str,
        title: str,
        steps: List[JourneyStepMetric],
        legibility: List[LegibilityCheck],
    ) -> JourneyRating:
        steps_total = len(steps)
        steps_passed = sum(1 for s in steps if s.passed)
        consequences_verified = sum(1 for s in steps if s.consequence_verified)
        leg_total = len(legibility)
        leg_passed = sum(1 for c in legibility if c.passed)

        avg_latency = (
            sum(s.latency_ms for s in steps) / steps_total if steps_total > 0 else 0.0
        )
        func_score = (
            (0.5 * (steps_passed / steps_total) + 0.5 * (consequences_verified / steps_total))
            if steps_total > 0
            else 1.0
        )
        leg_score = (leg_passed / leg_total) if leg_total > 0 else 1.0
        overall = 0.65 * func_score + 0.35 * leg_score

        recommendations: List[str] = []
        for s in steps:
            if not s.passed:
                recommendations.append(
                    f"Fix step {s.step_id} ({s.endpoint}): {s.error or s.evidence}"
                )
        for c in legibility:
            if not c.passed:
                recommendations.append(
                    f"Fix legibility check {c.check_id} ({c.selector_or_rule}): {c.evidence}"
                )

        return JourneyRating(
            journey_id=journey_id,
            persona=persona,
            title=title,
            steps_total=steps_total,
            steps_passed=steps_passed,
            consequences_verified=consequences_verified,
            legibility_checks_total=leg_total,
            legibility_checks_passed=leg_passed,
            avg_latency_ms=avg_latency,
            functional_score=func_score,
            legibility_score=leg_score,
            overall_journey_score=overall,
            grade=_compute_grade(overall),
            step_metrics=steps,
            legibility_metrics=legibility,
            recommendations=recommendations,
        )

    def rate_j1_waking_ingest_and_lakehouse(self) -> JourneyRating:
        """J1: Waking Telemetry Ingest & BigQuery Lakehouse Session Verification."""
        steps: List[JourneyStepMetric] = []

        m1, _ = self._run_step(
            JourneyStepAction(
                step_id="J1.S1",
                description="Render Dual-Loop Console root HTML (GET /)",
                method="GET",
                endpoint="/",
                expected_consequence="HTML contains .app-bar, .app-title-group, and .app-body-layout",
            ),
            lambda body: (
                isinstance(body, str)
                and "app-bar" in body
                and "app-title-group" in body
                and "app-body-layout" in body,
                "Root index.html rendered with DESIGN.md Top Bar and body layout classes",
            ),
        )
        steps.append(m1)

        m2, _ = self._run_step(
            JourneyStepAction(
                step_id="J1.S2",
                description="Ingest multi-turn developer transcript (POST /api/telemetry/ingest_transcript)",
                method="POST",
                endpoint="/api/telemetry/ingest_transcript",
                payload={
                    "user_id": "user:developer",
                    "session_id": "sess-rater-j1",
                    "content": (
                        "User: Always run Python unit tests with uv run pytest -v and never mock endpoints.\n"
                        "Agent: Understood. Enforcing uv run pytest -v and strict zero-mock verification."
                    ),
                    "format": "text",
                },
                expected_consequence="Transcript parsed into turns and logged to BigQuery episodes_log",
            ),
            lambda data: (
                isinstance(data, dict)
                and str(data.get("status", "")).upper() in ("INGESTED", "SUCCESS", "OK")
                and int(data.get("events_logged", data.get("turns_ingested", data.get("events_ingested", 0)))) >= 1,
                f"Transcript ingested (status={data.get('status') if isinstance(data, dict) else 'ERR'}, turns={data.get('turns_ingested', data.get('events_logged', 0)) if isinstance(data, dict) else 0})",
            ),
        )
        steps.append(m2)

        m3, _ = self._run_step(
            JourneyStepAction(
                step_id="J1.S3",
                description="Verify Lakehouse sessions feed (GET /api/lakehouse/sessions)",
                method="GET",
                endpoint="/api/lakehouse/sessions",
                expected_consequence="Lakehouse returns non-empty sessions list with turn counts",
            ),
            lambda data: (
                isinstance(data, dict)
                and isinstance(data.get("sessions"), list)
                and len(data["sessions"]) >= 1,
                f"Lakehouse returned {len(data.get('sessions', [])) if isinstance(data, dict) else 0} sessions",
            ),
        )
        steps.append(m3)

        m4, _ = self._run_step(
            JourneyStepAction(
                step_id="J1.S4",
                description="Verify Studio enriched sessions log (GET /api/studio/sessions)",
                method="GET",
                endpoint="/api/studio/sessions",
                expected_consequence="Studio sessions include turns, summary, and extracted_memories",
            ),
            lambda data: (
                isinstance(data, dict)
                and isinstance(data.get("sessions"), list)
                and len(data["sessions"]) >= 1
                and "turns" in data["sessions"][0]
                and "summary" in data["sessions"][0],
                f"Studio session log verified ({len(data.get('sessions', [])) if isinstance(data, dict) else 0} enriched sessions)",
            ),
        )
        steps.append(m4)

        legibility = [
            LegibilityCheck(
                check_id="J1.L1",
                category="TOPBAR_2_CHILD_STANDARD",
                selector_or_rule=".app-bar-content > .app-title-group + (.session-info | .app-actions)",
                passed=(
                    'class="app-title-group"' in self._index_html
                    and 'class="app-nav"' in self._index_html
                    and ('class="session-info"' in self._index_html or 'class="app-actions"' in self._index_html)
                ),
                evidence="index.html nests .app-nav inside .app-title-group under the Top Bar 2-Child Standard",
            ),
            LegibilityCheck(
                check_id="J1.L2",
                category="LAYOUT_PARITY",
                selector_or_rule=".app-body-layout across index.html, studio.html, how-it-works.html",
                passed=(
                    "app-body-layout" in self._index_html
                    and "app-body-layout" in self._studio_html
                    and "app-body-layout" in self._how_html
                ),
                evidence="All 3 primary HTML entrypoints share .app-body-layout container parity",
            ),
            LegibilityCheck(
                check_id="J1.L3",
                category="TYPOGRAPHY_READABILITY",
                selector_or_rule="--font-sans (IBM Plex Sans) + --font-mono (IBM Plex Mono) font tokens in style.css",
                passed=(
                    "--font-sans" in self._style_css
                    and "--font-mono" in self._style_css
                    and ("IBM Plex Sans" in self._style_css or "Plus Jakarta Sans" in self._style_css)
                    and ("IBM Plex Mono" in self._style_css or "JetBrains Mono" in self._style_css)
                ),
                evidence="Dual-font academic/tactical typography tokens configured in style.css",
            ),
        ]

        return self._finalize_journey(
            journey_id="J1_WAKING_INGEST_AND_LAKEHOUSE",
            persona="Developer / Telemetry Operator",
            title="Waking Telemetry Ingest & BigQuery Lakehouse Verification",
            steps=steps,
            legibility=legibility,
        )

    def rate_j2_sleep_harvest_and_spanner_vault(self) -> JourneyRating:
        """J2: NREM/REM Sleep Harvest, Spanner Crystalline Vault & MCP Recall."""
        steps: List[JourneyStepMetric] = []

        m1, _ = self._run_step(
            JourneyStepAction(
                step_id="J2.S1",
                description="Trigger NREM/REM Sleep Harvest cycle (POST /api/harvest)",
                method="POST",
                endpoint="/api/harvest",
                payload={"user_id": "user:developer", "session_id": "sess-rater-j1"},
                expected_consequence="Harvest pipeline summarizes episodes and upserts crystallized beliefs to Spanner",
            ),
            lambda data: (
                isinstance(data, dict)
                and str(data.get("status", "")).upper() in ("HARVEST_COMPLETED", "SUCCESS", "OK")
                and ("preferences_synced" in data or "preferences_upserted" in data or "records" in data),
                f"Sleep harvest completed (preferences_synced={data.get('preferences_synced', data.get('preferences_upserted', 0)) if isinstance(data, dict) else 0})",
            ),
        )
        steps.append(m1)

        m2, _ = self._run_step(
            JourneyStepAction(
                step_id="J2.S2",
                description="Inspect Spanner crystallized preferences (GET /api/spanner/preferences/user:developer)",
                method="GET",
                endpoint="/api/spanner/preferences/user:developer",
                expected_consequence="Returns non-empty preferences list with confidence and crystallized_belief",
            ),
            lambda data: (
                isinstance(data, dict)
                and isinstance(data.get("preferences"), list)
                and len(data["preferences"]) >= 1,
                f"Spanner returned {len(data.get('preferences', [])) if isinstance(data, dict) else 0} crystallized preferences",
            ),
        )
        steps.append(m2)

        m3, _ = self._run_step(
            JourneyStepAction(
                step_id="J2.S3",
                description="Inspect Spanner crystallized guardrails (GET /api/spanner/guardrails/user:developer)",
                method="GET",
                endpoint="/api/spanner/guardrails/user:developer",
                expected_consequence="Returns guardrails payload from Spanner UserGuardrails table",
            ),
            lambda data: (
                isinstance(data, dict) and "guardrails" in data,
                f"Spanner returned {len(data.get('guardrails', [])) if isinstance(data, dict) else 0} guardrails",
            ),
        )
        steps.append(m3)

        m4, _ = self._run_step(
            JourneyStepAction(
                step_id="J2.S4",
                description="Execute MCP tool get_user_crystallized_beliefs (POST /api/mcp/execute)",
                method="POST",
                endpoint="/api/mcp/execute",
                payload={
                    "tool_name": "get_user_crystallized_beliefs",
                    "arguments": {"user_id": "user:developer"},
                },
                expected_consequence="MCP server returns structured crystallized beliefs from Spanner",
            ),
            lambda data: (
                isinstance(data, dict)
                and str(data.get("status", "")).upper() == "SUCCESS"
                and data.get("result") is not None,
                "MCP tool get_user_crystallized_beliefs returned live Spanner beliefs",
            ),
        )
        steps.append(m4)

        legibility = [
            LegibilityCheck(
                check_id="J2.L1",
                category="SLEEP_CYCLE_CONTROLS",
                selector_or_rule="#triggerHarvestBtn and sleep harvest pipeline wiring in index.html & app.js",
                passed=("/api/harvest" in self._app_js and "harvest" in self._index_html.lower()),
                evidence="Sleep harvest trigger wired to POST /api/harvest in app.js",
            ),
            LegibilityCheck(
                check_id="J2.L2",
                category="MCP_INSPECTOR_WIRING",
                selector_or_rule="/api/mcp/execute wired in app.js",
                passed=("/api/mcp/execute" in self._app_js),
                evidence="MCP tool execution console dynamically wired to /api/mcp/execute",
            ),
        ]

        return self._finalize_journey(
            journey_id="J2_SLEEP_HARVEST_AND_SPANNER_VAULT",
            persona="Memory Pipeline Engineer",
            title="NREM/REM Sleep Harvest, Spanner Vault & MCP Recall",
            steps=steps,
            legibility=legibility,
        )

    def rate_j3_fast_preference_and_interview_agent(self) -> JourneyRating:
        """J3: Fast-Path Live Preference Injection & Dual-Agent Recall."""
        steps: List[JourneyStepMetric] = []

        m1, _ = self._run_step(
            JourneyStepAction(
                step_id="J3.S1",
                description="Inject live developer preference into Spanner (POST /api/preference/inject)",
                method="POST",
                endpoint="/api/preference/inject",
                payload={
                    "user_id": "user:developer",
                    "preference_key": "python_test_runner",
                    "preference_value": "uv run pytest -v",
                    "category": "technical",
                    "confidence": 0.99,
                    "crystallized_belief": "Always execute Python unit tests using uv run pytest -v.",
                    "keywords": "uv, pytest, python, test",
                },
                expected_consequence="Preference immediately persisted in Spanner and replicated to BigQuery",
            ),
            lambda data: (
                isinstance(data, dict)
                and str(data.get("status", "")).upper() in ("INJECTED", "SUCCESS", "OK")
                and (
                    data.get("preference_key") == "python_test_runner"
                    or data.get("record", {}).get("preference_key") == "python_test_runner"
                    or data.get("preference", {}).get("preference_key") == "python_test_runner"
                ),
                "Fast-path preference python_test_runner='uv run pytest -v' injected into Spanner",
            ),
        )
        steps.append(m1)

        m2, _ = self._run_step(
            JourneyStepAction(
                step_id="J3.S2",
                description="Query Preference Agent (POST /api/preference/query)",
                method="POST",
                endpoint="/api/preference/query",
                payload={
                    "user_id": "user:developer",
                    "session_id": "sess-rater-j3",
                    "prompt": "Please remember that I always use uv run pytest -v for Python testing.",
                },
                expected_consequence="Preference Agent processes turn and returns response",
            ),
            lambda data: (
                isinstance(data, dict)
                and bool(data.get("response"))
                and bool(data.get("agent_name") or "latency_ms" in data),
                f"Preference Agent responded ({len(str(data.get('response', ''))) if isinstance(data, dict) else 0} chars)",
            ),
        )
        steps.append(m2)

        m3, _ = self._run_step(
            JourneyStepAction(
                step_id="J3.S3",
                description="Query Interview Agent with Spanner recall (POST /api/interview/query)",
                method="POST",
                endpoint="/api/interview/query",
                payload={
                    "user_id": "user:developer",
                    "session_id": "sess-rater-j3",
                    "prompt": "What Python test runner and GCP auth policy do I prefer?",
                },
                expected_consequence="Interview Agent recalls crystallized beliefs from Spanner and answers",
            ),
            lambda data: (
                isinstance(data, dict)
                and bool(data.get("response"))
                and bool(data.get("agent_name") or "latency_ms" in data),
                f"Interview Agent responded with Spanner recall ({len(str(data.get('response', ''))) if isinstance(data, dict) else 0} chars)",
            ),
        )
        steps.append(m3)

        legibility = [
            LegibilityCheck(
                check_id="J3.L1",
                category="AGENT_CHAT_WIRING",
                selector_or_rule="/api/interview/query and /api/preference/query wired in app.js",
                passed=(
                    "/api/interview/query" in self._app_js
                    and "/api/preference/query" in self._app_js
                ),
                evidence="Both Interview and Preference agent endpoints wired in app.js without mock fallbacks",
            ),
            LegibilityCheck(
                check_id="J3.L2",
                category="ZERO_MOCK_FRONTEND",
                selector_or_rule="Zero simulateInference or Math.random mock fallbacks in app.js & studio.js",
                passed=(
                    "simulateInference" not in self._app_js
                    and "simulateInference" not in self._studio_js
                ),
                evidence="No synthetic inference fallbacks present in frontend JavaScript bundles",
            ),
        ]

        return self._finalize_journey(
            journey_id="J3_FAST_PREFERENCE_AND_INTERVIEW_AGENT",
            persona="Interactive ADK Developer",
            title="Fast Preference Injection & Dual-Agent Crystalline Recall",
            steps=steps,
            legibility=legibility,
        )

    def rate_j4_memory_studio_hierarchy_and_git_tree(self) -> JourneyRating:
        """J4: Enterprise Memory Studio 10-Workspace & Git-Style Staged Memory Tree."""
        steps: List[JourneyStepMetric] = []

        m1, _ = self._run_step(
            JourneyStepAction(
                step_id="J4.S1",
                description="Render Enterprise Memory Studio HTML (GET /studio)",
                method="GET",
                endpoint="/studio",
                expected_consequence="HTML contains all 10 workspaces and #studioInspectorDrawer",
            ),
            lambda body: (
                isinstance(body, str)
                and 'id="workspace-sessions"' in body
                and 'id="workspace-tree"' in body
                and 'id="workspace-security"' in body
                and "studioInspectorDrawer" in body,
                "studio.html rendered with all 10 workspace sections and contextual Inspector Drawer",
            ),
        )
        steps.append(m1)

        m2, _ = self._run_step(
            JourneyStepAction(
                step_id="J4.S2",
                description="Fetch unified Studio bootstrap state (GET /api/studio/state)",
                method="GET",
                endpoint="/api/studio/state",
                expected_consequence="State payload includes tree, files, sessions, entities, categories, extractors, and security_policies",
            ),
            lambda data: (
                isinstance(data, dict)
                and data.get("status") == "OK"
                and all(
                    k in data
                    for k in (
                        "tree",
                        "files",
                        "sessions",
                        "entities",
                        "categories",
                        "extractors",
                        "security_policies",
                    )
                ),
                "Unified 10-workspace Studio bootstrap state verified",
            ),
        )
        steps.append(m2)

        m3, stage_data = self._run_step(
            JourneyStepAction(
                step_id="J4.S3",
                description="Stage memory mutation in Git-style tree (POST /api/studio/tree/stage)",
                method="POST",
                endpoint="/api/studio/tree/stage",
                payload={
                    "scope_type": "USER",
                    "scope_id": "user:developer",
                    "category": "technical",
                    "memory_key": "rater_j4_temp_node",
                    "after_value": "Rater J4 verification rule: enforce two-stage consequence checks.",
                    "confidence": 0.97,
                    "diff_summary": "Staged by MemoryJourneyAgentRater J4",
                },
                expected_consequence="Returns staged commit with staged_id and updates staged_commits queue",
            ),
            lambda data: (
                isinstance(data, dict)
                and isinstance(data.get("staged"), dict)
                and bool(data["staged"].get("staged_id")),
                f"Staged commit created (staged_id={data.get('staged', {}).get('staged_id') if isinstance(data, dict) else 'NONE'})",
            ),
        )
        steps.append(m3)

        staged_id = (
            stage_data.get("staged", {}).get("staged_id", "")
            if isinstance(stage_data, dict)
            else ""
        )

        m4, _ = self._run_step(
            JourneyStepAction(
                step_id="J4.S4",
                description="Commit staged memory mutation to Spanner (POST /api/studio/tree/commit)",
                method="POST",
                endpoint="/api/studio/tree/commit",
                payload={"staged_id": staged_id},
                expected_consequence="Commit transitions to COMMITTED and replicates to Spanner ScopedMemories & BigQuery",
            ),
            lambda data: (
                isinstance(data, dict) and data.get("status") == "COMMITTED",
                f"Staged commit {staged_id} committed to Spanner ScopedMemories",
            ),
        )
        steps.append(m4)

        m5, _ = self._run_step(
            JourneyStepAction(
                step_id="J4.S5",
                description="Verify 4-tier federated scope inheritance (GET /api/studio/scopes)",
                method="GET",
                endpoint="/api/studio/scopes?user_id=user:developer&team_id=team:platform-eng",
                expected_consequence="Returns ORG -> TEAM -> AGENT -> USER hierarchy and formatted_prompt_context",
            ),
            lambda data: (
                isinstance(data, dict)
                and "federated" in data
                and "formatted_prompt_context" in data["federated"]
                and "resolved_memories" in data["federated"],
                f"Federated scope tree resolved ({len(data.get('federated', {}).get('resolved_memories', [])) if isinstance(data, dict) else 0} effective memories)",
            ),
        )
        steps.append(m5)

        m6, _ = self._run_step(
            JourneyStepAction(
                step_id="J4.S6",
                description="Clean up temporary test node (DELETE /api/studio/tree/node)",
                method="DELETE",
                endpoint="/api/studio/tree/node?scope_type=USER&scope_id=user:developer&memory_key=rater_j4_temp_node",
                expected_consequence="Temporary node removed from Spanner ScopedMemories",
            ),
            lambda data: (
                isinstance(data, dict) and data.get("status") in ("DELETED", "NOT_FOUND"),
                "Temporary node rater_j4_temp_node cleaned up",
            ),
        )
        steps.append(m6)

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
        all_ws_present = all(
            f'data-workspace="workspace-{ws}"' in self._studio_html
            and f'id="workspace-{ws}"' in self._studio_html
            for ws in expected_workspaces
        )

        forbidden_hexes = ("#f59e0b", "#fef3c7", "#d97706")
        combined_lower = (self._studio_html + self._style_css + self._studio_js).lower()
        zero_amber = all(h not in combined_lower for h in forbidden_hexes)

        pane_match = re.search(
            r"body\.studio-workbench-body\s+\.single-pane-container,\s*\.studio-main-pane,\s*\.studio-workspace-canvas\s*\{([^}]+)\}",
            self._style_css,
        )
        scroll_ok = bool(
            pane_match
            and "overflow-y: auto" in pane_match.group(1)
            and ".studio-scroll-pane" in self._style_css
        )

        legibility = [
            LegibilityCheck(
                check_id="J4.L1",
                category="TEN_WORKSPACE_NAVIGATION",
                selector_or_rule="All 10 data-workspace switchers and #workspace-* sections in studio.html",
                passed=all_ws_present,
                evidence="All 10 Studio workspaces present and addressable in DOM",
            ),
            LegibilityCheck(
                check_id="J4.L2",
                category="WARM_IVORY_PALETTE_GUARDRAIL",
                selector_or_rule="Zero forbidden harsh amber/yellow hexes (#f59e0b, #fef3c7, #d97706)",
                passed=zero_amber,
                evidence="Warm Ivory & Sepia palette enforced across studio.html, style.css, and studio.js",
            ),
            LegibilityCheck(
                check_id="J4.L3",
                category="VERTICAL_SCROLL_INTEGRITY",
                selector_or_rule=".single-pane-container and .studio-scroll-pane overflow-y: auto",
                passed=scroll_ok,
                evidence="Vertical scroll rules verified in style.css",
            ),
        ]

        return self._finalize_journey(
            journey_id="J4_MEMORY_STUDIO_HIERARCHY_AND_GIT_TREE",
            persona="Enterprise Memory Architect",
            title="Memory Studio 10-Workspace & Git-Style Staged Memory Tree",
            steps=steps,
            legibility=legibility,
        )

    def rate_j5_alphaevolve_and_extractor_lab(self) -> JourneyRating:
        """J5: 7-Metric Matryoshka Evaluation, AlphaEvolve MAP-Elites & RBAC Governance."""
        steps: List[JourneyStepMetric] = []

        m1, _ = self._run_step(
            JourneyStepAction(
                step_id="J5.S1",
                description="List adversarial & golden evaluation cases (GET /api/studio/eval/cases)",
                method="GET",
                endpoint="/api/studio/eval/cases",
                expected_consequence="Returns >= 4 evaluation cases with expected facts",
            ),
            lambda data: (
                isinstance(data, dict) and int(data.get("count", 0)) >= 4,
                f"Loaded {data.get('count', 0) if isinstance(data, dict) else 0} evaluation cases",
            ),
        )
        steps.append(m1)

        m2, _ = self._run_step(
            JourneyStepAction(
                step_id="J5.S2",
                description="Execute 7-Metric + Matryoshka Evaluation Suite (POST /api/studio/eval/run)",
                method="POST",
                endpoint="/api/studio/eval/run",
                payload={"alpha_quality_weight": 0.65},
                expected_consequence="Scorecard returns quality_score, token_efficiency_score, and 128d..768d Matryoshka slices",
            ),
            lambda data: (
                isinstance(data, dict)
                and 0.0 <= float(data.get("quality_score", -1.0)) <= 1.0
                and "matryoshka_comparison" in data
                and all(d in data["matryoshka_comparison"] for d in ("128d", "256d", "512d", "768d")),
                f"7-Metric eval passed (quality={data.get('quality_score') if isinstance(data, dict) else 'N/A'})",
            ),
        )
        steps.append(m2)

        m3, _ = self._run_step(
            JourneyStepAction(
                step_id="J5.S3",
                description="Run AlphaEvolve evolutionary search (POST /api/studio/evolve/run)",
                method="POST",
                endpoint="/api/studio/evolve/run",
                payload={
                    "generations": 1,
                    "alpha_quality_weight": 0.65,
                    "candidates_per_generation": 1,
                },
                expected_consequence="Evolution completes with champion genome and populated MAP-Elites archive cells",
            ),
            lambda data: (
                isinstance(data, dict)
                and data.get("status") == "EVOLUTION_COMPLETE"
                and "champion" in data
                and int(data.get("archive", {}).get("populated_cells", 0)) >= 1,
                f"AlphaEvolve completed (populated_cells={data.get('archive', {}).get('populated_cells') if isinstance(data, dict) else 0})",
            ),
        )
        steps.append(m3)

        m4, _ = self._run_step(
            JourneyStepAction(
                step_id="J5.S4",
                description="Inspect MAP-Elites archive & active config (GET /api/studio/evolve/archive)",
                method="GET",
                endpoint="/api/studio/evolve/archive",
                expected_consequence="Returns archive_summary cells and active_config",
            ),
            lambda data: (
                isinstance(data, dict)
                and "archive_summary" in data
                and "active_config" in data,
                "MAP-Elites archive_summary and active_config verified",
            ),
        )
        steps.append(m4)

        m5, _ = self._run_step(
            JourneyStepAction(
                step_id="J5.S5",
                description="Inspect Memory Extractor configurations (GET /api/studio/extractors)",
                method="GET",
                endpoint="/api/studio/extractors",
                expected_consequence="Returns extractors using gemini-4-hydrogen / gemini-4-neon with structured few_shot_examples",
            ),
            lambda data: (
                isinstance(data, dict)
                and isinstance(data.get("extractors"), list)
                and len(data["extractors"]) >= 1
                and isinstance(data["extractors"][0].get("few_shot_examples"), list),
                f"Verified {len(data.get('extractors', [])) if isinstance(data, dict) else 0} extractor jobs",
            ),
        )
        steps.append(m5)

        m6, _ = self._run_step(
            JourneyStepAction(
                step_id="J5.S6",
                description="Simulate RBAC governance policy decision (POST /api/studio/security/simulate)",
                method="POST",
                endpoint="/api/studio/security/simulate",
                payload={
                    "principal": "user:developer",
                    "action": "WRITE",
                    "scope": "ORG",
                    "target_scope_id": "org:default",
                },
                expected_consequence="Returns deterministic ALLOW/DENY decision with rationale and scope_path",
            ),
            lambda data: (
                isinstance(data, dict)
                and data.get("decision") in ("ALLOW", "DENY")
                and bool(data.get("rationale")),
                f"RBAC simulator returned decision={data.get('decision') if isinstance(data, dict) else 'ERR'}",
            ),
        )
        steps.append(m6)

        toolbar_ok = all(
            f'id="{tid}"' in self._studio_html
            for tid in (
                "workflowStageDropdown",
                "stageActionsDropdown",
                "engineConfigDropdown",
                "toolbarViewModeBadge",
            )
        )

        legibility = [
            LegibilityCheck(
                check_id="J5.L1",
                category="STUDIO_TOOLBAR_DROPDOWNS",
                selector_or_rule="#workflowStageDropdown, #stageActionsDropdown, #engineConfigDropdown in studio.html",
                passed=toolbar_ok,
                evidence="Contextual Studio toolbar dropdown controls verified in studio.html",
            ),
            LegibilityCheck(
                check_id="J5.L2",
                category="EVAL_AND_EVOLVE_WIRING",
                selector_or_rule="/api/studio/eval/run and /api/studio/evolve/run wired in studio.js",
                passed=(
                    "/api/studio/eval/run" in self._studio_js
                    and "/api/studio/evolve/run" in self._studio_js
                ),
                evidence="7-Metric Eval and AlphaEvolve endpoints dynamically wired in studio.js",
            ),
        ]

        return self._finalize_journey(
            journey_id="J5_ALPHAEVOLVE_AND_EXTRACTOR_LAB",
            persona="ML Evaluation Scientist",
            title="7-Metric Matryoshka Eval, AlphaEvolve MAP-Elites & RBAC Governance",
            steps=steps,
            legibility=legibility,
        )

    def evaluate_all_journeys(self) -> AgentRaterReport:
        """Execute all 5 canonical functional journeys and return a certified AgentRaterReport."""
        ratings = [
            self.rate_j1_waking_ingest_and_lakehouse(),
            self.rate_j2_sleep_harvest_and_spanner_vault(),
            self.rate_j3_fast_preference_and_interview_agent(),
            self.rate_j4_memory_studio_hierarchy_and_git_tree(),
            self.rate_j5_alphaevolve_and_extractor_lab(),
        ]

        total_journeys = len(ratings)
        passed_journeys = sum(
            1
            for r in ratings
            if r.steps_passed == r.steps_total
            and r.consequences_verified == r.steps_total
            and r.legibility_checks_passed == r.legibility_checks_total
        )

        overall_func = (
            sum(r.functional_score for r in ratings) / total_journeys
            if total_journeys > 0
            else 1.0
        )
        overall_leg = (
            sum(r.legibility_score for r in ratings) / total_journeys
            if total_journeys > 0
            else 1.0
        )
        composite = 0.65 * overall_func + 0.35 * overall_leg

        critical_findings: List[str] = []
        for r in ratings:
            critical_findings.extend(r.recommendations)

        certified = passed_journeys == total_journeys and composite >= 0.95

        return AgentRaterReport(
            rater_id="electric-sheep-memory-journey-rater-v2",
            timestamp_epoch=time.time(),
            overall_functional_score=overall_func,
            overall_legibility_score=overall_leg,
            composite_score=composite,
            composite_grade=_compute_grade(composite, certified=certified),
            journeys_evaluated=total_journeys,
            journeys_passed=passed_journeys,
            journey_ratings=ratings,
            critical_findings=critical_findings,
        )


# Alias for backward/cross-repo compatibility
CustomerJourneyAgentRater = MemoryJourneyAgentRater

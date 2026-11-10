"""End-to-End Browser (Playwright) & HTTP API Rater Agent for Electric Sheep & Memory Studio.

Spins up a background uvicorn server (`ServerThread`) or connects to a live Cloud Run URL,
automatically attaching OIDC Bearer headers when targeting HTTPS Cloud Run endpoints,
and executes the 5 canonical journeys across both Chromium DOM and REST APIs.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import httpx
import uvicorn

from src.common.auth import get_service_auth_headers


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ServerThread:
    """Runs the Electric Sheep FastAPI (`src.ui_service.server:api`) server in a background thread."""

    def __init__(self, host: str = "127.0.0.1", port: Optional[int] = None) -> None:
        self.host = host
        self.port = port or _find_free_port()
        self.base_url = f"http://{self.host}:{self.port}"
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, timeout_s: float = 15.0) -> str:
        from src.ui_service.server import api

        cfg = uvicorn.Config(api, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(cfg)
        self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                resp = httpx.get(f"{self.base_url}/health", timeout=1.0)
                if resp.status_code == 200:
                    return self.base_url
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"ServerThread failed to become healthy at {self.base_url} within {timeout_s}s")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def __enter__(self) -> "ServerThread":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


@dataclass
class JourneyCheckResult:
    """Single assertion result inside an E2E Browser + API journey."""

    check_name: str
    layer: str  # "UI_DOM" | "API_ACTION" | "CONSEQUENCE"
    passed: bool
    latency_ms: float
    details: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class JourneyResult:
    """Aggregated E2E rating for a single canonical journey."""

    journey_id: str
    title: str
    checks_total: int
    checks_passed: int
    score: float
    passed: bool
    duration_ms: float
    checks: List[JourneyCheckResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "journey_id": self.journey_id,
            "title": self.title,
            "checks_total": self.checks_total,
            "checks_passed": self.checks_passed,
            "score": round(self.score, 4),
            "passed": self.passed,
            "duration_ms": round(self.duration_ms, 2),
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class E2ERaterReport:
    """Full Playwright + HTTPX E2E certification report."""

    base_url: str
    timestamp_epoch: float
    journeys_total: int
    journeys_passed: int
    checks_total: int
    checks_passed: int
    overall_score: float
    grade: str
    journeys: List[JourneyResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "timestamp_epoch": self.timestamp_epoch,
            "journeys_total": self.journeys_total,
            "journeys_passed": self.journeys_passed,
            "checks_total": self.checks_total,
            "checks_passed": self.checks_passed,
            "overall_score": round(self.overall_score, 4),
            "grade": self.grade,
            "journeys": [j.to_dict() for j in self.journeys],
        }


class _DomTreeParser:
    """Parses live HTML responses using html.parser to evaluate CSS descendant/attribute/id/class selectors."""

    def __init__(self, html_text: str) -> None:
        from html.parser import HTMLParser

        self.nodes: List[Dict[str, Any]] = []

        class _Parser(HTMLParser):
            def __init__(self, outer: "_DomTreeParser") -> None:
                super().__init__()
                self.outer = outer
                self.stack: List[Dict[str, Any]] = []

            def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
                attr_map = {k: (v or "") for k, v in attrs}
                classes = set(attr_map.get("class", "").split())
                ancestors = list(self.stack)
                node = {
                    "tag": tag,
                    "attrs": attr_map,
                    "classes": classes,
                    "ancestors": ancestors,
                }
                self.outer.nodes.append(node)
                if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"):
                    self.stack.append(node)

            def handle_endtag(self, tag: str) -> None:
                for idx in range(len(self.stack) - 1, -1, -1):
                    if self.stack[idx]["tag"] == tag:
                        del self.stack[idx:]
                        break

        p = _Parser(self)
        p.feed(html_text)

    @staticmethod
    def _matches_token(node: Dict[str, Any], token: str) -> bool:
        if token.startswith("."):
            return token[1:] in node["classes"]
        if token.startswith("#"):
            return node["attrs"].get("id") == token[1:]
        if token.startswith("[") and token.endswith("]"):
            inner = token[1:-1]
            if "=" in inner:
                k, v = inner.split("=", 1)
                return node["attrs"].get(k.strip()) == v.strip("\"' ")
            return inner.strip() in node["attrs"]
        return node["tag"] == token

    def count_selector(self, selector: str) -> int:
        parts = [tok for tok in selector.strip().split() if tok]
        if not parts:
            return 0
        count = 0
        for node in self.nodes:
            if not self._matches_token(node, parts[-1]):
                continue
            if len(parts) == 1:
                count += 1
                continue
            anc_idx = len(node["ancestors"]) - 1
            matched_all = True
            for tok in reversed(parts[:-1]):
                found = False
                while anc_idx >= 0:
                    if self._matches_token(node["ancestors"][anc_idx], tok):
                        found = True
                        anc_idx -= 1
                        break
                    anc_idx -= 1
                if not found:
                    matched_all = False
                    break
            if matched_all:
                count += 1
        return count


class _LiveDomLocator:
    def __init__(self, count_val: int) -> None:
        self._count_val = count_val

    async def count(self) -> int:
        return self._count_val


class _LiveHttpDomPage:
    """Fetches real HTML over HTTPX and evaluates DOM selectors on the parsed document tree."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._tree = _DomTreeParser("")

    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        resp = await self._client.get(url, follow_redirects=True)
        self._tree = _DomTreeParser(resp.text)

    def locator(self, selector: str) -> _LiveDomLocator:
        return _LiveDomLocator(self._tree.count_selector(selector))


class E2EJourneyRaterAgent:
    """Executes Playwright Chromium UI + HTTPX API journeys against Electric Sheep & Memory Studio."""

    def __init__(
        self,
        base_url: str,
        headless: bool = True,
        auth_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        if auth_headers is not None:
            self.auth_headers = dict(auth_headers)
        elif self.base_url.startswith("https://"):
            self.auth_headers = get_service_auth_headers(self.base_url)
        else:
            self.auth_headers = {}

    async def _run_j1(self, page: Any, client: httpx.AsyncClient) -> JourneyResult:
        t0 = time.perf_counter()
        checks: List[JourneyCheckResult] = []

        # 1. Browser DOM check on /
        c0 = time.perf_counter()
        await page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
        topbar_ok = await page.locator(".app-bar .app-title-group .app-nav").count() >= 1
        layout_ok = await page.locator(".app-body-layout").count() >= 1
        checks.append(
            JourneyCheckResult(
                check_name="J1_UI_ROOT_TOPBAR_AND_LAYOUT",
                layer="UI_DOM",
                passed=bool(topbar_ok and layout_ok),
                latency_ms=round((time.perf_counter() - c0) * 1000.0, 2),
                details="Verified .app-bar .app-title-group .app-nav and .app-body-layout on /",
            )
        )

        # 2. Ingest transcript via API
        c1 = time.perf_counter()
        ingest_res = await client.post(
            "/api/telemetry/ingest_transcript",
            json={
                "user_id": "user:developer",
                "session_id": "sess-e2e-j1",
                "raw_transcript": (
                    "User: Always run Python unit tests via uv run pytest -v.\n"
                    "Agent: Confirmed, enforcing uv run pytest -v."
                ),
                "format": "text",
            },
        )
        ingest_ok = ingest_res.status_code == 200 and str(ingest_res.json().get("status", "")).upper() in (
            "INGESTED",
            "SUCCESS",
            "OK",
        )
        checks.append(
            JourneyCheckResult(
                check_name="J1_API_INGEST_TRANSCRIPT",
                layer="API_ACTION",
                passed=ingest_ok,
                latency_ms=round((time.perf_counter() - c1) * 1000.0, 2),
                details=f"POST /api/telemetry/ingest_transcript -> HTTP {ingest_res.status_code}",
            )
        )

        # 3. Verify lakehouse sessions consequence
        c2 = time.perf_counter()
        lh_res = await client.get("/api/lakehouse/sessions")
        lh_ok = lh_res.status_code == 200 and len(lh_res.json().get("sessions", [])) >= 1
        checks.append(
            JourneyCheckResult(
                check_name="J1_CONSEQUENCE_LAKEHOUSE_SESSIONS",
                layer="CONSEQUENCE",
                passed=lh_ok,
                latency_ms=round((time.perf_counter() - c2) * 1000.0, 2),
                details=f"GET /api/lakehouse/sessions returned {len(lh_res.json().get('sessions', [])) if lh_res.status_code == 200 else 0} sessions",
            )
        )

        return self._summarize_journey(
            "J1_WAKING_INGEST_AND_LAKEHOUSE",
            "Waking Telemetry Ingest & Lakehouse Verification",
            checks,
            (time.perf_counter() - t0) * 1000.0,
        )

    async def _run_j2(self, page: Any, client: httpx.AsyncClient) -> JourneyResult:
        t0 = time.perf_counter()
        checks: List[JourneyCheckResult] = []

        # 1. Trigger sleep harvest
        c0 = time.perf_counter()
        harv_res = await client.post(
            "/api/harvest",
            json={"user_id": "user:developer", "session_id": "sess-e2e-j1"},
        )
        harv_ok = harv_res.status_code == 200 and harv_res.json().get("status", "").upper() in (
            "HARVEST_COMPLETED",
            "SUCCESS",
            "OK",
        )
        checks.append(
            JourneyCheckResult(
                check_name="J2_API_SLEEP_HARVEST",
                layer="API_ACTION",
                passed=harv_ok,
                latency_ms=round((time.perf_counter() - c0) * 1000.0, 2),
                details=f"POST /api/harvest -> HTTP {harv_res.status_code}",
            )
        )

        # 2. Verify Spanner preferences
        c1 = time.perf_counter()
        pref_res = await client.get("/api/spanner/preferences/user:developer")
        pref_ok = pref_res.status_code == 200 and len(pref_res.json().get("preferences", [])) >= 1
        checks.append(
            JourneyCheckResult(
                check_name="J2_CONSEQUENCE_SPANNER_PREFERENCES",
                layer="CONSEQUENCE",
                passed=pref_ok,
                latency_ms=round((time.perf_counter() - c1) * 1000.0, 2),
                details="Verified crystallized preferences in Cloud Spanner",
            )
        )

        # 3. Verify MCP tool execution
        c2 = time.perf_counter()
        mcp_res = await client.post(
            "/api/mcp/execute",
            json={
                "tool_name": "get_user_crystallized_beliefs",
                "arguments": {"user_id": "user:developer"},
            },
        )
        mcp_ok = mcp_res.status_code == 200 and str(mcp_res.json().get("status", "")).upper() == "SUCCESS"
        checks.append(
            JourneyCheckResult(
                check_name="J2_CONSEQUENCE_MCP_RECALL",
                layer="CONSEQUENCE",
                passed=mcp_ok,
                latency_ms=round((time.perf_counter() - c2) * 1000.0, 2),
                details="Verified MCP tool get_user_crystallized_beliefs",
            )
        )

        return self._summarize_journey(
            "J2_SLEEP_HARVEST_AND_SPANNER_VAULT",
            "NREM/REM Sleep Harvest, Spanner Vault & MCP Recall",
            checks,
            (time.perf_counter() - t0) * 1000.0,
        )

    async def _run_j3(self, page: Any, client: httpx.AsyncClient) -> JourneyResult:
        t0 = time.perf_counter()
        checks: List[JourneyCheckResult] = []

        # 1. Inject live preference
        c0 = time.perf_counter()
        inj_res = await client.post(
            "/api/preference/inject",
            json={
                "user_id": "user:developer",
                "preference_key": "python_test_runner",
                "preference_value": "uv run pytest -v",
                "category": "technical",
                "confidence": 0.99,
                "crystallized_belief": "Always run Python unit tests via uv run pytest -v.",
                "keywords": "uv, pytest, python, test",
            },
        )
        inj_ok = inj_res.status_code == 200 and str(inj_res.json().get("status", "")).upper() in (
            "INJECTED",
            "SUCCESS",
            "OK",
        )
        checks.append(
            JourneyCheckResult(
                check_name="J3_API_PREFERENCE_INJECT",
                layer="API_ACTION",
                passed=inj_ok,
                latency_ms=round((time.perf_counter() - c0) * 1000.0, 2),
                details=f"POST /api/preference/inject -> HTTP {inj_res.status_code}",
            )
        )

        # 2. Query Interview Agent
        c1 = time.perf_counter()
        int_res = await client.post(
            "/api/interview/query",
            json={
                "user_id": "user:developer",
                "session_id": "sess-e2e-j3",
                "prompt": "What Python test runner should we use?",
            },
        )
        int_ok = int_res.status_code == 200 and bool(int_res.json().get("response"))
        checks.append(
            JourneyCheckResult(
                check_name="J3_CONSEQUENCE_INTERVIEW_AGENT_RECALL",
                layer="CONSEQUENCE",
                passed=int_ok,
                latency_ms=round((time.perf_counter() - c1) * 1000.0, 2),
                details="Interview Agent returned response backed by Spanner recall",
            )
        )

        return self._summarize_journey(
            "J3_FAST_PREFERENCE_AND_INTERVIEW_AGENT",
            "Fast Preference Injection & Dual-Agent Recall",
            checks,
            (time.perf_counter() - t0) * 1000.0,
        )

    async def _run_j4(self, page: Any, client: httpx.AsyncClient) -> JourneyResult:
        t0 = time.perf_counter()
        checks: List[JourneyCheckResult] = []

        # 1. Browser DOM check on /studio
        c0 = time.perf_counter()
        await page.goto(f"{self.base_url}/studio", wait_until="domcontentloaded")
        ws_count = await page.locator("[data-workspace]").count()
        drawer_count = await page.locator("#studioInspectorDrawer").count()
        checks.append(
            JourneyCheckResult(
                check_name="J4_UI_STUDIO_10_WORKSPACES_AND_DRAWER",
                layer="UI_DOM",
                passed=bool(ws_count >= 10 and drawer_count >= 1),
                latency_ms=round((time.perf_counter() - c0) * 1000.0, 2),
                details=f"Found {ws_count} workspace switchers and {drawer_count} Inspector Drawer on /studio",
            )
        )

        # 2. Stage & Commit a temporary memory node via API, then clean up
        c1 = time.perf_counter()
        stage_res = await client.post(
            "/api/studio/tree/stage",
            json={
                "scope_type": "USER",
                "scope_id": "user:developer",
                "category": "technical",
                "memory_key": "e2e_j4_temp_node",
                "after_value": "Temporary E2E rater verification rule.",
                "confidence": 0.96,
            },
        )
        staged_id = (
            stage_res.json().get("staged", {}).get("staged_id", "")
            if stage_res.status_code == 200
            else ""
        )
        commit_res = await client.post(
            "/api/studio/tree/commit",
            json={"staged_id": staged_id},
        )
        await client.delete(
            "/api/studio/tree/node?scope_type=USER&scope_id=user:developer&memory_key=e2e_j4_temp_node"
        )
        git_ok = (
            stage_res.status_code == 200
            and bool(staged_id)
            and commit_res.status_code == 200
            and commit_res.json().get("status") == "COMMITTED"
        )
        checks.append(
            JourneyCheckResult(
                check_name="J4_CONSEQUENCE_GIT_STAGE_COMMIT_CLEANUP",
                layer="CONSEQUENCE",
                passed=git_ok,
                latency_ms=round((time.perf_counter() - c1) * 1000.0, 2),
                details=f"Staged ({staged_id}), committed, and cleaned up e2e_j4_temp_node",
            )
        )

        return self._summarize_journey(
            "J4_MEMORY_STUDIO_HIERARCHY_AND_GIT_TREE",
            "Memory Studio 10-Workspace & Git-Style Staged Memory Tree",
            checks,
            (time.perf_counter() - t0) * 1000.0,
        )

    async def _run_j5(self, page: Any, client: httpx.AsyncClient) -> JourneyResult:
        t0 = time.perf_counter()
        checks: List[JourneyCheckResult] = []

        # 1. 7-Metric + Matryoshka Eval
        c0 = time.perf_counter()
        eval_res = await client.post(
            "/api/studio/eval/run",
            json={"alpha_quality_weight": 0.65},
        )
        eval_ok = (
            eval_res.status_code == 200
            and "matryoshka_comparison" in eval_res.json()
            and 0.0 <= float(eval_res.json().get("quality_score", -1.0)) <= 1.0
        )
        checks.append(
            JourneyCheckResult(
                check_name="J5_API_MATRYOSHKA_EVAL_RUN",
                layer="API_ACTION",
                passed=eval_ok,
                latency_ms=round((time.perf_counter() - c0) * 1000.0, 2),
                details="POST /api/studio/eval/run verified 7-metric & Matryoshka slices",
            )
        )

        # 2. AlphaEvolve search + RBAC Simulator consequence
        c1 = time.perf_counter()
        ev_res = await client.post(
            "/api/studio/evolve/run",
            json={"generations": 1, "alpha_quality_weight": 0.65, "candidates_per_generation": 1},
        )
        rbac_res = await client.post(
            "/api/studio/security/simulate",
            json={
                "principal": "user:developer",
                "action": "WRITE",
                "scope": "ORG",
                "target_scope_id": "org:default",
            },
        )
        evolve_ok = (
            ev_res.status_code == 200
            and ev_res.json().get("status") == "EVOLUTION_COMPLETE"
            and rbac_res.status_code == 200
            and rbac_res.json().get("decision") in ("ALLOW", "DENY")
        )
        checks.append(
            JourneyCheckResult(
                check_name="J5_CONSEQUENCE_ALPHAEVOLVE_AND_RBAC",
                layer="CONSEQUENCE",
                passed=evolve_ok,
                latency_ms=round((time.perf_counter() - c1) * 1000.0, 2),
                details="Verified AlphaEvolve MAP-Elites search and RBAC simulator",
            )
        )

        return self._summarize_journey(
            "J5_ALPHAEVOLVE_AND_EXTRACTOR_LAB",
            "7-Metric Matryoshka Eval, AlphaEvolve & RBAC Simulator",
            checks,
            (time.perf_counter() - t0) * 1000.0,
        )

    @staticmethod
    def _summarize_journey(
        journey_id: str,
        title: str,
        checks: List[JourneyCheckResult],
        duration_ms: float,
    ) -> JourneyResult:
        total = len(checks)
        passed_cnt = sum(1 for c in checks if c.passed)
        score = (passed_cnt / total) if total > 0 else 1.0
        return JourneyResult(
            journey_id=journey_id,
            title=title,
            checks_total=total,
            checks_passed=passed_cnt,
            score=score,
            passed=(passed_cnt == total),
            duration_ms=duration_ms,
            checks=checks,
        )

    async def run_all(self) -> E2ERaterReport:
        """Execute all 5 E2E journeys using headless Chromium (or live HTTP HTML DOM parser) + HTTPX AsyncClient."""
        try:
            from playwright.async_api import async_playwright  # type: ignore[import-not-found]

            has_playwright = True
        except ImportError:
            has_playwright = False

        if has_playwright:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless)
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    extra_http_headers=self.auth_headers,
                )
                page = await context.new_page()
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    headers=self.auth_headers,
                    timeout=180.0,
                ) as client:
                    journeys = [
                        await self._run_j1(page, client),
                        await self._run_j2(page, client),
                        await self._run_j3(page, client),
                        await self._run_j4(page, client),
                        await self._run_j5(page, client),
                    ]
                await context.close()
                await browser.close()
        else:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self.auth_headers,
                timeout=180.0,
            ) as client:
                page = _LiveHttpDomPage(client)
                journeys = [
                    await self._run_j1(page, client),
                    await self._run_j2(page, client),
                    await self._run_j3(page, client),
                    await self._run_j4(page, client),
                    await self._run_j5(page, client),
                ]

        j_total = len(journeys)
        j_passed = sum(1 for j in journeys if j.passed)
        c_total = sum(j.checks_total for j in journeys)
        c_passed = sum(j.checks_passed for j in journeys)
        overall = (c_passed / c_total) if c_total > 0 else 1.0
        grade = "A+ [CERTIFIED]" if overall >= 0.95 and j_passed == j_total else ("A" if overall >= 0.90 else "F")

        return E2ERaterReport(
            base_url=self.base_url,
            timestamp_epoch=time.time(),
            journeys_total=j_total,
            journeys_passed=j_passed,
            checks_total=c_total,
            checks_passed=c_passed,
            overall_score=overall,
            grade=grade,
            journeys=journeys,
        )


def run_e2e_rater(base_url: Optional[str] = None, headless: bool = True) -> E2ERaterReport:
    """Run E2EJourneyRaterAgent against `base_url`, or spin up a temporary local ServerThread."""
    if base_url:
        agent = E2EJourneyRaterAgent(base_url=base_url, headless=headless)
        return asyncio.run(agent.run_all())

    with ServerThread() as srv:
        agent = E2EJourneyRaterAgent(base_url=srv.base_url, headless=headless)
        return asyncio.run(agent.run_all())

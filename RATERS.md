# Electric Sheep & Enterprise Memory Studio — Rater Platform (`RATERS.md`)

> **Version**: 2.0 (Functional + Visual Legibility Agentic Rater Suite)  
> **Scope**: End-to-end functional user journeys, two-stage action $\rightarrow$ consequence verification, and `DESIGN.md` visual legibility across the Electric Sheep Dual-Loop Console (`/`) and the 10-Workspace Enterprise Memory Studio (`/studio`).

---

## 1. Core Design Philosophy

1. **Strict Zero-Mock Directive**: Rater agents never use mock data, fake responses, or synthetic pass flags. Every journey step executes real FastAPI/Starlette HTTP mutations against `src.ui_service.server:api` (or a live uvicorn/Cloud Run server) and inspects real DOM, CSS, and JavaScript assets.
2. **Two-Stage Action $\rightarrow$ Consequence Verification**: Every user action is graded on both:
   - **Action Trigger**: Did the HTTP endpoint or UI control accept the request with a valid status and schema?
   - **Downstream Consequence**: Did the state mutation propagate to the expected persistence/read layer (e.g., BigQuery `episodes_log` lakehouse, Cloud Spanner `ScopedMemories` / `StagedMemoryCommits`, MCP server tool recall, or MAP-Elites archive)?
3. **Dual-Track Execution**:
   - **In-Memory ASGI Rater (`MemoryJourneyAgentRater`)**: Runs in `< 2s` during unit tests and CI/CD via `fastapi.testclient.TestClient` and static DOM/CSS AST inspection, exposed both via CLI (`scripts.rate_memory_journeys`) and REST (`POST /api/rate/journeys`).
   - **Live Browser + HTTP E2E Rater (`E2EJourneyRaterAgent`)**: Launches a real background `uvicorn` server (`ServerThread`) or targets a live Cloud Run URL, driving headless Playwright Chromium + `httpx.AsyncClient` with automatic OIDC Bearer authentication (`scripts.rate_ui_and_api`).

---

## 2. The 5 Canonical Functional User Journeys

| Journey ID | Persona | Functional Loop & Two-Stage Consequence Checks |
| :--- | :--- | :--- |
| **`J1_WAKING_INGEST_AND_LAKEHOUSE`** | Developer / Telemetry Operator | **Waking Ingest & Lakehouse Verification**: Renders `/` (`index.html`), verifies top-bar 2-child layout (`.app-nav` inside `.app-title-group`), ingests a multi-turn coding session via `POST /api/telemetry/ingest_transcript`, and verifies the session appears in `GET /api/lakehouse/sessions` and `GET /api/studio/sessions`. |
| **`J2_SLEEP_HARVEST_AND_SPANNER_VAULT`** | Memory Pipeline Engineer | **NREM/REM Sleep Harvest & Spanner Crystalline Vault**: Triggers sleep-cycle harvesting via `POST /api/harvest`, verifies crystallized beliefs and guardrails in `GET /api/spanner/preferences/{user_id}` and `GET /api/spanner/guardrails/{user_id}`, and executes live MCP tool recall via `POST /api/mcp/execute` (`get_user_crystallized_beliefs`). |
| **`J3_FAST_PREFERENCE_AND_INTERVIEW_AGENT`** | Interactive ADK User | **Fast Preference Injection & Dual-Agent Recall**: Injects a live explicit preference (`uv run pytest -v`) via `POST /api/preference/inject`, queries the Preference Agent (`POST /api/preference/query`) and Interview Agent (`POST /api/interview/query`), and verifies low-latency Spanner-backed context recall. |
| **`J4_MEMORY_STUDIO_HIERARCHY_AND_GIT_TREE`** | Enterprise Memory Architect | **Memory Studio 10-Workspace & Git-Style Staged Tree**: Renders `/studio` (`studio.html`), fetches unified state (`GET /api/studio/state`), stages a memory file mutation (`POST /api/studio/tree/stage`), commits to Spanner & BigQuery (`POST /api/studio/tree/commit`), verifies 4-tier federated scope inheritance (`GET /api/studio/scopes`), and cleans up temporary test nodes (`DELETE /api/studio/tree/node`). |
| **`J5_ALPHAEVOLVE_AND_EXTRACTOR_LAB`** | ML Evaluation Scientist | **7-Metric Matryoshka Eval, AlphaEvolve & RBAC Simulator**: Executes the 7-metric + Matryoshka embedding slice evaluation (`POST /api/studio/eval/run`), runs multi-generation AlphaEvolve MAP-Elites search (`POST /api/studio/evolve/run`), inspects the archive (`GET /api/studio/evolve/archive`), verifies extractor configurations (`GET /api/studio/extractors`), and simulates hierarchical RBAC governance (`POST /api/studio/security/simulate`). |

---

## 3. Visual Legibility & Design System Guardrails (`DESIGN.md`)

Every journey includes automated `LegibilityCheck` assertions enforcing `DESIGN.md`:

- **Section 3.1.1 — Top App Bar 2-Child Standard**: `.app-bar-content` contains `.app-title-group` (housing brand + `.app-nav`) and `.app-actions`, preventing awkward flex-wrap collisions across `/`, `/studio`, and `/how-it-works.html`.
- **Section 3.1.2 — `.app-body-layout` Parity**: Consistent outer container margins and vertical scroll behavior (`overflow-y: auto` on `.studio-scroll-pane` and `.single-pane-container`).
- **Warm Ivory & Sepia Palette Guardrails**: Prohibits legacy harsh amber/yellow warning hex codes (`#f59e0b`, `#fef3c7`, `#d97706`) across `studio.html`, `style.css`, and `studio.js`.
- **10-Workspace Progressive Disclosure**: Verifies all 10 Studio workspaces (`sessions`, `tree`, `playground`, `eval`, `entities`, `categories`, `ingest`, `extractors`, `backend`, `security`) and the contextual right-hand Inspector Drawer (`#studioInspectorDrawer`).

---

## 4. Running the Raters

### 4.1 Fast In-Memory Journey Rater (CLI)
```bash
uv run python -m scripts.rate_memory_journeys
uv run python -m scripts.rate_memory_journeys --json
```

### 4.2 Live Browser + API E2E Rater (Playwright + HTTPX)
```bash
# Spins up a local ServerThread automatically:
uv run python -m scripts.rate_ui_and_api

# Or target a live Cloud Run deployment (automatically attaches OIDC Bearer token):
ELECTRIC_SHEEP_URL="https://your-ui-service-url.a.run.app" \
  uv run python -m scripts.rate_ui_and_api
```

### 4.3 Pytest Verification Suites
```bash
# Fast unit + functional + visual legibility rater suite (< 3s)
uv run pytest tests/test_agent_raters.py -q

# Full Playwright E2E rater suite
uv run pytest tests/e2e/test_rater_agent.py -q
```

### 4.4 REST API Endpoint
```bash
curl -X POST http://localhost:8080/api/rate/journeys
```

---
name: ui-clarity-and-tdd
description: >-
  Mandatory planning, TDD, affordance enforcement, and clarity standards for web applications,
  engineering studios, and operational consoles. Enforces zero false affordances, quiet typography,
  Simple vs. Everything mode partitioning, layout integrity, strict TDD with uv, and live Cloud Run verification.
---

# UI Clarity, Affordance Enforcement & TDD Protocol

This skill governs the design, implementation, and automated testing of user interfaces for web applications, engineering studios (such as Electric Sheep Memory Studio), and high-signal operational consoles. It establishes strict engineering rules to eliminate **dead UI**, **false affordances**, **confetti pills**, **confusing toggles**, and **unexplained engineering jargon**, enforcing complete visual parity with underlying domain and telemetry data models through rigorous Test-Driven Development (TDD) and live production verification.

---

## 1. Core Philosophy & Principles of UI Truthfulness

> **"If it looks like a button, chip, selector, or card, it must do something; if it does nothing, style it like quiet text."**

False affordances break user trust. When an interface element presents interactive visual signifiers—such as rounded borders, elevated surfaces, badge pill containers, hover states, or action icons—the user instinctively expects a responsive interaction (click, drill-down, expansion, or detailed tooltip). In engineering and data studio environments, non-responsive decorations cause cognitive friction and degrade situational awareness.

### The Four Principles of UI Truthfulness:
1. **Interactive Elements Must Respond**: Any component styled as a selector chip, intent pill, elevated card, or button MUST provide meaningful interaction (execute a query, trigger memory consolidation, toggle a viewport view, or inspect a trajectory trace).
2. **Static Data Must Look Static (Quiet Typography)**: Purely informational metadata or telemetry that cannot be tapped or expanded (e.g. query latency `14.2 ms`, confidence score `0.94`, token count `1,240 tokens`) must be presented using **quiet typography**—monospace font (`IBM Plex Mono` / `JetBrains Mono`), tabular figures (`tnum`), inline colored signal dots (`●`), zero elevated pill backgrounds, and no misleading hover cursors.
3. **The Cognitive Overload Principle (Simple vs. Everything Mode)**: A clean engineering studio separates high-level domain workflows from micro-level diagnostic telemetry:
   - **Simple Mode / Macro View (`OVERVIEW` / `STUDIO WORKSPACE`)**: Spotlights core domains (Left nav for core domains, Center for main workspace/stream), overall system status (`NOMINAL` / `SYNCING` / `DEGRADED`), trajectory counters, and governing crystallized memory cards.
   - **Everything Mode / Micro View (`TRACE INSPECTOR` / `DIAGNOSTIC DRAWER`)**: Reveals step-by-step agent trajectory logs, raw JSON payloads, vector similarity distributions, and schema validation diagnostics in an expandable right drawer or detail view.
4. **The High-Value Control Principle (No Frivolous / Confusing Toggles)**: Avoid decorative toggles for internal display flags. Provide **high-value controls** that map to real backend operations:
   - Real Memory Scope Selection (`[GLOBAL BELIEFS]` vs `[WORKSPACE MEMORY]` vs `[SESSION CONTEXT]`).
   - Real Memory Consolidation / Synthesis Execution (`[CONSOLIDATE MEMORY]`).
   - Real Session / State Reset (`[RESET SESSION STATE]`).
   - Real Viewport / Drawer Switching (`OVERVIEW` $\leftrightarrow$ `TRAJECTORY INSPECTOR`).

---

## 2. The Four Mandatory Gates

Before writing code or marking any UI task complete, engineers and agents must pass through four mandatory gates. **Work is NEVER complete until the live version is deployed to Cloud Run (`GOOGLE_CLOUD_PROJECT`) and verified via live HTTP probe.**

```
┌───────────────────────────────────────────────────────────────────────────────────┐
│ GATE 1: Affordance, Clarity & Cognitive Layout Specification                      │
│ • Zero-False-Affordance Rule (every pill/card has action or quiet styling)        │
│ • Plain Language & Three Context Questions (What, Why, What Changed; no enums)   │
│ • Complete Visual Parity Rule (confidence scores, provenance, latency exposed)    │
│ • Confetti Pill Anti-Pattern & Quiet Typography Mandate (colored signal dots)     │
│ • Simple vs. Everything Mode Partitioning (Macro Workspace vs. Micro Inspector)   │
│ • High-Value Controls (Scope Selection, Memory Consolidation, Session Reset)      │
│ • Distraction-Free Layout Integrity (2-3 panel layout, Top App Bar 2-Child Rule)  │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│ GATE 2: Test-Driven Development (TDD) Protocol                                    │
│ • Fast local tests with `uv run pytest -v` (under ~3 seconds)                     │
│ • Domain & state machine tests (`test_domain.py`: state transitions, invariants)  │
│ • API & schema contract tests (`test_api_routes.py`: trajectory serialization)    │
│ • Memory consolidation & retrieval tests (`test_memory_service.py`)               │
│ • UI DOM & layout integrity tests (`test_ui_service.py`: topbar & layout checks)  │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│ GATE 3: Automated Validation & Pre-Commit Review                                  │
│ • `uv run pytest -v` 100% pass rate                                               │
│ • Dead UI DOM audit (no non-interactive divs masquerading as buttons)             │
│ • STRICT NEVER MOCK verification: All flows dynamically wired to real routes       │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│ GATE 4: Deployment & Live Verification Standard (MANDATORY)                       │
│ • Container build and deployment to Cloud Run (GOOGLE_CLOUD_PROJECT in us-central1)│
│ • Verify via live HTTP probe (`curl -s -i <service-url>` / live endpoint scripts) │
│ • Live Playwright browser E2E verification (`test_ui_flow.py`)                    │
│ • Work is NOT done until live service URL is verified and reported to user        │
└───────────────────────────────────────────────────────────────────────────────────┘
```

---

### Gate 1: Affordance, Clarity & Cognitive Layout Specification

#### Rule 1.1: The Zero-False-Affordance Rule
Every UI component featuring any of the following visual attributes is classified as **Affordance-Bearing**:
- Rounded pill backgrounds (`border-radius: 4px` to `12px`)
- Accent outlines or highlighted borders
- Elevated surfaces or button drop shadows
- Hover cursor set to `cursor: pointer`

**Requirement**: Every Affordance-Bearing element **MUST** have an interactive handler:
- An `onclick` handler that triggers an API request, opens a detail modal/drawer, runs memory consolidation, or resets state.
- A tab/view switcher that changes the active workspace or inspector panel.
- At minimum, an accessible contextual tooltip explaining the parameter if no modal is required.

If an element does not warrant interaction or tooltip, you **MUST** strip its pill/card styling and render it as flat, quiet inline text (e.g., `<span class="quiet-param">Confidence: 0.94</span>`).

#### Rule 1.2: The Plain Language & Context Rule
Raw internal enums and cryptic database error codes are **strictly prohibited** in user-facing surfaces.
- **Forbidden**: `ERR_SPANNER_MUTATION_409`, `STATUS_DISPATCH_RUNNING_200`, `MEM_SCOPE_ENUM_02`, raw UUIDs (`c9f28d81-8b31-419b-a010-84cf07289b41`).
- **Required**: Clear, plain-English operational terms (`NOMINAL`, `CONSOLIDATION FAILED (SCHEMA MISMATCH)`, `CRYSTALLIZED BELIEF #042 ACTIVE`).

Furthermore, every status badge, decision pill, or alert card must answer the **Three Context Questions**:
1. **What is it?** A plain-English label of the current operational state.
2. **Why is it in this state?** An inspectable explanation of the underlying cause (e.g. `Promoted from 12 recurring session trajectories with 0.96 confidence`).
3. **What changed / What is next?** A clear indication of the previous state, timestamp, or actionable next step (`Click to inspect source trajectories or demote rule`).

#### Rule 1.3: The Complete Visual Parity Rule
The UI must never conceal rich domain or telemetry metadata. If the underlying data entity includes:
- Memory belief properties (scope, confidence score, promotion timestamp, source session count).
- Trajectory step arrays (tool calls, reasoning steps, token counts, execution latency).
- Evaluation metrics (precision, recall, latency percentiles $p_{50}, p_{95}$, uplift $\Delta\text{Score}$).

The UI **MUST** provide a concrete path for the user to view this data—via the contextual right drawer, trajectory inspector modal, or telemetry tab.

#### Rule 1.4: The "Confetti Pill" Anti-Pattern & Quiet Typography Mandate
Packaging every metric readout into separate rounded capsules creates visual clutter and degrades readability:
- **Forbidden**: Wrapping static readouts in pill containers (e.g. `<div class="pill-badge">Latency: 14.2 ms</div>`).
- **Required**: Use quiet typography with minimal visual container weight:
  - **Status indicators**: Quiet inline signal dot (`●`) with plain text (`● Service Nominal`, `<span class="alarm">● Sync Degraded</span>`).
  - **Metrics / Telemetry Readouts**: Key-value pairs in monospace (`Confidence: 0.94  |  Latency: 14.2 ms  |  Tokens: 1,240`).
  - **Scope Tags**: Simple clean labels (`Scope: Global Developer Beliefs`).

#### Rule 1.5: Simple vs. Everything Mode Partitioning
- **Simple Mode / Macro Workspace**:
  - Highlights core navigation, active memory beliefs, trajectory summary counters, and compact Intent Pills.
  - One-click actions: Filter Scope, Search Memories, Run Consolidation, Reset Filter.
- **Everything Mode / Micro Inspector**:
  - Reveals the step-by-step trajectory JSON inspector, raw prompt/response payloads, vector similarity breakdowns, and evaluation diagnostics.

#### Rule 1.6: Layout Integrity & Distraction-Free Aesthetics
Adhere to the project's `DESIGN.md` layout and aesthetic standards:
- **Layout Architecture**: Strict 2-3 panel layout (Left nav for core domains, Center for main workspace/stream, Right expandable drawer for contextual docs/chat/inspector).
- **Top App Bar & Body Integrity**: Enforce the Top App Bar 2-Child Standard (ensuring `.app-nav` is inside `.app-title-group`) and `.app-body-layout` consistency across all pages so navigation never wraps or breaks structure.
- **Aesthetic**: Distraction-free sepia or academic light styling with readable typography (avoid tiny, unreadable font sizes) and compact Intent Pills instead of bulky static banners.
- **Typography**: Clean sans-serif for UI chrome and prose, monospace with tabular numbers (`tnum`) for metrics, timestamps, and code snippets.

---

### Gate 2: Test-Driven Development (TDD) Protocol

All features must follow strict TDD using `uv run pytest -v`:

1. **Domain & State Machine Tests (`tests/test_domain.py` / `tests/test_memory_service.py`)**:
   - Write tests verifying state transitions, scope isolation, belief crystallization, and error handling before touching UI.
2. **API & Schema Contract Tests (`tests/test_api_routes.py` / `tests/test_schemas.py`)**:
   - Verify that every REST/WebSocket endpoint emits structured responses matching the schema contracts (`step_index`, `session_id`, `confidence`, `status`).
3. **Consolidation & Evaluation Pipeline Tests (`tests/test_consolidation.py`)**:
   - Test trajectory clustering, belief promotion rules, and evaluation metrics.
4. **UI Layout & DOM Integrity Tests (`tests/test_ui_service.py`)**:
   - Verify layout integrity (`test_ui_service_topbar_and_layout_integrity`) and assert that all interactive controls are wired to real endpoints.

---

### Gate 3: Automated Validation & Pre-Commit Review

- Run the full suite: `uv run pytest -v` (must execute in ~3s with 100% pass rate).
- Inspect DOM: Ensure zero decorative containers masquerade as interactive buttons.
- STRICT NEVER MOCK check: Verify zero simulated fallbacks (`simulateInference()`, `Math.random()`, dummy arrays) in server endpoints or UI scripts.

---

### Gate 4: Deployment & Live Verification Standard (MANDATORY)

A task involving services or web applications is **NOT complete until built, deployed to Cloud Run (`GOOGLE_CLOUD_PROJECT`), and verified via live HTTP probe**:
1. Build container image and deploy service to Cloud Run in `us-central1` (e.g., `./scripts/deploy_cloud_run_source.sh`).
2. Probe live endpoints with `curl -s -i <service-url>` and project validation scripts (e.g., `python3 -m scripts.validate_live_endpoints`) to confirm HTTP 200 across all routes.
3. Execute Playwright browser verification against the live endpoint.
4. Always provide the live service URL to the user in the final report.

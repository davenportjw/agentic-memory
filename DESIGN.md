# Google Executive & Admin Control Center Design Standard
**Definitive UI Architecture, Visual Aesthetics & Design Principles for `electric-sheep` and `smartrouter` (`gemini-router`)**

---

## 1. Executive Summary & Design Philosophy

This document (`DESIGN.md`) defines the unified **Google Executive & Admin Control Center Design Standard**, establishing the core visual aesthetics, component typography, layout engineering, and interaction principles for **`electric-sheep`** (Google ADK 2.0 Multi-Agent Demo Control Center) and its shared reference approach with **`smartrouter`** (`gemini-router`).

### 1.1 Reference Default (`smartrouter`)
When evaluating design patterns, navigational structure, color palettes, or component styling, **`smartrouter`** is the default reference model. Both systems share:
- **Professional Administrative Density**: Engineered for operators, architects, and autonomous agents requiring high-density telemetry, real-time streaming inspection, and precise control without visual clutter.
- **Pristine Editorial Light Mode**: An off-white alabaster/cream canvas paired with crisp white and cool slate glassmorphism surfaces, ensuring maximum readability and reduced eye strain over long operational sessions.
- **Strict Color Hygiene**: Eliminating heavy saturated warm tones (zero yellow/amber saturation rule) in favor of cool Swiss editorial slates, royal blue/indigo gradients for brand identity, emerald/teal for live/active states, and cool crimson/ruby for anomaly highlights and destructive actions.

---

## 2. Core Visual Aesthetics & Color Contract

### 2.1 The Editorial Light-Mode Color Palette
All user interfaces MUST strictly adhere to the light-mode color tokens defined in `src/ui_service/static/style.css`. No dark-mode overrides or saturated warm colors are permitted.

| Token Name | CSS Custom Property | Color Value / Hex | Purpose / Usage |
| :--- | :--- | :--- | :--- |
| **Canvas Background** | `--bg-color` | `#f8fafc` (`hsl(210, 20%, 98%)`) | Main viewport background canvas, stage container backgrounds |
| **Surface Card** | `--surface-color` | `#ffffff` (`hsl(0, 0%, 100%)`) | Primary card containers, modals, table backgrounds |
| **Glassmorphic Surface** | `--surface-glass` | `rgba(255, 255, 255, 0.88)` | Sticky header (`.app-bar`) and floating controls with backdrop blur |
| **Slate Glass** | `--slate-glass` | `rgba(240, 244, 248, 0.75)` | Subtle panels, secondary code/info background sections |
| **Primary Brand** | `--primary-color` | `#1a73e8` (`hsl(215, 90%, 52%)`) | Primary buttons, active navigation markers, key links |
| **Primary Hover** | `--primary-hover` | `#1557b0` | Hover state for primary interactive elements |
| **Primary Light Tint** | `--primary-light` | `#e8f0fe` | Model badge backgrounds, subtle active step highlights |
| **Accent Success** | `--accent-color` | `#0f9d58` (`hsl(158, 64%, 40%)`) | Spanner cache hits (`< 10ms SLA`), success badges, live status |
| **Accent Light Tint** | `--accent-light` | `#e6f4ea` | Success chip backgrounds, live status indicators |
| **Highlight Brand (Dream)** | `--highlight-color` | `#6366f1` (`hsl(238, 83%, 67%)`) | Dream Harvest REM Synthesis highlights and specialized actions |
| **Highlight Light Tint** | `--highlight-light` | `#eef2ff` | Dream Harvest card accents, REM info block backgrounds |
| **Alert / Crimson** | `--warn-color` | `#d93025` (`hsl(348, 83%, 47%)`) | Alert states, warnings, error chips, destructive operations |
| **Alert Light Tint** | `--warn-light` | `#fce8e6` | Alert chip backgrounds and warning notice cards |
| **Primary Text** | `--text-primary` | `#1e293b` (`hsl(215, 25%, 15%)`) | Primary headings, body copy, table cell content |
| **Secondary Text** | `--text-secondary` | `#475569` (`hsl(215, 16%, 35%)`) | Subtitles, helper text, timestamps, table headers |
| **Border Slate** | `--border-color` | `#e2e8f0` (`hsl(214, 32%, 91%)`) | Card borders, table dividers, input borders |
| **Border Light** | `--border-light` | `#f1f5f9` | Subtle row separators and card header dividers |

### 2.2 Strict Color Hygiene (Zero Yellow/Amber Saturation Rule)
- **Prohibited**: Saturated amber, yellow, or orange backgrounds, borders, or text (e.g., `#f2994a`, `#fef8e7`, `#b06000`).
- **Replacement**: Warnings, special callouts, and REM Dream Harvest features MUST use either **Cool Indigo/Violet** (`--highlight-color: #6366f1`, `--highlight-light: #eef2ff`) or **Cool Crimson** (`--warn-color: #d93025`, `--warn-light: #fce8e6`).

### 2.3 Typography & Type Hierarchy
- **Primary Interface Font**: `'Roboto', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`.
- **Monospace & Code Font**: `'JetBrains Mono', 'Fira Code', monospace`. Used exclusively for JSON payloads, BigQuery SQL traces, Spanner schemas, latency metrics (`< 10ms`), and timestamps.
- **Type Scale**:
  - Top App Bar Title: `1.35rem` (`21.6px`), semi-bold (`600`), tight letter-spacing (`-0.3px`).
  - Card Title (`h2`): `1.25rem` (`20px`), bold (`700`).
  - Section Subtitle (`.card-subtitle`): `0.92rem`, regular (`400`), `--text-secondary`.
  - Table Headers & Badges: `0.78rem` – `0.85rem`, medium (`500`), uppercase or title case.

---

## 3. UI Architecture & Layout Engineering

### 3.1 Sticky Glassmorphic App Bar (`.app-bar`)
- Positioned at `top: 0` with `position: sticky` and `z-index: 1000`.
- Utilizes layered glassmorphism:
  ```css
  background-color: var(--surface-glass);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  ```
- With `@supports not (backdrop-filter: blur(12px))` fallback to solid `--surface-color`.
- Embeds the active model badge (`Model: Gemini 3.5 Flash Lite`), user/session identity inputs (`User ID`, `Session ID`), and an interactive help tooltip explaining multi-user crystalline memory vs episodic BigQuery traces.

### 3.1.1 Top App Bar 2-Child Layout Standard (Zero-Wrap Guarantee)
- **Problem statement**: When `.app-bar-content` has `justify-content: space-between`, adding more than two direct children (e.g., placing `.app-nav` as a separate sibling between `.app-title-group` and `.session-info`) causes flexbox to distribute three items across the full bar width. On normal or narrow viewports, the middle navigation element wraps onto an awkward second line ("ugly top bar").
- **Required DOM structure**: `.app-bar-content` MUST contain exactly **two direct children**:
  1. `div.app-title-group` on the left, containing the main heading (`h1`), the header navigation pill bar (`nav.app-nav`), and the model badge (`span.model-badge`).
  2. `div.session-info` on the right, containing the User ID, Session ID, and interactive info tooltip.
- **Single-Line Header Consolidation (Zero-Wrap Guarantee)**: To ensure `.app-bar-content` NEVER wraps onto a second line, `.app-bar-content` uses `flex-wrap: nowrap;`. The main heading is consolidated to `<h1>Google ADK 2.0</h1>` (avoiding redundancy with the "Control Center" navigation button), and session/user input widths are optimized (`130px`, shrinking to `110px` on narrower viewports) to keep the entire top bar on a single line across all viewports.
- **Why this works**: With exactly two direct children and `flex-wrap: nowrap`, `justify-content: space-between` places the title/nav group cleanly on the left and session controls on the right, eliminating lopsided wrapping and ensuring a pristine single-line administrative header.

### 3.1.2 Page-to-Page Structural Consistency (`.app-body-layout`)
- **Problem statement**: Navigating between primary views (e.g., from `index.html` Control Center to `how-it-works.html` Architecture Page) must never cause visual detachment or severe layout shifts that make users feel clicking a tab "breaks the rest of the page".
- **Required DOM structure**: All primary control center pages MUST share the exact same structural container hierarchy:
  - `<main class="app-body-layout">` enclosing:
    - `<aside class="left-nav">`: Sticky Table of Contents customized for the active page (`STAGE PROGRESSION` on `index.html`, `HOW IT WORKS` on `how-it-works.html`).
    - `<section class="single-pane-container">`: Continuous content pane displaying the cards for that view.
- **Why this works**: Preserving `.app-body-layout` and `.left-nav` across pages ensures zero horizontal jump when switching tabs, maintaining an integrated, single-application feel.

### 3.1.3 Automated Test Verification Contract
- **Testable Engineering Rule**: To prevent regressions where `.app-bar` is lopsided or secondary pages omit `.app-body-layout`, the automated test suite in `tests/test_ui_service.py` MUST execute programmatic DOM assertions (`test_ui_service_topbar_and_layout_integrity()`) verifying:
  1. `.app-bar-content` in `index.html` and `how-it-works.html` contains `<nav class="app-nav">` inside `.app-title-group`.
  2. Both `index.html` and `how-it-works.html` contain `<main class="app-body-layout">` with `<aside class="left-nav">` and `<section class="single-pane-container">`.
  3. Clicking or loading any tab never produces broken anchors or missing target IDs.

### 3.2 Split Navigation / Stage Progression Layout (`.app-body-layout`)
The control center follows a responsive split-pane architecture:
```
+------------------------------------------------------------------------------------+
| Sticky App Bar: Google ADK 2.0                | Model: Gemini 3.5 Flash Lite      |
+------------------------------------+-----------------------------------------------+
| ASIDE: Left Navigation Sidebar     | MAIN SECTION: Continuous Stage 1-5 Long Page  |
|                                    |                                               |
| STAGE PROGRESSION                  | +-------------------------------------------+ |
| 1. Interview Agent [Active]        | | Card 1: Interview Agent                   | |
|    |                               | | (Profile Builder, Waking Life Chat)       | |
| 2. Telemetry Logs                  | +-------------------------------------------+ |
|    |                               |                                               |
| 3. Dream Harvest                   | +-------------------------------------------+ |
|    |                               | | Card 2: BigQuery Telemetry Logs           | |
| 4. Spanner Memory                  | | (Real-Time Traces & Event Table)          | |
|    |                               | +-------------------------------------------+ |
| 5. Preference Agent                |                                               |
|                                    | +-------------------------------------------+ |
| 💡 Click any item to jump         | | Card 3: Dream Harvest                     | |
|                                    | | (REM Synthesis & BigQuery AI.GENERATE)    | |
|                                    | +-------------------------------------------+ |
|                                    |                                               |
|                                    | +-------------------------------------------+ |
|                                    | | Card 4: Google Cloud Spanner Memory     | |
|                                    | | (Crystalline Long-Term Beliefs Matrix)    | |
|                                    | +-------------------------------------------+ |
|                                    |                                               |
|                                    | +-------------------------------------------+ |
|                                    | | Card 5: Preference Agent                | |
|                                    | | (< 10ms Spanner Cache Recall SLA)       | |
|                                    | +-------------------------------------------+ |
+------------------------------------+-----------------------------------------------+
```

- **Left Navigation Sidebar (`aside.left-nav`)**:
  - Sticky table-of-contents tracking stages 1 through 5.
  - Step icons (`.nav-step-icon`) with connecting vertical lines (`.nav-connector`).
  - Active stage highlighting with primary color borders and font weights.
- **Main Single-Pane Container (`section.single-pane-container`)**:
  - Contains all 5 stage cards arranged vertically with generous gap spacing (`32px`).
  - Employs `scroll-behavior: smooth` so clicking left navigation items smoothly scrolls to the target stage.

---

## 4. Stage 1–5 Component Specifications & Interaction Patterns

### 4.1 Stage 1: Interview Agent (Waking Life & Short-Term Chat)
- **Role**: Profile builder interactive chat terminal.
- **Components**:
  - Chat Box (`#interviewChatBox`) with distinct message bubbles for `InterviewAgent` (slate background) and `user` (primary blue background).
  - Collapsible Architecture & Code Inspector (`details.code-arch-collapse`) displaying syntax-highlighted Python snippets from `src/interview_agent/agent.py`.
- **Behavior**: Every user response is recorded in short-term memory and streamed immediately to BigQuery telemetry.

### 4.2 Stage 2: BigQuery Telemetry Logs (Episodic Traces)
- **Role**: Real-time event trace inspector for `adk_agent_telemetry.agent_logs`.
- **Components**:
  - Responsive table wrapper (`.table-wrapper`) with sticky column headers.
  - Structured columns: `Event` (`USER_PROMPT` or `AGENT_RESPONSE`), `Agent`, and `Message` / JSON payload summary.
  - Live Refresh button (`#refreshLogsBtn`) to query recent events.

### 4.3 Stage 3: Dream Harvest (REM Memory Consolidation)
- **Role**: Offline REM memory synthesis triggering BigQuery AI / `ML.GENERATE_TEXT` to distill raw logs into structured JSON preferences.
- **Components**:
  - Card Accent: Styled with Cool Indigo/Violet (`--highlight-color: #6366f1`) to visually represent sleep/dream consolidation.
  - Action Button (`#harvestBtn`): Vibrant indigo button with hover elevation.
  - Status Output Box (`#harvestStatus`): Displays real-time progress, distillation count, and Spanner sync confirmation.

### 4.4 Stage 4: Cloud Spanner Memory Matrix (Crystalline Beliefs)
- **Role**: Long-term relational memory viewer (`UserPreferences`).
- **Components**:
  - Table displaying `Key`, `Value`, `Category`, and `Conf.` (Confidence Score).
  - Monospaced key display and high-contrast confidence badges.
  - Interactive Refresh button (`#refreshSpannerBtn`).

### 4.5 Stage 5: Preference Agent (Sub-10ms Spanner Fast Cache)
- **Role**: Cache-first agent querying Spanner before falling back to LLM inference.
- **Components**:
  - Fast-Path Badge: `.chip-success` with text `Spanner Fast Cache` and animated pulsing status dot (`< 10ms SLA`).
  - Chat Box (`#preferenceChatBox`) rendering instantaneous cache hits alongside latency metrics.

---

## 5. Modern Web Development Best Practices

Synthesized from `modern-web-guidance` and industry standards:
1. **Layout & Performance (INP Optimization)**:
   - Avoid layout recalculations in data-heavy containers by using CSS Grid and `content-visibility: auto` on complex offscreen stage panels where appropriate.
   - Use `contain: layout style` on card containers to prevent style thrashed reflows.
2. **Glassmorphism & Backdrop Filtering**:
   - Apply `backdrop-filter: blur(12px)` with fallback background opacity (`rgba(255, 255, 255, 0.96)`) for browser compatibility.
3. **Micro-Animations & Status Feedback**:
   - Subtle button hover elevation (`box-shadow: var(--elevation-2); transform: translateY(-1px);`).
   - Active state tactile compression (`active: scale(0.98)`).
   - Animated pulsing status dots (`.status-dot.animate-pulse`) for active cache SLAs.
4. **Accessibility (WCAG 2.1 AA Compliance)**:
   - Contrast ratios minimum 4.5:1 for body text and 3:1 for large text/icons.
   - Reduced motion fallbacks:
     ```css
     @media (prefers-reduced-motion: reduce) {
       * {
         animation-duration: 0.01ms !important;
         transition-duration: 0.01ms !important;
       }
     }
     ```
   - Clear focus outlines (`focus-visible`) on all interactive form inputs and buttons.
5. **Agent Prompt & Response Code Formatting (Markdown Code Blocks)**:
   - All agent prompts and responses containing code, SQL queries, JSON schemas, or technical commands MUST be formatted inside fenced markdown code blocks (` ```language ... ``` `).
   - In UI chat containers (`.chat-box .msg`), code blocks should render within dedicated code boxes (`.code-snippet-box` or `<pre><code>`) using monospace fonts (`var(--code-font)`) and syntax highlighting.

---

## 6. Deployed State Testing & Deployment Policy (`AGENTS.md` Compliance)

1. **Test in Deployed State**: Any UI, HTML, CSS, JavaScript, or agent prompt change MUST be tested in the deployed state on Google Cloud Run. Local testing (`uv run pytest` / local web server) is required but not sufficient on its own.
2. **Deployment as Final Step**: Deployment to Google Cloud Run using `./scripts/deploy_cloud_run_source.sh` (or CI/CD) MUST always be the final step of any UI feature or bugfix workflow.
3. **Validate Live Production Endpoints**: After deploying to Google Cloud Run, execute:
   ```bash
   python3 -m scripts.validate_live_endpoints
   ```
   to verify that all 7 live REST API endpoints in `davenport-boutique` respond correctly.

---
name: ui-playwright-reviewer
description: >-
  Automated UI reviewer skill using Playwright browser control to validate web applications,
  Memory Studio layouts, topbar navigation integrity, interactive workflows, real-time state updates,
  and live Cloud Run endpoints.
---

# UI Playwright Reviewer Skill

This skill defines the rigorous protocol for automated visual, structural, and functional verification of web applications and engineering studios (such as **Electric Sheep Memory Studio**) using Playwright browser automation.

---

## 1. Purpose & Scope

Whenever frontend UI components, layout styles, API integrations, or Cloud Run deployments are modified, this reviewer skill:
1. Connects to the local test server (e.g., `http://127.0.0.1:8080`) or the live Cloud Run deployment in your target `GOOGLE_CLOUD_PROJECT`.
2. Directs a headless Chromium browser instance (`playwright.async_api` / `sync_api`) at standard desktop console viewport (`1400x900`).
3. Asserts DOM structure and layout integrity:
   - Top App Bar 2-Child Standard (ensuring `.app-nav` is inside `.app-title-group`) and `.app-body-layout` consistency across pages.
   - 2-3 Panel Studio Layout: Left navigation panel for core domains, Center workspace/stream (`#memory-stream`, `#trajectory-view`), and Right expandable drawer (`#context-drawer`) for contextual docs, chat, and trace inspection.
   - Interactive controls: Scope selector pills (`.intent-pill`), search/query inputs (`#search-input`), and action triggers (`#btn-consolidate`, `#btn-reset`).
4. Executes the complete end-to-end user journey:
   - Baseline state bootstrap $\to$ verifies active beliefs, session trajectory counters, and navigation links without layout wrapping.
   - Interactive query or trajectory ingestion $\to$ dynamic REST/WebSocket request $\to$ real-time UI state update with zero mock fallbacks.
   - Memory crystallization / consolidation trigger $\to$ extracts recurring patterns from trajectories $\to$ promotes crystallized belief card with provenance metadata and confidence score.
   - Scope filtering (`GLOBAL` vs `WORKSPACE` vs `SESSION`) $\to$ verifies strict scope isolation and dynamic view filtering.
   - Session state reset or filter clear $\to$ restores default studio workspace cleanly.
5. Asserts zero uncaught browser exceptions and zero console error logs.
6. Captures timestamped PNG screenshots in `screenshots/` for visual audit logs.

---

## 2. Test Execution Commands

```bash
# Run DOM & layout integrity unit/service tests
uv run pytest -v tests/test_ui_service.py

# Run full E2E Playwright browser test suite
uv run pytest -v tests/e2e/test_ui_flow.py

# Validate live production endpoints
python3 -m scripts.validate_live_endpoints
```

---

## 3. Playwright Verification Script Standard

```python
"""Automated Playwright verification script for Web & Memory Studio UI."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

SCREENSHOTS_DIR = Path("screenshots")
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

TARGET_URL = "http://127.0.0.1:8080"  # Or live Cloud Run service URL

async def verify_studio_ui():
    console_errors = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

        # 1. HTTP 200 Probe & Layout Integrity Bootstrap
        res = await page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
        assert res and res.status == 200, f"HTTP Error: {res.status if res else 'None'}"
        await page.wait_for_selector(".app-title-group .app-nav", timeout=10000)
        await page.wait_for_selector(".app-body-layout", timeout=10000)

        # 2. Baseline Workspace Verification
        await page.screenshot(path=str(SCREENSHOTS_DIR / "01_baseline_workspace.png"))

        # 3. Trigger Scope Selection or Interactive Search
        scope_pill = page.locator(".intent-pill").first
        if await scope_pill.count() > 0:
            await scope_pill.click()

        # 4. Trigger Memory Consolidation / Live Action
        consolidate_btn = page.locator("#btn-consolidate")
        if await consolidate_btn.count() > 0 and await consolidate_btn.is_enabled():
            await consolidate_btn.click()
            await page.wait_for_load_state("networkidle")
            await page.screenshot(path=str(SCREENSHOTS_DIR / "02_memory_consolidated.png"))

        # 5. Open Contextual Inspector Drawer
        trace_card = page.locator(".trajectory-card, .memory-card").first
        if await trace_card.count() > 0:
            await trace_card.click()
            await page.screenshot(path=str(SCREENSHOTS_DIR / "03_inspector_drawer.png"))

        # 6. Zero Console Errors
        assert len(console_errors) == 0, f"Detected browser errors: {console_errors}"
        await browser.close()

if __name__ == "__main__":
    asyncio.run(verify_studio_ui())
```

---

## 4. Verification Checklist

- [ ] **HTTP Probe**: Returns HTTP 200 from local server or live Cloud Run service.
- [ ] **Topbar & 2-3 Panel Layout Balance**: `.app-nav` resides inside `.app-title-group`, and `.app-body-layout` panels render without clipping or horizontal scrollbars at `1400x900`.
- [ ] **Dynamic Endpoint Wiring**: All interactive buttons, chips, and forms trigger real backend API routes with zero mock fallbacks.
- [ ] **Trajectory & Provenance Inspector**: Clicking a memory card or trajectory row reveals structured JSON metadata, confidence scores, and source session links.
- [ ] **Scope Isolation**: Switching between Global, Workspace, and Session scopes updates the stream accurately without layout shifts.
- [ ] **Readable Academic / Studio Aesthetic**: Distraction-free light/sepia styling, quiet monospace telemetry (`tnum`), and compact Intent Pills.
- [ ] **Zero Console Errors**: Clean browser console with zero unhandled JavaScript exceptions or failed network requests.
- [ ] **Visual Audit Trail**: PNG screenshots captured in `screenshots/` for each key operational milestone.

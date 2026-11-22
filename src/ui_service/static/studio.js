/**
 * Enterprise Memory Studio Frontend Controller (studio.js)
 * Dynamically wires all Studio workspaces to live memory store, evaluation, and tuning APIs.
 * Strictly adheres to DESIGN.md: Zero mock fallbacks, zero yellow/amber palette.
 */

const studioState = {
  activeScopeFilter: "ALL",
  memorySearchQuery: "",
  leftTreeSearchQuery: "",
  memoryCategoryFilter: "ALL",
  activeSignalFilter: "ALL",
  trajectoryCandidates: [],
  rawTelemetryEvents: [],
  ingestConfig: null,
  categories: [],
  syncStatus: null,
  scopesData: { ORG: [], TEAM: [], AGENT: [], USER: [] },
  federatedData: null,
  activeConfig: null,
  evalCases: [],
  lastScorecard: null,
  evolveArchive: null,
  championCandidate: null,
  inspectedConfigCandidate: null,
  quickEditedMemory: null,
  authenticatedEmail: "",
  sessionId: `studio-sess-${Date.now().toString(36)}`,
};

function getActiveContextFilters() {
  const rawUser = (document.getElementById("userIdInput")?.value || "").trim();
  return {
    team_id: (document.getElementById("teamIdInput")?.value || "").trim(),
    agent_id: (document.getElementById("agentIdInput")?.value || "").trim(),
    user_id: rawUser ? (rawUser.includes(":") ? rawUser : `user:${rawUser}`) : "",
  };
}

function getDefaultEntityScopeId(scopeType, fallbackId) {
  const targetType = String(scopeType || "USER").toUpperCase();
  const entities = Array.isArray(wsState?.entities) ? wsState.entities : [];
  const match = entities.find((e) => String(e.scope_type || "").toUpperCase() === targetType);
  return (match && match.scope_id) || fallbackId;
}

function resolveStudioUserScope() {
  const userEl = document.getElementById("userIdInput");
  const typed = userEl && userEl.value.trim();
  if (typed) return typed.includes(":") ? typed : `user:${typed}`;
  if (studioState.authenticatedEmail) {
    return `user:${studioState.authenticatedEmail}`;
  }
  try {
    const stored = sessionStorage.getItem("electric_sheep_user_email");
    if (stored && stored.trim()) return `user:${stored.trim()}`;
  } catch (_) {}
  return getDefaultEntityScopeId("USER", "user:developer");
}

function getScopeIds() {
  const teamEl = document.getElementById("teamIdInput");
  const agentEl = document.getElementById("agentIdInput");
  return {
    user_id: resolveStudioUserScope(),
    team_id: (teamEl && teamEl.value.trim()) || getDefaultEntityScopeId("TEAM", "team:platform-ml"),
    agent_id: (agentEl && agentEl.value.trim()) || getDefaultEntityScopeId("AGENT", "agent:code-reviewer-agent"),
    org_id: getDefaultEntityScopeId("ORG", "org:acme-org"),
  };
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showStatusBanner(message, isError = false) {
  const banner = document.getElementById("zeroResetStatus");
  if (!banner) return;
  banner.classList.remove("hidden", "status-error", "status-ok");
  banner.classList.add(isError ? "status-error" : "status-ok");
  banner.textContent = message;
}

const STUDIO_MODE_TITLES = {
  "stage-1-sandbox": "Import Logs",
  "stage-2-scopes": "Author Rules",
  "stage-3-eval": "Test Recall",
  "stage-4-evolve": "Save & Tune",
};

const STUDIO_STAGE_SHORT_LABELS = {
  "stage-1-sandbox": "Import Logs",
  "stage-2-scopes": "Author Rules",
  "stage-3-eval": "Test Recall",
  "stage-4-evolve": "Save & Tune",
};

const STUDIO_MODE_DESCS = {
  "stage-1-sandbox": "Import chat logs, inspect raw telemetry turns, and extract candidate memories with AI.",
  "stage-2-scopes": "Create or edit memories, categories, and trigger rules across Organization, Team, Agent, and User scopes.",
  "stage-3-eval": "Run test prompts to verify which memories trigger and measure how many prompt tokens they consume.",
  "stage-4-evolve": "Commit staged memories with 768d embeddings and auto-tune retrieval settings.",
};

const WORKSPACE_TITLES = {
  "workspace-sessions": "Sessions",
  "workspace-tree": "Memories",
  "workspace-playground": "Context Trace",
  "workspace-eval": "Optimize",
  "workspace-jobs": "Jobs",
  "workspace-entities": "Entities & Scope Hierarchy",
  "workspace-categories": "Memory Categories & Retention",
  "workspace-ingest": "Data Ingest & Live Telemetry",
  "workspace-extractors": "Memory Extractors & Co-Designer",
  "workspace-backend": "Memory Backend & Write-Back",
  "workspace-security": "Security & Access Control",
};

const WORKSPACE_DESCS = {
  "workspace-sessions": "Browse session turns, inspect memory attribution, and see which memories helped",
  "workspace-tree": "Explore the 4-tier memory hierarchy and review staged commits before saving",
  "workspace-playground": "Test live prompts, inspect retrieved memories, and compare token formats",
  "workspace-eval": "Benchmark memory quality vs. token cost and auto-tune retrieval settings",
  "workspace-jobs": "Monitor, start, cancel, and inspect background compaction, auto-tune, ingest, and transform jobs",
  "workspace-entities": "Manage Organization, Team, Agent, and User scope entities",
  "workspace-categories": "Define topic categories and default similarity thresholds",
  "workspace-ingest": "Configure activity log and session transcript ingestion connectors",
  "workspace-extractors": "Customize AI memory extraction rules with the Gemini 4 Neon Co-Designer",
  "workspace-backend": "Inspect store sync health and rollback agent memory mutations",
  "workspace-security": "Configure access rules and check scope-level read/write permissions",
};

const WORKSPACE_TO_STAGE_MAP = {
  "workspace-sessions": "stage-1-sandbox",
  "workspace-ingest": "stage-1-sandbox",
  "workspace-tree": "stage-2-scopes",
  "workspace-entities": "stage-2-scopes",
  "workspace-categories": "stage-2-scopes",
  "workspace-extractors": "stage-2-scopes",
  "workspace-playground": "stage-3-eval",
  "workspace-eval": "stage-3-eval",
  "workspace-jobs": "stage-4-evolve",
  "workspace-backend": "stage-4-evolve",
  "workspace-security": "stage-4-evolve",
};

function closeAllToolbarDropdowns() {
  document.querySelectorAll("details.toolbar-dropdown[open]").forEach((d) => {
    d.removeAttribute("open");
  });
  document.querySelectorAll("[popover]:popover-open").forEach((pop) => {
    try {
      if (typeof pop.hidePopover === "function") pop.hidePopover();
    } catch (_) {}
  });
}

function switchStudioMode(targetStageId) {
  if (!targetStageId) return;
  closeAllToolbarDropdowns();

  document.querySelectorAll(".studio-workspace-view").forEach((ws) => {
    ws.classList.add("hidden");
    ws.classList.remove("active-workspace");
  });
  document.querySelectorAll(".studio-workspace-nav-btn").forEach((btn) => {
    btn.classList.remove("active");
    btn.setAttribute("aria-selected", "false");
  });
  document.querySelectorAll(".studio-lifecycle-step").forEach((btn) => {
    btn.classList.remove("active");
    btn.removeAttribute("aria-current");
  });
  document.querySelectorAll(".studio-stage-panel").forEach((panel) => {
    const isTarget = panel.id === targetStageId;
    panel.classList.toggle("active-stage", isTarget);
    panel.classList.toggle("hidden", !isTarget);
  });
  document.querySelectorAll(".studio-intent-tab").forEach((item) => {
    const isTarget = item.getAttribute("data-stage") === targetStageId;
    item.classList.toggle("active", isTarget);
    item.setAttribute("aria-selected", isTarget ? "true" : "false");
  });
  // Contextual Command Bar: Show ONLY the command group matching the active top intent tab
  document.querySelectorAll(".studio-cmd-group").forEach((grp) => {
    const forStage = grp.getAttribute("data-for-stage");
    grp.classList.toggle("hidden", forStage !== targetStageId);
  });
  const commitsTabBtn = document.getElementById("tabBtnCommitsInspector");
  if (commitsTabBtn) commitsTabBtn.classList.add("hidden");

  const modeLabel = document.getElementById("activeStudioModeLabel");
  if (modeLabel && STUDIO_MODE_TITLES[targetStageId]) {
    modeLabel.textContent = STUDIO_MODE_TITLES[targetStageId];
  }
  const modeDesc = document.getElementById("activeStudioIntentDesc");
  if (modeDesc && STUDIO_MODE_DESCS[targetStageId]) {
    modeDesc.textContent = STUDIO_MODE_DESCS[targetStageId];
  }

  const stagePill = document.getElementById("activeStageDropdownLabel");
  if (stagePill && STUDIO_STAGE_SHORT_LABELS[targetStageId]) {
    stagePill.textContent = STUDIO_STAGE_SHORT_LABELS[targetStageId];
  }
  const viewBadge = document.getElementById("toolbarViewModeBadge");
  if (viewBadge) {
    viewBadge.textContent = "Quick Walkthrough";
  }
  const viewTitle = document.getElementById("toolbarCurrentTitle");
  if (viewTitle && STUDIO_MODE_TITLES[targetStageId]) {
    viewTitle.textContent = STUDIO_MODE_TITLES[targetStageId];
  }
  const viewSub = document.getElementById("toolbarCurrentSubtitle");
  if (viewSub && STUDIO_MODE_DESCS[targetStageId]) {
    viewSub.textContent = STUDIO_MODE_DESCS[targetStageId];
  }
}

function switchInspectorTab(tabName) {
  const detailBtn = document.getElementById("tabBtnContextDetail");
  const commitsBtn = document.getElementById("tabBtnCommitsInspector");
  const settingsBtn = document.getElementById("tabBtnRunSettings");
  const jsonBtn = document.getElementById("tabBtnJsonInspector");
  const guideBtn = document.getElementById("tabBtnStudioGuide");

  const detailPane = document.getElementById("inspectorContextDetailPane");
  const commitsPane = document.getElementById("inspectorCommitsPane");
  const settingsPane = document.getElementById("inspectorRunSettingsPane");
  const jsonPane = document.getElementById("inspectorJsonPane");
  const guidePane = document.getElementById("inspectorGuidePane");

  const isDetail = tabName === "detail";
  const isCommits = tabName === "commits";
  const isJson = tabName === "json";
  const isGuide = tabName === "guide";
  const isSettings = tabName === "settings" || (!isDetail && !isCommits && !isJson && !isGuide);

  if (detailBtn) {
    detailBtn.classList.toggle("active", isDetail);
    detailBtn.setAttribute("aria-selected", isDetail ? "true" : "false");
  }
  if (commitsBtn) {
    commitsBtn.classList.toggle("active", isCommits);
    commitsBtn.setAttribute("aria-selected", isCommits ? "true" : "false");
  }
  if (settingsBtn) {
    settingsBtn.classList.toggle("active", isSettings);
    settingsBtn.setAttribute("aria-selected", isSettings ? "true" : "false");
  }
  if (jsonBtn) {
    jsonBtn.classList.toggle("active", isJson);
    jsonBtn.setAttribute("aria-selected", isJson ? "true" : "false");
  }
  if (guideBtn) {
    guideBtn.classList.toggle("active", isGuide);
    guideBtn.setAttribute("aria-selected", isGuide ? "true" : "false");
  }

  if (detailPane) detailPane.classList.toggle("hidden", !isDetail);
  if (commitsPane) commitsPane.classList.toggle("hidden", !isCommits);
  if (settingsPane) settingsPane.classList.toggle("hidden", !isSettings);
  if (jsonPane) jsonPane.classList.toggle("hidden", !isJson);
  if (guidePane) guidePane.classList.toggle("hidden", !isGuide);
}

function formatInspectorLabel(rawKey) {
  return String(rawKey || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function renderStructuredInspectorRows(payloadObj) {
  if (!payloadObj || typeof payloadObj !== "object") {
    return `<div class="inspector-kv-row"><span class="inspector-kv-key">Result</span><span class="inspector-kv-val">${escapeHtml(String(payloadObj ?? "—"))}</span></div>`;
  }
  const entries = Object.entries(payloadObj).filter(([, val]) => val !== undefined && val !== null && val !== "");
  if (entries.length === 0) {
    return `<div class="text-xs text-muted">No details recorded.</div>`;
  }
  return entries
    .map(([key, val]) => {
      let displayVal = "";
      if (key === "embedding" && Array.isArray(val)) {
        displayVal = `${val.length} dims`;
      } else if (Array.isArray(val)) {
        displayVal = val.length === 0 ? "—" : val.map((item) => (typeof item === "object" ? Object.values(item).slice(0, 2).join(": ") : String(item))).join(", ");
      } else if (typeof val === "object") {
        displayVal = Object.entries(val)
          .slice(0, 4)
          .map(([k, v]) => `${formatInspectorLabel(k)}: ${v}`)
          .join(" · ");
      } else {
        displayVal = String(val);
      }
      return `<div class="inspector-kv-row"><span class="inspector-kv-key">${escapeHtml(formatInspectorLabel(key))}</span><span class="inspector-kv-val">${escapeHtml(displayVal)}</span></div>`;
    })
    .join("");
}

function openInspectorDrawer(title, subtitle, payloadObj, promotableConfig = null) {
  const drawer = document.getElementById("studioInspectorDrawer");
  const titleEl = document.getElementById("inspectorTitle");
  const subEl = document.getElementById("inspectorSubtitle");
  const contentEl = document.getElementById("inspectorContent");
  const actionsEl = document.getElementById("inspectorActions");

  if (!drawer || !contentEl) return;
  drawer.classList.remove("collapsed");
  switchInspectorTab("json");
  if (titleEl) titleEl.textContent = title || "Record Details";
  if (subEl) {
    subEl.textContent = subtitle || "";
    subEl.classList.toggle("hidden", !subtitle);
  }
  contentEl.innerHTML = renderStructuredInspectorRows(payloadObj);

  studioState.inspectedConfigCandidate = promotableConfig;
  if (actionsEl) {
    if (promotableConfig) {
      actionsEl.classList.remove("hidden");
    } else {
      actionsEl.classList.add("hidden");
    }
  }
}

function updateActiveConfigSidebar(cfg) {
  if (!cfg) return;
  studioState.activeConfig = cfg;
  const dim = Number(cfg.embedding_dimensions || 768);
  const fmt = cfg.injection_format || "COMPACT_BULLET";
  const thresh = Number(cfg.retrieval_cosine_threshold || 0.65);

  const setText = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  };

  setText("sidebarConfigId", cfg.config_id || "baseline-default");
  setText("sidebarConfigDim", `${dim}d`);
  setText("sidebarConfigFormat", fmt);
  setText("sidebarConfigThresh", thresh.toFixed(2));
  setText("topbarConfigId", cfg.config_id || "baseline-default");
  setText("topbarConfigSlice", `${dim}d`);
  setText("topbarConfigFormat", fmt);
  setText("activeFormatLabel", fmt);
  setText("activeDimLabel", `${dim}d`);
  setText("playgroundFormatBadge", `${fmt} • ${dim}d`);
  setText("cmdBarDimVal", `${dim}d`);
  setText("cmdBarFmtVal", fmt);
  setText("settingDimBadge", `${dim}d`);
  setText("settingCosineVal", thresh.toFixed(2));

  // Sync Right Rail Run Settings controls
  document.querySelectorAll("#settingDimButtonGroup .seg-pill").forEach((btn) => {
    btn.classList.toggle("active", Number(btn.getAttribute("data-dim")) === dim);
  });
  const fmtSelect = document.getElementById("settingFormatSelect");
  if (fmtSelect) fmtSelect.value = fmt;
  const cosSlider = document.getElementById("settingCosineSlider");
  if (cosSlider) cosSlider.value = String(thresh);
}

// ============================================================================
// STAGE 1: Zero Reset, Multi-Scope Demo Seed, and Live Feedback Sandbox
// ============================================================================

async function handleZeroReset() {
  const btn = document.getElementById("zeroResetBtn");
  const scopeType = document.getElementById("resetScopeTypeSelect")?.value || "USER";
  const resetAll = Boolean(document.getElementById("resetAllScopesCheckbox")?.checked);
  const clearCases = Boolean(document.getElementById("clearEvalCasesCheckbox")?.checked);
  const ids = getScopeIds();

  let targetScopeId = ids.user_id;
  if (scopeType === "TEAM") targetScopeId = ids.team_id;
  if (scopeType === "AGENT") targetScopeId = ids.agent_id;
  if (scopeType === "ORG") targetScopeId = ids.org_id;

  if (btn) {
    btn.disabled = true;
    btn.textContent = "Clearing Memories...";
  }

  try {
    const res = await fetch("/api/studio/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope_id: targetScopeId,
        scope_type: scopeType,
        clear_eval_cases: clearCases,
        reset_all_scopes: resetAll,
        team_id: ids.team_id,
        agent_id: ids.agent_id,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Reset failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    showStatusBanner(
      `✓ Cleared ${data.memories_deleted} memories in ${data.scope_type}:${data.scope_id} (Remaining: ${data.remaining_memories}).`
    );
    await refreshScopesExplorer();
    if (clearCases) {
      await refreshEvalCases();
    }
  } catch (err) {
    showStatusBanner(`Error clearing memories: ${err.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Clear Selected Memories";
    }
  }
}

async function handleSeedMultiScopeDemo() {
  const btn = document.getElementById("seedMultiScopeBtn");
  const ids = getScopeIds();
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Loading Sample Memories...";
  }
  try {
    const res = await fetch("/api/studio/seed-demo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ids),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Seed demo failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    applyScopesPayload(data);
    showStatusBanner(
      `✓ Loaded sample memories across ORG, TEAM, AGENT, and USER scopes (${data.counts?.TOTAL || 0} memories, ${data.federated?.conflicts_resolved?.length || 0} scope overrides resolved).`
    );
  } catch (err) {
    showStatusBanner(`Failed to load sample memories: ${err.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Load Sample Memories";
    }
  }
}

async function handleSandboxChatSubmit(evt) {
  if (evt) evt.preventDefault();
  const inputEl = document.getElementById("studioPromptInput");
  const routeSelect = document.getElementById("studioAgentRouteSelect");
  const sendBtn = document.getElementById("studioSendPromptBtn");
  const streamEl = document.getElementById("studioChatStream");

  const prompt = inputEl ? inputEl.value.trim() : "";
  if (!prompt) return;
  const endpoint = routeSelect ? routeSelect.value : "/api/preference/query";
  const ids = getScopeIds();

  if (sendBtn) {
    sendBtn.disabled = true;
    sendBtn.textContent = "Querying...";
  }

  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: ids.user_id,
        session_id: studioState.sessionId,
        prompt: prompt,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Agent query failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    if (inputEl) inputEl.value = "";
    appendSandboxInteractionCard(streamEl, prompt, data);

    // Pre-fill the direct feedback form with the latest interaction for convenience
    const fbPrompt = document.getElementById("fbPromptInput");
    const fbResp = document.getElementById("fbResponseInput");
    if (fbPrompt) fbPrompt.value = prompt;
    if (fbResp) fbResp.value = data.response || "";
  } catch (err) {
    showStatusBanner(`Sandbox query error: ${err.message}`, true);
  } finally {
    if (sendBtn) {
      sendBtn.disabled = false;
      sendBtn.textContent = "Send & Trace";
    }
  }
}

function appendSandboxInteractionCard(streamEl, prompt, data) {
  if (!streamEl) return;
  const emptyEl = streamEl.querySelector(".empty-state-compact");
  if (emptyEl) emptyEl.remove();

  const card = document.createElement("div");
  card.className = "studio-turn-card";
  const sourceBadge = data.source || "ADK_LIVE";
  const latencyText = data.latency_ms !== undefined ? `${data.latency_ms} ms` : "live";

  card.innerHTML = `
    <div class="studio-turn-user">
      <span class="badge badge-slate">USER</span>
      <span class="studio-turn-prompt">${escapeHtml(prompt)}</span>
    </div>
    <div class="studio-turn-agent">
      <div class="studio-turn-meta">
        <span class="badge badge-indigo">${escapeHtml(data.agent_name || "Agent")}</span>
        <span class="badge badge-teal">${escapeHtml(sourceBadge)}</span>
        <span class="text-xs text-muted">${escapeHtml(latencyText)}</span>
      </div>
      <div class="studio-turn-response">${escapeHtml(data.response || "")}</div>
      <div class="studio-turn-feedback-bar">
        <span class="text-xs text-muted">Rate Recall Quality:</span>
        <button type="button" class="btn btn-secondary btn-xs btn-rate-good">
          👍 Helpful (Save as Test Case)
        </button>
        <button type="button" class="btn btn-danger-outline btn-xs btn-rate-bad">
          👎 Wrong Answer (Save as Test Case)
        </button>
        <span class="studio-turn-fb-status text-xs"></span>
      </div>
    </div>
  `;

  const goodBtn = card.querySelector(".btn-rate-good");
  const badBtn = card.querySelector(".btn-rate-bad");
  const statusSpan = card.querySelector(".studio-turn-fb-status");

  if (goodBtn) {
    goodBtn.addEventListener("click", async () => {
      await submitInteractionFeedback({
        prompt,
        agent_response: data.response || "",
        rating: "good",
        expected_fact: (data.spanner_record && data.spanner_record.preference_value) || "",
        failure_tag: "reinforcement",
        statusSpan,
      });
    });
  }

  if (badBtn) {
    badBtn.addEventListener("click", async () => {
      const expectedInputEl = document.getElementById("fbExpectedFactInput");
      const expectedFact = expectedInputEl?.value.trim() || "";
      if (!expectedFact) {
        document.getElementById("manualFeedbackDrawer")?.classList.remove("hidden");
        const fbPromptEl = document.getElementById("fbPromptInput");
        const fbRespEl = document.getElementById("fbResponseInput");
        const fbRatingEl = document.getElementById("fbRatingSelect");
        if (fbPromptEl) fbPromptEl.value = prompt;
        if (fbRespEl) fbRespEl.value = data.response || "";
        if (fbRatingEl) fbRatingEl.value = "bad";
        if (expectedInputEl) expectedInputEl.focus();
        if (statusSpan) {
          statusSpan.textContent = "Enter the expected answer in the Test Case Builder →";
        }
        showStatusBanner(
          "Please enter what the agent should have remembered in 'Expected Ground-Truth Fact' to save this wrong answer as a test case."
        );
        return;
      }
      await submitInteractionFeedback({
        prompt,
        agent_response: data.response || "",
        rating: "bad",
        expected_fact: expectedFact,
        failure_tag: "contradiction",
        statusSpan,
      });
    });
  }

  streamEl.prepend(card);
}

async function submitInteractionFeedback({
  prompt,
  agent_response,
  rating,
  expected_fact,
  failure_tag,
  statusSpan,
}) {
  const ids = getScopeIds();
  try {
    if (statusSpan) statusSpan.textContent = "Recording EvalCase...";
    const res = await fetch("/api/studio/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: studioState.sessionId,
        user_id: ids.user_id,
        team_id: ids.team_id,
        agent_id: ids.agent_id,
        prompt,
        agent_response,
        rating,
        expected_fact,
        failure_tag,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Feedback API error (${res.status}): ${errText}`);
    }
    const result = await res.json();
    if (statusSpan) {
      statusSpan.textContent = `✓ Saved EvalCase ${result.eval_case?.case_id || ""}`;
    }
    showStatusBanner(
      `✓ Recorded ${result.rating.toUpperCase()} feedback → Created EvalCase ${result.eval_case?.case_id} (${result.eval_case?.scenario_type}). Total cases: ${result.total_eval_cases}.`
    );
    await refreshEvalCases();
  } catch (err) {
    if (statusSpan) statusSpan.textContent = `Error: ${err.message}`;
    showStatusBanner(`Failed to record feedback: ${err.message}`, true);
  }
}

async function handleDirectFeedbackForm(evt) {
  if (evt) evt.preventDefault();
  const prompt = document.getElementById("fbPromptInput")?.value.trim() || "";
  const agent_response = document.getElementById("fbResponseInput")?.value.trim() || "";
  const rating = document.getElementById("fbRatingSelect")?.value || "bad";
  const failure_tag = document.getElementById("fbFailureTagSelect")?.value || "contradiction";
  const expected_fact = document.getElementById("fbExpectedFactInput")?.value.trim() || "";

  await submitInteractionFeedback({
    prompt,
    agent_response,
    rating,
    expected_fact,
    failure_tag,
    statusSpan: null,
  });
}

// ============================================================================
// STAGE 1B: Trajectory Explorer & Candidate Memory Extractor
// ============================================================================

async function loadTrajectorySessions() {
  const selectEl = document.getElementById("trajectorySessionSelect");
  if (!selectEl) return;
  const ids = getScopeIds();
  try {
    const res = await fetch(`/api/lakehouse/sessions?user_id=${encodeURIComponent(ids.user_id)}`);
    if (!res.ok) return;
    const data = await res.json();
    const sessions = data.sessions || [];
    const currentVal = selectEl.value;
    selectEl.innerHTML = `<option value="">-- Recent Lakehouse Session (Auto-Select) --</option>`;
    for (const s of sessions.slice(0, 15)) {
      const sid = s.session_id || "";
      const turns = s.turn_count !== undefined ? ` (${s.turn_count} turns)` : "";
      const opt = document.createElement("option");
      opt.value = sid;
      opt.textContent = `${sid}${turns}`;
      selectEl.appendChild(opt);
    }
    if (currentVal) selectEl.value = currentVal;
  } catch (_err) {
    // Non-fatal if lakehouse sessions endpoint is empty
  }
}

async function handleExtractTrajectoryCandidates() {
  const btn = document.getElementById("extractTrajectoryCandidatesBtn");
  const selectEl = document.getElementById("trajectorySessionSelect");
  const rawInputEl = document.getElementById("rawTrajectoryInput");
  const ids = getScopeIds();

  const sessionId = selectEl ? selectEl.value.trim() : "";
  const rawText = rawInputEl ? rawInputEl.value.trim() : "";

  if (btn) {
    btn.disabled = true;
    btn.textContent = "Mining Signals...";
  }

  try {
    const res = await fetch("/api/studio/trajectories/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: ids.user_id,
        session_id: sessionId || null,
        raw_transcript: rawText || null,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Trajectory extraction failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    studioState.trajectoryCandidates = data.candidates || [];
    const badge = document.getElementById("trajectoryCandidateCountBadge");
    if (badge) {
      badge.textContent = `${studioState.trajectoryCandidates.length} Candidate${studioState.trajectoryCandidates.length === 1 ? "" : "s"}`;
    }
    renderTrajectoryCandidates();
    showStatusBanner(
      `✓ Mined ${studioState.trajectoryCandidates.length} candidate memories from ${data.source} (${data.turn_count || 0} turns analyzed).`
    );
  } catch (err) {
    showStatusBanner(`Trajectory extraction error: ${err.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Mine Candidate Memories";
    }
  }
}

function renderTrajectoryCandidates() {
  const listEl = document.getElementById("trajectoryCandidatesList");
  if (!listEl) return;

  const sigFilter = (studioState.activeSignalFilter || "ALL").toUpperCase();
  const all = studioState.trajectoryCandidates || [];
  const filtered =
    sigFilter === "ALL"
      ? all
      : all.filter((c) => {
          const sig = (c.signal_type || "").toUpperCase();
          const mtype = (c.memory_type || "").toUpperCase();
          const cat = (c.category || "").toLowerCase();
          if (sig === sigFilter) return true;
          if (sigFilter === "CORRECTION") return sig.includes("CORRECTION");
          if (sigFilter === "PREFERENCE") return sig.includes("PREFERENCE") || mtype === "PREFERENCE";
          if (sigFilter === "TOOL_PATTERN") {
            return sig.includes("TOOL") || sig.includes("WORKFLOW") || mtype === "PROCEDURAL_SKILL";
          }
          if (sigFilter === "GUARDRAIL") {
            return sig.includes("GUARDRAIL") || mtype === "INVARIANT_GUARDRAIL" || cat === "guardrail";
          }
          return false;
        });

  if (filtered.length === 0) {
    listEl.innerHTML = `
      <div class="empty-state-compact">
        ${
          all.length === 0
            ? "Select a session or paste a raw transcript above and click <strong>Mine Candidate Memories</strong> to extract implicit preferences, corrections, and workflow rules."
            : `No candidates matching signal filter <code>${escapeHtml(sigFilter)}</code>.`
        }
      </div>
    `;
    return;
  }

  listEl.innerHTML = "";
  const ids = getScopeIds();

  for (const cand of filtered) {
    const card = document.createElement("div");
    card.className = "studio-candidate-card";
    const sigType = cand.signal_type || "IMPLICIT_PREFERENCE";
    const sigBadgeClass =
      sigType === "EXPLICIT_CORRECTION"
        ? "badge-indigo"
        : sigType === "REPEATED_WORKFLOW"
        ? "badge-teal"
        : "badge-slate";

    card.innerHTML = `
      <div class="studio-candidate-top">
        <div class="studio-mem-badges">
          <span class="badge ${sigBadgeClass}">${escapeHtml(sigType)}</span>
          <span class="badge badge-slate">${escapeHtml(cand.category || "tooling")}</span>
          <span class="text-xs text-muted">Conf: <code>${Number(cand.confidence || 0.9).toFixed(2)}</code></span>
        </div>
        <code class="text-xs text-secondary">${escapeHtml(cand.candidate_id || "")}</code>
      </div>
      <div class="studio-mem-kv">
        <code class="studio-mem-key">${escapeHtml(cand.memory_key)}</code>
        <span class="studio-mem-val">${escapeHtml(cand.memory_value)}</span>
      </div>
      <p class="studio-mem-belief">${escapeHtml(cand.crystallized_belief || "")}</p>
      ${
        cand.evidence_quote
          ? `<div class="studio-candidate-quote">“${escapeHtml(cand.evidence_quote)}”</div>`
          : ""
      }
      <div class="studio-candidate-actions">
        <button type="button" class="btn btn-primary btn-xs btn-commit-user">Commit to USER</button>
        <button type="button" class="btn btn-secondary btn-xs btn-commit-team">Commit to TEAM</button>
        <button type="button" class="btn btn-secondary btn-xs btn-commit-org">Commit to ORG</button>
        <button type="button" class="btn btn-secondary btn-xs btn-edit-cand">✎ Edit in Designer</button>
        <button type="button" class="btn btn-secondary btn-xs btn-guard-eval">+ Guard as EvalCase</button>
      </div>
    `;

    const commitToScope = async (scopeType, scopeId) => {
      await upsertMemoryRecord({
        scope_type: scopeType,
        scope_id: scopeId,
        memory_key: cand.memory_key,
        memory_value: cand.memory_value,
        memory_type: cand.memory_type || "PREFERENCE",
        category: cand.category || "tooling",
        crystallized_belief: cand.crystallized_belief || `${cand.memory_key}: ${cand.memory_value}`,
        keywords: cand.keywords || [cand.memory_key, cand.category || "preference"],
        confidence: Number(cand.confidence || 0.95),
        source_session_count: 1,
      });
    };

    card.querySelector(".btn-commit-user")?.addEventListener("click", () => commitToScope("USER", ids.user_id));
    card.querySelector(".btn-commit-team")?.addEventListener("click", () => commitToScope("TEAM", ids.team_id));
    card.querySelector(".btn-commit-org")?.addEventListener("click", () => commitToScope("ORG", ids.org_id));

    card.querySelector(".btn-edit-cand")?.addEventListener("click", () => {
      switchStudioMode("stage-2-scopes");
      openMemoryDesignerWithRecord({
        scope_type: cand.suggested_scope || "USER",
        scope_id: ids.user_id,
        memory_type: cand.memory_type || "PREFERENCE",
        category: cand.category || "tooling",
        memory_key: cand.memory_key,
        memory_value: cand.memory_value,
        keywords: cand.keywords || [],
        confidence: cand.confidence || 0.95,
        crystallized_belief: cand.crystallized_belief || "",
      });
    });

    card.querySelector(".btn-guard-eval")?.addEventListener("click", async () => {
      await submitInteractionFeedback({
        prompt: `How should I configure ${cand.memory_key} in this workflow?`,
        agent_response: cand.crystallized_belief || `${cand.memory_key} = ${cand.memory_value}`,
        rating: "good",
        expected_fact: `${cand.memory_key} = ${cand.memory_value}`,
        failure_tag: "reinforcement",
        statusSpan: null,
      });
    });

    listEl.appendChild(card);
  }
}

// ============================================================================
// STAGE 2: Multi-Scope Hierarchy Explorer, Inline CRUD, Pack Sharing & Consensus
// ============================================================================

async function refreshScopesExplorer() {
  const ids = getScopeIds();
  const queryInput = document.getElementById("scopeQueryInput");
  const rawQuery = queryInput ? queryInput.value.trim() : "";
  const queryText =
    rawQuery || "What security auth policy, Python package manager, and test runner standard should we use?";

  const params = new URLSearchParams({
    user_id: ids.user_id,
    team_id: ids.team_id,
    agent_id: ids.agent_id,
    org_id: ids.org_id,
    query_text: queryText,
  });

  try {
    const res = await fetch(`/api/studio/scopes?${params.toString()}`);
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Failed to fetch scopes (${res.status}): ${errText}`);
    }
    const data = await res.json();
    applyScopesPayload(data);
  } catch (err) {
    showStatusBanner(`Scope Explorer error: ${err.message}`, true);
  }
}

function applyScopesPayload(data) {
  if (!data) return;
  studioState.scopesData = data.scopes || { ORG: [], TEAM: [], AGENT: [], USER: [] };
  studioState.federatedData = data.federated || null;
  if (data.active_config) {
    updateActiveConfigSidebar(data.active_config);
  }

  const counts = data.counts || {
    ORG: (studioState.scopesData.ORG || []).length,
    TEAM: (studioState.scopesData.TEAM || []).length,
    AGENT: (studioState.scopesData.AGENT || []).length,
    USER: (studioState.scopesData.USER || []).length,
  };
  const total =
    counts.TOTAL !== undefined
      ? counts.TOTAL
      : counts.ORG + counts.TEAM + counts.AGENT + counts.USER;

  const setCount = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = String(val);
  };
  setCount("countScopeAll", total);
  setCount("countScopeOrg", counts.ORG || 0);
  setCount("countScopeTeam", counts.TEAM || 0);
  setCount("countScopeAgent", counts.AGENT || 0);
  setCount("countScopeUser", counts.USER || 0);

  const railBadge = document.getElementById("railTotalMemoriesBadge");
  if (railBadge) railBadge.textContent = `${total} Active`;

  renderScopeMemoryCards();
  renderLeftMemoryTree();
  renderFederatedAuditPanel(studioState.federatedData);
  renderTriggerTelemetryMatrix();
  loadSyncStatus();
}

function renderScopeMemoryCards() {
  const gridEl = document.getElementById("scopeMemoriesGrid");
  if (!gridEl) return;

  const filter = studioState.activeScopeFilter || "ALL";
  const searchQuery = (studioState.memorySearchQuery || "").toLowerCase().trim();
  const catFilter = (studioState.memoryCategoryFilter || "ALL").toLowerCase();
  const tiers = filter === "ALL" ? ["ORG", "TEAM", "AGENT", "USER"] : [filter];
  const allCards = [];

  for (const tier of tiers) {
    const mems = studioState.scopesData[tier] || [];
    for (const m of mems) {
      const cardObj = { ...m, scope_type: m.scope_type || tier };
      if (catFilter !== "all") {
        const memCat = String(cardObj.category || "").toLowerCase();
        const memType = String(cardObj.memory_type || "").toLowerCase();
        if (memCat !== catFilter && memType !== catFilter) {
          continue;
        }
      }
      if (searchQuery) {
        const hay = [
          cardObj.memory_key,
          cardObj.memory_value,
          cardObj.crystallized_belief,
          cardObj.category,
          cardObj.memory_type,
          Array.isArray(cardObj.keywords) ? cardObj.keywords.join(" ") : "",
        ]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(searchQuery)) {
          continue;
        }
      }
      allCards.push(cardObj);
    }
  }

  if (allCards.length === 0) {
    gridEl.innerHTML = `
      <div class="empty-state-compact">
        No memories match the active filter in <strong>${escapeHtml(filter)}</strong> scope. Click <strong>➕ Design / Edit Memory</strong> to author one, or open <strong>📖 Interactive Guide</strong> to click <strong>Load Sample Memories</strong>.
      </div>
    `;
    return;
  }

  gridEl.innerHTML = "";
  const ids = getScopeIds();

  for (const mem of allCards) {
    const st = (mem.scope_type || "USER").toUpperCase();
    const scopeBadgeClass = `scope-badge-${st.toLowerCase()}`;
    const mtype = mem.memory_type || "PREFERENCE";
    const isGuardrail = mtype === "INVARIANT_GUARDRAIL";

    const card = document.createElement("div");
    card.className = `studio-memory-card studio-rich-memory-card scope-border-${st.toLowerCase()}`;

    let promoteButtonHtml = "";
    if (st === "USER") {
      promoteButtonHtml = `
        <button type="button" class="btn btn-secondary btn-xs btn-promote-mem"
          data-src-type="USER" data-src-id="${escapeHtml(mem.scope_id || ids.user_id)}"
          data-key="${escapeHtml(mem.memory_key)}"
          data-tgt-type="TEAM" data-tgt-id="${escapeHtml(ids.team_id)}">
          ↑ Promote USER → TEAM
        </button>
      `;
    } else if (st === "TEAM") {
      promoteButtonHtml = `
        <button type="button" class="btn btn-secondary btn-xs btn-promote-mem"
          data-src-type="TEAM" data-src-id="${escapeHtml(mem.scope_id || ids.team_id)}"
          data-key="${escapeHtml(mem.memory_key)}"
          data-tgt-type="ORG" data-tgt-id="${escapeHtml(ids.org_id)}">
          ↑ Promote TEAM → ORG
        </button>
      `;
    }

    const trigCount = Number(mem.trigger_count || 0);
    const tokenFootprint = Number(mem.token_footprint || 12);
    const syncStatus = String(mem.sync_status || "SYNCED").toUpperCase();
    const syncBadgeClass = syncStatus === "STAGED" ? "badge-indigo" : "badge-emerald";
    const trigCond = mem.trigger_conditions || {};
    const trigMode = trigCond.match_mode || (isGuardrail ? "ALWAYS_PINNED" : "HYBRID_COSINE_AND_KEYWORD");
    const trigCos = Number(trigCond.min_cosine_similarity !== undefined ? trigCond.min_cosine_similarity : 0.72).toFixed(2);

    const lineageHtml = mem.promoted_from_scope
      ? `<div class="studio-mem-lineage">Lineage: <code>${escapeHtml(mem.promoted_from_scope)}</code> • Triggered: <strong>${trigCount}x</strong> • Condition: <code>${escapeHtml(trigMode)} (cos≥${trigCos})</code> • Cost: <code>~${tokenFootprint} tok</code></div>`
      : `<div class="studio-mem-lineage">Triggered: <strong>${trigCount}x</strong> • Condition: <code>${escapeHtml(trigMode)} (cos≥${trigCos})</code> • Cost: <code>~${tokenFootprint} tok</code> • Conf: <code>${Number(mem.confidence || 1.0).toFixed(2)}</code></div>`;

    card.innerHTML = `
      <div class="studio-mem-header">
        <div class="studio-mem-badges">
          <span class="badge ${scopeBadgeClass}">${escapeHtml(st)}: ${escapeHtml(mem.scope_id || "")}</span>
          <span class="badge ${isGuardrail ? "badge-indigo" : "badge-slate"}">${escapeHtml(mtype)}</span>
          ${mem.category ? `<span class="badge badge-slate">${escapeHtml(mem.category)}</span>` : ""}
          <span class="badge ${syncBadgeClass}" title="Sync status">${escapeHtml(syncStatus)}</span>
          <span class="badge badge-slate" title="Empirical Trigger Hit Count">${trigCount}x Triggered</span>
        </div>
        <button type="button" class="btn-link-xs btn-inspect-mem" title="View details">Details</button>
      </div>
      <div class="studio-mem-kv">
        <code class="studio-mem-key">${escapeHtml(mem.memory_key)}</code>
        <span class="studio-mem-val">${escapeHtml(mem.memory_value)}</span>
      </div>
      ${mem.crystallized_belief ? `<p class="studio-mem-belief">${escapeHtml(mem.crystallized_belief)}</p>` : ""}
      <div class="studio-mem-footer">
        ${lineageHtml}
        <div class="studio-mem-actions">
          <button type="button" class="btn btn-secondary btn-xs btn-edit-mem" title="Edit rule">Edit</button>
          <button type="button" class="btn btn-secondary btn-xs btn-test-recall" title="Test recall">Test</button>
          ${promoteButtonHtml}
          <button type="button" class="btn btn-danger-outline btn-xs btn-delete-mem" title="Delete rule">Delete</button>
        </div>
      </div>
    `;

    card.querySelector(".btn-inspect-mem")?.addEventListener("click", () => {
      openInspectorDrawer(
        `Scoped Memory: ${mem.memory_key}`,
        `Scope ${st} (${mem.scope_id}) • Type: ${mtype} • Sync: ${syncStatus}`,
        mem,
        null
      );
    });

    card.querySelector(".btn-edit-mem")?.addEventListener("click", () => {
      openMemoryDesignerWithRecord(mem);
    });

    card.querySelector(".btn-test-recall")?.addEventListener("click", async () => {
      switchStudioMode("stage-3-eval");
      const scenarioPromptEl = document.getElementById("scenarioRetestPromptInput");
      const scenarioExpectedEl = document.getElementById("scenarioRetestExpectedInput");
      if (scenarioPromptEl) {
        scenarioPromptEl.value = `What is our active rule or preference regarding ${mem.memory_key}?`;
      }
      if (scenarioExpectedEl) {
        scenarioExpectedEl.value = mem.memory_value || "";
      }
      await handleRunScenarioRetest();
    });

    const promoteBtn = card.querySelector(".btn-promote-mem");
    if (promoteBtn) {
      promoteBtn.addEventListener("click", async () => {
        await handlePromoteMemory({
          source_scope_type: promoteBtn.getAttribute("data-src-type"),
          source_scope_id: promoteBtn.getAttribute("data-src-id"),
          memory_key: promoteBtn.getAttribute("data-key"),
          target_scope_type: promoteBtn.getAttribute("data-tgt-type"),
          target_scope_id: promoteBtn.getAttribute("data-tgt-id"),
        });
      });
    }

    card.querySelector(".btn-delete-mem")?.addEventListener("click", async () => {
      await handleDeleteScopedMemory(st, mem.scope_id || ids.user_id, mem.memory_key);
    });

    gridEl.appendChild(card);
  }
}

function renderLeftMemoryTree() {
  const treeEl = document.getElementById("leftMemoryTreeList");
  if (!treeEl) return;

  const filter = studioState.activeScopeFilter || "ALL";
  const searchQuery = (studioState.leftTreeSearchQuery || "").toLowerCase().trim();
  const tiers = filter === "ALL" ? ["ORG", "TEAM", "AGENT", "USER"] : [filter];
  const treeSource = (wsState.treePayload && wsState.treePayload.tree) ? wsState.treePayload.tree : studioState.scopesData;
  const matched = [];

  for (const tier of tiers) {
    const mems = treeSource[tier] || [];
    for (const m of mems) {
      const nodeObj = { ...m, scope_type: m.scope_type || tier };
      if (searchQuery) {
        const hay = [
          nodeObj.memory_key,
          nodeObj.memory_value,
          nodeObj.natural_language_belief,
          nodeObj.category,
          nodeObj.memory_type,
          Array.isArray(nodeObj.keywords) ? nodeObj.keywords.join(" ") : "",
        ]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(searchQuery)) {
          continue;
        }
      }
      matched.push(nodeObj);
    }
  }

  if (matched.length === 0) {
    const scopeLabels = { ALL: "All", ORG: "Organization", TEAM: "Team", AGENT: "Agent", USER: "Personal" };
    const scopeLabel = scopeLabels[filter.toUpperCase()] || "this";
    treeEl.innerHTML = `<div class="empty-state-compact" style="font-size:0.72rem; padding:0.45rem;">No memories in ${escapeHtml(scopeLabel)} scope. Click <strong>Add note</strong> to create one.</div>`;
    return;
  }

  treeEl.innerHTML = "";
  const activeKey = studioState.quickEditedMemory?.memory_key || "";
  const activeScope = studioState.quickEditedMemory?.scope_type || "";

  for (const mem of matched) {
    const st = (mem.scope_type || "USER").toUpperCase();
    const syncStatus = String(mem.sync_status || "SYNCED").toUpperCase();
    const isStaged = syncStatus === "STAGED";
    const isSelected = activeKey === mem.memory_key && activeScope === st;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `left-memory-node ${isSelected ? "active" : ""}`;
    btn.title = `${st}:${mem.scope_id || ""} • ${mem.memory_key} = ${mem.memory_value}`;
    btn.innerHTML = `
      <div class="left-mem-node-top">
        <span class="badge scope-badge-${escapeHtml(st.toLowerCase())}" style="font-size:0.62rem; padding:0.05rem 0.3rem;">${escapeHtml(st)}</span>
        <code class="left-mem-node-key">${escapeHtml(mem.memory_key)}</code>
        <span class="left-mem-sync-dot ${isStaged ? "dot-staged" : "dot-synced"}" title="${escapeHtml(syncStatus)}"></span>
      </div>
      <div class="left-mem-node-val">${escapeHtml(mem.memory_value)}</div>
    `;
    btn.addEventListener("click", () => {
      openLeftMemoryQuickEditor(mem);
      openMemoryDesignerWithRecord(mem);
    });
    treeEl.appendChild(btn);
  }
}

function openLeftMemoryQuickEditor(mem = null) {
  const editor = document.getElementById("leftMemoryQuickEditor");
  if (!editor) return;
  editor.classList.remove("hidden");
  const ids = getScopeIds();

  const titleEl = document.getElementById("leftQuickEditTitle");
  const keyEl = document.getElementById("leftQuickEditKey");
  const valEl = document.getElementById("leftQuickEditValue");
  const scopeEl = document.getElementById("leftQuickEditScope");
  const catEl = document.getElementById("leftQuickEditCategory");
  const kwEl = document.getElementById("leftQuickEditKeywords");
  const confEl = document.getElementById("leftQuickEditConfidence");

  if (!mem) {
    studioState.quickEditedMemory = null;
    if (titleEl) titleEl.textContent = "New Memory Record";
    if (keyEl) keyEl.value = "";
    if (valEl) valEl.value = "";
    if (scopeEl) scopeEl.value = studioState.activeScopeFilter !== "ALL" ? studioState.activeScopeFilter : "USER";
    if (catEl) catEl.value = "tooling";
    if (kwEl) kwEl.value = "";
    if (confEl) confEl.value = "0.95";
    renderLeftMemoryTree();
    return;
  }

  const st = (mem.scope_type || "USER").toUpperCase();
  studioState.quickEditedMemory = {
    ...mem,
    scope_type: st,
    scope_id:
      mem.scope_id ||
      (st === "ORG" ? ids.org_id : st === "TEAM" ? ids.team_id : st === "AGENT" ? ids.agent_id : ids.user_id),
  };
  if (titleEl) titleEl.textContent = `Edit: ${mem.memory_key}`;
  if (keyEl) keyEl.value = mem.memory_key || "";
  if (valEl) valEl.value = mem.memory_value || "";
  if (scopeEl) scopeEl.value = st;
  if (catEl) catEl.value = mem.category || "tooling";
  if (kwEl) kwEl.value = Array.isArray(mem.keywords) ? mem.keywords.join(", ") : mem.keywords || "";
  if (confEl) confEl.value = String(mem.confidence !== undefined ? mem.confidence : 0.95);
  renderLeftMemoryTree();
}

async function handleLeftQuickSaveMemory() {
  const ids = getScopeIds();
  const scopeType = (document.getElementById("leftQuickEditScope")?.value || "USER").toUpperCase();
  let scopeId = ids.user_id;
  if (scopeType === "TEAM") scopeId = ids.team_id;
  if (scopeType === "AGENT") scopeId = ids.agent_id;
  if (scopeType === "ORG") scopeId = ids.org_id;

  const memoryKey = (document.getElementById("leftQuickEditKey")?.value || "").trim();
  const memoryValue = (document.getElementById("leftQuickEditValue")?.value || "").trim();
  const category = (document.getElementById("leftQuickEditCategory")?.value || "tooling").trim();
  const kwRaw = document.getElementById("leftQuickEditKeywords")?.value || "";
  const keywords = kwRaw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const confidence = Number(document.getElementById("leftQuickEditConfidence")?.value || 0.95);

  if (!memoryKey || !memoryValue) {
    showStatusBanner("Memory Key and Value are required in the Quick Editor.", true);
    return;
  }

  try {
    const res = await fetch("/api/studio/tree/stage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope_type: scopeType,
        scope_id: scopeId,
        memory_key: memoryKey,
        after_value: memoryValue,
        category,
        natural_language_belief: `${memoryKey}: ${memoryValue}`,
        confidence,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(`Staged '${memoryKey}' into Memory Tree (${scopeType}). Review and commit in Memory Tree.`);
    await loadWsMemoryTree();
  } catch (err) {
    showStatusBanner(`Stage memory failed: ${err.message}`, true);
  }
}

async function handleLeftQuickPromote() {
  const ids = getScopeIds();
  const mem = studioState.quickEditedMemory;
  const key = (document.getElementById("leftQuickEditKey")?.value || mem?.memory_key || "").trim();
  const srcType = (document.getElementById("leftQuickEditScope")?.value || mem?.scope_type || "USER").toUpperCase();
  if (!key) {
    showStatusBanner("Select or enter a Memory Key to promote.", true);
    return;
  }
  if (srcType === "ORG") {
    showStatusBanner("Memory is already at ORG scope (highest precedence).", false);
    return;
  }
  const srcId =
    srcType === "TEAM" ? ids.team_id : srcType === "AGENT" ? ids.agent_id : ids.user_id;
  const tgtType = srcType === "USER" || srcType === "AGENT" ? "TEAM" : "ORG";
  const tgtId = tgtType === "TEAM" ? ids.team_id : ids.org_id;

  await handlePromoteMemory({
    source_scope_type: srcType,
    source_scope_id: srcId,
    memory_key: key,
    target_scope_type: tgtType,
    target_scope_id: tgtId,
  });
}

async function handleLeftQuickDelete() {
  const ids = getScopeIds();
  const mem = studioState.quickEditedMemory;
  const key = (document.getElementById("leftQuickEditKey")?.value || mem?.memory_key || "").trim();
  const scopeType = (document.getElementById("leftQuickEditScope")?.value || mem?.scope_type || "USER").toUpperCase();
  if (!key) {
    showStatusBanner("Select a Memory Key to delete.", true);
    return;
  }
  const scopeId =
    mem?.scope_id ||
    (scopeType === "ORG"
      ? ids.org_id
      : scopeType === "TEAM"
      ? ids.team_id
      : scopeType === "AGENT"
      ? ids.agent_id
      : ids.user_id);

  await handleDeleteScopedMemory(scopeType, scopeId, key);
  studioState.quickEditedMemory = null;
  document.getElementById("leftMemoryQuickEditor")?.classList.add("hidden");
}

function updateDesignerTokenPreview() {
  const keyVal = document.getElementById("designerMemoryKey")?.value || "";
  const valVal = document.getElementById("designerMemoryValue")?.value || "";
  const beliefVal = document.getElementById("designerBelief")?.value || "";
  const raw = `${keyVal} ${valVal} ${beliefVal}`.trim();
  const est = Math.max(8, Math.round(raw.split(/\s+/).filter(Boolean).length * 1.35) + 6);
  const badge = document.getElementById("designerTokenPreviewBadge");
  if (badge) {
    badge.textContent = `Est. Cost: ~${est} tokens`;
  }
}

function openMemoryDesignerWithRecord(mem = null) {
  const card = document.getElementById("memoryDesignerCard");
  if (!card) return;
  card.classList.remove("hidden");
  const ids = getScopeIds();

  const scopeTypeEl = document.getElementById("designerScopeType");
  const scopeIdEl = document.getElementById("designerScopeId");
  const memTypeEl = document.getElementById("designerMemoryType");
  const catEl = document.getElementById("designerCategory");
  const keyEl = document.getElementById("designerMemoryKey");
  const valEl = document.getElementById("designerMemoryValue");
  const kwEl = document.getElementById("designerKeywords");
  const confEl = document.getElementById("designerConfidence");
  const beliefEl = document.getElementById("designerBelief");
  const matchModeEl = document.getElementById("designerMatchMode");
  const cosineEl = document.getElementById("designerCosineThreshold");

  if (!mem) {
    if (scopeTypeEl) scopeTypeEl.value = "USER";
    if (scopeIdEl) scopeIdEl.value = ids.user_id;
    updateDesignerTokenPreview();
    return;
  }

  const st = (mem.scope_type || "USER").toUpperCase();
  if (scopeTypeEl) scopeTypeEl.value = st;
  if (scopeIdEl) {
    scopeIdEl.value =
      mem.scope_id ||
      (st === "ORG" ? ids.org_id : st === "TEAM" ? ids.team_id : st === "AGENT" ? ids.agent_id : ids.user_id);
  }
  if (memTypeEl) memTypeEl.value = mem.memory_type || "PREFERENCE";
  if (catEl) catEl.value = mem.category || "tooling";
  if (keyEl) keyEl.value = mem.memory_key || "";
  if (valEl) valEl.value = mem.memory_value || "";
  if (kwEl) kwEl.value = Array.isArray(mem.keywords) ? mem.keywords.join(", ") : mem.keywords || "";
  if (confEl) confEl.value = String(mem.confidence !== undefined ? mem.confidence : 0.95);
  if (beliefEl) beliefEl.value = mem.crystallized_belief || "";
  if (matchModeEl) {
    matchModeEl.value = mem.trigger_conditions?.match_mode || "HYBRID_COSINE_AND_KEYWORD";
  }
  if (cosineEl) {
    cosineEl.value = String(
      mem.trigger_conditions?.min_cosine_similarity !== undefined
        ? mem.trigger_conditions.min_cosine_similarity
        : 0.72
    );
  }
  updateDesignerTokenPreview();
}

async function upsertMemoryRecord(payload) {
  try {
    const res = await fetch("/api/studio/memories/upsert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Upsert memory failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    const syncState = data.record?.sync_status || "SYNCED";
    showStatusBanner(
      `✓ Upserted [${data.record?.scope_type}:${data.record?.scope_id}] ${data.record?.memory_key} = ${data.record?.memory_value} (${syncState}).`
    );
    await refreshScopesExplorer();
    return data;
  } catch (err) {
    showStatusBanner(`Memory upsert error: ${err.message}`, true);
    return null;
  }
}

async function handleMemoryDesignerSubmit(evt) {
  if (evt) evt.preventDefault();
  const ids = getScopeIds();
  const scopeType = document.getElementById("designerScopeType")?.value || "USER";
  let defaultScopeId = ids.user_id;
  if (scopeType === "TEAM") defaultScopeId = ids.team_id;
  if (scopeType === "AGENT") defaultScopeId = ids.agent_id;
  if (scopeType === "ORG") defaultScopeId = ids.org_id;

  const scopeId = document.getElementById("designerScopeId")?.value.trim() || defaultScopeId;
  const memoryType = document.getElementById("designerMemoryType")?.value || "PREFERENCE";
  const category = document.getElementById("designerCategory")?.value || "tooling";
  const memoryKey = document.getElementById("designerMemoryKey")?.value.trim() || "";
  const memoryValue = document.getElementById("designerMemoryValue")?.value.trim() || "";
  const kwRaw = document.getElementById("designerKeywords")?.value || "";
  const keywords = kwRaw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const confidence = Number(document.getElementById("designerConfidence")?.value || 0.95);
  const crystallizedBelief =
    document.getElementById("designerBelief")?.value.trim() || `${memoryKey}: ${memoryValue}`;
  const matchMode = document.getElementById("designerMatchMode")?.value || "HYBRID_COSINE_AND_KEYWORD";
  const minCosine = Number(document.getElementById("designerCosineThreshold")?.value || 0.72);
  const syncImmediately = Boolean(document.getElementById("designerSyncImmediatelyCheckbox")?.checked);

  if (!memoryKey || !memoryValue) {
    showStatusBanner("Memory Key and Memory Value are required.", true);
    return;
  }

  const res = await upsertMemoryRecord({
    scope_type: scopeType,
    scope_id: scopeId,
    memory_key: memoryKey,
    memory_value: memoryValue,
    memory_type: memoryType,
    category,
    crystallized_belief: crystallizedBelief,
    keywords,
    confidence,
    source_session_count: 1,
    sync_immediately: syncImmediately,
    trigger_conditions: {
      match_mode: matchMode,
      min_cosine_similarity: minCosine,
      required_keywords: keywords.slice(0, 5),
      auto_inject_scopes: [scopeType],
    },
  });

  if (res) {
    document.getElementById("memoryDesignerCard")?.classList.add("hidden");
  }
}

async function handleDeleteScopedMemory(scopeType, scopeId, memoryKey) {
  try {
    const res = await fetch("/api/studio/memories/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope_type: scopeType,
        scope_id: scopeId,
        memory_key: memoryKey,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Delete memory failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    showStatusBanner(
      `Deleted '${data.memory_key}' from ${data.scope_type}:${data.scope_id}.`
    );
    await refreshScopesExplorer();
  } catch (err) {
    showStatusBanner(`Memory delete error: ${err.message}`, true);
  }
}

async function handleExportMemoryPack() {
  const ids = getScopeIds();
  const params = new URLSearchParams({
    user_id: ids.user_id,
    team_id: ids.team_id,
    agent_id: ids.agent_id,
    org_id: ids.org_id,
    scope_filter: studioState.activeScopeFilter || "ALL",
  });
  try {
    const res = await fetch(`/api/studio/memories/export?${params.toString()}`);
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Export pack failed (${res.status}): ${errText}`);
    }
    const pack = await res.json();
    const drawer = document.getElementById("memoryPackDrawer");
    const textarea = document.getElementById("memoryPackJsonTextarea");
    if (drawer) drawer.classList.remove("hidden");
    if (textarea) textarea.value = JSON.stringify(pack, null, 2);
    showStatusBanner(
      `✓ Exported Portable Memory Pack (${pack.total_memories || 0} memories across ${(pack.scopes_included || []).join(", ")}).`
    );
  } catch (err) {
    showStatusBanner(`Memory Pack export error: ${err.message}`, true);
  }
}

async function handleImportMemoryPack() {
  const textarea = document.getElementById("memoryPackJsonTextarea");
  const raw = textarea ? textarea.value.trim() : "";
  if (!raw) {
    showStatusBanner("Paste a JSON Memory Pack payload before importing.", true);
    return;
  }
  try {
    const parsed = JSON.parse(raw);
    const memories = Array.isArray(parsed) ? parsed : parsed.memories || [];
    const res = await fetch("/api/studio/memories/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ memories }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Import pack failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    showStatusBanner(`Imported ${data.imported_count || 0} rules.`);
    await refreshScopesExplorer();
  } catch (err) {
    showStatusBanner(`Memory Pack import error: ${err.message}`, true);
  }
}

function renderFederatedAuditPanel(fed) {
  const conflictsBadge = document.getElementById("conflictsResolvedBadge");
  const conflictListEl = document.getElementById("conflictResolutionList");
  const tokenSavingsBadge = document.getElementById("contextTokenSavingsBadge");
  const tokenCountBadge = document.getElementById("contextTokenCountBadge");
  const contextPre = document.getElementById("formattedContextPreview");
  const playgroundContextPre = document.getElementById("playgroundContextPre");
  const playgroundConflictList = document.getElementById("playgroundConflictMiniList");
  const playgroundSavingsBadge = document.getElementById("playgroundTokenSavingsBadge");
  const cmdBarSavingsVal = document.getElementById("cmdBarTokenSavingsVal");

  if (!fed) return;
  const conflicts = fed.conflicts_resolved || [];
  if (conflictsBadge) {
    conflictsBadge.textContent = `${conflicts.length} Conflict${conflicts.length === 1 ? "" : "s"} Resolved`;
  }

  const conflictsHtml =
    conflicts.length === 0
      ? `<div class="empty-state-compact">No overlapping keys with conflicting values across scopes.</div>`
      : conflicts
          .map(
            (c) => `
          <div class="studio-conflict-item">
            <div class="studio-conflict-top">
              <code>${escapeHtml(c.memory_key)}</code>
              <span class="badge badge-indigo">${escapeHtml(c.rule_applied)}</span>
            </div>
            <div class="studio-conflict-vs">
              <span class="conflict-winner">✓ Winner (${escapeHtml(c.winning_scope)}): <strong>${escapeHtml(c.winning_value)}</strong></span>
              <span class="conflict-loser">✕ Overridden (${escapeHtml(c.overridden_scope)}): <del>${escapeHtml(c.overridden_value)}</del></span>
            </div>
          </div>
        `
          )
          .join("");

  if (conflictListEl) conflictListEl.innerHTML = conflictsHtml;
  if (playgroundConflictList) playgroundConflictList.innerHTML = conflictsHtml;

  const savingsPctStr = `${Number(fed.token_savings_pct || 0).toFixed(1)}%`;
  if (tokenSavingsBadge) tokenSavingsBadge.textContent = `${savingsPctStr} Token Savings`;
  if (playgroundSavingsBadge) playgroundSavingsBadge.textContent = `${savingsPctStr} Token Savings`;
  if (cmdBarSavingsVal) cmdBarSavingsVal.textContent = `${savingsPctStr} saved`;

  if (tokenCountBadge) {
    tokenCountBadge.textContent = `${fed.estimated_tokens || 0} / ${fed.baseline_tokens || 0} est. tokens`;
  }
  const formattedText = fed.formatted_context || "// Empty context (0 active memories matched)";
  if (contextPre) contextPre.textContent = formattedText;
  if (playgroundContextPre) playgroundContextPre.textContent = formattedText;
}

async function handlePromoteMemory(payload) {
  try {
    const res = await fetch("/api/studio/scopes/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Promotion failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    const pm = data.promoted_memory || {};
    showStatusBanner(
      `Promoted '${pm.memory_key}' from ${payload.source_scope_type} → ${payload.target_scope_type}:${payload.target_scope_id} (Support count: ${pm.support_count}).`
    );
    await refreshScopesExplorer();
    await loadWsMemoryTree();
  } catch (err) {
    showStatusBanner(`Memory promotion error: ${err.message}`, true);
  }
}

async function handleRunConsensus() {
  const btn = document.getElementById("runConsensusBtn");
  const ids = getScopeIds();
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Detecting Team Consensus...";
  }
  try {
    const res = await fetch("/api/studio/scopes/consensus", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        team_id: ids.team_id,
        user_ids: ["user:alice", "user:bob"],
        min_consensus_users: 2,
        target_scope_type: "TEAM",
        target_scope_id: ids.team_id,
        seed_if_empty: true,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Consensus failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    showStatusBanner(
      `✓ Team Consensus Evaluated across [user:alice, user:bob]: Auto-promoted ${data.promoted_count} shared belief(s) into ${data.target_scope_type}:${data.target_scope_id}.`
    );
    await refreshScopesExplorer();
  } catch (err) {
    showStatusBanner(`Consensus promotion error: ${err.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Run Team Consensus Auto-Promotion";
    }
  }
}

// ============================================================================
// STAGE 3: 7-Metric Memory Evaluation Engine & Adversarial Archetypes
// ============================================================================

async function refreshEvalCases() {
  try {
    const res = await fetch("/api/studio/eval/cases");
    if (!res.ok) return;
    const data = await res.json();
    studioState.evalCases = data.cases || [];
    renderEvalCasesList(studioState.evalCases, null);
    renderWsEvalCasesTable(studioState.evalCases, studioState.lastScorecard?.diagnostics || null);
  } catch (err) {
    console.error("Failed to load eval cases:", err);
  }
}

async function handleGenerateAdversarialCases() {
  const btn = document.getElementById("generateAdversarialBtn");
  const topic = document.getElementById("evalTopicInput")?.value.trim() || "cloud data science";

  if (btn) {
    btn.disabled = true;
    btn.textContent = "Adding Edge-Case Tests...";
  }
  try {
    const res = await fetch("/api/studio/eval/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, count: 4 }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Edge-case test generation failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    studioState.evalCases = data.cases || data.generated_cases || [];
    renderEvalCasesList(studioState.evalCases, null);
    renderWsEvalCasesTable(studioState.evalCases, studioState.lastScorecard?.diagnostics || null);
    showStatusBanner(
      `✓ Added ${data.generated_count} edge-case test prompts on '${data.topic}' (Total active test cases: ${data.count}).`
    );
  } catch (err) {
    showStatusBanner(`Test generator error: ${err.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "+ Add 4 Edge-Case Tests";
    }
  }
}

async function handleRunEvalSuite() {
  const btn = document.getElementById("runEvalSuiteBtn");
  const alphaSlider = document.getElementById("evalAlphaSlider");
  const alpha = alphaSlider ? parseFloat(alphaSlider.value) : 0.65;

  if (btn) {
    btn.disabled = true;
    btn.textContent = "Running Benchmark...";
  }
  try {
    const res = await fetch("/api/studio/eval/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        alpha_quality_weight: alpha,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Benchmark run failed (${res.status}): ${errText}`);
    }
    const scorecard = await res.json();
    studioState.lastScorecard = scorecard;
    renderEvalScorecard(scorecard);
    await refreshEvalCases();
    renderEvalCasesList(studioState.evalCases, scorecard.diagnostics || []);
    showStatusBanner(
      `✓ Benchmark Complete: Quality=${(scorecard.quality_score * 100).toFixed(1)}%, Token Savings=${scorecard.token_savings_pct}%, Overall Score=${scorecard.pareto_fitness.toFixed(4)}.`
    );
  } catch (err) {
    showStatusBanner(`Benchmark error: ${err.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Run Benchmark";
    }
  }
}

function renderEvalScorecard(sc) {
  if (!sc) return;
  const setText = (id, txt) => {
    const el = document.getElementById(id);
    if (el) el.textContent = txt;
  };

  setText("kpiQualityScore", `${(Number(sc.quality_score || 0) * 100).toFixed(1)}%`);
  setText("kpiTokenEfficiency", `${(Number(sc.token_efficiency_score || 0) * 100).toFixed(1)}%`);
  setText("kpiTokenSavingsSub", `${Number(sc.token_savings_pct || 0).toFixed(1)}% Context Token Reduction`);
  setText("kpiParetoFitness", Number(sc.pareto_fitness || 0).toFixed(4));
  setText("kpiEvalCasesCount", `${sc.cases_evaluated || 0} cases`);
  setText("kpiEvalLatencySub", `${sc.latency_ms || 0} ms • ${sc.embedding_dimensions || 768}d • ${sc.injection_format || ""}`);

  const mb = sc.metrics_breakdown || {};
  const metricsMeta = [
    { key: "precision_at_k", label: "1. Retrieval Precision@K", val: mb.precision_at_k },
    { key: "recall_at_k", label: "2. Ground-Truth Fact Recall@K", val: mb.recall_at_k },
    { key: "contradiction_resolution_accuracy", label: "3. Contradiction Update Accuracy", val: mb.contradiction_resolution_accuracy },
    { key: "hallucination_rejection_rate", label: "4. Hallucination Trap Rejection Rate", val: mb.hallucination_rejection_rate },
    { key: "scope_conflict_resolution_accuracy", label: "5. Multi-Scope Hierarchy Precedence", val: mb.scope_conflict_resolution_accuracy },
    { key: "deduplication_efficiency", label: "6. Paraphrase Deduplication Efficiency", val: mb.deduplication_efficiency },
    { key: "token_savings_ratio", label: "7. Prompt Context Token Compression", val: mb.token_savings_ratio },
  ];

  const barsEl = document.getElementById("evalMetricsBars");
  if (barsEl) {
    barsEl.innerHTML = metricsMeta
      .map((m) => {
        const pct = Math.round(Number(m.val || 0) * 1000) / 10;
        return `
          <div class="studio-metric-row">
            <div class="studio-metric-label-row">
              <span>${escapeHtml(m.label)}</span>
              <strong class="font-mono">${pct.toFixed(1)}%</strong>
            </div>
            <div class="studio-progress-track">
              <div class="studio-progress-fill" style="width: ${Math.min(100, Math.max(4, pct))}%"></div>
            </div>
          </div>
        `;
      })
      .join("");
  }

  const matBody = document.getElementById("matryoshkaTableBody");
  const matComp = sc.matryoshka_comparison || {};
  const dims = ["128d", "256d", "512d", "768d"];
  if (matBody && Object.keys(matComp).length > 0) {
    matBody.innerHTML = dims
      .map((dk) => {
        const row = matComp[dk];
        if (!row) return "";
        const isCurrent = Number(row.dimensions) === Number(sc.embedding_dimensions);
        return `
          <tr class="${isCurrent ? "matryoshka-active-row" : ""}">
            <td><code>${escapeHtml(dk)}</code> ${isCurrent ? '<span class="badge badge-indigo">ACTIVE</span>' : ""}</td>
            <td class="font-mono">${(Number(row.quality_score || 0) * 100).toFixed(1)}%</td>
            <td class="font-mono">${(Number(row.recall_at_k || 0) * 100).toFixed(1)}%</td>
            <td class="font-mono">${Number(row.storage_saving_pct || 0).toFixed(1)}%</td>
            <td class="font-mono">${Number(row.relative_latency_speedup || 1.0).toFixed(1)}×</td>
          </tr>
        `;
      })
      .join("");
  }
}

function renderEvalCasesList(cases, diagnostics) {
  const listEl = document.getElementById("evalCasesList");
  const countBadge = document.getElementById("evalDiagnosticsCountBadge");
  const fbBadge = document.getElementById("feedbackCaseCountBadge");

  const diagByCaseId = {};
  if (Array.isArray(diagnostics)) {
    for (const d of diagnostics) {
      if (d.case_id) diagByCaseId[d.case_id] = d;
    }
  }

  const items = Array.isArray(cases) ? cases : [];
  if (countBadge) countBadge.textContent = `${items.length} Cases`;
  if (fbBadge) fbBadge.textContent = `${items.length} Test Cases`;

  if (!listEl) return;
  if (items.length === 0) {
    listEl.innerHTML = `<div class="empty-state-compact">No test cases yet. Click <strong>+ Add 4 Edge-Case Tests</strong> or rate a playground response with 👍/👎.</div>`;
    return;
  }

  listEl.innerHTML = "";
  for (const c of items) {
    const diag = diagByCaseId[c.case_id];
    const row = document.createElement("div");
    row.className = "studio-eval-case-item";

    let statusPill = `<span class="badge badge-slate">READY</span>`;
    if (diag) {
      statusPill = diag.passed
        ? `<span class="badge badge-green">✓ PASSED (Recall ${(Number(diag.recall_at_k || 0) * 100).toFixed(0)}%)</span>`
        : `<span class="badge badge-danger">✕ REGRESSION</span>`;
    }

    row.innerHTML = `
      <div class="studio-eval-case-top">
        <div class="studio-eval-case-badges">
          <code>${escapeHtml(c.case_id)}</code>
          <span class="badge badge-indigo">${escapeHtml(c.scenario_type)}</span>
          ${statusPill}
        </div>
        <button type="button" class="btn-link-xs btn-inspect-case">Inspect Case &amp; Trace ↗</button>
      </div>
      <div class="studio-eval-case-title">${escapeHtml(c.title)}</div>
      <div class="studio-eval-case-meta">
        <span><strong>Query:</strong> ${escapeHtml(c.query_text)}</span>
        <span><strong>Expected:</strong> <code>${escapeHtml((c.expected_facts || []).join(", ") || "—")}</code></span>
        ${(c.forbidden_facts || []).length ? `<span><strong>Forbidden:</strong> <code>${escapeHtml(c.forbidden_facts.join(", "))}</code></span>` : ""}
      </div>
    `;

    const inspectBtn = row.querySelector(".btn-inspect-case");
    if (inspectBtn) {
      inspectBtn.addEventListener("click", () => {
        openInspectorDrawer(
          `EvalCase: ${c.case_id}`,
          `${c.scenario_type} • ${c.title}`,
          { eval_case: c, diagnostic: diag || "Run 7-Metric Suite to populate diagnostic trace" },
          null
        );
      });
    }

    listEl.appendChild(row);
  }
}

// ============================================================================
// STAGE 4: AlphaEvolve Self-Optimizing Memory Architecture (MAP-Elites)
// ============================================================================

function renderWsEvolveArchiveGrid(cells, activeConfig) {
  const cfg = activeConfig || {};
  const setText = (id, val) => {
    const el = document.getElementById(id);
    if (el && val) el.textContent = val;
  };
  if (cfg.config_id) {
    setText("wsEvalChampionVal", cfg.config_id);
    setText("wsEvalChampionSub", `${cfg.embedding_dimensions || 768}d • ${cfg.injection_format || "COMPACT_BULLET"}`);
  }
  const gridEl = document.getElementById("wsEvalMapElitesGrid");
  if (!gridEl || !Array.isArray(cells) || !cells.length) return;
  gridEl.innerHTML = `<div class="studio-designer-grid">${cells
    .map(
      (c, idx) => `
      <div class="studio-audit-card ws-clickable-row" data-cell-niche="${escapeHtml(c.niche || "")}" data-cell-idx="${idx}">
        <div class="font-mono text-xs font-semibold">${escapeHtml(c.niche || "")}</div>
        <div class="text-sm font-semibold mt-2">Score: ${Number(c.pareto_fitness || 0).toFixed(3)}</div>
        <div class="text-xs text-secondary">${escapeHtml(c.config?.injection_format || "")} • τ=${escapeHtml(String(c.config?.retrieval_cosine_threshold || 0.65))}</div>
      </div>
    `
    )
    .join("")}</div>`;
  gridEl.querySelectorAll("[data-cell-idx]").forEach((card) => {
    card.addEventListener("click", async () => {
      const idx = Number(card.getAttribute("data-cell-idx"));
      const cell = cells[idx];
      if (cell?.config) {
        await handlePromoteConfig(cell.config);
      }
    });
  });
}

async function refreshEvolveArchive() {
  try {
    const res = await fetch("/api/studio/evolve/archive");
    if (!res.ok) return;
    const data = await res.json();
    if (data.active_config) {
      updateActiveConfigSidebar(data.active_config);
    }
    if (data.archive) {
      studioState.evolveArchive = data.archive;
      renderMapElitesGrid(data.archive);
    }
    const cells = data.archive_summary?.cells || [];
    if (cells.length) {
      renderWsEvolveArchiveGrid(cells, data.active_config);
    }
  } catch (err) {
    console.error("Failed to fetch AlphaEvolve archive:", err);
  }
}

async function handleRunAlphaEvolve() {
  const btn = document.getElementById("runAlphaEvolveBtn");
  const promoteBtn = document.getElementById("promoteChampionBtn");
  const gens = parseInt(document.getElementById("evolveGenerationsSelect")?.value || "3", 10);
  const cands = parseInt(document.getElementById("evolveCandidatesSelect")?.value || "2", 10);
  const alpha = parseFloat(
    document.getElementById("evalAlphaSlider")?.value ||
      document.getElementById("evolveAlphaSlider")?.value ||
      "0.65"
  );

  if (btn) {
    btn.disabled = true;
    btn.textContent = `Auto-Tuning (${gens} Rounds)...`;
  }

  try {
    const res = await fetch("/api/studio/evolve/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        generations: gens,
        alpha_quality_weight: alpha,
        candidates_per_generation: cands,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Auto-tune failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    studioState.evolveArchive = data.archive;
    studioState.championCandidate = data.champion;
    if (data.active_config) {
      updateActiveConfigSidebar(data.active_config);
    }
    renderAlphaEvolveResults(data);
    if (promoteBtn && data.champion?.config) {
      promoteBtn.disabled = false;
    }
    showStatusBanner(
      `✓ Auto-Tune Complete (${gens} rounds): Recommended settings '${data.champion?.config?.config_id}' achieved Overall Score ${Number(data.champion?.pareto_fitness || 0).toFixed(4)} (${data.token_reduction_pct}% token savings).`
    );
  } catch (err) {
    showStatusBanner(`Auto-tune error: ${err.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Auto-tune";
    }
  }
}

function renderAlphaEvolveResults(data) {
  if (!data) return;
  const base = data.baseline || {};
  const champ = data.champion || {};
  const arch = data.archive || {};

  const setText = (id, txt) => {
    const el = document.getElementById(id);
    if (el) el.textContent = txt;
  };

  setText("evolveBaselineFitness", Number(base.pareto_fitness || 0).toFixed(4));
  setText(
    "evolveBaselineSub",
    `${base.config?.embedding_dimensions || 768}d • ${base.config?.injection_format || "FULL_BELIEF"}`
  );
  setText("evolveChampionFitness", Number(champ.pareto_fitness || 0).toFixed(4));
  const delta = Number(data.fitness_improvement || 0);
  setText(
    "evolveChampionDelta",
    `${delta >= 0 ? "+" : ""}${delta.toFixed(4)} score gain vs baseline`
  );
  setText("evolveTokenSavings", `${Number(data.token_reduction_pct || 0).toFixed(1)}%`);
  setText(
    "evolveChampionFormatSub",
    `${champ.config?.embedding_dimensions || 256}d • ${champ.config?.injection_format || "MINIMAL_KV"}`
  );
  setText(
    "evolveArchiveCoverage",
    `${arch.populated_cells || 0} / ${arch.total_cells || 12} cells`
  );
  setText("evolveTotalEvalsSub", `${arch.total_evaluations || 0} setting combinations tested`);
  setText("championIdBadge", champ.config?.config_id || "Recommended");

  renderMapElitesGrid(arch);
  renderChampionConfigDiff(base.config, champ.config, champ.mutation_notes);
  renderGenerationTrajectory(data.generation_trajectory || []);
}

function renderMapElitesGrid(arch) {
  const container = document.getElementById("mapElitesGridContainer");
  if (!container || !arch) return;

  const vNiches = arch.vector_niches || ["128d", "256d", "512d", "768d"];
  const tNiches = arch.token_niches || [
    "compact_<80_tok",
    "balanced_80_160_tok",
    "expansive_>160_tok",
  ];
  const tokenNicheLabels = {
    "compact_<80_tok": "Compact (<80 tokens)",
    "balanced_80_160_tok": "Balanced (80–160 tokens)",
    "expansive_>160_tok": "Detailed (>160 tokens)",
  };
  const cells = arch.cells || {};
  const champId = arch.champion?.config?.config_id;

  let html = `
    <table class="map-elites-grid-table">
      <thead>
        <tr>
          <th>Embedding Size \\ Prompt Budget</th>
          ${tNiches.map((tn) => `<th>${escapeHtml(tokenNicheLabels[tn] || tn)}</th>`).join("")}
        </tr>
      </thead>
      <tbody>
  `;

  for (const vn of vNiches) {
    html += `<tr><th class="map-elites-row-header"><code>${escapeHtml(vn)}</code></th>`;
    for (const tn of tNiches) {
      const key = `${tn}::${vn}`;
      const cell = cells[key];
      if (!cell) {
        html += `<td class="map-elite-cell empty-cell"><span class="text-xs text-muted">Not Tested Yet</span></td>`;
      } else {
        const isChamp = cell.config?.config_id === champId;
        html += `
          <td class="map-elite-cell populated-cell ${isChamp ? "champion-cell" : ""}" data-cell-key="${escapeHtml(key)}">
            <div class="elite-cell-top">
              <code>${escapeHtml(cell.config?.config_id || "preset")}</code>
              ${isChamp ? '<span class="badge badge-green">★ BEST</span>' : ""}
            </div>
            <div class="elite-cell-fitness">Score: <strong>${Number(cell.pareto_fitness || 0).toFixed(4)}</strong></div>
            <div class="elite-cell-meta">
              <span>Accuracy: ${(Number(cell.quality_score || 0) * 100).toFixed(0)}%</span>
              <span>Saved: ${Number(cell.token_savings_pct || 0).toFixed(0)}%</span>
            </div>
            <div class="elite-cell-fmt"><code>${escapeHtml(cell.config?.injection_format || "")}</code></div>
          </td>
        `;
      }
    }
    html += `</tr>`;
  }
  html += `</tbody></table>`;
  container.innerHTML = html;

  container.querySelectorAll(".populated-cell").forEach((td) => {
    td.addEventListener("click", () => {
      const k = td.getAttribute("data-cell-key");
      const cellObj = cells[k];
      if (cellObj) {
        openInspectorDrawer(
          `Settings Cell: ${k}`,
          `Preset ${cellObj.config?.config_id} • Overall Score ${Number(cellObj.pareto_fitness || 0).toFixed(4)}`,
          cellObj,
          cellObj.config
        );
      }
    });
  });
}

function renderChampionConfigDiff(baseCfg, champCfg, mutationNotes) {
  const diffEl = document.getElementById("championConfigDiff");
  if (!diffEl || !baseCfg || !champCfg) return;

  const keys = [
    "embedding_dimensions",
    "injection_format",
    "retrieval_cosine_threshold",
    "dedup_cosine_threshold",
    "top_k_per_scope",
    "consensus_min_users",
  ];

  const rowsHtml = keys
    .map((k) => {
      const bVal = baseCfg[k];
      const cVal = champCfg[k];
      const changed = String(bVal) !== String(cVal);
      return `
        <tr class="${changed ? "diff-row-changed" : ""}">
          <td><code>${escapeHtml(k)}</code></td>
          <td class="font-mono text-muted">${escapeHtml(String(bVal))}</td>
          <td class="font-mono"><strong>${escapeHtml(String(cVal))}</strong> ${changed ? '<span class="badge badge-teal">UPDATED</span>' : ""}</td>
        </tr>
      `;
    })
    .join("");

  diffEl.innerHTML = `
    <table class="math-comparison-table">
      <thead>
        <tr>
          <th>Setting</th>
          <th>Baseline</th>
          <th>Recommended</th>
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>
    ${
      Array.isArray(mutationNotes) && mutationNotes.length
        ? `<div class="mt-8 text-xs text-secondary"><strong>Tuning Changes:</strong> <code>${escapeHtml(mutationNotes.join(" → "))}</code></div>`
        : ""
    }
  `;
}

function renderGenerationTrajectory(traj) {
  const listEl = document.getElementById("evolveTrajectoryList");
  if (!listEl) return;
  if (!Array.isArray(traj) || traj.length === 0) {
    listEl.innerHTML = `<div class="text-xs text-muted">No tuning rounds executed yet.</div>`;
    return;
  }

  listEl.innerHTML = traj
    .map(
      (g) => `
      <div class="studio-traj-item">
        <span class="badge badge-slate">Round ${g.generation}</span>
        <span>Best: <code>${escapeHtml(g.best_config_id)}</code> (Score <strong>${Number(g.best_fitness || 0).toFixed(4)}</strong>)</span>
        <span>Quality: ${(Number(g.best_quality || 0) * 100).toFixed(1)}%</span>
        <span>Token Savings: ${Number(g.best_token_savings_pct || 0).toFixed(1)}%</span>
      </div>
    `
    )
    .join("");
}

async function handlePromoteConfig(cfgObj) {
  if (!cfgObj) return;
  try {
    const res = await fetch("/api/studio/evolve/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        config: cfgObj,
        config_id: cfgObj.config_id,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Settings update failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    updateActiveConfigSidebar(data.active_config);
    showStatusBanner(
      `✓ Applied Retrieval Settings '${data.active_config?.config_id}' (${data.active_config?.embedding_dimensions}d • ${data.active_config?.injection_format}).`
    );
    await refreshScopesExplorer();
  } catch (err) {
    showStatusBanner(`Failed to apply retrieval settings: ${err.message}`, true);
  }
}

// ============================================================================
// Intent 1: Configure Ingest & Raw Telemetry Inspector
// ============================================================================

async function loadIngestConfig() {
  try {
    const res = await fetch("/api/studio/ingest/config");
    if (!res.ok) return;
    const data = await res.json();
    const cfg = data.ingest_config || {};
    studioState.ingestConfig = cfg;

    const srcEl = document.getElementById("ingestSourceSelect");
    const winEl = document.getElementById("ingestLookbackHoursInput") || document.getElementById("ingestChunkWindowSelect");
    const minTokEl = document.getElementById("ingestMinTokensInput");
    const piiEl = document.getElementById("ingestPiiScrubbingCheckbox") || document.getElementById("ingestPiiRedactionSelect");
    const autoExtractEl = document.getElementById("ingestAutoExtractCheckbox");

    if (srcEl && cfg.source_stream) srcEl.value = cfg.source_stream;
    if (winEl && cfg.lookback_hours !== undefined && winEl.id === "ingestLookbackHoursInput") winEl.value = String(cfg.lookback_hours);
    if (minTokEl && cfg.min_turn_tokens !== undefined) minTokEl.value = String(cfg.min_turn_tokens);
    if (piiEl && piiEl.type === "checkbox" && cfg.pii_scrubbing !== undefined) piiEl.checked = Boolean(cfg.pii_scrubbing);
    if (autoExtractEl && cfg.auto_extract_signals !== undefined) {
      autoExtractEl.checked = Boolean(cfg.auto_extract_signals);
    }
  } catch (_err) {
    // Non-fatal
  }
}

async function handleSaveIngestConfig() {
  const piiRedactionVal = document.getElementById("ingestPiiRedactionSelect")?.value;
  const piiCheckbox = document.getElementById("ingestPiiScrubbingCheckbox");
  const payload = {
    source_stream: document.getElementById("ingestSourceSelect")?.value || "BIGQUERY_AND_ANTIGRAVITY",
    lookback_hours: Number(document.getElementById("ingestLookbackHoursInput")?.value || 24),
    min_turn_tokens: Number(document.getElementById("ingestMinTokensInput")?.value || 4),
    pii_scrubbing: piiCheckbox ? Boolean(piiCheckbox.checked) : piiRedactionVal !== "NONE",
    auto_extract_signals: Boolean(document.getElementById("ingestAutoExtractCheckbox")?.checked ?? true),
    signal_detectors: [
      "EXPLICIT_CORRECTION",
      "TOOL_PREFERENCE",
      "ARCHITECTURAL_RULE",
      "FRUSTRATION_SIGNAL",
    ],
  };

  try {
    const res = await fetch("/api/studio/ingest/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Save ingest config failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    studioState.ingestConfig = data.ingest_config;
    showStatusBanner(
      `✓ Saved Ingest Pipeline Config (${data.ingest_config?.source_stream}, ${data.ingest_config?.lookback_hours}h window, min ${data.ingest_config?.min_turn_tokens} tok).`
    );
    await loadRawTelemetryStream();
  } catch (err) {
    showStatusBanner(`Ingest config error: ${err.message}`, true);
  }
}

async function loadRawTelemetryStream() {
  const tbody = document.getElementById("rawTelemetryTableBody");
  if (!tbody) return;
  const sessionSelect = document.getElementById("trajectorySessionSelect");
  const sessionId = sessionSelect?.value || "all";

  try {
    const res = await fetch(`/api/studio/ingest/raw?session_id=${encodeURIComponent(sessionId)}&limit=25`);
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Raw telemetry load failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    const events = data.raw_events || [];
    studioState.rawEvents = events;

    const badge =
      document.getElementById("rawStreamCountBadge") ||
      document.getElementById("rawEventCountBadge");
    if (badge) {
      badge.textContent = `${data.passed_filter_count || events.length} / ${data.total_count || events.length} Turns`;
    }

    if (events.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state-compact">No raw telemetry turns found.</td></tr>`;
      return;
    }

    tbody.innerHTML = "";
    for (const ev of events) {
      const tr = document.createElement("tr");
      const sig = ev.detected_signal || "NONE";
      const sigBadgeClass =
        sig === "EXPLICIT_CORRECTION" || sig === "ARCHITECTURAL_RULE"
          ? "badge-indigo"
          : sig === "TOOL_PREFERENCE"
          ? "badge-emerald"
          : "badge-slate";

      tr.innerHTML = `
        <td class="raw-scope-cell"><code>${escapeHtml(ev.session_id)}</code><small class="raw-event-id">${escapeHtml(ev.event_id)}</small></td>
        <td><span class="badge badge-slate">${escapeHtml(ev.event_type)}</span></td>
        <td class="raw-turn-cell">${escapeHtml(ev.raw_text)}</td>
        <td><code>${Number(ev.token_count || 0)} tok</code></td>
        <td>
          <span class="badge ${sigBadgeClass}">${escapeHtml(sig)}</span>
          ${
            ev.detected_candidate
              ? `<button type="button" class="btn-link-xs btn-stage-from-raw" title="Open in Memory Designer">→ Design</button>`
              : ""
          }
        </td>
      `;

      tr.querySelector(".btn-stage-from-raw")?.addEventListener("click", () => {
        const cand = ev.detected_candidate || {};
        switchStudioMode("stage-2-scopes");
        openMemoryDesignerWithRecord({
          scope_type: cand.suggested_scope_type || "USER",
          scope_id: cand.suggested_scope_id || getScopeIds().user_id,
          memory_key: cand.memory_key || "",
          memory_value: cand.memory_value || "",
          memory_type: cand.memory_type || "PREFERENCE",
          category: cand.category || "tooling",
          crystallized_belief: cand.crystallized_belief || ev.raw_text,
          keywords: cand.keywords || [],
          confidence: cand.confidence || 0.94,
        });
      });

      tbody.appendChild(tr);
    }
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-state-compact">Error loading raw stream: ${escapeHtml(err.message)}</td></tr>`;
  }
}

// ============================================================================
// Intent 2: Category Taxonomy Studio
// ============================================================================

async function loadStudioCategories() {
  try {
    const res = await fetch("/api/studio/categories");
    if (!res.ok) return;
    const data = await res.json();
    studioState.categories = data.categories || [];
    renderCategoryTaxonomyStrip();
  } catch (_err) {
    // Non-fatal
  }
}

function renderCategoryTaxonomyStrip() {
  const strip = document.getElementById("categoryTaxonomyStrip");
  if (!strip) return;
  const cats = studioState.categories || [];
  if (cats.length === 0) {
    strip.innerHTML = `<span class="empty-state-compact">No categories defined.</span>`;
    return;
  }

  strip.innerHTML = "";
  const catFilterEl = document.getElementById("memoryCategoryFilter");
  const designerCatEl = document.getElementById("designerCategory");

  for (const cat of cats) {
    const pill = document.createElement("button");
    pill.type = "button";
    const isActive = (studioState.memoryCategoryFilter || "ALL").toLowerCase() === String(cat.category_id).toLowerCase();
    pill.className = `studio-category-pill ${isActive ? "active" : ""}`;
    pill.title = `${cat.description || ""} (Default Scope: ${cat.default_scope || "USER"}, Match: ${cat.default_match_mode || "HYBRID"}, Cos≥${cat.min_cosine_similarity || 0.72})`;
    pill.innerHTML = `
      <span class="cat-pill-label">${escapeHtml(cat.label || cat.category_id)}</span>
      <code class="cat-pill-meta">${escapeHtml(cat.default_scope || "USER")} • cos≥${Number(cat.min_cosine_similarity || 0.72).toFixed(2)}</code>
    `;
    pill.addEventListener("click", () => {
      const nextVal = isActive ? "ALL" : cat.category_id;
      studioState.memoryCategoryFilter = nextVal;
      if (catFilterEl) catFilterEl.value = nextVal;
      renderCategoryTaxonomyStrip();
      renderScopeMemoryCards();
    });
    strip.appendChild(pill);

    if (catFilterEl && !Array.from(catFilterEl.options).some((o) => o.value === cat.category_id)) {
      const opt = document.createElement("option");
      opt.value = cat.category_id;
      opt.textContent = `Category: ${cat.label || cat.category_id}`;
      catFilterEl.appendChild(opt);
    }
    if (designerCatEl && !Array.from(designerCatEl.options).some((o) => o.value === cat.category_id)) {
      const opt = document.createElement("option");
      opt.value = cat.category_id;
      opt.textContent = cat.category_id;
      designerCatEl.appendChild(opt);
    }
  }
}

async function handleCategoryCreatorSubmit(evt) {
  if (evt) evt.preventDefault();
  const catId = document.getElementById("newCatIdInput")?.value.trim() || "";
  const label = document.getElementById("newCatLabelInput")?.value.trim() || catId;
  const defaultScope = document.getElementById("newCatDefaultScopeSelect")?.value || "TEAM";
  const minCosine = Number(document.getElementById("newCatCosineThresholdInput")?.value || 0.75);
  const description = document.getElementById("newCatDescInput")?.value.trim() || "";

  if (!catId) {
    showStatusBanner("Category ID is required.", true);
    return;
  }

  try {
    const res = await fetch("/api/studio/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        category_id: catId,
        label,
        default_scope: defaultScope,
        default_match_mode: defaultScope === "ORG" ? "ALWAYS_PINNED" : "HYBRID_COSINE_AND_KEYWORD",
        min_cosine_similarity: minCosine,
        description,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Save category failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    studioState.categories = data.categories || [];
    renderCategoryTaxonomyStrip();
    document.getElementById("categoryCreatorDrawer")?.classList.add("hidden");
    showStatusBanner(`✓ Saved Memory Category '${data.category?.category_id}' (${data.category?.label}).`);
  } catch (err) {
    showStatusBanner(`Category save error: ${err.message}`, true);
  }
}

// ============================================================================
// Intent 3: Scenario Re-Test Workbench & Trigger Telemetry Matrix
// ============================================================================

async function handleRunScenarioRetest() {
  const promptEl = document.getElementById("scenarioRetestPromptInput");
  const expectedEl = document.getElementById("scenarioRetestExpectedInput");
  const prompt = (promptEl?.value || "").trim() || "How should we run Python tests and deploy our service?";
  const expectedFact = (expectedEl?.value || "").trim();
  const ids = getScopeIds();

  const btn = document.getElementById("runScenarioRetestBtn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Re-Testing...";
  }

  try {
    const res = await fetch("/api/studio/scenarios/retest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt,
        expected_fact: expectedFact,
        user_id: ids.user_id,
        team_id: ids.team_id,
        agent_id: ids.agent_id,
        org_id: ids.org_id,
      }),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Scenario retest failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    renderScenarioRetestResults(data);
    await refreshScopesExplorer();
    showStatusBanner(
      `✓ Scenario Re-Tested: ${data.triggered_count || 0} memories triggered, ${data.suppressed_count || 0} suppressed (${data.token_telemetry?.total_context_tokens || 0} total context tokens).`
    );
  } catch (err) {
    showStatusBanner(`Scenario re-test error: ${err.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "⚡ Re-Test Scenario & Measure Tokens";
    }
  }
}

function renderScenarioRetestResults(data) {
  const grid = document.getElementById("scenarioRetestResultsGrid");
  if (grid) grid.classList.remove("hidden");

  const trigBadge = document.getElementById("scenarioTriggeredCountBadge");
  const supBadge = document.getElementById("scenarioSuppressedCountBadge");
  const tokBadge = document.getElementById("scenarioTokenBudgetBadge");

  const triggered = data.triggered_memories || [];
  const suppressed = data.suppressed_memories || [];
  const tok = data.token_telemetry || {};

  if (trigBadge) trigBadge.textContent = `${triggered.length} Triggered`;
  if (supBadge) supBadge.textContent = `${suppressed.length} Suppressed`;
  if (tokBadge) {
    tokBadge.textContent = `${tok.injected_memory_tokens || 0} / ${tok.max_token_budget || 256} tok (${tok.budget_utilization_pct || 0}%)`;
  }

  const trigList = document.getElementById("scenarioTriggeredList");
  if (trigList) {
    if (triggered.length === 0) {
      trigList.innerHTML = `<div class="empty-state-compact">No memories triggered for this prompt.</div>`;
    } else {
      trigList.innerHTML = triggered
        .map(
          (m) => `
          <div class="scenario-mem-item">
            <div>
              <span class="badge scope-badge-${escapeHtml((m.scope_type || "USER").toLowerCase())}">${escapeHtml(m.scope_type || "USER")}</span>
              <code>${escapeHtml(m.memory_key)}</code> = <strong>${escapeHtml(m.memory_value)}</strong>
            </div>
            <div class="scenario-mem-meta">
              Reason: <code>${escapeHtml(m.trigger_reason || "KEYWORD_OR_COSINE_MATCH")}</code> •
              Cos: <code>${Number(m.cosine_similarity || 0.85).toFixed(2)}</code> •
              Cost: <code>~${Number(m.token_footprint || 12)} tok</code> •
              Total Hits: <strong>${Number(m.trigger_count || 1)}x</strong>
            </div>
          </div>
        `
        )
        .join("");
    }
  }

  const supList = document.getElementById("scenarioSuppressedList");
  if (supList) {
    const conflicts = data.conflicts_resolved || [];
    if (suppressed.length === 0 && conflicts.length === 0) {
      supList.innerHTML = `<div class="empty-state-compact">0 suppressed memories or scope overrides.</div>`;
    } else {
      const supHtml = suppressed
        .map(
          (s) => `
          <div class="scenario-mem-item">
            <div>
              <span class="badge badge-slate">${escapeHtml(s.scope_type || "USER")}</span>
              <code>${escapeHtml(s.memory_key)}</code> = <span>${escapeHtml(s.memory_value)}</span>
            </div>
            <div class="scenario-mem-meta">Suppressed: <code>${escapeHtml(s.suppression_reason || "BELOW_THRESHOLD")}</code></div>
          </div>
        `
        )
        .join("");
      const confHtml = conflicts
        .map(
          (c) => `
          <div class="scenario-mem-item">
            <div>
              <span class="badge badge-indigo">OVERRIDE</span>
              <code>${escapeHtml(c.memory_key)}</code>: <strong>${escapeHtml(c.winning_scope)}</strong> overrode ${escapeHtml(c.overridden_scope)}
            </div>
            <div class="scenario-mem-meta">Rule: <code>${escapeHtml(c.resolution_rule || "PRECEDENCE")}</code></div>
          </div>
        `
        )
        .join("");
      supList.innerHTML = supHtml + confHtml;
    }
  }

  const tokBox = document.getElementById("scenarioTokenTelemetryBox");
  if (tokBox) {
    const matchBadge = data.expected_fact_matched
      ? `<span class="badge badge-emerald">✓ Expected Fact Recalled</span>`
      : `<span class="badge badge-indigo">⚠ Expected Fact Missing</span>`;
    tokBox.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.35rem;">
        <span>Prompt: <code>${tok.prompt_tokens || 0} tok</code> + Memory: <code>${tok.injected_memory_tokens || 0} tok</code> = <strong>${tok.total_context_tokens || 0} tok</strong></span>
        ${matchBadge}
      </div>
      <div style="font-size:0.74rem; color:var(--text-secondary);">
        Format: <code>${escapeHtml(tok.injection_format || "XML_TAGGED")}</code> • Latency: <code>${Number(data.latency_ms || 0).toFixed(1)} ms</code>
      </div>
    `;
  }
}

function renderTriggerTelemetryMatrix() {
  const tbody = document.getElementById("memoryTriggerTelemetryTableBody");
  if (!tbody) return;

  const tiers = ["ORG", "TEAM", "AGENT", "USER"];
  const allMems = [];
  for (const t of tiers) {
    for (const m of studioState.scopesData[t] || []) {
      allMems.push({ ...m, scope_type: m.scope_type || t });
    }
  }

  if (allMems.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state-compact">No memories loaded yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = "";
  for (const m of allMems) {
    const st = (m.scope_type || "USER").toUpperCase();
    const trigCond = m.trigger_conditions || {};
    const trigMode = trigCond.match_mode || "HYBRID_COSINE_AND_KEYWORD";
    const trigCos = Number(trigCond.min_cosine_similarity !== undefined ? trigCond.min_cosine_similarity : 0.72).toFixed(2);
    const syncStatus = String(m.sync_status || "SYNCED").toUpperCase();
    const syncClass = syncStatus === "STAGED" ? "badge-indigo" : "badge-emerald";

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><span class="badge scope-badge-${escapeHtml(st.toLowerCase())}">${escapeHtml(st)}</span></td>
      <td><code>${escapeHtml(m.memory_key)}</code></td>
      <td><code>${escapeHtml(trigMode)} (cos≥${trigCos})</code></td>
      <td><strong>${Number(m.trigger_count || 0)}x</strong></td>
      <td><code>${escapeHtml(m.last_trigger_reason || "—")}</code></td>
      <td><code>~${Number(m.token_footprint || 12)} tok</code></td>
      <td><span class="badge ${syncClass}">${escapeHtml(syncStatus)}</span></td>
    `;
    tbody.appendChild(tr);
  }
}

// ============================================================================
// Intent 4: Memory Store Sync (Staging -> Saved Commit)
// ============================================================================

async function loadSyncStatus() {
  const ids = getScopeIds();
  const params = new URLSearchParams({
    user_id: ids.user_id,
    team_id: ids.team_id,
    agent_id: ids.agent_id,
    org_id: ids.org_id,
  });

  try {
    const res = await fetch(`/api/studio/sync/status?${params.toString()}`);
    if (!res.ok) return;
    const data = await res.json();
    studioState.syncStatus = data;
    renderSyncStatusView(data);
  } catch (_err) {
    // Non-fatal
  }
}

function renderSyncStatusView(data) {
  if (!data) return;
  const stagedCount = Number(data.staged_count || 0);
  const syncedCount = Number(data.synced_count || 0);

  const stagedBadge = document.getElementById("syncStagedCountBadge");
  const syncedBadge = document.getElementById("syncSyncedCountBadge");
  const cmdBarStagedBadge = document.getElementById("cmdBarStagedSyncBadge");

  if (stagedBadge) stagedBadge.textContent = `${stagedCount} Staged`;
  if (syncedBadge) syncedBadge.textContent = `${syncedCount} Synced`;
  if (cmdBarStagedBadge) {
    cmdBarStagedBadge.textContent =
      stagedCount > 0 ? `${stagedCount} Staged` : `${syncedCount} Synced`;
  }

  const tbody = document.getElementById("stagedSyncTableBody");
  if (!tbody) return;

  const stagedList = data.staged_memories || [];
  const syncedList = data.synced_memories || [];
  const combined = [...stagedList, ...syncedList];

  if (combined.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-state-compact">No memories found across scopes.</td></tr>`;
    return;
  }

  tbody.innerHTML = "";
  for (const m of combined) {
    const st = (m.scope_type || "USER").toUpperCase();
    const syncStatus = String(m.sync_status || "SYNCED").toUpperCase();
    const syncClass = syncStatus === "STAGED" ? "badge-indigo" : "badge-emerald";
    const trigCond = m.trigger_conditions || {};
    const trigMode = trigCond.match_mode || "HYBRID_COSINE_AND_KEYWORD";

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><span class="badge scope-badge-${escapeHtml(st.toLowerCase())}">${escapeHtml(st)}: ${escapeHtml(m.scope_id || "")}</span></td>
      <td><code>${escapeHtml(m.memory_key)}</code> = <span>${escapeHtml(m.memory_value)}</span></td>
      <td><span class="badge badge-slate">${escapeHtml(m.category || m.memory_type || "general")}</span></td>
      <td><code>${escapeHtml(trigMode)}</code></td>
      <td><code>~${Number(m.token_footprint || 12)} tok</code></td>
      <td><span class="badge ${syncClass}">${escapeHtml(syncStatus)}</span></td>
    `;
    tbody.appendChild(tr);
  }
}

async function handlePushToSpanner() {
  const ids = getScopeIds();
  const btn = document.getElementById("pushToSpannerBtn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Saving...";
  }

  try {
    const res = await fetch("/api/studio/sync/push", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ids),
    });
    if (!res.ok) {
      const errText = await res.text();
      throw new Error(`Save failed (${res.status}): ${errText}`);
    }
    const data = await res.json();
    const receiptBox = document.getElementById("spannerSyncReceiptBox");
    if (receiptBox) {
      receiptBox.classList.remove("hidden");
      receiptBox.innerHTML = `
        <div><strong>Saved:</strong> <code>${escapeHtml(data.transaction_tag || "")}</code></div>
        <div style="font-size:0.78rem; color:var(--text-secondary); margin-top:0.2rem;">
          Records: <strong>${Number(data.synced_count || 0)}</strong> •
          Saved At: <code>${escapeHtml(data.committed_at || "")}</code>
        </div>
      `;
    }
    showStatusBanner(
      `Saved ${data.synced_count || 0} staged rule(s) (${data.transaction_tag}).`
    );
    await refreshScopesExplorer();
    await loadSyncStatus();
  } catch (err) {
    showStatusBanner(`Save error: ${err.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Save staged";
    }
  }
}

// ============================================================================
// Initialization & Layout Event Listeners
// ============================================================================

document.addEventListener("DOMContentLoaded", async () => {
  // 1. Left Rail Mode Switcher & Collapse Toggle
  const leftNav = document.getElementById("leftNavSidebar");
  const bodyLayout = document.getElementById("appBodyLayout");
  const toggleBtn = document.getElementById("leftNavToggleBtn");
  let userOverrodeLeftNav = false;
  if (toggleBtn && leftNav && bodyLayout) {
    toggleBtn.addEventListener("click", () => {
      userOverrodeLeftNav = true;
      const collapsed = leftNav.classList.toggle("collapsed");
      bodyLayout.classList.toggle("nav-collapsed", collapsed);
      toggleBtn.textContent = collapsed ? "▶" : "◀";
    });
  }

  document.querySelectorAll(".left-nav-item").forEach((item) => {
    item.addEventListener("click", (evt) => {
      evt.preventDefault();
      const targetStage = item.getAttribute("data-stage");
      if (targetStage) {
        switchStudioMode(targetStage);
      }
    });
  });

  // 2. Topbar Info Disclosure (native <details>/<summary> inside #viewMenuPopover needs no JS toggle)
  const infoBtn = document.getElementById("sessionInfoBtn");
  const infoTooltip = document.getElementById("sessionInfoTooltip");
  if (infoBtn && infoTooltip && infoBtn.tagName !== "SUMMARY") {
    infoBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      infoTooltip.classList.toggle("hidden");
    });
    document.addEventListener("click", () => {
      infoTooltip.classList.add("hidden");
    });
  }

  // 3. Native <dialog id="githubModal">
  const githubBtn = document.getElementById("githubPopoutBtn");
  const githubModal = document.getElementById("githubModal");
  const closeGithubBtn = document.getElementById("closeGithubModalBtn");
  if (githubBtn && githubModal) {
    githubBtn.addEventListener("click", () => {
      if (typeof githubModal.showModal === "function") {
        githubModal.showModal();
      } else {
        githubModal.setAttribute("open", "open");
      }
    });
  }
  if (closeGithubBtn && githubModal) {
    closeGithubBtn.addEventListener("click", () => {
      if (typeof githubModal.close === "function") {
        githubModal.close();
      } else {
        githubModal.removeAttribute("open");
      }
    });
  }

  // 4. Right Details, Settings & Guide Drawer + View Popover controls
  const viewMenuPopover = document.getElementById("viewMenuPopover");
  const hideViewPopover = () => {
    if (viewMenuPopover && typeof viewMenuPopover.hidePopover === "function") {
      try { viewMenuPopover.hidePopover(); } catch (_) {}
    }
  };

  const applySavedTheme = (theme) => {
    const isDark = theme === "dark";
    if (isDark) {
      document.documentElement.setAttribute("data-theme", "dark");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    document.querySelectorAll("[data-toggle-theme]").forEach((btn) => {
      btn.textContent = isDark ? "Light theme" : "Dark theme";
    });
  };
  let initialTheme = localStorage.getItem("sheep-theme") || "light";
  try {
    const queryTheme = new URLSearchParams(window.location.search).get("theme");
    if (queryTheme === "dark" || queryTheme === "light") initialTheme = queryTheme;
  } catch (_) {}
  applySavedTheme(initialTheme);
  document.querySelectorAll("[data-toggle-theme]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      localStorage.setItem("sheep-theme", next);
      applySavedTheme(next);
      hideViewPopover();
    });
  });

  const closeInspectorBtn = document.getElementById("closeInspectorBtn");
  const inspectorDrawer = document.getElementById("studioInspectorDrawer");
  const toggleInspectorBtn = document.getElementById("toggleInspectorBtn");
  const openStudioGuideBtn = document.getElementById("openStudioGuideBtn");
  const inspectorPromoteBtn = document.getElementById("inspectorPromoteConfigBtn");
  const tabBtnRunSettings = document.getElementById("tabBtnRunSettings");
  const tabBtnJsonInspector = document.getElementById("tabBtnJsonInspector");
  const tabBtnStudioGuide = document.getElementById("tabBtnStudioGuide");
  let userOverrodeInspector = false;

  if (tabBtnRunSettings) {
    tabBtnRunSettings.addEventListener("click", () => switchInspectorTab("settings"));
  }
  if (tabBtnJsonInspector) {
    tabBtnJsonInspector.addEventListener("click", () => switchInspectorTab("json"));
  }
  if (tabBtnStudioGuide) {
    tabBtnStudioGuide.addEventListener("click", () => switchInspectorTab("guide"));
  }
  if (openStudioGuideBtn && inspectorDrawer) {
    openStudioGuideBtn.addEventListener("click", () => {
      userOverrodeInspector = true;
      hideViewPopover();
      inspectorDrawer.classList.remove("collapsed");
      switchInspectorTab("guide");
    });
  }

  if (closeInspectorBtn && inspectorDrawer) {
    closeInspectorBtn.addEventListener("click", () => {
      userOverrodeInspector = true;
      inspectorDrawer.classList.add("collapsed");
    });
  }
  if (toggleInspectorBtn && inspectorDrawer) {
    toggleInspectorBtn.addEventListener("click", () => {
      userOverrodeInspector = true;
      hideViewPopover();
      const willOpen = inspectorDrawer.classList.contains("collapsed");
      if (willOpen) {
        openInspectorDrawer(
          "Active Retrieval Settings",
          "",
          studioState.activeConfig || {},
          studioState.activeConfig
        );
      } else {
        inspectorDrawer.classList.add("collapsed");
      }
    });
  }

  // Responsive viewport auto-collapse for right inspector (<= 1200px) & left nav (<= 900px)
  if (typeof window.matchMedia === "function") {
    const mqInspector = window.matchMedia("(max-width: 1200px)");
    const syncInspectorViewport = (matches) => {
      if (!inspectorDrawer || userOverrodeInspector) return;
      if (matches) {
        inspectorDrawer.classList.add("collapsed");
      }
    };
    syncInspectorViewport(mqInspector.matches);
    if (typeof mqInspector.addEventListener === "function") {
      mqInspector.addEventListener("change", (e) => syncInspectorViewport(e.matches));
    }

    const mqLeftNav = window.matchMedia("(max-width: 900px)");
    const syncLeftNavViewport = (matches) => {
      if (!leftNav || !bodyLayout || userOverrodeLeftNav) return;
      const shouldCollapse = Boolean(matches);
      leftNav.classList.toggle("collapsed", shouldCollapse);
      bodyLayout.classList.toggle("nav-collapsed", shouldCollapse);
      if (toggleBtn) toggleBtn.textContent = shouldCollapse ? "▶" : "◀";
    };
    syncLeftNavViewport(mqLeftNav.matches);
    if (typeof mqLeftNav.addEventListener === "function") {
      mqLeftNav.addEventListener("change", (e) => syncLeftNavViewport(e.matches));
    }
  }
  if (inspectorPromoteBtn) {
    inspectorPromoteBtn.addEventListener("click", async () => {
      if (studioState.inspectedConfigCandidate) {
        await handlePromoteConfig(studioState.inspectedConfigCandidate);
      }
    });
  }

  // Interactive Run Settings Controls (Matryoshka Slice, Format, Cosine Threshold, Pareto Alpha)
  let selectedRunDim = 768;
  document.querySelectorAll("#settingDimButtonGroup .seg-pill").forEach((pill) => {
    pill.addEventListener("click", () => {
      document.querySelectorAll("#settingDimButtonGroup .seg-pill").forEach((p) => p.classList.remove("active"));
      pill.classList.add("active");
      selectedRunDim = Number(pill.getAttribute("data-dim") || 768);
      const dimBadge = document.getElementById("settingDimBadge");
      if (dimBadge) dimBadge.textContent = `${selectedRunDim}d`;
    });
  });

  const settingCosineSlider = document.getElementById("settingCosineSlider");
  const settingCosineVal = document.getElementById("settingCosineVal");
  if (settingCosineSlider && settingCosineVal) {
    settingCosineSlider.addEventListener("input", () => {
      settingCosineVal.textContent = Number(settingCosineSlider.value).toFixed(2);
    });
  }

  const syncAlphaWeightValue = (rawVal) => {
    const num = Number(rawVal);
    const v = num.toFixed(2);
    const evalAlpha = document.getElementById("evalAlphaSlider");
    const evalAlphaText = document.getElementById("evalAlphaVal");
    const evolveAlpha = document.getElementById("evolveAlphaSlider");
    const evolveAlphaText = document.getElementById("evolveAlphaVal");
    const settingAlpha = document.getElementById("settingAlphaSlider");
    const settingAlphaText = document.getElementById("settingAlphaVal");
    const optimizeAlpha = document.getElementById("optimizeAlphaSlider");
    const optimizeAlphaText = document.getElementById("optimizeAlphaVal");
    if (evalAlpha) evalAlpha.value = v;
    if (evalAlphaText) evalAlphaText.textContent = v;
    if (evolveAlpha) evolveAlpha.value = v;
    if (evolveAlphaText) evolveAlphaText.textContent = v;
    if (settingAlpha) settingAlpha.value = v;
    if (settingAlphaText) settingAlphaText.textContent = v;
    if (optimizeAlpha) optimizeAlpha.value = v;
    if (optimizeAlphaText) optimizeAlphaText.textContent = `${Math.round(num * 100)}% Quality`;
  };

  const evalAlphaSliderEl = document.getElementById("evalAlphaSlider");
  if (evalAlphaSliderEl) {
    evalAlphaSliderEl.addEventListener("input", () => {
      syncAlphaWeightValue(evalAlphaSliderEl.value);
    });
  }
  const evolveAlphaSliderEl = document.getElementById("evolveAlphaSlider");
  if (evolveAlphaSliderEl && evolveAlphaSliderEl.type === "range") {
    evolveAlphaSliderEl.addEventListener("input", () => {
      syncAlphaWeightValue(evolveAlphaSliderEl.value);
    });
  }
  const settingAlphaSlider = document.getElementById("settingAlphaSlider");
  if (settingAlphaSlider) {
    settingAlphaSlider.addEventListener("input", () => {
      syncAlphaWeightValue(settingAlphaSlider.value);
    });
  }
  const optimizeAlphaSlider = document.getElementById("optimizeAlphaSlider");
  if (optimizeAlphaSlider) {
    optimizeAlphaSlider.addEventListener("input", () => {
      syncAlphaWeightValue(optimizeAlphaSlider.value);
    });
  }

  document.getElementById("applyRunSettingsBtn")?.addEventListener("click", async () => {
    const activePill = document.querySelector("#settingDimButtonGroup .seg-pill.active");
    const dim = activePill ? Number(activePill.getAttribute("data-dim")) : selectedRunDim;
    const fmt = document.getElementById("settingFormatSelect")?.value || "COMPACT_BULLET";
    const thresh = Number(document.getElementById("settingCosineSlider")?.value || 0.65);
    const baseCfg = studioState.activeConfig || {};
    const customGenome = {
      ...baseCfg,
      config_id: `studio-custom-${dim}d-${fmt.toLowerCase()}`,
      embedding_dimensions: dim,
      injection_format: fmt,
      retrieval_cosine_threshold: thresh,
    };
    await handlePromoteConfig(customGenome);
  });

  function shortenContextName(text, maxLen = 18) {
    const clean = String(text || "").trim();
    if (!clean) return "";
    const head = clean.split("—")[0].split("(")[0].trim();
    return head.length > maxLen ? `${head.slice(0, maxLen - 1)}…` : head;
  }

  function updateSignedInUserReadout() {
    const readoutEl = document.getElementById("activeSignedInUserBadge");
    if (!readoutEl) return;
    const override = (document.getElementById("userIdInput")?.value || "").trim();
    let stored = "";
    try {
      stored = sessionStorage.getItem("electric_sheep_user_email") || "";
    } catch (_) {}
    readoutEl.textContent = override || studioState.authenticatedEmail || stored || "Signed-in user";
  }

  function updateTopScopeTriggerLabel() {
    const labelEl = document.getElementById("topScopeTriggerLabel");
    if (!labelEl) return;
    const teamEl = document.getElementById("teamIdInput");
    const agentEl = document.getElementById("agentIdInput");
    const userVal = (document.getElementById("userIdInput")?.value || "").trim();
    const parts = [];
    if (teamEl && teamEl.value.trim()) {
      const optLabel = teamEl.selectedOptions?.[0]?.textContent || teamEl.value.trim();
      parts.push(shortenContextName(optLabel));
    }
    if (agentEl && agentEl.value.trim()) {
      const optLabel = agentEl.selectedOptions?.[0]?.textContent || agentEl.value.trim();
      parts.push(shortenContextName(optLabel));
    }
    if (userVal) {
      parts.push(shortenContextName(userVal));
    }
    labelEl.textContent = parts.length ? `Context · ${parts.join(" / ")}` : "Context";
  }

  // 4B. Top-Bar Active Context Picker & Scope Tier Filter
  ["userIdInput", "teamIdInput", "agentIdInput"].forEach((id) => {
    document.getElementById(id)?.addEventListener("change", async () => {
      updateSignedInUserReadout();
      updateTopScopeTriggerLabel();
      await loadWsMemoryTree();
      await refreshScopesExplorer();
      await loadSyncStatus();
    });
  });

  document.getElementById("contextManageScopesBtn")?.addEventListener("click", async () => {
    closeAllToolbarDropdowns();
    if (window.history && typeof window.history.replaceState === "function") {
      window.history.replaceState(null, "", "#settings");
    }
    await switchWorkspace("workspace-entities");
  });

  function syncScopeFilterPillsAndTabs(nextScopeFilter) {
    studioState.activeScopeFilter = nextScopeFilter || "ALL";
    document.querySelectorAll("#leftScopePillsContainer .left-scope-pill[data-scope-filter]").forEach((pill) => {
      const isMatch = pill.getAttribute("data-scope-filter") === studioState.activeScopeFilter;
      pill.classList.toggle("active", isMatch);
      pill.setAttribute("aria-pressed", isMatch ? "true" : "false");
    });
    document.querySelectorAll(".scope-tab").forEach((tab) => {
      tab.classList.toggle("active", tab.getAttribute("data-scope-filter") === studioState.activeScopeFilter);
    });
    updateTopScopeTriggerLabel();
    renderWsMemoryTree();
    renderScopeMemoryCards();
    renderLeftMemoryTree();
    if (wsState.memoriesViewMode === "graph") {
      renderWsMemoriesGraph();
    }
  }

  document.querySelectorAll("#leftScopePillsContainer .left-scope-pill[data-scope-filter]").forEach((pill) => {
    pill.addEventListener("click", () => {
      syncScopeFilterPillsAndTabs(pill.getAttribute("data-scope-filter") || "ALL");
    });
  });

  const streamSourceMapping = {
    session_transcripts: "ANTIGRAVITY_TRANSCRIPTS",
    live_chat_events: "BIGQUERY_AND_ANTIGRAVITY",
    activity_log: "BIGQUERY_LAKEHOUSE",
    mcp_events: "MCP_EVENTS",
  };
  document.querySelectorAll(".file-tree-item[data-stream-source]").forEach((item) => {
    item.addEventListener("click", async () => {
      document.querySelectorAll(".file-tree-item[data-stream-source]").forEach((el) => el.classList.remove("active"));
      item.classList.add("active");
      const srcKey = item.getAttribute("data-stream-source") || "live_chat_events";
      const mappedStream = streamSourceMapping[srcKey] || "BIGQUERY_AND_ANTIGRAVITY";
      const srcSelect = document.getElementById("ingestSourceSelect");
      if (srcSelect) {
        srcSelect.value = mappedStream;
      }
      switchStudioMode("stage-1-sandbox");
      await loadRawTelemetryStream();
    });
  });

  document.getElementById("fileTreeExportPackNode")?.addEventListener("click", async () => {
    switchStudioMode("stage-2-scopes");
    await handleExportMemoryPack();
  });
  document.getElementById("fileTreeImportPackNode")?.addEventListener("click", () => {
    switchStudioMode("stage-2-scopes");
    document.getElementById("memoryPackDrawer")?.classList.remove("hidden");
  });

  const leftMemoryTreeSearch = document.getElementById("leftMemoryTreeSearch");
  if (leftMemoryTreeSearch) {
    leftMemoryTreeSearch.addEventListener("input", () => {
      studioState.leftTreeSearchQuery = leftMemoryTreeSearch.value || "";
      renderLeftMemoryTree();
    });
  }
  document.getElementById("leftNewMemoryBtn")?.addEventListener("click", () => {
    openLeftMemoryQuickEditor(null);
  });
  document.getElementById("closeLeftQuickEditorBtn")?.addEventListener("click", () => {
    document.getElementById("leftMemoryQuickEditor")?.classList.add("hidden");
  });
  document.getElementById("leftQuickSaveMemoryBtn")?.addEventListener("click", handleLeftQuickSaveMemory);
  document.getElementById("leftQuickPromoteBtn")?.addEventListener("click", handleLeftQuickPromote);
  document.getElementById("leftQuickDeleteBtn")?.addEventListener("click", handleLeftQuickDelete);

  // 5. Mode 1 Playground & Drawer Listeners (including Guide Pane Sample Loaders)
  document.getElementById("zeroResetBtn")?.addEventListener("click", handleZeroReset);
  document.getElementById("seedMultiScopeBtn")?.addEventListener("click", handleSeedMultiScopeDemo);
  document.getElementById("studioChatForm")?.addEventListener("submit", handleSandboxChatSubmit);
  document.getElementById("directFeedbackForm")?.addEventListener("submit", handleDirectFeedbackForm);

  document.getElementById("loadSampleTranscriptGuideBtn")?.addEventListener("click", async () => {
    switchStudioMode("stage-1-sandbox");
    document.getElementById("trajectoryExtractorCard")?.classList.remove("hidden");
    document.getElementById("rawTrajectoryDrawer")?.classList.remove("hidden");
    const rawInput = document.getElementById("rawTrajectoryInput");
    if (rawInput) {
      rawInput.value =
        "User: Always use uv run pytest -v for Python testing and never mock endpoints; enforce Google Cloud ADC auth and use Apple Container instead of Docker.";
    }
    await handleExtractTrajectoryCandidates();
  });

  const toggleCtxBtn = document.getElementById("toggleSystemContextBtn");
  const ctxDrawerBody =
    document.getElementById("playgroundSystemContextBody") ||
    document.getElementById("systemContextDrawerBody");
  if (toggleCtxBtn && ctxDrawerBody) {
    toggleCtxBtn.addEventListener("click", () => {
      const hidden = ctxDrawerBody.classList.toggle("hidden");
      toggleCtxBtn.textContent = hidden ? "Show Context Prompt ▼" : "Hide Context Prompt ▲";
    });
  }

  const toggleFbBuilderBtn = document.getElementById("toggleManualFeedbackBuilderBtn");
  const closeFbDrawerBtn =
    document.getElementById("closeManualFeedbackBtn") ||
    document.getElementById("closeManualFeedbackDrawerBtn");
  const fbDrawer = document.getElementById("manualFeedbackDrawer");
  if (toggleFbBuilderBtn && fbDrawer) {
    toggleFbBuilderBtn.addEventListener("click", () => {
      fbDrawer.classList.toggle("hidden");
    });
  }
  if (closeFbDrawerBtn && fbDrawer) {
    closeFbDrawerBtn.addEventListener("click", () => {
      fbDrawer.classList.add("hidden");
    });
  }

  document.querySelectorAll(".studio-prompt-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      switchStudioMode("stage-1-sandbox");
      const promptInput = document.getElementById("studioPromptInput");
      if (promptInput) {
        promptInput.value = chip.getAttribute("data-prompt") || "";
        promptInput.focus();
      }
    });
  });

  // 5B. Trajectory Candidate Extractor Listeners
  const toggleTrajBtn = document.getElementById("toggleTrajectoryPanelBtn");
  const trajCard = document.getElementById("trajectoryExtractorCard");
  if (toggleTrajBtn && trajCard) {
    toggleTrajBtn.addEventListener("click", () => {
      trajCard.classList.toggle("hidden");
      if (!trajCard.classList.contains("hidden")) {
        loadTrajectorySessions();
      }
    });
  }

  document.getElementById("extractTrajectoryCandidatesBtn")?.addEventListener("click", handleExtractTrajectoryCandidates);

  const toggleRawTrajBtn = document.getElementById("toggleRawTrajectoryBtn");
  const rawTrajDrawer = document.getElementById("rawTrajectoryDrawer");
  if (toggleRawTrajBtn && rawTrajDrawer) {
    toggleRawTrajBtn.addEventListener("click", () => {
      const hidden = rawTrajDrawer.classList.toggle("hidden");
      toggleRawTrajBtn.textContent = hidden ? "Paste Raw Transcript ▼" : "Hide Raw Transcript ▲";
    });
  }

  document.querySelectorAll(".studio-signal-pill").forEach((pill) => {
    pill.addEventListener("click", () => {
      document.querySelectorAll(".studio-signal-pill").forEach((p) => p.classList.remove("active"));
      pill.classList.add("active");
      studioState.activeSignalFilter =
        pill.getAttribute("data-signal-filter") || pill.getAttribute("data-signal") || "ALL";
      renderTrajectoryCandidates();
    });
  });

  // 6. Mode 2 Listeners (Scoped Memory Bank, Search/Filter, Inline Designer & Pack Sharing)
  document.getElementById("runFederatedLookupBtn")?.addEventListener("click", refreshScopesExplorer);
  document.getElementById("runConsensusBtn")?.addEventListener("click", handleRunConsensus);

  document.querySelectorAll(".scope-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      syncScopeFilterPillsAndTabs(tab.getAttribute("data-scope-filter") || "ALL");
    });
  });

  const memorySearchInput = document.getElementById("memorySearchInput");
  if (memorySearchInput) {
    memorySearchInput.addEventListener("input", () => {
      studioState.memorySearchQuery = memorySearchInput.value;
      renderScopeMemoryCards();
    });
  }

  const memoryCategoryFilter = document.getElementById("memoryCategoryFilter");
  if (memoryCategoryFilter) {
    memoryCategoryFilter.addEventListener("change", () => {
      studioState.memoryCategoryFilter = memoryCategoryFilter.value || "ALL";
      renderScopeMemoryCards();
    });
  }

  const toggleMemoryDesignerBtn = document.getElementById("toggleMemoryDesignerBtn");
  const closeMemoryDesignerBtn = document.getElementById("closeMemoryDesignerBtn");
  const memoryDesignerCard = document.getElementById("memoryDesignerCard");
  if (toggleMemoryDesignerBtn && memoryDesignerCard) {
    toggleMemoryDesignerBtn.addEventListener("click", () => {
      const willShow = memoryDesignerCard.classList.contains("hidden");
      if (willShow) {
        openMemoryDesignerWithRecord(null);
      } else {
        memoryDesignerCard.classList.add("hidden");
      }
    });
  }
  if (closeMemoryDesignerBtn && memoryDesignerCard) {
    closeMemoryDesignerBtn.addEventListener("click", () => {
      memoryDesignerCard.classList.add("hidden");
    });
  }
  document.getElementById("memoryDesignerForm")?.addEventListener("submit", handleMemoryDesignerSubmit);
  ["designerMemoryKey", "designerMemoryValue", "designerBelief"].forEach((id) => {
    document.getElementById(id)?.addEventListener("input", updateDesignerTokenPreview);
  });

  // Intent 1 Ingest Config & Raw Telemetry Listeners
  document.getElementById("saveIngestConfigBtn")?.addEventListener("click", handleSaveIngestConfig);
  document.getElementById("refreshRawIngestBtn")?.addEventListener("click", loadRawTelemetryStream);

  // Intent 2 Category Taxonomy Listeners
  const toggleCategoryCreatorBtn = document.getElementById("toggleCategoryCreatorBtn");
  const closeCategoryCreatorBtn = document.getElementById("closeCategoryCreatorBtn");
  const categoryCreatorDrawer = document.getElementById("categoryCreatorDrawer");
  if (toggleCategoryCreatorBtn && categoryCreatorDrawer) {
    toggleCategoryCreatorBtn.addEventListener("click", () => {
      categoryCreatorDrawer.classList.toggle("hidden");
    });
  }
  if (closeCategoryCreatorBtn && categoryCreatorDrawer) {
    closeCategoryCreatorBtn.addEventListener("click", () => {
      categoryCreatorDrawer.classList.add("hidden");
    });
  }
  document.getElementById("categoryCreatorForm")?.addEventListener("submit", handleCategoryCreatorSubmit);

  document.getElementById("exportMemoryPackBtn")?.addEventListener("click", handleExportMemoryPack);
  const importMemoryPackBtn = document.getElementById("importMemoryPackBtn");
  const memoryPackDrawer = document.getElementById("memoryPackDrawer");
  const closeMemoryPackDrawerBtn = document.getElementById("closeMemoryPackDrawerBtn");
  if (importMemoryPackBtn && memoryPackDrawer) {
    importMemoryPackBtn.addEventListener("click", () => {
      memoryPackDrawer.classList.remove("hidden");
    });
  }
  if (closeMemoryPackDrawerBtn && memoryPackDrawer) {
    closeMemoryPackDrawerBtn.addEventListener("click", () => {
      memoryPackDrawer.classList.add("hidden");
    });
  }
  document.getElementById("confirmImportMemoryPackBtn")?.addEventListener("click", handleImportMemoryPack);
  document.getElementById("copyMemoryPackJsonBtn")?.addEventListener("click", async () => {
    const textarea = document.getElementById("memoryPackJsonTextarea");
    if (textarea && textarea.value && navigator.clipboard) {
      await navigator.clipboard.writeText(textarea.value);
      showStatusBanner("✓ Copied Portable Memory Pack JSON to clipboard.");
    }
  });

  // 7. Mode 3 Listeners (Scenario Re-Test Workbench, Trigger Telemetry Matrix, Eval Suite)
  document.getElementById("runScenarioRetestBtn")?.addEventListener("click", handleRunScenarioRetest);
  document.querySelectorAll(".scenario-preset-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      switchStudioMode("stage-3-eval");
      const p = btn.getAttribute("data-scenario-prompt") || "";
      const exp = btn.getAttribute("data-scenario-expected") || "";
      const pEl = document.getElementById("scenarioRetestPromptInput");
      const eEl = document.getElementById("scenarioRetestExpectedInput");
      if (pEl) pEl.value = p;
      if (eEl) eEl.value = exp;
      await handleRunScenarioRetest();
    });
  });

  const evalAlphaSlider = document.getElementById("evalAlphaSlider");
  const evalAlphaVal = document.getElementById("evalAlphaVal");
  if (evalAlphaSlider && evalAlphaVal) {
    evalAlphaSlider.addEventListener("input", () => {
      evalAlphaVal.textContent = Number(evalAlphaSlider.value).toFixed(2);
    });
  }
  document.getElementById("generateAdversarialBtn")?.addEventListener("click", handleGenerateAdversarialCases);
  document.getElementById("runEvalSuiteBtn")?.addEventListener("click", handleRunEvalSuite);

  // 8. Mode 4 Listeners (Memory Store Sync & Optimizer)
  document.getElementById("refreshSyncStatusBtn")?.addEventListener("click", loadSyncStatus);
  document.getElementById("pushToSpannerBtn")?.addEventListener("click", handlePushToSpanner);

  const evolveAlphaSlider = document.getElementById("evolveAlphaSlider");
  const evolveAlphaVal = document.getElementById("evolveAlphaVal");
  if (evolveAlphaSlider && evolveAlphaVal) {
    evolveAlphaSlider.addEventListener("input", () => {
      evolveAlphaVal.textContent = Number(evolveAlphaSlider.value).toFixed(2);
    });
  }
  document.getElementById("runAlphaEvolveBtn")?.addEventListener("click", handleRunAlphaEvolve);
  document.getElementById("promoteChampionBtn")?.addEventListener("click", async () => {
    if (studioState.championCandidate?.config) {
      await handlePromoteConfig(studioState.championCandidate.config);
    }
  });

  // 9. Contextual Top Command Bar Actions (Scoped per Active Intent) & Dropdown Auto-Close
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.toolbar-dropdown[open]").forEach((dropdown) => {
      if (!dropdown.contains(e.target)) {
        dropdown.removeAttribute("open");
      }
    });
  });

  document.querySelectorAll("details.toolbar-dropdown").forEach((dropdown) => {
    dropdown.addEventListener("toggle", () => {
      if (dropdown.open) {
        document.querySelectorAll("details.toolbar-dropdown[open]").forEach((other) => {
          if (other !== dropdown) other.removeAttribute("open");
        });
      }
    });
  });

  // Intent 1 Commands
  document.getElementById("cmdConfigureIngestBtn")?.addEventListener("click", () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-1-sandbox");
    document.getElementById("ingestConfigCard")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  const handleToolbarRefreshStream = async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-1-sandbox");
    await loadRawTelemetryStream();
    showStatusBanner("Reloaded session logs.");
  };
  document.getElementById("refreshRawStreamCmdBtn")?.addEventListener("click", handleToolbarRefreshStream);
  document.getElementById("cmdRefreshRawStreamBtn")?.addEventListener("click", handleToolbarRefreshStream);

  document.getElementById("saveIngestConfigCmdBtn")?.addEventListener("click", async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-1-sandbox");
    await handleSaveIngestConfig();
  });

  document.getElementById("openTrajectoryIngestCmdBtn")?.addEventListener("click", async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-1-sandbox");
    document.getElementById("trajectoryExtractorCard")?.classList.remove("hidden");
    await loadTrajectorySessions();
    await handleExtractTrajectoryCandidates();
  });

  // Intent 2 Commands
  document.getElementById("openMemoryDesignerCmdBtn")?.addEventListener("click", () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-2-scopes");
    openMemoryDesignerWithRecord(null);
  });
  const handleToolbarOpenCategoryCreator = () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-2-scopes");
    document.getElementById("categoryCreatorDrawer")?.classList.remove("hidden");
    document.getElementById("newCatIdInput")?.focus();
  };
  document.getElementById("openCategoryCreatorCmdBtn")?.addEventListener("click", handleToolbarOpenCategoryCreator);
  document.getElementById("cmdOpenCategoryCreatorBtn")?.addEventListener("click", handleToolbarOpenCategoryCreator);

  document.getElementById("intentSeedMultiScope")?.addEventListener("click", async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-2-scopes");
    await handleSeedMultiScopeDemo();
  });
  document.getElementById("intentRunConsensus")?.addEventListener("click", async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-2-scopes");
    await handleRunConsensus();
  });
  document.getElementById("intentJumpToScenarioCmdBtn")?.addEventListener("click", () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-3-eval");
    document.getElementById("scenarioRetestCard")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  // Intent 3 Commands
  const handleToolbarRunScenarioRetest = async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-3-eval");
    await handleRunScenarioRetest();
  };
  document.getElementById("runScenarioRetestCmdBtn")?.addEventListener("click", handleToolbarRunScenarioRetest);
  document.getElementById("cmdRunScenarioRetestBtn")?.addEventListener("click", handleToolbarRunScenarioRetest);

  document.getElementById("intentSynthesizeAdversarialCmdBtn")?.addEventListener("click", async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-3-eval");
    await handleGenerateAdversarialCases();
  });
  document.getElementById("intentRunRedTeam")?.addEventListener("click", async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-3-eval");
    await handleGenerateAdversarialCases();
    await handleRunEvalSuite();
  });
  document.getElementById("cmdCreateMemoryFromScenarioBtn")?.addEventListener("click", () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-2-scopes");
    openMemoryDesignerWithRecord(null);
  });

  // Intent 4 Commands
  const handleToolbarPushToSpanner = async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-4-evolve");
    await handlePushToSpanner();
  };
  document.getElementById("pushToSpannerCmdBtn")?.addEventListener("click", handleToolbarPushToSpanner);
  document.getElementById("cmdPushSpannerSyncBtn")?.addEventListener("click", handleToolbarPushToSpanner);

  document.getElementById("intentRunAlphaEvolve")?.addEventListener("click", async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-4-evolve");
    await handleRunAlphaEvolve();
  });
  document.getElementById("openMemoryPackCmdBtn")?.addEventListener("click", async () => {
    closeAllToolbarDropdowns();
    switchStudioMode("stage-2-scopes");
    await handleExportMemoryPack();
  });

  // ==========================================================================
  // TOP-BAR LIFECYCLE TABS & CONTEXT-ADAPTIVE WORKSPACE NAVIGATION
  // ==========================================================================
  document.querySelectorAll(".studio-top-tab").forEach((link) => {
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      const wsId = link.getAttribute("data-workspace") || "workspace-tree";
      const topTab = link.getAttribute("data-top-tab") || "memories";
      const targetHash = topTab === "memories" ? "" : `#${topTab}`;
      if (window.location.hash !== targetHash) {
        history.replaceState(null, "", targetHash || window.location.pathname);
      }
      await switchWorkspace(wsId);
    });
  });

  document.querySelectorAll(".studio-workspace-nav-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const wsId = btn.getAttribute("data-workspace") || "workspace-sessions";
      await switchWorkspace(wsId);
    });
  });

  document.querySelectorAll(".studio-lifecycle-step").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const wsId = btn.getAttribute("data-lifecycle-ws") || "workspace-sessions";
      await switchWorkspace(wsId);
    });
  });

  document.querySelectorAll("[data-guide-workspace]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const wsId = btn.getAttribute("data-guide-workspace") || "workspace-sessions";
      await switchWorkspace(wsId);
    });
  });

  document.getElementById("tabBtnContextDetail")?.addEventListener("click", () => switchInspectorTab("detail"));
  document.getElementById("tabBtnCommitsInspector")?.addEventListener("click", () => switchInspectorTab("commits"));
  document.getElementById("inspectorCommitAllStagedBtn")?.addEventListener("click", handleWsTreeCommitAll);

  // Workspace 1: Sessions & Usefulness Attribution
  document.getElementById("wsRefreshSessionsBtn")?.addEventListener("click", loadWsSessions);
  document.getElementById("wsSessionSearchInput")?.addEventListener("input", renderWsSessionsTable);
  document.getElementById("wsSessionScopeSelect")?.addEventListener("change", renderWsSessionsTable);

  // Workspace 2: Memories (Tree | Graph Switcher + Proposals)
  document.getElementById("wsMemoriesViewTreeBtn")?.addEventListener("click", () => setMemoriesViewMode("tree"));
  document.getElementById("wsMemoriesViewGraphBtn")?.addEventListener("click", () => setMemoriesViewMode("graph"));
  document.getElementById("wsTreeRefreshBtn")?.addEventListener("click", loadWsMemoryTree);
  document.getElementById("wsTreeSearchInput")?.addEventListener("input", () => {
    renderWsMemoryTree();
    if (wsState.memoriesViewMode === "graph") renderWsMemoriesGraph();
  });
  document.getElementById("wsTreeStatusFilter")?.addEventListener("change", () => {
    renderWsMemoryTree();
    if (wsState.memoriesViewMode === "graph") renderWsMemoriesGraph();
  });
  document.getElementById("wsTreeOpenStageModalBtn")?.addEventListener("click", () => {
    const card = document.getElementById("wsTreeStageFormCard");
    if (!card) return;
    card.classList.toggle("hidden");
    if (!card.classList.contains("hidden")) {
      const scopeSelect = document.getElementById("wsTreeStageScopeType");
      if (scopeSelect) {
        scopeSelect.value =
          studioState.activeScopeFilter && studioState.activeScopeFilter !== "ALL"
            ? studioState.activeScopeFilter
            : "USER";
      }
    }
  });
  document.getElementById("wsTreeCloseStageFormBtn")?.addEventListener("click", () => {
    document.getElementById("wsTreeStageFormCard")?.classList.add("hidden");
  });
  document.getElementById("wsTreeSubmitStageBtn")?.addEventListener("click", handleWsTreeStageNode);
  document.getElementById("wsTreeCommitAllBtn")?.addEventListener("click", handleWsTreeCommitAll);
  document.getElementById("wsTreeDiscardAllBtn")?.addEventListener("click", () => handleWsTreeDiscard("", true));
  document.getElementById("wsTreeOpenCommitsInspectorBtn")?.addEventListener("click", () => {
    document.getElementById("studioInspectorDrawer")?.classList.remove("collapsed");
    switchInspectorTab("commits");
  });

  // Workspace 3: Playground & Context Trace
  document.getElementById("wsPgPromptForm")?.addEventListener("submit", handleWsPlaygroundSubmit);
  document.getElementById("wsPgDimSelect")?.addEventListener("change", loadWsPlaygroundContext);
  document.getElementById("wsPgFormatSelect")?.addEventListener("change", loadWsPlaygroundContext);

  // Workspace 4: Optimize (Test Cases & AlphaEvolve Auto-Tune)
  function setOptimizeSubTab(tabName) {
    const active = tabName === "autotune" ? "autotune" : "tests";
    document.querySelectorAll("[data-optimize-tab]").forEach((btn) => {
      const isTarget = btn.getAttribute("data-optimize-tab") === active;
      btn.classList.toggle("active", isTarget);
      btn.setAttribute("aria-selected", isTarget ? "true" : "false");
    });
    document.getElementById("optimizePanelTests")?.classList.toggle("hidden", active !== "tests");
    document.getElementById("optimizePanelAutotune")?.classList.toggle("hidden", active !== "autotune");
  }

  document.querySelectorAll("[data-optimize-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      setOptimizeSubTab(btn.getAttribute("data-optimize-tab") || "tests");
    });
  });

  document.getElementById("openRetrievalSettingsBtn")?.addEventListener("click", () => {
    document.getElementById("studioInspectorDrawer")?.classList.remove("collapsed");
    switchInspectorTab("settings");
  });

  document.getElementById("wsEvalSynthesizeAdvBtn")?.addEventListener("click", async () => {
    setOptimizeSubTab("tests");
    await handleGenerateAdversarialCases();
    await handleWsEvalRunSuite();
  });
  document.getElementById("wsEvalRunSuiteBtn")?.addEventListener("click", async () => {
    setOptimizeSubTab("tests");
    await handleWsEvalRunSuite();
  });
  document.getElementById("wsEvalRunAlphaEvolveBtn")?.addEventListener("click", async () => {
    setOptimizeSubTab("autotune");
    await handleWsEvalRunAlphaEvolve();
  });
  document.getElementById("wsOptimizeRunAlphaEvolveBtn")?.addEventListener("click", async () => {
    setOptimizeSubTab("autotune");
    await handleWsEvalRunAlphaEvolve();
  });

  // Workspace: Jobs Queue & Controls
  document.querySelectorAll("[data-run-job]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const jobType = btn.getAttribute("data-run-job");
      closeAllToolbarDropdowns();
      if (jobType) {
        await startStudioJob(jobType, "Jobs");
      }
    });
  });
  document.getElementById("wsJobsClearBtn")?.addEventListener("click", clearFinishedStudioJobs);
  document.querySelectorAll("#wsJobsCategoryPills [data-job-category]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const cat = btn.getAttribute("data-job-category") || "ALL";
      wsState.jobsFilterCategory = cat;
      document.querySelectorAll("#wsJobsCategoryPills [data-job-category]").forEach((p) => {
        p.classList.toggle("active", p === btn);
      });
      renderWsJobsTable();
    });
  });
  document.getElementById("wsJobsStatusSelect")?.addEventListener("change", (e) => {
    wsState.jobsFilterStatus = e.target.value || "ALL";
    renderWsJobsTable();
  });
  document.getElementById("wsJobsSearchInput")?.addEventListener("input", (e) => {
    wsState.jobsSearchQuery = (e.target.value || "").trim().toLowerCase();
    renderWsJobsTable();
  });

  // Workspace 5: Entities & Scope Hierarchy
  document.getElementById("wsRefreshEntitiesBtn")?.addEventListener("click", loadWsEntities);
  document.getElementById("wsCreateEntityBtn")?.addEventListener("click", handleWsSaveEntity);

  // Workspace 6: Memory Categories
  document.getElementById("wsRefreshCategoriesBtn")?.addEventListener("click", loadWsCategories);
  document.getElementById("wsCreateCategoryBtn")?.addEventListener("click", handleWsSaveCategory);

  // Workspace 7: Data Ingest Connectors
  document.getElementById("wsRefreshIngestServicesBtn")?.addEventListener("click", loadWsIngestServices);
  document.getElementById("wsSaveIngestServiceBtn")?.addEventListener("click", handleWsSaveIngestService);

  // Workspace 8: Memory Extractors & Gemini 4 Neon Co-Designer Chat
  document.getElementById("wsRefreshExtractorsBtn")?.addEventListener("click", loadWsExtractors);
  document.getElementById("wsSaveExtractorBtn")?.addEventListener("click", handleWsSaveExtractor);
  document.getElementById("wsExtractorChatForm")?.addEventListener("submit", handleWsExtractorChatSubmit);
  document.querySelectorAll(".ws-extractor-chat-chip").forEach((chip) => {
    chip.addEventListener("click", async () => {
      const chatInput = document.getElementById("wsExtractorChatInput");
      if (chatInput) {
        chatInput.value = chip.getAttribute("data-chat-prompt") || "";
        await handleWsExtractorChatSubmit();
      }
    });
  });

  // Workspace 9: Dual-Plane Memory Backend & Mutation Rollback
  document.getElementById("wsBackendRefreshBtn")?.addEventListener("click", loadWsBackendStatus);
  document.getElementById("wsBackendSyncBtn")?.addEventListener("click", handleWsBackendSync);
  document.getElementById("wsAgentWriteBtn")?.addEventListener("click", handleWsAgentWriteBack);

  // Workspace 10: Security & Access Control
  document.getElementById("wsRefreshSecurityBtn")?.addEventListener("click", loadWsSecurityPolicies);
  document.getElementById("wsSaveRbacPolicyBtn")?.addEventListener("click", handleWsSaveRbacPolicy);
  document.getElementById("wsRbacSimRunBtn")?.addEventListener("click", handleWsRbacSimulate);

  // 10. Initial data hydration across workspaces (resolve deep-link hash or land on Memories)
  function resolveWorkspaceFromHash() {
    const hash = (window.location.hash || "").replace(/^#/, "").toLowerCase();
    if (hash === "sessions") return "workspace-sessions";
    if (hash === "optimize" || hash === "benchmarks" || hash === "eval") return "workspace-eval";
    if (hash === "jobs") return "workspace-jobs";
    if (hash === "settings" || hash === "admin") return "workspace-entities";
    return "workspace-tree";
  }

  window.addEventListener("hashchange", async () => {
    await switchWorkspace(resolveWorkspaceFromHash());
  });

  await loadWsEntities();
  await loadWsSessions();
  await loadWsMemoryTree();
  await loadIngestConfig();
  await loadRawTelemetryStream();
  await loadStudioCategories();
  await loadTrajectorySessions();
  await refreshScopesExplorer();
  await refreshEvalCases();
  await refreshEvolveArchive();
  await loadSyncStatus();
  await loadWsJobs();
  connectStudioJobsStream();
  updateSignedInUserReadout();
  updateTopScopeTriggerLabel();
  await switchWorkspace(resolveWorkspaceFromHash());
});

// ============================================================================
// PHASE 3: WORKSPACE MASTER-DETAIL CONTROLLER & LIVE API INTEGRATIONS
// ============================================================================

const wsState = {
  activeWorkspace: "workspace-tree",
  memoriesViewMode: "tree",
  sessions: [],
  selectedSessionId: null,
  selectedMemoryKey: null,
  treePayload: null,
  entities: [],
  categories: [],
  ingestServices: [],
  extractors: [],
  activeExtractorId: "extractor:crystalline-default",
  backendStatus: null,
  securityPolicies: [],
  jobs: [],
  jobsActiveCount: 0,
  jobsCounts: {},
  jobsFilterCategory: "ALL",
  jobsFilterStatus: "ALL",
  jobsSearchQuery: "",
  selectedJobId: null,
  jobsEventSource: null,
  jobsTickTimer: null,
};

async function switchWorkspace(workspaceId) {
  if (!workspaceId) return;
  wsState.activeWorkspace = workspaceId;
  closeAllToolbarDropdowns();

  // Hide 4-stage legacy panels when a 10-workspace panel is selected
  document.querySelectorAll(".studio-stage-panel").forEach((panel) => {
    panel.classList.remove("active-stage");
    panel.classList.add("hidden");
  });

  // Clear Guided Workflow tab active highlight so there is no conflicting active state
  document.querySelectorAll(".studio-intent-tab").forEach((item) => {
    item.classList.remove("active");
    item.setAttribute("aria-selected", "false");
  });

  // Show only the selected workspace in the center canvas
  document.querySelectorAll(".studio-workspace-view").forEach((ws) => {
    const isTarget = ws.id === workspaceId;
    ws.classList.toggle("active-workspace", isTarget);
    ws.classList.toggle("hidden", !isTarget);
  });

  // Update Left Rail workspace active button state
  document.querySelectorAll(".studio-workspace-nav-btn").forEach((btn) => {
    const isTarget = btn.getAttribute("data-workspace") === workspaceId;
    btn.classList.toggle("active", isTarget);
    btn.setAttribute("aria-selected", isTarget ? "true" : "false");
  });

  // Sync top Lifecycle Breadcrumb Strip active state
  document.querySelectorAll(".studio-lifecycle-step").forEach((btn) => {
    const isTarget = btn.getAttribute("data-lifecycle-ws") === workspaceId;
    btn.classList.toggle("active", isTarget);
    if (isTarget) {
      btn.setAttribute("aria-current", "step");
    } else {
      btn.removeAttribute("aria-current");
    }
  });

  // Sync Top-Bar Lifecycle Tabs & Context-Specific Left Pane (Memories | Sessions | Optimize | Settings)
  const adminWorkspaces = new Set([
    "workspace-entities",
    "workspace-categories",
    "workspace-ingest",
    "workspace-extractors",
    "workspace-backend",
    "workspace-security",
  ]);
  let activeTopTab = "memories";
  if (workspaceId === "workspace-sessions") activeTopTab = "sessions";
  else if (workspaceId === "workspace-eval" || workspaceId === "workspace-playground") activeTopTab = "optimize";
  else if (workspaceId === "workspace-jobs") activeTopTab = "jobs";
  else if (adminWorkspaces.has(workspaceId)) activeTopTab = "settings";

  const bodyLayoutEl = document.getElementById("appBodyLayout");
  if (bodyLayoutEl) {
    bodyLayoutEl.setAttribute("data-active-tab", activeTopTab);
  }

  document.querySelectorAll(".studio-top-tab").forEach((link) => {
    const isTarget = link.getAttribute("data-top-tab") === activeTopTab;
    link.classList.toggle("active", isTarget);
    if (isTarget) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });

  const railContextTitles = {
    memories: "Explorer",
    sessions: "Log Sources",
    settings: "Settings",
  };
  const railTitleEl = document.getElementById("leftRailContextTitle");
  if (railTitleEl && railContextTitles[activeTopTab]) {
    railTitleEl.textContent = railContextTitles[activeTopTab];
  }
  document.querySelectorAll("[data-rail-context]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.getAttribute("data-rail-context") !== activeTopTab);
  });

  const adminDetails = document.getElementById("studioAdminNavSection");
  if (adminDetails && adminWorkspaces.has(workspaceId)) {
    adminDetails.open = true;
  }

  // Map workspace to its corresponding contextual Quick Actions group
  const mappedStage = WORKSPACE_TO_STAGE_MAP[workspaceId] || "stage-1-sandbox";
  document.querySelectorAll(".studio-cmd-group").forEach((grp) => {
    const forStage = grp.getAttribute("data-for-stage");
    grp.classList.toggle("hidden", forStage !== mappedStage);
  });
  const stagePill = document.getElementById("activeStageDropdownLabel");
  if (stagePill && STUDIO_STAGE_SHORT_LABELS[mappedStage]) {
    stagePill.textContent = STUDIO_STAGE_SHORT_LABELS[mappedStage];
  }

  // Update Top Toolbar Current View Indicator
  const viewBadge = document.getElementById("toolbarViewModeBadge");
  if (viewBadge) {
    viewBadge.textContent = "Tools";
  }
  const viewTitle = document.getElementById("toolbarCurrentTitle");
  if (viewTitle && WORKSPACE_TITLES[workspaceId]) {
    viewTitle.textContent = WORKSPACE_TITLES[workspaceId];
  }
  const viewSub = document.getElementById("toolbarCurrentSubtitle");
  if (viewSub && WORKSPACE_DESCS[workspaceId]) {
    viewSub.textContent = WORKSPACE_DESCS[workspaceId];
  }

  // Contextual Commits Tab in Right Inspector: ONLY visible when 2. Memory Tree is active
  const commitsTabBtn = document.getElementById("tabBtnCommitsInspector");
  if (commitsTabBtn) {
    const isTree = workspaceId === "workspace-tree";
    commitsTabBtn.classList.toggle("hidden", !isTree);
    if (!isTree && commitsTabBtn.classList.contains("active")) {
      switchInspectorTab("detail");
    }
  }

  const wsBadge = document.getElementById("inspectorDetailWorkspaceBadge");
  if (wsBadge && WORKSPACE_TITLES[workspaceId]) {
    wsBadge.textContent = WORKSPACE_TITLES[workspaceId];
  }

  // Hydrate active workspace from live memory store endpoints
  if (workspaceId === "workspace-sessions") {
    await loadWsSessions();
  } else if (workspaceId === "workspace-tree") {
    await loadWsMemoryTree();
  } else if (workspaceId === "workspace-playground") {
    await loadWsPlaygroundContext();
  } else if (workspaceId === "workspace-eval") {
    await refreshEvalCases();
    await refreshEvolveArchive();
  } else if (workspaceId === "workspace-jobs") {
    await loadWsJobs();
  } else if (workspaceId === "workspace-entities") {
    await loadWsEntities();
  } else if (workspaceId === "workspace-categories") {
    await loadWsCategories();
  } else if (workspaceId === "workspace-ingest") {
    await loadWsIngestServices();
  } else if (workspaceId === "workspace-extractors") {
    await loadWsExtractors();
  } else if (workspaceId === "workspace-backend") {
    await loadWsBackendStatus();
  } else if (workspaceId === "workspace-security") {
    await loadWsSecurityPolicies();
  }
}

function updateContextDetailPane(title, subtitle, bodyHtml, statusLabel = "LIVE", rawPayload = null) {
  const drawer = document.getElementById("studioInspectorDrawer");
  if (drawer) drawer.classList.remove("collapsed");
  const titleEl = document.getElementById("inspectorDetailTitle");
  const subEl = document.getElementById("inspectorDetailSubtitle");
  const bodyEl = document.getElementById("inspectorDetailBody");
  const statusEl = document.getElementById("inspectorDetailStatusBadge");
  if (titleEl) titleEl.textContent = title || "Item Detail";
  if (subEl) subEl.textContent = subtitle || "";
  if (bodyEl) bodyEl.innerHTML = bodyHtml || "";
  if (statusEl) statusEl.textContent = statusLabel;
  if (rawPayload && typeof rawPayload === "object") {
    const recordEl = document.getElementById("inspectorContent");
    if (recordEl) {
      const entries = Object.entries(rawPayload).filter(([, v]) => v !== undefined && v !== null && v !== "");
      recordEl.innerHTML = entries
        .slice(0, 18)
        .map(([k, v]) => {
          const valStr = Array.isArray(v)
            ? `${v.length} items`
            : typeof v === "object"
              ? `${Object.keys(v).length} fields`
              : String(v).slice(0, 160);
          return `<div class="engine-kv-row"><span class="engine-kv-label">${escapeHtml(k)}</span><strong class="mono">${escapeHtml(valStr)}</strong></div>`;
        })
        .join("");
    }
  }
  switchInspectorTab("detail");
}

// ----------------------------------------------------------------------------
// WORKSPACE 1: Session Logs, Summarization & Memory Usefulness Attribution
// ----------------------------------------------------------------------------
async function loadWsSessions() {
  try {
    const res = await fetch("/api/studio/sessions?limit=30");
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    wsState.sessions = Array.isArray(data.sessions) ? data.sessions : [];
    const badge = document.getElementById("wsBadgeSessions");
    if (badge) badge.textContent = String(wsState.sessions.length);
    renderWsSessionsTable();
  } catch (err) {
    showStatusBanner(`Failed to load studio sessions: ${err.message}`, true);
  }
}

function renderWsSessionsTable() {
  const tbody = document.getElementById("wsSessionsTableBody");
  if (!tbody) return;
  const query = (document.getElementById("wsSessionSearchInput")?.value || "").trim().toLowerCase();
  const scopeFilter = document.getElementById("wsSessionScopeSelect")?.value || "ALL";

  const filtered = wsState.sessions.filter((s) => {
    if (scopeFilter !== "ALL" && String(s.scope_type || "").toUpperCase() !== scopeFilter) {
      return false;
    }
    if (!query) return true;
    const hay = `${s.session_id} ${s.title} ${s.summary || ""} ${(s.triggered_memories || []).join(" ")}`.toLowerCase();
    return hay.includes(query);
  });

  // Update KPIs
  let summarizedCount = 0;
  let totalTriggered = 0;
  let totalHelpful = 0;
  wsState.sessions.forEach((s) => {
    if (s.status === "SUMMARIZED" || s.summary) summarizedCount += 1;
    const trig = Array.isArray(s.triggered_memories) ? s.triggered_memories.length : 0;
    const help = Array.isArray(s.useful_memories) ? s.useful_memories.length : 0;
    totalTriggered += trig;
    totalHelpful += help;
  });

  const setKpi = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = String(val);
  };
  setKpi("wsSessionsTotalCount", wsState.sessions.length);
  setKpi("wsSessionsSummarizedCount", summarizedCount);
  setKpi("wsSessionsTriggeredTotal", totalTriggered);
  setKpi(
    "wsSessionsHelpfulRate",
    totalTriggered > 0 ? `${Math.round((totalHelpful / totalTriggered) * 100)}% (${totalHelpful}/${totalTriggered})` : "—"
  );

  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-center text-muted">No sessions match the current filter.</td></tr>`;
    return;
  }

  const formatSessionStatus = (raw, hasSummary) => {
    const k = String(raw || "").trim().toUpperCase();
    if (k === "EXTRACTED_AND_SYNCED") return "Synced";
    if (k === "PENDING_EXTRACTION") return "Pending";
    if (k === "SUMMARIZED" || hasSummary) return "Summarized";
    if (k === "RAW_CAPTURED") return "Captured";
    return raw ? String(raw) : "Ready";
  };

  tbody.innerHTML = filtered
    .map((s) => {
      const trigList = Array.isArray(s.triggered_memories) ? s.triggered_memories : [];
      const usefulList = Array.isArray(s.useful_memories) ? s.useful_memories : [];
      const turnCount = Number(s.turn_count || (s.turns || []).length || 0);
      const isSummarized = s.status === "SUMMARIZED" || Boolean(s.summary);
      const statusLabel = formatSessionStatus(s.status, isSummarized);
      return `
        <tr class="ws-clickable-row" data-session-id="${escapeHtml(s.session_id)}">
          <td>
            <div class="font-mono text-xs font-semibold">${escapeHtml(s.session_id)}</div>
            <div class="font-mono text-xs text-muted">${escapeHtml((s.scope_type || "User").toLowerCase())} · ${escapeHtml(s.user_id || getScopeIds().user_id || "—")}</div>
          </td>
          <td>
            <div class="font-semibold text-sm">${escapeHtml(s.title || s.session_id)}</div>
            <div class="text-xs text-secondary mt-2">${escapeHtml(s.summary || "Click Extract to summarize this session.")}</div>
          </td>
          <td class="font-mono text-xs">${turnCount} turns</td>
          <td class="font-mono text-xs text-secondary">
            ${trigList.length} triggered · ${usefulList.length} helpful
          </td>
          <td class="font-mono text-xs text-secondary">
            ${escapeHtml(statusLabel)}
          </td>
          <td>
            <button type="button" class="btn btn-secondary btn-xs ws-summarize-session-btn" data-session-id="${escapeHtml(s.session_id)}">
              Extract
            </button>
          </td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll(".ws-clickable-row").forEach((row) => {
    row.addEventListener("click", (evt) => {
      if (evt.target.closest(".ws-summarize-session-btn")) return;
      const sid = row.getAttribute("data-session-id");
      const sessionObj = wsState.sessions.find((item) => item.session_id === sid);
      if (sessionObj) inspectWsSession(sessionObj);
    });
  });

  tbody.querySelectorAll(".ws-summarize-session-btn").forEach((btn) => {
    btn.addEventListener("click", async (evt) => {
      evt.stopPropagation();
      const sid = btn.getAttribute("data-session-id");
      if (sid) await handleWsSummarizeSession(sid, btn);
    });
  });
}

function getInitialPromptForSession(sessionObj) {
  if (!sessionObj) return "";
  const turns = Array.isArray(sessionObj.turns) ? sessionObj.turns : [];
  const firstUser = turns.find((t) => String(t.role || "").toLowerCase() === "user" && (t.content || t.user_prompt));
  if (firstUser) return String(firstUser.content || firstUser.user_prompt || "").trim();
  if (turns.length && (turns[0].content || turns[0].user_prompt)) {
    return String(turns[0].content || turns[0].user_prompt || "").trim();
  }
  return String(sessionObj.title || sessionObj.summary || "").trim();
}

function getLinkedSessionsForMemory(memoryKey, sourceSessionId = "") {
  const mk = String(memoryKey || "").trim();
  const seen = new Map();
  const addLink = (sid, relation, sessionObj = null) => {
    const cleanId = String(sid || "").trim();
    if (!cleanId) return;
    if (!seen.has(cleanId)) {
      const found = sessionObj || (wsState.sessions || []).find((s) => s.session_id === cleanId) || null;
      seen.set(cleanId, {
        session_id: cleanId,
        relation,
        title: found?.title || cleanId,
        initial_prompt: getInitialPromptForSession(found),
        sessionObj: found,
      });
    } else if (relation === "proposed") {
      seen.get(cleanId).relation = "proposed";
    }
  };

  if (sourceSessionId) addLink(sourceSessionId, "proposed");

  const staged = Array.isArray(wsState.treePayload?.staged_commits) ? wsState.treePayload.staged_commits : [];
  staged.forEach((sc) => {
    if (String(sc.memory_key || "") === mk && sc.source_session_id) {
      addLink(sc.source_session_id, "proposed");
    }
  });

  (wsState.sessions || []).forEach((s) => {
    const ext = Array.isArray(s.extracted_memories) ? s.extracted_memories : [];
    const trig = Array.isArray(s.triggered_memories) ? s.triggered_memories : [];
    if (ext.includes(mk)) addLink(s.session_id, "proposed", s);
    else if (trig.includes(mk)) addLink(s.session_id, "triggered", s);
  });

  return Array.from(seen.values());
}

function findMemoryNodeByKey(memoryKey) {
  const tree = wsState.treePayload?.tree || {};
  for (const tier of ["ORG", "TEAM", "AGENT", "USER"]) {
    const found = (tree[tier] || []).find((n) => n.memory_key === memoryKey);
    if (found) return found;
  }
  return null;
}

function renderSessionLogCenter(sessionObj) {
  const titleEl = document.getElementById("wsSessionLogTitle");
  const metaEl = document.getElementById("wsSessionLogMeta");
  const bodyEl = document.getElementById("wsSessionLogBody");
  const chatBtn = document.getElementById("wsSessionOpenInChatBtn");
  const extractBtn = document.getElementById("wsSessionLogExtractBtn");
  if (!bodyEl || !sessionObj) return;

  const turns = Array.isArray(sessionObj.turns) ? sessionObj.turns : [];
  const trig = Array.isArray(sessionObj.triggered_memories) ? sessionObj.triggered_memories : [];

  if (titleEl) titleEl.textContent = sessionObj.title || sessionObj.session_id;
  if (metaEl) metaEl.textContent = `${sessionObj.session_id} · ${turns.length} turns`;

  if (chatBtn) {
    chatBtn.href = `index.html?session_id=${encodeURIComponent(sessionObj.session_id)}`;
    chatBtn.classList.remove("hidden");
  }
  if (extractBtn) {
    extractBtn.classList.remove("hidden");
    extractBtn.onclick = () => handleWsSummarizeSession(sessionObj.session_id, extractBtn);
  }

  const trigBar = trig.length
    ? `<div class="ws-session-log-recalled mb-8">
         <span class="text-xs text-muted">Recalled memories:</span>
         ${trig
           .map(
             (k) =>
               `<button type="button" class="chip ws-jump-memory-chip" data-memory-key="${escapeHtml(k)}">${escapeHtml(k)}</button>`
           )
           .join("")}
       </div>`
    : "";

  const turnsHtml = turns.length
    ? turns
        .map(
          (t, idx) => `
          <div class="ws-session-turn-row ws-session-turn-${escapeHtml(String(t.role || "user").toLowerCase())}">
            <div class="ws-session-turn-role font-mono text-xs">${escapeHtml(String(t.role || "user").toUpperCase())} · #${idx + 1}</div>
            <div class="ws-session-turn-content text-xs">${escapeHtml(t.content || "")}</div>
          </div>
        `
        )
        .join("")
    : `<div class="text-xs text-muted">No turns recorded in this session yet.</div>`;

  bodyEl.innerHTML = `${trigBar}<div class="ws-session-turns-stack">${turnsHtml}</div>`;

  bodyEl.querySelectorAll(".ws-jump-memory-chip").forEach((chip) => {
    chip.addEventListener("click", async () => {
      const mk = chip.getAttribute("data-memory-key");
      await switchWorkspace("workspace-tree");
      const node = findMemoryNodeByKey(mk);
      if (node) inspectWsMemoryNode(node);
    });
  });
}

function inspectWsSession(sessionObj, opts = {}) {
  wsState.selectedSessionId = sessionObj.session_id;
  if (!opts.skipCenterLog) {
    renderSessionLogCenter(sessionObj);
  }

  const trig = Array.isArray(sessionObj.triggered_memories) ? sessionObj.triggered_memories : [];
  const usefulSet = new Set(Array.isArray(sessionObj.useful_memories) ? sessionObj.useful_memories : []);
  const extracted = Array.isArray(sessionObj.extracted_memories) ? sessionObj.extracted_memories : [];
  const stagedAll = Array.isArray(wsState.treePayload?.staged_commits) ? wsState.treePayload.staged_commits : [];
  const sessionProposals = stagedAll.filter((sc) => sc.source_session_id === sessionObj.session_id);
  const initialPrompt = getInitialPromptForSession(sessionObj);
  const chatHref = `index.html?session_id=${encodeURIComponent(sessionObj.session_id)}`;

  const proposalsHtml = sessionProposals.length
    ? sessionProposals
        .map(
          (p) => `
          <div class="inspector-linked-card">
            <div class="inspector-linked-card-top">
              <span class="commit-status-pill ${String(p.commit_status || "NEW") === "CONFLICT" ? "commit-pill-conflict" : String(p.commit_status || "NEW") === "MODIFIED" ? "commit-pill-modified" : "commit-pill-new"}">${escapeHtml(p.commit_status || "NEW")}</span>
              <button type="button" class="inline-flow-link font-mono text-xs ws-inspector-open-memory" data-memory-key="${escapeHtml(p.memory_key)}">${escapeHtml(p.memory_key)}</button>
            </div>
            <div class="text-xs text-secondary mt-2">${escapeHtml(p.after_value || p.proposed_content || "")}</div>
            <div class="guide-btn-grid mt-4">
              <button type="button" class="btn btn-secondary btn-xs ws-inspector-commit-staged" data-staged-id="${escapeHtml(p.staged_id || p.commit_id)}">Approve</button>
              <button type="button" class="btn btn-danger-outline btn-xs ws-inspector-discard-staged" data-staged-id="${escapeHtml(p.staged_id || p.commit_id)}">Dismiss</button>
            </div>
          </div>
        `
        )
        .join("")
    : extracted.length
      ? `<div class="inspector-link-list">
           ${extracted
             .map(
               (k) =>
                 `<button type="button" class="inspector-link-row ws-inspector-open-memory" data-memory-key="${escapeHtml(k)}">
                    <span class="font-mono text-xs">${escapeHtml(k)}</span>
                    <span class="text-xs text-muted">Open in Memories →</span>
                  </button>`
             )
             .join("")}
         </div>`
      : `<div class="text-xs text-muted">No proposals extracted from this session yet.</div>`;

  const checklistHtml = trig.length
    ? trig
        .map(
          (key) => `
        <div class="inspector-useful-row mt-4">
          <label class="studio-checkbox-label">
            <input type="checkbox" class="ws-session-useful-checkbox" value="${escapeHtml(key)}" ${usefulSet.has(key) ? "checked" : ""}>
            <span class="font-mono text-xs">${escapeHtml(key)}</span>
          </label>
          <button type="button" class="inline-flow-link text-xs ws-inspector-open-memory" data-memory-key="${escapeHtml(key)}">Inspect</button>
        </div>
      `
        )
        .join("")
    : `<div class="text-xs text-muted">No memories triggered in this session.</div>`;

  const html = `
    <div class="mb-8">
      <div class="text-xs font-semibold">Proposals from this Session (${sessionProposals.length || extracted.length})</div>
      <div class="mt-4">${proposalsHtml}</div>
      <div class="guide-btn-grid mt-8">
        <button type="button" id="inspectorExtractSessionBtn" class="btn btn-secondary btn-xs" data-session-id="${escapeHtml(sessionObj.session_id)}">
          Extract
        </button>
        <button type="button" id="inspectorOpenTreeFromSessionBtn" class="btn btn-secondary btn-xs">
          View tree
        </button>
      </div>
    </div>
    <div class="mb-8">
      <div class="text-xs font-semibold">Memories Used (${trig.length})</div>
      <div id="inspectorSessionUsefulList" class="mt-4">${checklistHtml}</div>
      ${
        trig.length
          ? `<button type="button" id="inspectorSaveUsefulnessBtn" class="btn btn-secondary btn-xs w-full mt-8" data-session-id="${escapeHtml(sessionObj.session_id)}">Save feedback</button>`
          : ""
      }
    </div>
    <div class="mb-8">
      <div class="text-xs font-semibold">Test &amp; Benchmark</div>
      <div class="guide-btn-grid mt-4">
        <a href="${chatHref}" class="btn btn-secondary btn-xs text-center">Open in Chat</a>
        <button type="button" id="inspectorAddSessionToBenchmarkBtn" class="btn btn-secondary btn-xs">Add eval</button>
      </div>
      ${
        opts.fromGraph
          ? `<button type="button" id="inspectorJumpToSessionsBtn" class="btn btn-secondary btn-xs w-full mt-4">View log</button>`
          : ""
      }
    </div>
    <div class="mb-8">
      <div class="text-xs font-semibold">Summary</div>
      <p class="text-xs text-secondary mt-2">${escapeHtml(sessionObj.summary || "Not yet summarized.")}</p>
    </div>
  `;

  updateContextDetailPane(
    sessionObj.title || sessionObj.session_id,
    `${sessionObj.session_id} · ${sessionObj.scope_type || "User"} (${sessionObj.user_id || getScopeIds().user_id || "—"})`,
    html,
    sessionObj.status || "Captured",
    sessionObj
  );

  document.getElementById("inspectorSaveUsefulnessBtn")?.addEventListener("click", async () => {
    const checkedKeys = Array.from(document.querySelectorAll(".ws-session-useful-checkbox:checked")).map((cb) => cb.value);
    await handleWsSaveSessionUsefulness(sessionObj.session_id, trig, checkedKeys);
  });

  document.getElementById("inspectorExtractSessionBtn")?.addEventListener("click", async (evt) => {
    await handleWsSummarizeSession(sessionObj.session_id, evt.currentTarget);
  });

  document.getElementById("inspectorOpenTreeFromSessionBtn")?.addEventListener("click", async () => {
    await switchWorkspace("workspace-tree");
  });

  document.getElementById("inspectorJumpToSessionsBtn")?.addEventListener("click", async () => {
    await switchWorkspace("workspace-sessions");
    inspectWsSession(sessionObj);
  });

  document.getElementById("inspectorAddSessionToBenchmarkBtn")?.addEventListener("click", async (evt) => {
    const btn = evt.currentTarget;
    btn.disabled = true;
    btn.textContent = "Saving...";
    try {
      const ids = getScopeIds();
      const res = await fetch("/api/studio/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionObj.session_id,
          user_id: sessionObj.user_id || ids.user_id,
          team_id: ids.team_id,
          agent_id: ids.agent_id,
          prompt: initialPrompt || sessionObj.title || sessionObj.session_id,
          agent_response: sessionObj.summary || "Session trajectory baseline",
          retrieved_memories: trig,
          rating: "POSITIVE",
          expected_fact: trig.join(", ") || sessionObj.summary || "",
          notes: `Added from session ${sessionObj.session_id}`,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await refreshEvalCases();
      showStatusBanner(`✓ Added "${sessionObj.title || sessionObj.session_id}" to Benchmarks.`);
    } catch (err) {
      showStatusBanner(`Could not add benchmark case: ${err.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Add to Benchmarks";
    }
  });

  document.querySelectorAll(".ws-inspector-open-memory").forEach((el) => {
    el.addEventListener("click", async () => {
      const mk = el.getAttribute("data-memory-key");
      await switchWorkspace("workspace-tree");
      const node = findMemoryNodeByKey(mk);
      if (node) inspectWsMemoryNode(node);
    });
  });

  document.querySelectorAll(".ws-inspector-commit-staged").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = btn.getAttribute("data-staged-id");
      if (sid) {
        await handleWsTreeCommitSingle(sid);
        inspectWsSession(sessionObj);
      }
    });
  });

  document.querySelectorAll(".ws-inspector-discard-staged").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = btn.getAttribute("data-staged-id");
      if (sid) {
        await handleWsTreeDiscard(sid, false);
        inspectWsSession(sessionObj);
      }
    });
  });
}

async function handleWsSummarizeSession(sessionId, btnEl = null) {
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "Summarizing...";
  }
  try {
    const res = await fetch(`/api/studio/sessions/${encodeURIComponent(sessionId)}/summarize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_stage_candidates: true }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    showStatusBanner(
      `✓ Summarized session ${sessionId} with Gemini 4 Hydrogen & staged ${(data.staged_commits || []).length} candidate memories in Memory Tree.`
    );
    await loadWsSessions();
    await loadWsMemoryTree();
    if (data.session) inspectWsSession(data.session);
  } catch (err) {
    showStatusBanner(`Session summarization failed: ${err.message}`, true);
  } finally {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = "Extract";
    }
  }
}

async function handleWsSaveSessionUsefulness(sessionId, triggeredMemories, usefulMemories) {
  try {
    const res = await fetch(`/api/studio/sessions/${encodeURIComponent(sessionId)}/usefulness`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        triggered_memories: triggeredMemories,
        useful_memories: usefulMemories,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(`✓ Updated memory usefulness attribution for ${sessionId} (${usefulMemories.length} helpful).`);
    await loadWsSessions();
    await loadWsMemoryTree();
  } catch (err) {
    showStatusBanner(`Failed to save usefulness attribution: ${err.message}`, true);
  }
}

// ----------------------------------------------------------------------------
// WORKSPACE 2: Memory Tree & Top Pending Staged Commits Bar
// ----------------------------------------------------------------------------
async function loadWsMemoryTree() {
  const filters = getActiveContextFilters();
  try {
    const params = new URLSearchParams();
    if (filters.team_id) params.set("team_id", filters.team_id);
    if (filters.agent_id) params.set("agent_id", filters.agent_id);
    if (filters.user_id) params.set("user_id", filters.user_id);
    const qs = params.toString();
    const res = await fetch(qs ? `/api/studio/tree?${qs}` : "/api/studio/tree");
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    wsState.treePayload = data;
    renderWsMemoryTree();
    renderInspectorStagedCommits();
    if (wsState.memoriesViewMode === "graph") {
      renderWsMemoriesGraph();
    }
  } catch (err) {
    showStatusBanner(`Failed to load Memory Tree: ${err.message}`, true);
  }
}

function setMemoriesViewMode(mode) {
  wsState.memoriesViewMode = mode === "graph" ? "graph" : "tree";
  const isGraph = wsState.memoriesViewMode === "graph";

  const treeBtn = document.getElementById("wsMemoriesViewTreeBtn");
  const graphBtn = document.getElementById("wsMemoriesViewGraphBtn");
  const treeEl = document.getElementById("wsTreeHierarchyContainer");
  const proposalsEl = document.getElementById("wsTreeProposalsSection");
  const graphEl = document.getElementById("wsTreeGraphContainer");

  if (treeBtn) {
    treeBtn.classList.toggle("active", !isGraph);
    treeBtn.setAttribute("aria-pressed", String(!isGraph));
  }
  if (graphBtn) {
    graphBtn.classList.toggle("active", isGraph);
    graphBtn.setAttribute("aria-pressed", String(isGraph));
  }
  if (treeEl) treeEl.classList.toggle("hidden", isGraph);
  if (proposalsEl) {
    const hasProposals = proposalsEl.dataset.hasProposals === "true";
    proposalsEl.classList.toggle("hidden", isGraph || !hasProposals);
  }
  if (graphEl) graphEl.classList.toggle("hidden", !isGraph);

  if (isGraph) {
    renderWsMemoriesGraph();
  }
}

async function runInspectorQuickTrace(promptText, resultEl) {
  if (!resultEl) return;
  const cleanPrompt = String(promptText || "").trim();
  if (!cleanPrompt) {
    resultEl.innerHTML = `<div class="text-xs text-muted">Enter a prompt to trace recall.</div>`;
    return;
  }
  resultEl.innerHTML = `<div class="text-xs text-muted">Tracing recall...</div>`;
  try {
    const ids = getScopeIds();
    const res = await fetch("/api/studio/playground/turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: cleanPrompt,
        user_id: ids.user_id,
        team_id: ids.team_id,
        agent_id: ids.agent_id,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const resolved = Array.isArray(data.resolved_memories) ? data.resolved_memories : [];
    const conflicts = Array.isArray(data.conflicts) ? data.conflicts : [];
    const savings = data.token_savings_pct ?? 0;

    const matchedHtml = resolved.length
      ? resolved
          .map(
            (m) => `
            <div class="inspector-trace-row">
              <span class="badge badge-slate">${escapeHtml(String(m.scope_type || "USER").toUpperCase())}</span>
              <code class="font-mono text-xs">${escapeHtml(m.memory_key || "")}</code>
            </div>
          `
          )
          .join("")
      : `<div class="text-xs text-muted">No memories matched this prompt.</div>`;

    const conflictHtml = conflicts.length
      ? `<div class="text-xs text-secondary mt-4">Overrides: ${conflicts.map((c) => escapeHtml(c.memory_key || c.key || "conflict")).join(", ")}</div>`
      : "";

    resultEl.innerHTML = `
      <div class="inspector-trace-meta text-xs text-muted mb-4">${resolved.length} recalled · ${conflicts.length} overrides · ${escapeHtml(String(savings))}% token savings</div>
      <div class="inspector-trace-list">${matchedHtml}</div>
      ${conflictHtml}
    `;
  } catch (err) {
    resultEl.innerHTML = `<div class="text-xs text-muted">Trace failed: ${escapeHtml(err.message)}</div>`;
  }
}

function inspectWsMemoryNode(nodeObj) {
  if (!nodeObj) return;
  wsState.selectedMemoryKey = nodeObj.memory_key;
  const status = String(nodeObj.commit_status || "SYNCED").toUpperCase();
  const isStaged = status !== "SYNCED" && Boolean(nodeObj.staged_id);
  const scopeType = String(nodeObj.scope_type || "USER").toUpperCase();
  const nextScopeMap = { USER: "TEAM", AGENT: "TEAM", TEAM: "ORG" };
  const nextScope = nextScopeMap[scopeType] || "";
  const linkedSessions = getLinkedSessionsForMemory(nodeObj.memory_key, nodeObj.source_session_id || "");
  const defaultPrompt =
    (linkedSessions.find((s) => s.initial_prompt)?.initial_prompt) ||
    `How should we handle ${String(nodeObj.memory_key || "").replace(/_/g, " ")}?`;

  const diffHtml =
    status === "MODIFIED" || status === "CONFLICT"
      ? `<div class="tree-node-diff font-mono text-xs mt-4">
           <span class="diff-before">Before: ${escapeHtml(nodeObj.before_value || "—")}</span> →
           <span class="diff-after">After: ${escapeHtml(nodeObj.memory_value || nodeObj.after_value || "")}</span>
         </div>`
      : "";

  const actionsHtml = isStaged
    ? `<div class="guide-btn-grid mt-8">
         <button type="button" id="inspectorApproveNodeBtn" class="btn btn-secondary btn-xs">Approve</button>
         <button type="button" id="inspectorDismissNodeBtn" class="btn btn-danger-outline btn-xs">Dismiss</button>
       </div>`
    : `<div class="guide-btn-grid mt-8">
         ${nextScope ? `<button type="button" id="inspectorPromoteNodeBtn" class="btn btn-secondary btn-xs">Promote → ${escapeHtml(nextScope)}</button>` : ""}
         <button type="button" id="inspectorDeleteNodeBtn" class="btn btn-danger-outline btn-xs">Delete</button>
       </div>`;

  const sessionsHtml = linkedSessions.length
    ? `<div class="inspector-link-list mt-4">
         ${linkedSessions
           .map(
             (ls) => `
             <div class="inspector-linked-card">
               <div class="inspector-linked-card-top">
                 <button type="button" class="inline-flow-link font-mono text-xs ws-inspector-jump-session" data-session-id="${escapeHtml(ls.session_id)}">${escapeHtml(ls.session_id)}</button>
                 <span class="badge ${ls.relation === "proposed" ? "badge-indigo" : "badge-teal"}">${escapeHtml(ls.relation === "proposed" ? "Proposed" : "Recalled")}</span>
               </div>
               <div class="text-xs text-secondary mt-2">${escapeHtml(ls.initial_prompt || ls.title)}</div>
               <div class="mt-4">
                 <a href="index.html?session_id=${encodeURIComponent(ls.session_id)}" class="inline-flow-link text-xs">Open in Chat →</a>
               </div>
             </div>
           `
           )
           .join("")}
       </div>`
    : `<div class="text-xs text-muted mt-4">No source sessions linked yet.</div>`;

  const html = `
    <div class="mb-8">
      <div class="text-xs font-semibold">Rule Statement</div>
      <div class="text-xs text-primary mt-2"><strong>Value:</strong> ${escapeHtml(nodeObj.memory_value || nodeObj.after_value || "")}</div>
      <div class="text-xs text-secondary mt-2">${escapeHtml(nodeObj.natural_language_belief || nodeObj.diff_summary || "")}</div>
      ${diffHtml}
      ${actionsHtml}
    </div>
    <div class="mb-8">
      <div class="text-xs font-semibold">Source Sessions (${linkedSessions.length})</div>
      ${sessionsHtml}
    </div>
    <div class="mb-8">
      <div class="text-xs font-semibold">Quick Trace</div>
      <form id="inspectorQuickTraceForm" class="inspector-trace-form mt-4">
        <input type="text" id="inspectorQuickTraceInput" class="studio-input studio-input-sm w-full" value="${escapeHtml(defaultPrompt)}" placeholder="Test prompt against live memories...">
        <button type="submit" class="btn btn-secondary btn-xs mt-4 w-full">Trace recall</button>
      </form>
      <div id="inspectorQuickTraceResult" class="inspector-trace-box mt-4">
        <div class="text-xs text-muted">Run a trace on the source prompt to verify recall and scope precedence.</div>
      </div>
    </div>
  `;

  updateContextDetailPane(
    nodeObj.memory_key || "Memory",
    `${scopeType} · ${nodeObj.category || "technical"} · ${nodeObj.scope_id || ""}`,
    html,
    isStaged ? status : "LIVE",
    nodeObj
  );

  document.getElementById("inspectorApproveNodeBtn")?.addEventListener("click", async () => {
    if (nodeObj.staged_id) await handleWsTreeCommitSingle(nodeObj.staged_id);
  });
  document.getElementById("inspectorDismissNodeBtn")?.addEventListener("click", async () => {
    if (nodeObj.staged_id) await handleWsTreeDiscard(nodeObj.staged_id, false);
  });
  document.getElementById("inspectorPromoteNodeBtn")?.addEventListener("click", async () => {
    const ids = getScopeIds();
    const targetId = nextScope === "ORG" ? ids.org_id : ids.team_id;
    await handlePromoteMemory({
      source_scope_type: scopeType,
      source_scope_id: nodeObj.scope_id || ids.user_id,
      target_scope_type: nextScope,
      target_scope_id: targetId,
      memory_key: nodeObj.memory_key,
    });
  });
  document.getElementById("inspectorDeleteNodeBtn")?.addEventListener("click", async () => {
    await handleWsTreeDeleteNode(scopeType, nodeObj.scope_id || getScopeIds().user_id, nodeObj.memory_key);
  });

  document.querySelectorAll(".ws-inspector-jump-session").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = btn.getAttribute("data-session-id");
      await switchWorkspace("workspace-sessions");
      const found = (wsState.sessions || []).find((s) => s.session_id === sid);
      if (found) inspectWsSession(found);
    });
  });

  document.getElementById("inspectorQuickTraceForm")?.addEventListener("submit", async (evt) => {
    evt.preventDefault();
    const promptVal = document.getElementById("inspectorQuickTraceInput")?.value || "";
    const resultEl = document.getElementById("inspectorQuickTraceResult");
    await runInspectorQuickTrace(promptVal, resultEl);
  });
}

let wsMemoryGraphController = null;
let wsGraphEvolveAbortController = null;

async function startGraphAlphaEvolveStream(opts) {
  if (!wsMemoryGraphController) return;
  if (wsGraphEvolveAbortController) {
    try {
      wsGraphEvolveAbortController.abort();
    } catch (_) {}
  }
  const abortCtrl = new AbortController();
  wsGraphEvolveAbortController = abortCtrl;

  const payload = {
    generations: Number(opts?.generations || 3),
    candidates_per_generation: Number(opts?.candidates_per_generation || 2),
    alpha_quality_weight: Number(opts?.alpha_quality_weight ?? 0.65),
    auto_apply_min_gain:
      opts?.auto_apply_min_gain !== null && opts?.auto_apply_min_gain !== undefined
        ? Number(opts.auto_apply_min_gain)
        : null,
  };

  wsMemoryGraphController.updateEvolutionProgress({
    type: "start",
    step: 0,
    total_steps: 1 + payload.generations * payload.candidates_per_generation,
    message: `Starting evolution (${payload.generations} gens · ${payload.candidates_per_generation} candidates/gen)…`,
  });

  try {
    const res = await fetch("/api/studio/evolve/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: abortCtrl.signal,
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    }
    if (!res.body || typeof res.body.getReader !== "function") {
      throw new Error("Streaming response unavailable in this browser.");
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let receivedTerminal = false;

    const processFrame = (frame) => {
      const dataLines = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim());
      if (!dataLines.length) return;
      let evt;
      try {
        evt = JSON.parse(dataLines.join("\n"));
      } catch (_) {
        return;
      }
      if (wsGraphEvolveAbortController !== abortCtrl) return;
      wsMemoryGraphController.updateEvolutionProgress(evt);
      if (evt.type === "complete" && evt.result) {
        receivedTerminal = true;
        if (typeof renderAlphaEvolveResults === "function") {
          renderAlphaEvolveResults(evt.result);
        }
        if (evt.result.auto_applied && evt.result.active_config) {
          updateActiveConfigSidebar(evt.result.active_config);
          showStatusBanner(`✓ ${evt.result.auto_apply_reason || "Winning retrieval settings applied."}`);
        } else if (evt.result.auto_apply_reason) {
          showStatusBanner(evt.result.auto_apply_reason);
        } else {
          showStatusBanner("✓ Evolution complete. Review and apply winner from the graph bar.");
        }
      } else if (evt.type === "error") {
        receivedTerminal = true;
        showStatusBanner(`Evolution failed: ${evt.message || "Unknown error"}`, true);
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        if (buffer.trim()) {
          for (const frame of buffer.split("\n\n")) {
            processFrame(frame);
          }
        }
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        processFrame(frame);
      }
    }

    if (!receivedTerminal && wsGraphEvolveAbortController === abortCtrl) {
      wsMemoryGraphController.updateEvolutionProgress({
        type: "error",
        message: "Connection closed before evolution finished.",
      });
      showStatusBanner("Evolution interrupted: connection closed before completion.", true);
    }
  } catch (err) {
    if (wsGraphEvolveAbortController !== abortCtrl) {
      return;
    }
    if (err && err.name === "AbortError") {
      wsMemoryGraphController.updateEvolutionProgress({
        type: "cancelled",
        message: "Evolution stopped",
      });
      showStatusBanner("Evolution stopped.");
    } else {
      const msg = err?.message || String(err);
      wsMemoryGraphController.updateEvolutionProgress({
        type: "error",
        message: `Evolution failed: ${msg}`,
      });
      showStatusBanner(`Evolution failed: ${msg}`, true);
    }
  } finally {
    if (wsGraphEvolveAbortController === abortCtrl) {
      wsGraphEvolveAbortController = null;
    }
  }
}

async function applyGraphEvolveWinner(configObj) {
  if (!configObj) return;
  try {
    const res = await fetch("/api/studio/evolve/promote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        config: configObj,
        config_id: configObj.config_id || null,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    const activeCfg = data.active_config || configObj;
    updateActiveConfigSidebar(activeCfg);
    if (wsMemoryGraphController) {
      wsMemoryGraphController.setEvolutionApplied(activeCfg);
    }
    showStatusBanner(`✓ Applied winning config (${activeCfg.embedding_dimensions || 256}d · ${activeCfg.injection_format || "COMPACT_BULLET"}).`);
  } catch (err) {
    showStatusBanner(`Apply failed: ${err?.message || err}`, true);
  }
}

function renderWsMemoriesGraph() {
  const container = document.getElementById("wsTreeGraphContainer");
  if (!container || !wsState.treePayload) return;

  const searchQuery = document.getElementById("wsTreeSearchInput")?.value || "";
  const statusFilter = document.getElementById("wsTreeStatusFilter")?.value || "ALL";
  const sessions = wsState.sessions || [];
  const staged = Array.isArray(wsState.treePayload.staged_commits) ? wsState.treePayload.staged_commits : [];
  const tree = wsState.treePayload.tree || {};
  const liveNodes = [];
  ["ORG", "TEAM", "AGENT", "USER"].forEach((tier) => {
    (tree[tier] || []).forEach((n) => {
      if (String(n.commit_status || "SYNCED").toUpperCase() === "SYNCED") {
        liveNodes.push(n);
      }
    });
  });

  if (window.MemoryGraph && typeof window.MemoryGraph.MemoryGraphController === "function") {
    if (!wsMemoryGraphController || wsMemoryGraphController.container !== container) {
      wsMemoryGraphController = new window.MemoryGraph.MemoryGraphController(container, {
        onSelectNode: (node) => {
          if (!node) return;
          if (node.kind === "session" && node.payload) {
            inspectWsSession(node.payload, { fromGraph: true, skipCenterLog: true });
          } else if (node.kind === "proposal") {
            const target = findMemoryNodeByKey(node.memoryKey) || node.payload || staged.find((p) => p.memory_key === node.memoryKey);
            if (target) inspectWsMemoryNode(target);
          } else if (node.kind === "memory") {
            const target = node.payload || liveNodes.find((n) => n.memory_key === node.memoryKey);
            if (target) inspectWsMemoryNode(target);
          }
        },
        onStartEvolve: (opts) => startGraphAlphaEvolveStream(opts),
        onCancelEvolve: () => {
          if (wsGraphEvolveAbortController) {
            try {
              wsGraphEvolveAbortController.abort();
            } catch (_) {}
          }
        },
        onApplyEvolveWinner: (cfg) => applyGraphEvolveWinner(cfg),
      });
    }
    wsMemoryGraphController.update(wsState.treePayload, sessions, {
      searchQuery,
      statusFilter,
      scopeFilter: studioState.activeScopeFilter || "ALL",
    });
    return;
  }

  container.innerHTML = `<div class="text-xs text-muted p-4">Graph renderer unavailable.</div>`;
}

const EVAL_SCENARIO_LABELS = {
  CONTRADICTION_UPDATE: {
    type: "Contradiction",
    intent: "Honor updated preference over stale memory",
  },
  HALLUCINATION_TRAP: {
    type: "Hallucination",
    intent: "Avoid recalling unsupported or unrelated facts",
  },
  ORG_GUARDRAIL_OVERRIDE: {
    type: "Org Guardrail",
    intent: "Enforce organization guardrail over user preference",
  },
  COMPACT_TOKEN_RECALL: {
    type: "Token Budget",
    intent: "Recall key preference within compact token budget",
  },
  LIVE_FEEDBACK_CORRECTION: {
    type: "Feedback Fix",
    intent: "Recall corrected memory from session feedback",
  },
  LIVE_FEEDBACK_REINFORCEMENT: {
    type: "Feedback Keep",
    intent: "Keep recalling helpful memory from session feedback",
  },
};

const EVAL_SCOPE_LABELS = {
  ORGANIZATION: "Org",
  ORG: "Org",
  TEAM: "Team",
  AGENT: "Agent",
  USER: "User",
};

function formatEvalCaseDisplay(c) {
  const scenarioRaw = String(c.scenario_type || c.archetype || "").trim();
  const scenarioKey = scenarioRaw.toUpperCase();
  const scenarioInfo = EVAL_SCENARIO_LABELS[scenarioKey] || {
    type: scenarioRaw ? scenarioRaw.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (ch) => ch.toUpperCase()) : "Verification",
    intent: "",
  };

  const promptText = String(c.query_text || c.prompt || c.user_prompt || "").trim();
  const rawTitle = String(c.title || "").trim();
  const hasDescriptiveTitle = rawTitle && rawTitle.toUpperCase() !== scenarioKey;
  const derivedIntent = hasDescriptiveTitle
    ? rawTitle
    : scenarioInfo.intent || (promptText ? promptText.slice(0, 96) : "Verify memory recall");

  const recallList = Array.isArray(c.expected_facts) && c.expected_facts.length
    ? c.expected_facts.map((x) => String(x).trim()).filter(Boolean)
    : Array.isArray(c.expected_memory_keys) && c.expected_memory_keys.length
      ? c.expected_memory_keys.map((x) => String(x).trim()).filter(Boolean)
      : c.expected_fact || c.expected_behavior
        ? [String(c.expected_fact || c.expected_behavior).trim()]
        : [];

  const blockList = Array.isArray(c.forbidden_facts) && c.forbidden_facts.length
    ? c.forbidden_facts.map((x) => String(x).trim()).filter(Boolean)
    : [];

  const winningScopeRaw = String(c.expected_winning_scope || "").trim().toUpperCase();
  const winningScope = EVAL_SCOPE_LABELS[winningScopeRaw] || winningScopeRaw;

  const rawSession = String(c.source_session_id || c.session_id || "").trim();
  const sessionId = rawSession && rawSession !== "STUDIO_EVAL" ? rawSession : "";

  const notes = String(c.notes || "").trim();

  const dedupeKey = JSON.stringify([
    scenarioKey,
    derivedIntent,
    promptText,
    recallList,
    blockList,
    winningScope,
    sessionId,
  ]);

  return {
    scenarioKey,
    typeLabel: scenarioInfo.type,
    derivedIntent,
    promptText,
    recallList,
    blockList,
    winningScope,
    sessionId,
    notes,
    dedupeKey,
  };
}

function renderWsEvalCasesTable(cases, diagnostics = null) {
  const tbody = document.getElementById("wsEvalCasesTableBody");
  const countEl = document.getElementById("wsEvalCasesCountBadge");
  if (!tbody) return;
  const list = Array.isArray(cases) ? cases : [];

  if (!list.length) {
    if (countEl) countEl.textContent = "0 cases";
    tbody.innerHTML = `<tr><td colspan="4" class="text-center text-muted">No verification cases yet. Add feedback from a Session or click Add tests.</td></tr>`;
    return;
  }

  const diagMap = new Map();
  if (Array.isArray(diagnostics)) {
    diagnostics.forEach((d) => {
      if (d.case_id) diagMap.set(d.case_id, d);
    });
  }

  // Group identical verification cases so repeated runs don't flood the table with duplicate rows
  const grouped = [];
  const groupIndex = new Map();
  list.forEach((c, idx) => {
    const info = formatEvalCaseDisplay(c);
    const cid = c.case_id || c.id || `case-${idx}`;
    const diag = diagMap.get(cid) || null;
    if (groupIndex.has(info.dedupeKey)) {
      const existing = grouped[groupIndex.get(info.dedupeKey)];
      existing.count += 1;
      if (!existing.diag && diag) existing.diag = diag;
    } else {
      groupIndex.set(info.dedupeKey, grouped.length);
      grouped.push({ evalCase: c, info, diag, count: 1 });
    }
  });

  if (countEl) {
    countEl.textContent =
      grouped.length < list.length
        ? `${grouped.length} unique · ${list.length} stored`
        : `${list.length} cases`;
  }

  tbody.innerHTML = grouped
    .map(({ evalCase, info, diag, count }, idx) => {
      const promptHtml =
        info.promptText && info.promptText !== info.derivedIntent
          ? `<div class="eval-prompt-sub">“${escapeHtml(info.promptText.slice(0, 140))}”</div>`
          : "";

      const expectLines = [];
      if (info.recallList.length) {
        expectLines.push(
          `<div class="eval-expect-recall"><strong>Recall:</strong> ${escapeHtml(info.recallList.join(", ").slice(0, 96))}</div>`
        );
      }
      if (info.blockList.length) {
        expectLines.push(
          `<div class="eval-expect-block"><strong>Block:</strong> ${escapeHtml(info.blockList.join(", ").slice(0, 96))}</div>`
        );
      }
      if (info.winningScope) {
        expectLines.push(
          `<div class="eval-expect-scope"><strong>Winner:</strong> ${escapeHtml(info.winningScope)} scope</div>`
        );
      }
      if (!expectLines.length && info.notes) {
        expectLines.push(`<div class="eval-expect-scope">${escapeHtml(info.notes.slice(0, 110))}</div>`);
      }
      if (!expectLines.length) {
        expectLines.push(`<div class="text-muted">No expected facts specified</div>`);
      }

      const sourceSub = info.sessionId
        ? `<div class="font-mono text-xs text-muted mt-2">${escapeHtml(info.sessionId.slice(0, 22))}</div>`
        : "";
      const dupBadge = count > 1 ? `<span class="eval-dup-count" title="${count} identical stored cases">×${count}</span>` : "";

      const statusText = diag ? (diag.passed ? "Pass" : "Fail") : "Not run";
      const statusClass = diag ? (diag.passed ? "eval-status-pass" : "eval-status-fail") : "text-muted";

      return `
        <tr class="ws-clickable-row ws-eval-case-row" data-eval-group-idx="${idx}">
          <td>
            <div class="eval-intent-primary">${escapeHtml(info.derivedIntent)}${dupBadge}</div>
            ${promptHtml}
          </td>
          <td><div class="eval-expect-stack">${expectLines.join("")}</div></td>
          <td>
            <div class="text-xs font-semibold">${escapeHtml(info.typeLabel)}</div>
            ${sourceSub}
          </td>
          <td class="font-mono text-xs ${statusClass}">${escapeHtml(statusText)}</td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll(".ws-eval-case-row").forEach((row) => {
    row.addEventListener("click", () => {
      const idx = Number(row.getAttribute("data-eval-group-idx"));
      const entry = grouped[idx];
      if (!entry) return;
      inspectWsEvalCase(entry.evalCase, entry.diag || null, entry.count);
    });
  });
}

function inspectWsEvalCase(evalCase, diagnostic = null, duplicateCount = 1) {
  if (!evalCase) return;
  const info = formatEvalCaseDisplay(evalCase);

  const sourceHtml = info.sessionId
    ? `<div class="mt-4">
         <button type="button" id="inspectorEvalJumpSessionBtn" class="inline-flow-link font-mono text-xs">${escapeHtml(info.sessionId)}</button>
         <span class="text-xs text-muted"> · </span>
         <a href="index.html?session_id=${encodeURIComponent(info.sessionId)}" class="inline-flow-link text-xs">Open in Chat →</a>
       </div>`
    : `<div class="text-xs text-muted mt-2">${escapeHtml(info.typeLabel)} benchmark case${duplicateCount > 1 ? ` (${duplicateCount} stored copies)` : ""}</div>`;

  const outcomeRows = [];
  if (info.recallList.length) {
    outcomeRows.push(`<div class="eval-expect-recall"><strong>Must recall:</strong> ${escapeHtml(info.recallList.join(", "))}</div>`);
  }
  if (info.blockList.length) {
    outcomeRows.push(`<div class="eval-expect-block"><strong>Must not recall:</strong> ${escapeHtml(info.blockList.join(", "))}</div>`);
  }
  if (info.winningScope) {
    outcomeRows.push(`<div class="eval-expect-scope"><strong>Winning scope:</strong> ${escapeHtml(info.winningScope)}</div>`);
  }
  if (info.notes) {
    outcomeRows.push(`<div class="text-xs text-secondary mt-2">${escapeHtml(info.notes)}</div>`);
  }
  if (!outcomeRows.length) {
    outcomeRows.push(`<div class="text-xs text-muted">No expected outcome specified.</div>`);
  }

  const html = `
    <div class="mb-8">
      <div class="text-xs font-semibold">Intent</div>
      <p class="text-xs text-primary mt-2">${escapeHtml(info.derivedIntent)}</p>
      ${sourceHtml}
    </div>
    <div class="mb-8">
      <div class="text-xs font-semibold">User Prompt</div>
      <p class="text-xs text-secondary mt-2">${escapeHtml(info.promptText || "—")}</p>
    </div>
    <div class="mb-8">
      <div class="text-xs font-semibold">What Should Happen</div>
      <div class="eval-expect-stack mt-2">${outcomeRows.join("")}</div>
    </div>
    <div class="mb-8">
      <div class="text-xs font-semibold">Live Recall Trace</div>
      <div id="inspectorEvalTraceResult" class="inspector-trace-box mt-4">
        <div class="text-xs text-muted">Tracing prompt against live memories...</div>
      </div>
    </div>
  `;

  updateContextDetailPane(
    evalCase.case_id || "Verification Case",
    `${info.typeLabel} · ${diagnostic ? (diagnostic.passed ? "Pass" : "Fail") : "Not run"}`,
    html,
    diagnostic ? (diagnostic.passed ? "PASS" : "FAIL") : "READY",
    evalCase
  );

  document.getElementById("inspectorEvalJumpSessionBtn")?.addEventListener("click", async () => {
    await switchWorkspace("workspace-sessions");
    const found = (wsState.sessions || []).find((s) => s.session_id === info.sessionId);
    if (found) inspectWsSession(found);
  });

  if (info.promptText) {
    runInspectorQuickTrace(info.promptText, document.getElementById("inspectorEvalTraceResult"));
  } else {
    const traceEl = document.getElementById("inspectorEvalTraceResult");
    if (traceEl) traceEl.innerHTML = `<div class="text-xs text-muted">No prompt text available to trace.</div>`;
  }
}

function renderWsMemoryTree() {
  const data = wsState.treePayload;
  if (!data) return;
  const summary = data.summary || {};
  const newCount = Number(summary.new_count || 0);
  const modCount = Number(summary.modified_count || 0);
  const confCount = Number(summary.conflict_count || 0);
  const totalStaged = Number(summary.pending_staged_total || newCount + modCount + confCount);
  const totalSynced = Number(summary.synced_total || 0);

  const setText = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  };
  setText("treeStagedNewBadge", `${newCount} NEW`);
  setText("treeStagedModifiedBadge", `${modCount} MODIFIED`);
  setText("treeStagedConflictBadge", `${confCount} CONFLICT`);
  setText("treeSyncedSummaryText", `• ${totalSynced} Live`);
  setText("wsBadgeTreeStaged", `${totalStaged} proposals`);
  setText("inspectorCommitsCountBadge", String(totalStaged));
  setText("cmdBarStagedSyncBadge", `${totalStaged} Proposals • ${totalSynced} Live`);

  const container = document.getElementById("wsTreeHierarchyContainer");
  if (!container) return;

  const query = (document.getElementById("wsTreeSearchInput")?.value || "").trim().toLowerCase();
  const statusFilter = document.getElementById("wsTreeStatusFilter")?.value || "ALL";
  const scopeFilter = (studioState.activeScopeFilter || "ALL").toUpperCase();
  const tree = data.tree || {};

  // Update inline tier filter counts from live tree payload
  const tierCounts = {
    ORG: Array.isArray(tree.ORG) ? tree.ORG.length : 0,
    TEAM: Array.isArray(tree.TEAM) ? tree.TEAM.length : 0,
    AGENT: Array.isArray(tree.AGENT) ? tree.AGENT.length : 0,
    USER: Array.isArray(tree.USER) ? tree.USER.length : 0,
  };
  const allTierTotal = tierCounts.ORG + tierCounts.TEAM + tierCounts.AGENT + tierCounts.USER;
  setText("leftCountScopeAll", String(allTierTotal));
  setText("leftCountScopeOrg", String(tierCounts.ORG));
  setText("leftCountScopeTeam", String(tierCounts.TEAM));
  setText("leftCountScopeAgent", String(tierCounts.AGENT));
  setText("leftCountScopeUser", String(tierCounts.USER));

  // Render dedicated Proposals section at top of Tree view when proposals exist
  const proposalsSection = document.getElementById("wsTreeProposalsSection");
  const rawStagedList = Array.isArray(data.staged_commits) ? data.staged_commits : [];
  const stagedList = scopeFilter === "ALL"
    ? rawStagedList
    : rawStagedList.filter((p) => String(p.scope_type || "USER").toUpperCase() === scopeFilter);
  if (proposalsSection) {
    const showProposals = stagedList.length > 0 && statusFilter !== "SYNCED";
    proposalsSection.dataset.hasProposals = showProposals ? "true" : "false";
    proposalsSection.classList.toggle("hidden", !showProposals || wsState.memoriesViewMode === "graph");
    if (showProposals) {
      proposalsSection.innerHTML = `
        <div class="ws-tree-tier-header">
          <span class="font-semibold text-xs">Proposals Waiting for Review</span>
          <span class="badge badge-indigo">${stagedList.length} pending</span>
        </div>
        <div class="ws-tree-tier-nodes">
          ${stagedList
            .map((p) => {
              const st = String(p.commit_status || "NEW").toUpperCase();
              const pillCls = st === "CONFLICT" ? "commit-pill-conflict" : st === "MODIFIED" ? "commit-pill-modified" : "commit-pill-new";
              return `
                <div class="ws-tree-node-card ws-proposal-card" data-memory-key="${escapeHtml(p.memory_key)}" data-staged-id="${escapeHtml(p.staged_id || "")}">
                  <div class="ws-tree-node-main">
                    <div class="ws-tree-node-header">
                      <span class="commit-status-pill ${pillCls}">${escapeHtml(st)}</span>
                      <code class="font-mono font-semibold text-sm">${escapeHtml(p.memory_key)}</code>
                      <span class="badge badge-slate">${escapeHtml(p.scope_type || "USER")}</span>
                      ${p.source_session_id ? `<span class="text-xs font-mono text-muted">From ${escapeHtml(p.source_session_id)}</span>` : ""}
                    </div>
                    <div class="text-xs text-primary mt-2">${escapeHtml(p.after_value || p.proposed_content || "")}</div>
                  </div>
                  <div class="ws-tree-node-actions">
                    <button type="button" class="btn btn-secondary btn-xs ws-tree-commit-node-btn" data-staged-id="${escapeHtml(p.staged_id || "")}">Approve</button>
                    <button type="button" class="btn btn-danger-outline btn-xs ws-tree-discard-node-btn" data-staged-id="${escapeHtml(p.staged_id || "")}">Dismiss</button>
                  </div>
                </div>
              `;
            })
            .join("")}
        </div>
      `;
      proposalsSection.querySelectorAll(".ws-proposal-card").forEach((card) => {
        card.addEventListener("click", (evt) => {
          if (evt.target.closest("button")) return;
          const mk = card.getAttribute("data-memory-key");
          const node = findMemoryNodeByKey(mk) || stagedList.find((p) => p.memory_key === mk);
          if (node) inspectWsMemoryNode(node);
        });
      });
      proposalsSection.querySelectorAll(".ws-tree-commit-node-btn").forEach((btn) => {
        btn.addEventListener("click", async (evt) => {
          evt.stopPropagation();
          const sid = btn.getAttribute("data-staged-id");
          if (sid) await handleWsTreeCommitSingle(sid);
        });
      });
      proposalsSection.querySelectorAll(".ws-tree-discard-node-btn").forEach((btn) => {
        btn.addEventListener("click", async (evt) => {
          evt.stopPropagation();
          const sid = btn.getAttribute("data-staged-id");
          if (sid) await handleWsTreeDiscard(sid, false);
        });
      });
    }
  }

  const tiers = [
    { key: "ORG", label: "Organization — Shared Guardrails" },
    { key: "TEAM", label: "Team — Engineering Conventions" },
    { key: "AGENT", label: "Agent — Role Workflows" },
    { key: "USER", label: "Personal — Developer Preferences" },
  ];
  const visibleTiers = scopeFilter === "ALL" ? tiers : tiers.filter((t) => t.key === scopeFilter);

  container.innerHTML = visibleTiers
    .map((tier) => {
      const nodes = Array.isArray(tree[tier.key]) ? tree[tier.key] : [];
      const filteredNodes = nodes.filter((n) => {
        const st = String(n.commit_status || "SYNCED").toUpperCase();
        if (statusFilter === "STAGED" && st === "SYNCED") return false;
        if (statusFilter === "SYNCED" && st !== "SYNCED") return false;
        if (!query) return true;
        const hay = `${n.memory_key} ${n.memory_value} ${n.natural_language_belief || ""} ${n.category || ""}`.toLowerCase();
        return hay.includes(query);
      });

      const nodesHtml = filteredNodes.length
        ? filteredNodes
            .map((n) => {
              const status = String(n.commit_status || "SYNCED").toUpperCase();
              let badgeClass = "commit-pill-synced";
              if (status === "NEW") badgeClass = "commit-pill-new";
              if (status === "MODIFIED") badgeClass = "commit-pill-modified";
              if (status === "CONFLICT") badgeClass = "commit-pill-conflict";

              const isStaged = status !== "SYNCED" && n.staged_id;
              const usefulCount = Number(n.usefulness_count || 0);
              const nodeScope = String(n.scope_type || tier.key).toUpperCase();
              const nextScopeMap = { USER: "TEAM", AGENT: "TEAM", TEAM: "ORG" };
              const nextScope = nextScopeMap[nodeScope] || "";
              const diffHtml =
                status === "MODIFIED" || status === "CONFLICT"
                  ? `<div class="tree-node-diff font-mono text-xs mt-2">
                       <span class="diff-before">Before: ${escapeHtml(n.before_value || "—")}</span> →
                       <span class="diff-after">After: ${escapeHtml(n.memory_value || "")}</span>
                     </div>`
                  : "";

              return `
                <div class="ws-tree-node-card" data-staged-id="${escapeHtml(n.staged_id || "")}" data-memory-key="${escapeHtml(n.memory_key)}" data-scope-type="${escapeHtml(nodeScope)}" data-scope-id="${escapeHtml(n.scope_id || "")}">
                  <div class="ws-tree-node-main">
                    <div class="ws-tree-node-header">
                      <span class="commit-status-pill ${badgeClass}">${escapeHtml(status)}</span>
                      <code class="font-mono font-semibold text-sm">${escapeHtml(n.memory_key)}</code>
                      <span class="badge badge-slate">${escapeHtml(n.category || "technical")}</span>
                      ${usefulCount > 0 ? `<span class="usefulness-attribution-pill">${usefulCount} helpful</span>` : ""}
                    </div>
                    <div class="text-xs text-primary mt-2"><strong>Value:</strong> ${escapeHtml(n.memory_value)}</div>
                    <div class="text-xs text-secondary">${escapeHtml(n.natural_language_belief || "")}</div>
                    ${diffHtml}
                  </div>
                  <div class="ws-tree-node-actions">
                    ${
                      isStaged
                        ? `
                          <button type="button" class="btn btn-secondary btn-xs ws-tree-commit-node-btn" data-staged-id="${escapeHtml(n.staged_id)}">Approve</button>
                          <button type="button" class="btn btn-danger-outline btn-xs ws-tree-discard-node-btn" data-staged-id="${escapeHtml(n.staged_id)}">Dismiss</button>
                        `
                        : `
                          ${
                            nextScope
                              ? `<button type="button" class="btn btn-secondary btn-xs ws-tree-promote-node-btn" data-source-scope-type="${escapeHtml(nodeScope)}" data-source-scope-id="${escapeHtml(n.scope_id || "")}" data-target-scope-type="${escapeHtml(nextScope)}" data-memory-key="${escapeHtml(n.memory_key)}" title="Promote from ${escapeHtml(nodeScope)} to ${escapeHtml(nextScope)}">Promote → ${escapeHtml(nextScope)}</button>`
                              : ""
                          }
                          <button type="button" class="btn btn-danger-outline btn-xs ws-tree-delete-node-btn" data-scope-type="${escapeHtml(nodeScope)}" data-scope-id="${escapeHtml(n.scope_id || "")}" data-memory-key="${escapeHtml(n.memory_key)}">Delete</button>
                        `
                    }
                  </div>
                </div>
              `;
            })
            .join("")
        : `<div class="text-xs text-muted p-4">No memory nodes in ${tier.key} matching filter.</div>`;

      return `
        <div class="ws-tree-tier-block mb-8">
          <div class="ws-tree-tier-header">
            <span class="font-semibold text-xs">${escapeHtml(tier.label)}</span>
            <span class="badge badge-slate">${filteredNodes.length} nodes</span>
          </div>
          <div class="ws-tree-tier-nodes">${nodesHtml}</div>
        </div>
      `;
    })
    .join("");

  container.querySelectorAll(".ws-tree-node-card").forEach((card) => {
    card.addEventListener("click", (evt) => {
      if (evt.target.closest("button")) return;
      const mkey = card.getAttribute("data-memory-key") || "";
      const stype = card.getAttribute("data-scope-type") || "";
      const tierNodes = Array.isArray(tree[stype]) ? tree[stype] : [];
      const found = tierNodes.find((n) => n.memory_key === mkey) || findMemoryNodeByKey(mkey);
      if (found) inspectWsMemoryNode(found);
    });
  });

  container.querySelectorAll(".ws-tree-commit-node-btn").forEach((btn) => {
    btn.addEventListener("click", async (evt) => {
      evt.stopPropagation();
      const sid = btn.getAttribute("data-staged-id");
      if (sid) await handleWsTreeCommitSingle(sid);
    });
  });

  container.querySelectorAll(".ws-tree-discard-node-btn").forEach((btn) => {
    btn.addEventListener("click", async (evt) => {
      evt.stopPropagation();
      const sid = btn.getAttribute("data-staged-id");
      if (sid) await handleWsTreeDiscard(sid, false);
    });
  });

  container.querySelectorAll(".ws-tree-promote-node-btn").forEach((btn) => {
    btn.addEventListener("click", async (evt) => {
      evt.stopPropagation();
      const ids = getScopeIds();
      const sourceType = btn.getAttribute("data-source-scope-type") || "USER";
      const sourceId = btn.getAttribute("data-source-scope-id") || (sourceType === "TEAM" ? ids.team_id : sourceType === "AGENT" ? ids.agent_id : ids.user_id);
      const targetType = btn.getAttribute("data-target-scope-type") || "TEAM";
      const targetId = targetType === "ORG" ? ids.org_id : ids.team_id;
      const mkey = btn.getAttribute("data-memory-key") || "";
      if (mkey) {
        await handlePromoteMemory({
          source_scope_type: sourceType,
          source_scope_id: sourceId,
          target_scope_type: targetType,
          target_scope_id: targetId,
          memory_key: mkey,
        });
      }
    });
  });

  container.querySelectorAll(".ws-tree-delete-node-btn").forEach((btn) => {
    btn.addEventListener("click", async (evt) => {
      evt.stopPropagation();
      const st = btn.getAttribute("data-scope-type") || "USER";
      const sid = btn.getAttribute("data-scope-id") || getScopeIds().user_id;
      const mkey = btn.getAttribute("data-memory-key") || "";
      if (mkey && sid) await handleWsTreeDeleteNode(st, sid, mkey);
    });
  });

  if (wsState.memoriesViewMode === "graph") {
    renderWsMemoriesGraph();
  }
  renderLeftMemoryTree();
}

function renderInspectorStagedCommits() {
  const listEl = document.getElementById("inspectorStagedCommitsList");
  if (!listEl || !wsState.treePayload) return;
  const staged = Array.isArray(wsState.treePayload.staged_commits) ? wsState.treePayload.staged_commits : [];
  if (!staged.length) {
    listEl.innerHTML = `<div class="text-xs text-muted">Working tree is clean. All rules are synced.</div>`;
    return;
  }
  listEl.innerHTML = staged
    .map((item) => {
      const st = String(item.commit_status || "NEW").toUpperCase();
      let pillClass = "commit-pill-new";
      if (st === "MODIFIED") pillClass = "commit-pill-modified";
      if (st === "CONFLICT") pillClass = "commit-pill-conflict";
      return `
        <div class="studio-audit-card mb-8">
          <div class="card-header-compact">
            <span class="commit-status-pill ${pillClass}">${escapeHtml(st)}</span>
            <code class="font-mono text-xs">${escapeHtml(item.scope_type)}:${escapeHtml(item.memory_key)}</code>
          </div>
          <div class="text-xs mt-2"><strong>Before:</strong> <code>${escapeHtml(item.before_value || "(none)")}</code></div>
          <div class="text-xs mt-2"><strong>After:</strong> <code>${escapeHtml(item.after_value || "")}</code></div>
          <div class="quick-editor-actions mt-4">
            <button type="button" class="btn btn-secondary btn-xs inspector-commit-single-btn" data-staged-id="${escapeHtml(item.staged_id)}">Approve</button>
            <button type="button" class="btn btn-danger-outline btn-xs inspector-discard-single-btn" data-staged-id="${escapeHtml(item.staged_id)}">Dismiss</button>
          </div>
        </div>
      `;
    })
    .join("");

  listEl.querySelectorAll(".inspector-commit-single-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = btn.getAttribute("data-staged-id");
      if (sid) await handleWsTreeCommitSingle(sid);
    });
  });
  listEl.querySelectorAll(".inspector-discard-single-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = btn.getAttribute("data-staged-id");
      if (sid) await handleWsTreeDiscard(sid, false);
    });
  });
}

async function handleWsTreeStageNode() {
  const scopeType = document.getElementById("wsTreeStageScopeType")?.value || "USER";
  const category = document.getElementById("wsTreeStageCategory")?.value || "technical";
  const memoryKey = (document.getElementById("wsTreeStageKey")?.value || "").trim();
  const memoryValue = (document.getElementById("wsTreeStageValue")?.value || "").trim();
  const belief = (document.getElementById("wsTreeStageBelief")?.value || "").trim();
  if (!memoryKey || !memoryValue) {
    showStatusBanner("Please provide both a Memory Key and Target Value to stage.", true);
    return;
  }
  const ids = getScopeIds();
  const scopeIdMap = { ORG: ids.org_id, TEAM: ids.team_id, AGENT: ids.agent_id, USER: ids.user_id };
  try {
    const res = await fetch("/api/studio/tree/stage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope_type: scopeType,
        scope_id: scopeIdMap[scopeType] || ids.user_id,
        category,
        memory_key: memoryKey,
        after_value: memoryValue,
        natural_language_belief: belief || `Prefer ${memoryValue} for ${memoryKey.replace(/_/g, " ")}.`,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    showStatusBanner(`✓ Staged ${data.staged?.commit_status || "NEW"} node '${memoryKey}' in working tree.`);
    document.getElementById("wsTreeStageFormCard")?.classList.add("hidden");
    await loadWsMemoryTree();
  } catch (err) {
    showStatusBanner(`Failed to stage memory node: ${err.message}`, true);
  }
}

async function handleWsTreeCommitSingle(stagedId) {
  try {
    const res = await fetch("/api/studio/tree/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ staged_id: stagedId }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    showStatusBanner(`Committed '${data.committed?.memory_key || stagedId}'.`);
    await loadWsMemoryTree();
    await refreshScopesExplorer();
  } catch (err) {
    showStatusBanner(`Commit failed: ${err.message}`, true);
  }
}

async function handleWsTreeCommitAll() {
  try {
    const res = await fetch("/api/studio/tree/commit-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    showStatusBanner(`Committed all ${data.committed_count || 0} staged rules.`);
    await loadWsMemoryTree();
    await refreshScopesExplorer();
  } catch (err) {
    showStatusBanner(`Commit All failed: ${err.message}`, true);
  }
}

async function handleWsTreeDiscard(stagedId, discardAll = false) {
  try {
    const res = await fetch("/api/studio/tree/discard", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ staged_id: stagedId, discard_all: discardAll }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(discardAll ? "✓ Discarded all pending staged commits." : `✓ Discarded staged commit ${stagedId}.`);
    await loadWsMemoryTree();
  } catch (err) {
    showStatusBanner(`Discard failed: ${err.message}`, true);
  }
}

async function handleWsTreeDeleteNode(scopeType, scopeId, memoryKey) {
  try {
    const qs = new URLSearchParams({ scope_type: scopeType, scope_id: scopeId, memory_key: memoryKey }).toString();
    const res = await fetch(`/api/studio/tree/node?${qs}`, { method: "DELETE" });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(`Deleted ${scopeType}:${memoryKey}.`);
    await loadWsMemoryTree();
    await refreshScopesExplorer();
  } catch (err) {
    showStatusBanner(`Delete node failed: ${err.message}`, true);
  }
}

// ----------------------------------------------------------------------------
// WORKSPACE 3: Playground & Context Trace
// ----------------------------------------------------------------------------
async function loadWsPlaygroundContext() {
  const ids = getScopeIds();
  try {
    const qs = new URLSearchParams(ids).toString();
    const res = await fetch(`/api/studio/scopes?${qs}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    const fed = data.federated || {};
    const preEl = document.getElementById("wsPgContextPre");
    if (preEl) preEl.textContent = fed.formatted_prompt_context || "// No active memories resolved.";
    const countBadge = document.getElementById("wsPgResolvedCountBadge");
    if (countBadge) countBadge.textContent = `${(fed.resolved_memories || []).length} Memories`;
    const conflictBadge = document.getElementById("wsPgConflictCountBadge");
    const conflicts = Array.isArray(fed.conflicts_resolved) ? fed.conflicts_resolved : [];
    if (conflictBadge) conflictBadge.textContent = `${conflicts.length} Overrides`;
    const conflictList = document.getElementById("wsPgConflictList");
    if (conflictList) {
      conflictList.innerHTML = conflicts.length
        ? conflicts
            .map(
              (c) => `
            <div class="conflict-mini-item">
              <code class="font-mono text-xs">${escapeHtml(c.memory_key)}</code>:
              <strong>${escapeHtml(c.winning_scope)}</strong> (${escapeHtml(c.winning_value)}) overrode
              <span class="text-muted">${escapeHtml((c.overridden || []).map((o) => o.scope_type).join(", "))}</span>
            </div>
          `
            )
            .join("")
        : `<div class="text-xs text-muted">No scope conflicts detected.</div>`;
    }
  } catch (err) {
    showStatusBanner(`Playground context refresh failed: ${err.message}`, true);
  }
}

async function handleWsPlaygroundSubmit(evt) {
  if (evt) evt.preventDefault();
  const inputEl = document.getElementById("wsPgPromptInput");
  const sendBtn = document.getElementById("wsPgSendBtn");
  const transcriptEl = document.getElementById("wsPgTranscriptList");
  const promptText = (inputEl?.value || "").trim();
  if (!promptText) return;

  const ids = getScopeIds();
  const dim = Number(document.getElementById("wsPgDimSelect")?.value || 768);
  const fmt = document.getElementById("wsPgFormatSelect")?.value || "COMPACT_BULLET";

  if (sendBtn) {
    sendBtn.disabled = true;
    sendBtn.textContent = "Running...";
  }
  try {
    const res = await fetch("/api/preference/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: ids.user_id,
        team_id: ids.team_id,
        agent_id: ids.agent_id,
        prompt: promptText,
        embedding_dimensions: dim,
        injection_format: fmt,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    if (inputEl) inputEl.value = "";
    if (transcriptEl) {
      const turnDiv = document.createElement("div");
      turnDiv.className = "chat-turn";
      turnDiv.innerHTML = `
        <div class="turn-badge">USER PROBE (${dim}d • ${escapeHtml(fmt)})</div>
        <p class="turn-text"><strong>Q:</strong> ${escapeHtml(promptText)}</p>
        <p class="turn-text mt-4"><strong>Agent Response:</strong> ${escapeHtml(data.response || data.answer || JSON.stringify(data))}</p>
      `;
      transcriptEl.prepend(turnDiv);
    }
    await loadWsPlaygroundContext();
  } catch (err) {
    showStatusBanner(`Playground turn failed: ${err.message}`, true);
  } finally {
    if (sendBtn) {
      sendBtn.disabled = false;
      sendBtn.textContent = "Run Turn ▶";
    }
  }
}

// ----------------------------------------------------------------------------
// WORKSPACE 4: Memory Quality & Token Cost Benchmark
// ----------------------------------------------------------------------------
async function handleWsEvalRunSuite() {
  const btn = document.getElementById("wsEvalRunSuiteBtn");
  const ids = getScopeIds();
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Running Benchmark...";
  }
  try {
    const res = await fetch("/api/studio/eval/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: ids.user_id,
        team_id: ids.team_id,
        agent_id: ids.agent_id,
        compare_matryoshka_slices: true,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    const sc = data.scorecard || {};
    const m = sc.metrics || {};
    const fmtPct = (v) => `${Math.round(Number(v || 0) * 100)}%`;

    const setText = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };
    setText("wsEvalParetoVal", Number(sc.pareto_fitness || 0).toFixed(3));
    setText("wsEvalAccuracyVal", fmtPct(m.retrieval_accuracy));
    setText("wsEvalGuardrailVal", fmtPct(m.guardrail_compliance));

    const sliceBody = document.getElementById("wsEvalSliceTableBody");
    const slices = data.slice_comparison?.slices || [];
    if (sliceBody && slices.length) {
      sliceBody.innerHTML = slices
        .map(
          (s) => `
          <tr>
            <td class="font-mono font-semibold">${escapeHtml(String(s.embedding_dimensions))}d</td>
            <td>${fmtPct(s.metrics?.retrieval_accuracy)}</td>
            <td>${fmtPct(s.metrics?.conflict_resolution_precision)}</td>
            <td>${fmtPct(s.metrics?.guardrail_compliance)}</td>
            <td class="font-mono font-semibold">${Number(s.pareto_fitness || 0).toFixed(3)}</td>
          </tr>
        `
        )
        .join("");
    }
    showStatusBanner(`✓ Benchmark completed (Overall Score: ${Number(sc.pareto_fitness || 0).toFixed(3)}).`);
    studioState.lastScorecard = data;
    renderWsEvalCasesTable(studioState.evalCases || [], data.diagnostics || []);
  } catch (err) {
    showStatusBanner(`Benchmark failed: ${err.message}`, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Run eval";
    }
  }
}

async function handleWsEvalRunAlphaEvolve() {
  const btn = document.getElementById("wsEvalRunAlphaEvolveBtn");
  const optBtn = document.getElementById("wsOptimizeRunAlphaEvolveBtn");
  const ids = getScopeIds();
  const alphaWeight = Number(document.getElementById("optimizeAlphaSlider")?.value || 0.7);
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Auto-tuning...";
  }
  if (optBtn) {
    optBtn.disabled = true;
    optBtn.textContent = "Auto-tuning...";
  }
  try {
    const job = await startStudioJob("alpha_evolve", "Optimize", {
      user_id: ids.user_id,
      team_id: ids.team_id,
      agent_id: ids.agent_id,
      generations: 4,
      candidates_per_generation: 2,
      alpha_quality_weight: alphaWeight,
    });
    if (job) {
      showStatusBanner("Auto-tune started in background — live progress in Jobs.");
    }
  } catch (err) {
    showStatusBanner(`Auto-tune failed: ${err.message}`, true);
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Auto-tune";
    }
    if (optBtn) {
      optBtn.disabled = false;
      optBtn.textContent = "Run Auto-Tune";
    }
  }
}

// ----------------------------------------------------------------------------
// WORKSPACE 5: Enterprise Entities & Scope Hierarchy
// ----------------------------------------------------------------------------
function populateContextEntitySelects() {
  const entities = Array.isArray(wsState.entities) ? wsState.entities : [];
  const fillSelect = (selectId, scopeType, defaultLabel) => {
    const selectEl = document.getElementById(selectId);
    if (!selectEl || selectEl.tagName !== "SELECT") return;
    const prevVal = selectEl.value;
    const matching = entities.filter((e) => String(e.scope_type || "").toUpperCase() === scopeType);
    selectEl.innerHTML = [
      `<option value="">${escapeHtml(defaultLabel)}</option>`,
      ...matching.map(
        (e) => `<option value="${escapeHtml(e.scope_id)}">${escapeHtml(e.display_name || e.scope_id)}</option>`
      ),
    ].join("");
    if (prevVal && matching.some((e) => e.scope_id === prevVal)) {
      selectEl.value = prevVal;
    }
  };
  fillSelect("teamIdInput", "TEAM", "All teams");
  fillSelect("agentIdInput", "AGENT", "All agents");

  const readoutEl = document.getElementById("activeSignedInUserBadge");
  if (readoutEl) {
    const override = (document.getElementById("userIdInput")?.value || "").trim();
    let stored = "";
    try {
      stored = sessionStorage.getItem("electric_sheep_user_email") || "";
    } catch (_) {}
    readoutEl.textContent = override || studioState.authenticatedEmail || stored || "Signed-in user";
  }
}

async function loadWsEntities() {
  try {
    const res = await fetch("/api/studio/entities");
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    if (data.authenticated_email) {
      studioState.authenticatedEmail = String(data.authenticated_email).trim();
      try {
        sessionStorage.setItem("electric_sheep_user_email", studioState.authenticatedEmail);
      } catch (_) {}
    }
    wsState.entities = Array.isArray(data.entities) ? data.entities : [];
    populateContextEntitySelects();
    const countBadge = document.getElementById("wsEntitiesCountBadge");
    if (countBadge) countBadge.textContent = `${wsState.entities.length} Entities`;
    const tbody = document.getElementById("wsEntitiesTableBody");
    if (!tbody) return;
    tbody.innerHTML = wsState.entities
      .map(
        (e) => `
        <tr class="ws-clickable-row" data-entity-id="${escapeHtml(e.scope_id)}">
          <td class="font-mono text-xs font-semibold">${escapeHtml(e.scope_id)}</td>
          <td><span class="badge badge-indigo">${escapeHtml(e.scope_type)}</span></td>
          <td>
            <div class="font-semibold text-xs">${escapeHtml(e.display_name)}</div>
            <div class="text-xs text-secondary">${escapeHtml(e.description || "")}</div>
          </td>
          <td class="font-mono text-xs">${escapeHtml(e.parent_scope_id || "— (Root)")}</td>
          <td class="font-mono text-xs">${Number(e.memory_count || 0)}</td>
          <td class="font-mono text-xs">${escapeHtml(e.owner || "")}</td>
          <td>
            ${
              e.scope_id !== "org:default" && e.scope_type !== "ORG"
                ? `<button type="button" class="btn btn-danger-outline btn-xs ws-delete-entity-btn" data-scope-type="${escapeHtml(e.scope_type)}" data-scope-id="${escapeHtml(e.scope_id)}">🗑 Delete</button>`
                : `<span class="badge badge-teal">Root Guardrail</span>`
            }
          </td>
        </tr>
      `
      )
      .join("");

    tbody.querySelectorAll(".ws-delete-entity-btn").forEach((btn) => {
      btn.addEventListener("click", async (evt) => {
        evt.stopPropagation();
        const st = btn.getAttribute("data-scope-type") || "TEAM";
        const sid = btn.getAttribute("data-scope-id") || "";
        if (sid) await handleWsDeleteEntity(st, sid);
      });
    });
  } catch (err) {
    showStatusBanner(`Failed to load entities: ${err.message}`, true);
  }
}

async function handleWsSaveEntity() {
  const scopeId = (document.getElementById("wsEntityScopeIdInput")?.value || "").trim();
  const scopeType = document.getElementById("wsEntityScopeTypeSelect")?.value || "TEAM";
  const displayName = (document.getElementById("wsEntityDisplayNameInput")?.value || "").trim();
  const parentScopeId = (document.getElementById("wsEntityParentScopeInput")?.value || "").trim();
  const owner = (document.getElementById("wsEntityOwnerInput")?.value || "").trim();
  if (!scopeId || !displayName) {
    showStatusBanner("Scope ID and Display Name are required to register an entity.", true);
    return;
  }
  try {
    const res = await fetch("/api/studio/entities", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope_id: scopeId,
        scope_type: scopeType,
        display_name: displayName,
        parent_scope_id: parentScopeId,
        owner,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(`Saved scope ${scopeType}:${scopeId}.`);
    await loadWsEntities();
  } catch (err) {
    showStatusBanner(`Save entity failed: ${err.message}`, true);
  }
}

async function handleWsDeleteEntity(scopeType, scopeId) {
  try {
    const res = await fetch(`/api/studio/entities/${encodeURIComponent(scopeType)}/${encodeURIComponent(scopeId)}`, {
      method: "DELETE",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(`✓ Deleted entity ${scopeType}:${scopeId}.`);
    await loadWsEntities();
  } catch (err) {
    showStatusBanner(`Delete entity failed: ${err.message}`, true);
  }
}

// ----------------------------------------------------------------------------
// WORKSPACE 6: Memory Categories & Cosine Thresholds
// ----------------------------------------------------------------------------
async function loadWsCategories() {
  try {
    const res = await fetch("/api/studio/categories");
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    wsState.categories = Array.isArray(data.categories) ? data.categories : [];
    const countBadge = document.getElementById("wsCategoriesCountBadge");
    if (countBadge) countBadge.textContent = `${wsState.categories.length} Categories`;
    const tbody = document.getElementById("wsCategoriesTableBody");
    if (!tbody) return;
    tbody.innerHTML = wsState.categories
      .map(
        (c) => `
        <tr>
          <td class="font-mono text-xs font-semibold">${escapeHtml(c.category_id)}</td>
          <td>
            <div class="font-semibold text-xs">${escapeHtml(c.label || c.name || c.category_id)}</div>
            <div class="text-xs text-secondary">${escapeHtml(c.description || "")}</div>
          </td>
          <td class="font-mono text-xs">${Number(c.default_threshold || c.trigger_threshold || 0.65).toFixed(2)}</td>
          <td class="font-mono text-xs">${Number(c.retention_days || 365)}d</td>
          <td><span class="badge ${c.auto_extract !== false ? "badge-teal" : "badge-slate"}">${c.auto_extract !== false ? "AUTO" : "MANUAL"}</span></td>
          <td class="font-mono text-xs">${Number(c.memory_count || 0)}</td>
          <td>
            <button type="button" class="btn btn-danger-outline btn-xs ws-delete-cat-btn" data-category-id="${escapeHtml(c.category_id)}">Delete</button>
          </td>
        </tr>
      `
      )
      .join("");

    tbody.querySelectorAll(".ws-delete-cat-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const cid = btn.getAttribute("data-category-id");
        if (cid) await handleWsDeleteCategory(cid);
      });
    });
  } catch (err) {
    showStatusBanner(`Failed to load categories: ${err.message}`, true);
  }
}

async function handleWsSaveCategory() {
  const categoryId = (document.getElementById("wsCatIdInput")?.value || "").trim();
  const label = (document.getElementById("wsCatLabelInput")?.value || "").trim();
  const threshold = Number(document.getElementById("wsCatThresholdInput")?.value || 0.68);
  const retentionDays = Number(document.getElementById("wsCatRetentionInput")?.value || 365);
  const description = (document.getElementById("wsCatDescInput")?.value || "").trim();
  if (!categoryId || !label) {
    showStatusBanner("Category ID and Display Label are required.", true);
    return;
  }
  try {
    const res = await fetch("/api/studio/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        category_id: categoryId,
        label,
        default_threshold: threshold,
        retention_days: retentionDays,
        description,
        auto_extract: true,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(`Saved category '${categoryId}'.`);
    await loadWsCategories();
  } catch (err) {
    showStatusBanner(`Save category failed: ${err.message}`, true);
  }
}

async function handleWsDeleteCategory(categoryId) {
  try {
    const res = await fetch(`/api/studio/categories/${encodeURIComponent(categoryId)}`, {
      method: "DELETE",
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(`✓ Deleted category '${categoryId}'.`);
    await loadWsCategories();
  } catch (err) {
    showStatusBanner(`Delete category failed: ${err.message}`, true);
  }
}

// ----------------------------------------------------------------------------
// WORKSPACE 7: Data Ingest Services & Virtualized Stream Log
// ----------------------------------------------------------------------------
async function loadWsIngestServices() {
  try {
    const res = await fetch("/api/studio/ingest/services");
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    wsState.ingestServices = Array.isArray(data.services) ? data.services : [];
    const countBadge = document.getElementById("wsIngestServicesCountBadge");
    if (countBadge) countBadge.textContent = `${wsState.ingestServices.length} Connectors`;
    const tbody = document.getElementById("wsIngestServicesTableBody");
    if (tbody) {
      tbody.innerHTML = wsState.ingestServices
        .map(
          (svc) => `
          <tr class="ws-clickable-row" data-service-id="${escapeHtml(svc.service_id)}">
            <td>
              <div class="font-semibold text-xs">${escapeHtml(svc.name)}</div>
              <code class="font-mono text-xs text-muted">${escapeHtml(svc.service_id)}</code>
            </td>
            <td class="font-mono text-xs">
              <span class="badge badge-slate">${escapeHtml(svc.source_type)}</span>
              <div class="mt-2">${escapeHtml(svc.endpoint_uri)}</div>
            </td>
            <td class="font-mono text-xs">${Number(svc.chunk_window_turns || 5)}t • τ≥${Number(svc.min_extraction_confidence || 0.75).toFixed(2)}</td>
            <td class="font-mono text-xs">${Number(svc.events_ingested || 0)}</td>
            <td><span class="badge badge-teal">${escapeHtml(svc.status || "ACTIVE")}</span></td>
          </tr>
        `
        )
        .join("");
    }

    // Also hydrate the virtualized stream log from /api/studio/ingest/raw-stream
    const rawRes = await fetch("/api/studio/ingest/raw-stream?limit=25");
    if (rawRes.ok) {
      const rawData = await rawRes.json();
      const events = Array.isArray(rawData.events) ? rawData.events : [];
      const streamList = document.getElementById("wsIngestStreamLogList");
      if (streamList && events.length) {
        streamList.innerHTML = events
          .map(
            (ev) => `
            <div class="ingest-stream-log-row">
              <span class="badge badge-slate font-mono">${escapeHtml(ev.scope_type || "USER")}</span>
              <code class="font-mono text-xs">${escapeHtml(ev.session_id || "sess")}</code>
              <span class="text-xs text-primary flex-1">${escapeHtml(ev.content || ev.raw_text || "")}</span>
              <span class="badge badge-indigo">${escapeHtml(ev.detected_signal || "SIGNAL")}</span>
            </div>
          `
          )
          .join("");
      }
    }
  } catch (err) {
    showStatusBanner(`Failed to load ingest services: ${err.message}`, true);
  }
}

async function handleWsSaveIngestService() {
  const serviceId = (document.getElementById("wsIngestIdInput")?.value || "").trim();
  const name = (document.getElementById("wsIngestNameInput")?.value || "").trim();
  const sourceType = document.getElementById("wsIngestSourceTypeSelect")?.value || "GCS_JSONL";
  const piiMode = document.getElementById("wsIngestPiiModeSelect")?.value || "STRICT_ZERO_SECRETS";
  const endpointUri = (document.getElementById("wsIngestEndpointInput")?.value || "").trim();
  if (!name || !endpointUri) {
    showStatusBanner("Connector Name and Endpoint URI are required.", true);
    return;
  }
  try {
    const res = await fetch("/api/studio/ingest/services", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        service_id: serviceId || undefined,
        name,
        source_type: sourceType,
        endpoint_uri: endpointUri,
        pii_redaction_mode: piiMode,
        status: "ACTIVE",
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(`Saved connector '${name}'.`);
    await loadWsIngestServices();
  } catch (err) {
    showStatusBanner(`Save ingest connector failed: ${err.message}`, true);
  }
}

// ----------------------------------------------------------------------------
// WORKSPACE 8: Memory Extractors & Interactive Gemini 4 Neon Co-Designer Chat
// ----------------------------------------------------------------------------
async function loadWsExtractors() {
  try {
    const res = await fetch("/api/studio/extractors");
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    wsState.extractors = Array.isArray(data.extractors) ? data.extractors : [];
    const countBadge = document.getElementById("wsExtractorsCountBadge");
    if (countBadge) countBadge.textContent = `${wsState.extractors.length} Extractors`;

    const listEl = document.getElementById("wsExtractorsList");
    if (listEl) {
      listEl.innerHTML = wsState.extractors
        .map((ext) => {
          const isActive = ext.extractor_id === wsState.activeExtractorId;
          return `
            <div class="studio-candidate-card ws-clickable-row ${isActive ? "active" : ""}" data-extractor-id="${escapeHtml(ext.extractor_id)}">
              <div class="card-header-compact">
                <span class="font-semibold text-xs">${escapeHtml(ext.name)}</span>
                <span class="badge badge-indigo font-mono">${escapeHtml(ext.model_name || "gemini-4-hydrogen")}</span>
              </div>
              <div class="text-xs text-secondary mt-2">Categories: <code>${escapeHtml((ext.target_categories || []).join(", "))}</code></div>
            </div>
          `;
        })
        .join("");

      listEl.querySelectorAll("[data-extractor-id]").forEach((card) => {
        card.addEventListener("click", () => {
          const eid = card.getAttribute("data-extractor-id");
          const ext = wsState.extractors.find((item) => item.extractor_id === eid);
          if (ext) selectWsExtractor(ext);
        });
      });
    }

    const activeExt =
      wsState.extractors.find((e) => e.extractor_id === wsState.activeExtractorId) || wsState.extractors[0];
    if (activeExt) selectWsExtractor(activeExt);
  } catch (err) {
    showStatusBanner(`Failed to load extractors: ${err.message}`, true);
  }
}

function selectWsExtractor(ext) {
  wsState.activeExtractorId = ext.extractor_id;
  const idInput = document.getElementById("wsActiveExtractorIdInput");
  const nameInput = document.getElementById("wsExtractorNameInput");
  const modelInput = document.getElementById("wsExtractorModelInput");
  const catInput = document.getElementById("wsExtractorCategoriesInput");
  const promptArea = document.getElementById("wsExtractorSystemPromptTextarea");

  if (idInput) idInput.value = ext.extractor_id;
  if (nameInput) nameInput.value = ext.name || "";
  if (modelInput) modelInput.value = ext.model_name || "gemini-4-hydrogen";
  if (catInput) catInput.value = (ext.target_categories || []).join(", ");
  if (promptArea) promptArea.value = ext.system_prompt || "";

  const examples = Array.isArray(ext.few_shot_examples) ? ext.few_shot_examples : [];
  const examplesHtml = examples.length
    ? examples
        .map(
          (ex) => `
        <div class="studio-audit-card mb-4">
          <div class="text-xs"><strong>Input Turn:</strong> ${escapeHtml(ex.input || "")}</div>
          <div class="text-xs font-mono text-secondary mt-2">→ ${escapeHtml(ex.extracted_key || "")} = ${escapeHtml(ex.extracted_value || "")} (${escapeHtml(ex.scope || "USER")})</div>
        </div>
      `
        )
        .join("")
    : `<div class="text-xs text-muted">No few-shot examples configured yet.</div>`;

  updateContextDetailPane(
    ext.name || ext.extractor_id,
    `${ext.extractor_id} • ${ext.model_name || "gemini-4-hydrogen"}`,
    `<div class="text-xs font-semibold mb-4">Few-Shot Extraction Examples (${examples.length})</div>${examplesHtml}`,
    ext.status || "ACTIVE",
    ext
  );
}

async function handleWsSaveExtractor() {
  const extractorId = document.getElementById("wsActiveExtractorIdInput")?.value || "extractor:crystalline-default";
  const name = (document.getElementById("wsExtractorNameInput")?.value || "").trim();
  const modelName = (document.getElementById("wsExtractorModelInput")?.value || "gemini-4-hydrogen").trim();
  const cats = (document.getElementById("wsExtractorCategoriesInput")?.value || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const systemPrompt = (document.getElementById("wsExtractorSystemPromptTextarea")?.value || "").trim();

  try {
    const res = await fetch("/api/studio/extractors", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        extractor_id: extractorId,
        name: name || extractorId,
        model_name: modelName,
        target_categories: cats,
        system_prompt: systemPrompt,
        status: "ACTIVE",
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(`Saved extractor '${name || extractorId}'.`);
    await loadWsExtractors();
  } catch (err) {
    showStatusBanner(`Save extractor failed: ${err.message}`, true);
  }
}

async function handleWsExtractorChatSubmit(evt) {
  if (evt) evt.preventDefault();
  const inputEl = document.getElementById("wsExtractorChatInput");
  const sendBtn = document.getElementById("wsExtractorChatSendBtn");
  const historyEl = document.getElementById("wsExtractorChatHistory");
  const message = (inputEl?.value || "").trim();
  if (!message) return;

  const extractorId = wsState.activeExtractorId || "extractor:crystalline-default";
  if (sendBtn) {
    sendBtn.disabled = true;
    sendBtn.textContent = "Co-Designing...";
  }
  try {
    const res = await fetch(`/api/studio/extractors/${encodeURIComponent(extractorId)}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, apply_updates: true }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    if (inputEl) inputEl.value = "";

    const reply = data.co_designer_reply || {};
    if (historyEl) {
      const turnEl = document.createElement("div");
      turnEl.className = "chat-turn";
      turnEl.innerHTML = `
        <div class="turn-badge">CO-DESIGNER TURN (${escapeHtml(extractorId)})</div>
        <p class="turn-text"><strong>You:</strong> ${escapeHtml(message)}</p>
        <p class="turn-text mt-4"><strong>Gemini 4 Neon Co-Designer:</strong> ${escapeHtml(reply.assistant_reply || "")}</p>
      `;
      historyEl.prepend(turnEl);
    }

    if (data.extractor) {
      selectWsExtractor(data.extractor);
    }
    await loadWsExtractors();
    showStatusBanner(`✓ Gemini 4 Neon Co-Designer updated system prompt & few-shot examples for ${extractorId}.`);
  } catch (err) {
    showStatusBanner(`Extractor Co-Designer chat failed: ${err.message}`, true);
  } finally {
    if (sendBtn) {
      sendBtn.disabled = false;
      sendBtn.textContent = "Co-Design ▶";
    }
  }
}

// ----------------------------------------------------------------------------
// WORKSPACE 9: Dual-Plane Memory Backend & Bidirectional Agent Write-Back Audit
// ----------------------------------------------------------------------------
async function loadWsBackendStatus() {
  const ids = getScopeIds();
  try {
    const qs = new URLSearchParams(ids).toString();
    const res = await fetch(`/api/studio/backend/status?${qs}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    wsState.backendStatus = data;

    const online = data.online_store || {};
    const offline = data.offline_store || {};
    const setText = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };
    setText("wsOnlineStatusBadge", online.status || "HEALTHY");
    setText("wsOnlineEngine", "Indexed Rule Store");
    setText("wsOnlineNodeCounts", `${online.synced_memories || 0} synced • ${online.pending_staged_commits || 0} staged`);
    setText("wsOfflineStatusBadge", offline.status || "HEALTHY");
    setText("wsOfflineEngine", "Session Log Store");
    setText("wsOfflineDataset", "session_logs");
    setText("wsOfflineConnectors", `${offline.active_ingest_connectors || 0} active streams`);

    const mutations = Array.isArray(data.agent_mutation_log) ? data.agent_mutation_log : [];
    setText("wsMutationsCountBadge", `${mutations.length} Mutations`);
    const tbody = document.getElementById("wsBackendMutationsTableBody");
    if (tbody) {
      tbody.innerHTML = mutations.length
        ? mutations
            .map((m) => {
              const isRolledBack = m.status === "ROLLED_BACK";
              return `
                <tr>
                  <td class="font-mono text-xs">${escapeHtml(m.mutation_id)}</td>
                  <td class="font-mono text-xs">${escapeHtml(m.source_agent || "agent:runtime")}</td>
                  <td class="font-mono text-xs">
                    <span class="badge badge-slate">${escapeHtml(m.scope_type)}</span>
                    <strong>${escapeHtml(m.memory_key)}</strong>
                  </td>
                  <td class="font-mono text-xs">
                    <span class="text-muted">${escapeHtml(m.before_value || "(empty)")}</span> →
                    <strong>${escapeHtml(m.after_value || "")}</strong>
                  </td>
                  <td>
                    <span class="badge ${isRolledBack ? "badge-slate" : "badge-teal"}">${escapeHtml(m.status || "COMMITTED")}</span>
                  </td>
                  <td>
                    ${
                      !isRolledBack
                        ? `<button type="button" class="btn btn-danger-outline btn-xs ws-rollback-mutation-btn" data-mutation-id="${escapeHtml(m.mutation_id)}">↩ Rollback</button>`
                        : `<span class="text-xs text-muted">Reverted</span>`
                    }
                  </td>
                </tr>
              `;
            })
            .join("")
        : `<tr><td colspan="6" class="text-center text-muted">No agent mutations logged yet.</td></tr>`;

      tbody.querySelectorAll(".ws-rollback-mutation-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const mid = btn.getAttribute("data-mutation-id");
          if (mid) await handleWsMutationRollback(mid);
        });
      });
    }
  } catch (err) {
    showStatusBanner(`Failed to load backend status: ${err.message}`, true);
  }
}

async function handleWsBackendSync() {
  const ids = getScopeIds();
  try {
    const res = await fetch("/api/studio/backend/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ids),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    showStatusBanner(`Sync complete: ${data.committed_from_staging || 0} staged rules committed.`);
    await loadWsBackendStatus();
    await loadWsMemoryTree();
  } catch (err) {
    showStatusBanner(`Dual-plane sync failed: ${err.message}`, true);
  }
}

async function handleWsAgentWriteBack() {
  const sourceAgent = (document.getElementById("wsAgentWriteSourceInput")?.value || "").trim() || "agent:assistant";
  const scopeType = document.getElementById("wsAgentWriteScopeSelect")?.value || "USER";
  const memoryKey = (document.getElementById("wsAgentWriteKeyInput")?.value || "").trim();
  const afterValue = (document.getElementById("wsAgentWriteValInput")?.value || "").trim();
  if (!memoryKey || !afterValue) {
    showStatusBanner("Memory Key and New Value are required to test agent write-back.", true);
    return;
  }
  const ids = getScopeIds();
  const scopeIdMap = { USER: ids.user_id, AGENT: ids.agent_id, TEAM: ids.team_id, ORG: ids.org_id };
  try {
    const res = await fetch("/api/studio/backend/test-agent-write", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_agent: sourceAgent,
        scope_type: scopeType,
        scope_id: scopeIdMap[scopeType] || ids.user_id,
        category: "technical",
        memory_key: memoryKey,
        after_value: afterValue,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    showStatusBanner(`✓ Live Agent Write-Back committed (${data.mutation?.mutation_id || ""}): ${memoryKey} = ${afterValue}.`);
    await loadWsBackendStatus();
    await loadWsMemoryTree();
  } catch (err) {
    showStatusBanner(`Agent write-back failed: ${err.message}`, true);
  }
}

async function handleWsMutationRollback(mutationId) {
  try {
    const res = await fetch(`/api/studio/backend/mutations/${encodeURIComponent(mutationId)}/rollback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    showStatusBanner(`✓ Rolled back agent mutation ${mutationId} (${data.rollback_action || "reverted"}).`);
    await loadWsBackendStatus();
    await loadWsMemoryTree();
  } catch (err) {
    showStatusBanner(`Mutation rollback failed: ${err.message}`, true);
  }
}

// ----------------------------------------------------------------------------
// WORKSPACE 10: Security & Access Control
// ----------------------------------------------------------------------------
async function loadWsSecurityPolicies() {
  try {
    const res = await fetch("/api/studio/security/policies");
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    wsState.securityPolicies = Array.isArray(data.policies) ? data.policies : [];
    const countBadge = document.getElementById("wsRbacPoliciesCountBadge");
    if (countBadge) countBadge.textContent = `${wsState.securityPolicies.length} Policies`;
    const tbody = document.getElementById("wsRbacPoliciesTableBody");
    if (!tbody) return;
    tbody.innerHTML = wsState.securityPolicies
      .map(
        (p) => `
        <tr class="ws-clickable-row" data-policy-id="${escapeHtml(p.policy_id)}">
          <td class="font-mono text-xs font-semibold">${escapeHtml(p.policy_id)}</td>
          <td>
            <div class="font-mono text-xs font-semibold">${escapeHtml(p.principal)}</div>
            <div class="text-xs text-secondary">${escapeHtml(p.role)}</div>
          </td>
          <td>
            <span class="commit-status-pill ${p.effect === "DENY" ? "commit-pill-conflict" : "commit-pill-new"}">${escapeHtml(p.effect)}</span>
          </td>
          <td class="font-mono text-xs">
            <strong>${escapeHtml(p.target_scope_id)}</strong>
            <span class="text-muted">(${p.applies_to_descendants !== false ? "includes descendants" : "exact node"})</span>
          </td>
          <td class="font-mono text-xs">${escapeHtml((p.actions || []).join(", "))}</td>
          <td><span class="badge badge-slate">${escapeHtml(p.pii_masking_level || "STRICT_ZERO_SECRETS")}</span></td>
        </tr>
      `
      )
      .join("");
  } catch (err) {
    showStatusBanner(`Failed to load RBAC policies: ${err.message}`, true);
  }
}

async function handleWsSaveRbacPolicy() {
  const principal = (document.getElementById("wsRbacPolicyPrincipalInput")?.value || "").trim();
  const role = (document.getElementById("wsRbacPolicyRoleInput")?.value || "").trim();
  const effect = document.getElementById("wsRbacPolicyEffectSelect")?.value || "ALLOW";
  const targetScopeId = (document.getElementById("wsRbacPolicyScopeInput")?.value || getScopeIds().team_id || "").trim();
  const actions = (document.getElementById("wsRbacPolicyActionsInput")?.value || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!principal || !role) {
    showStatusBanner("Principal and Role Name are required to save an RBAC policy.", true);
    return;
  }
  try {
    const res = await fetch("/api/studio/security/policies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        principal,
        role,
        effect,
        target_scope_id: targetScopeId,
        actions,
        applies_to_descendants: true,
        pii_masking_level: "STRICT_ZERO_SECRETS",
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    showStatusBanner(`✓ Saved access rule for '${principal}' on '${targetScopeId}'.`);
    await loadWsSecurityPolicies();
  } catch (err) {
    showStatusBanner(`Save RBAC policy failed: ${err.message}`, true);
  }
}

async function handleWsRbacSimulate() {
  const principal = (document.getElementById("wsRbacSimPrincipalInput")?.value || "").trim();
  const action = document.getElementById("wsRbacSimActionSelect")?.value || "WRITE_ORG";
  const targetScopeId = (document.getElementById("wsRbacSimTargetScopeInput")?.value || "org:default").trim();
  const resultBox = document.getElementById("wsRbacSimResultBox");

  try {
    const res = await fetch("/api/studio/security/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        principal,
        action,
        target_scope_id: targetScopeId,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    const allowed = Boolean(data.allowed);
    const pathStr = Array.isArray(data.scope_path) ? data.scope_path.join(" → ") : targetScopeId;

    if (resultBox) {
      resultBox.classList.remove("hidden");
      resultBox.innerHTML = `
        <div class="card-header-compact">
          <span class="commit-status-pill ${allowed ? "commit-pill-new" : "commit-pill-conflict"}">
            ${allowed ? "✓ ALLOWED" : "✕ DENIED"} (${escapeHtml(data.decision || "")})
          </span>
          <span class="font-mono text-xs">Scope Path: ${escapeHtml(pathStr)}</span>
        </div>
        <div class="text-xs mt-2">${escapeHtml(data.reason || "")}</div>
      `;
    }
    updateContextDetailPane(
      `RBAC Simulation: ${data.decision || (allowed ? "ALLOWED" : "DENIED")}`,
      `${principal} → ${action} on ${targetScopeId}`,
      `<div class="text-xs">${escapeHtml(data.reason || "")}</div>
       <div class="text-xs font-mono mt-4">Ancestor Path: ${escapeHtml(pathStr)}</div>`,
      data.decision || "SIMULATED",
      data
    );
  } catch (err) {
    showStatusBanner(`RBAC permission simulation failed: ${err.message}`, true);
  }
}

// ----------------------------------------------------------------------------
// WORKSPACE: Jobs Queue, Live SSE Stream & Cross-Tab Execution Control
// ----------------------------------------------------------------------------
const JOB_CATEGORY_LABELS = {
  compaction: "Compaction",
  optimize: "Auto-Tune",
  ingest: "Ingest",
  transform: "Transform",
};

const JOB_STATUS_LABELS = {
  QUEUED: "Queued",
  RUNNING: "Running",
  SUCCEEDED: "Completed",
  FAILED: "Failed",
  CANCELED: "Canceled",
};

function formatJobDurationMs(ms) {
  if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return "—";
  const totalSec = Math.max(0, Math.round(Number(ms) / 100) / 10);
  if (totalSec < 60) return `${totalSec.toFixed(totalSec < 10 ? 1 : 0)}s`;
  const mins = Math.floor(totalSec / 60);
  const rem = Math.round(totalSec % 60);
  return `${mins}m ${String(rem).padStart(2, "0")}s`;
}

function formatJobTimestamp(iso) {
  if (!iso) return "—";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "—";
  return dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function syncOptimizeAutoTuneButtonsFromJobs() {
  const runningEvolve = (wsState.jobs || []).find(
    (j) => j.job_type === "alpha_evolve" && (j.status === "RUNNING" || j.status === "QUEUED")
  );
  const btn = document.getElementById("wsEvalRunAlphaEvolveBtn");
  const optBtn = document.getElementById("wsOptimizeRunAlphaEvolveBtn");
  if (runningEvolve) {
    const label = runningEvolve.progress_pct ? `Auto-tuning (${runningEvolve.progress_pct}%)` : "Auto-tuning...";
    if (btn) {
      btn.disabled = true;
      btn.textContent = label;
    }
    if (optBtn) {
      optBtn.disabled = true;
      optBtn.textContent = label;
    }
  } else {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Auto-tune";
    }
    if (optBtn) {
      optBtn.disabled = false;
      optBtn.textContent = "Run Auto-Tune";
    }
  }
}

function syncJobsBadgesAndTicker() {
  const jobs = Array.isArray(wsState.jobs) ? wsState.jobs : [];
  const activeCount = jobs.filter((j) => j.status === "RUNNING" || j.status === "QUEUED").length;
  const succeededCount = jobs.filter((j) => j.status === "SUCCEEDED").length;
  const failedCount = jobs.filter((j) => j.status === "FAILED").length;
  wsState.jobsActiveCount = activeCount;

  const badgeEl = document.getElementById("wsBadgeJobsRunning");
  if (badgeEl) {
    badgeEl.classList.toggle("hidden", activeCount === 0);
    if (activeCount > 0) {
      badgeEl.textContent = `${activeCount} running`;
    }
  }

  const summaryEl = document.getElementById("wsJobsQuietSummary");
  if (summaryEl) {
    let text = `${activeCount} running · ${succeededCount} completed`;
    if (failedCount > 0) text += ` · ${failedCount} failed`;
    summaryEl.textContent = text;
  }

  syncOptimizeAutoTuneButtonsFromJobs();

  if (activeCount > 0 && !wsState.jobsTickTimer) {
    wsState.jobsTickTimer = window.setInterval(() => {
      document.querySelectorAll("[data-live-job-duration]").forEach((el) => {
        const startedIso = el.getAttribute("data-live-job-duration");
        if (!startedIso) return;
        const t0 = Date.parse(startedIso);
        if (!Number.isNaN(t0)) {
          el.textContent = formatJobDurationMs(Date.now() - t0);
        }
      });
    }, 1000);
  } else if (activeCount === 0 && wsState.jobsTickTimer) {
    window.clearInterval(wsState.jobsTickTimer);
    wsState.jobsTickTimer = null;
  }
}

async function loadWsJobs() {
  try {
    const res = await fetch("/api/studio/jobs?limit=50");
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    wsState.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    wsState.jobsCounts = data.counts || {};
    syncJobsBadgesAndTicker();
    renderWsJobsTable();
    if (wsState.selectedJobId) {
      const sel = wsState.jobs.find((j) => j.job_id === wsState.selectedJobId);
      if (sel && wsState.activeWorkspace === "workspace-jobs") {
        inspectWsJob(sel, true);
      }
    }
  } catch (err) {
    console.error("Failed to load Studio jobs:", err);
  }
}

function renderWsJobsTable() {
  const tbody = document.getElementById("wsJobsTableBody");
  if (!tbody) return;

  const catFilter = (wsState.jobsFilterCategory || "ALL").toLowerCase();
  const statusFilter = (wsState.jobsFilterStatus || "ALL").toUpperCase();
  const query = (wsState.jobsSearchQuery || "").trim().toLowerCase();

  const filtered = (wsState.jobs || []).filter((j) => {
    if (catFilter !== "all" && String(j.category || "").toLowerCase() !== catFilter) return false;
    if (statusFilter !== "ALL" && String(j.status || "").toUpperCase() !== statusFilter) return false;
    if (query) {
      const hay = `${j.title || ""} ${j.job_id || ""} ${j.result_summary || ""} ${j.current_step || ""} ${j.triggered_from || ""} ${j.scope_summary || ""}`.toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  });

  if (!filtered.length) {
    const emptyMsg = (wsState.jobs || []).length
      ? "No jobs match filter"
      : "No jobs yet";
    tbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted">${escapeHtml(emptyMsg)}</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered
    .map((j) => {
      const st = String(j.status || "QUEUED").toUpperCase();
      const stLower = st.toLowerCase();
      const stLabel = JOB_STATUS_LABELS[st] || st;
      const catLabel = JOB_CATEGORY_LABELS[String(j.category || "").toLowerCase()] || j.category || "Job";
      const isSelected = wsState.selectedJobId === j.job_id;
      const isRunning = st === "RUNNING";
      const isActive = st === "RUNNING" || st === "QUEUED";
      const startedIso = j.started_at || j.created_at || "";
      const durationHtml = isRunning && startedIso
        ? `<span data-live-job-duration="${escapeHtml(startedIso)}">${escapeHtml(formatJobDurationMs(Date.now() - Date.parse(startedIso)))}</span>`
        : escapeHtml(formatJobDurationMs(j.duration_ms));

      const progressHtml = isRunning
        ? `<div class="job-step-meta">
             <progress class="job-inline-progress" max="100" value="${Number(j.progress_pct || 10)}"></progress>
             <span class="text-xs text-secondary">${escapeHtml(j.current_step || "Running...")}</span>
           </div>`
        : `<div class="text-xs text-secondary mt-2">${escapeHtml(j.current_step || "")}</div>`;

      let actionBtnHtml = "";
      if (isActive) {
        actionBtnHtml = `<button type="button" class="btn btn-danger-outline btn-xs" data-job-cancel="${escapeHtml(j.job_id)}">Cancel</button>`;
      } else if (st === "FAILED" || st === "CANCELED") {
        actionBtnHtml = `<button type="button" class="btn btn-secondary btn-xs" data-job-retry="${escapeHtml(j.job_id)}">Retry</button>`;
      } else if (j.target_workspace) {
        const targetLabel = j.target_workspace === "workspace-eval"
          ? "View preset"
          : (j.target_workspace === "workspace-sessions" ? "View sessions" : "View memories");
        actionBtnHtml = `<button type="button" class="btn btn-secondary btn-xs" data-job-open-ws="${escapeHtml(j.target_workspace)}">${escapeHtml(targetLabel)}</button>`;
      }

      return `
        <tr class="ws-clickable-row ${isSelected ? "job-row-selected" : ""}" data-job-id="${escapeHtml(j.job_id)}">
          <td>
            <div class="font-semibold text-sm">${escapeHtml(j.title || j.job_type)}</div>
            <div class="text-xs text-muted">${escapeHtml(j.triggered_from || "Jobs")} · ${escapeHtml(j.scope_summary || "All scopes")}</div>
          </td>
          <td><span class="badge badge-slate">${escapeHtml(catLabel)}</span></td>
          <td>
            <span class="job-status-pill ${escapeHtml(stLower)}">
              ${isRunning ? '<span class="job-pulse-dot" aria-hidden="true"></span>' : ""}
              ${escapeHtml(stLabel)}
            </span>
            ${progressHtml}
          </td>
          <td class="text-xs">${escapeHtml(j.result_summary || j.error || "—")}</td>
          <td class="font-mono text-xs">${escapeHtml(formatJobTimestamp(startedIso))}</td>
          <td class="font-mono text-xs">${durationHtml}</td>
          <td>${actionBtnHtml}</td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll("tr[data-job-id]").forEach((row) => {
    row.addEventListener("click", () => {
      const jid = row.getAttribute("data-job-id");
      const job = (wsState.jobs || []).find((x) => x.job_id === jid);
      if (job) inspectWsJob(job, false);
    });
  });

  tbody.querySelectorAll("[data-job-cancel]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const jid = btn.getAttribute("data-job-cancel");
      if (jid) await cancelStudioJob(jid);
    });
  });

  tbody.querySelectorAll("[data-job-retry]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const jid = btn.getAttribute("data-job-retry");
      if (jid) await retryStudioJob(jid);
    });
  });

  tbody.querySelectorAll("[data-job-open-ws]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const targetWs = btn.getAttribute("data-job-open-ws");
      if (targetWs) await switchWorkspace(targetWs);
    });
  });
}

function inspectWsJob(job, silentDrawer = false) {
  if (!job) return;
  wsState.selectedJobId = job.job_id;
  if (!silentDrawer) {
    renderWsJobsTable();
  }
  const st = String(job.status || "QUEUED").toUpperCase();
  const stLabel = JOB_STATUS_LABELS[st] || st;
  const steps = Array.isArray(job.steps) ? job.steps : [];
  const timelineHtml = steps.length
    ? `<ol class="job-step-timeline">${steps
        .map(
          (s) => `
          <li class="job-step-timeline-item">
            <div class="job-step-timeline-head">
              <strong>${escapeHtml(s.label || "")}</strong>
              <span class="font-mono text-muted">${escapeHtml(formatJobTimestamp(s.ts))}</span>
            </div>
            <div class="job-step-timeline-detail">${escapeHtml(s.detail || "")}</div>
          </li>
        `
        )
        .join("")}</ol>`
    : '<div class="text-xs text-muted mt-4">No steps recorded yet.</div>';

  const metricsEntries = Object.entries(job.metrics || {});
  const metricsHtml = metricsEntries.length
    ? `<div class="mt-8">${metricsEntries
        .map(([k, v]) => `<div class="engine-kv-row"><span class="engine-kv-label">${escapeHtml(formatInspectorLabel(k))}</span><strong class="mono">${escapeHtml(String(v))}</strong></div>`)
        .join("")}</div>`
    : "";

  const isActive = st === "RUNNING" || st === "QUEUED";
  const buttonsHtml = `
    <div class="quick-editor-actions mt-8">
      ${isActive ? `<button type="button" class="btn btn-danger-outline btn-xs" id="inspectorCancelJobBtn">Cancel</button>` : ""}
      ${st === "FAILED" || st === "CANCELED" ? `<button type="button" class="btn btn-secondary btn-xs" id="inspectorRetryJobBtn">Retry</button>` : ""}
      ${st === "SUCCEEDED" && job.target_workspace ? `<button type="button" class="btn btn-primary btn-xs" id="inspectorOpenJobWsBtn">Open view</button>` : ""}
    </div>
  `;

  const bodyHtml = `
    <div class="engine-kv-row"><span class="engine-kv-label">Job ID</span><strong class="mono">${escapeHtml(job.job_id)}</strong></div>
    <div class="engine-kv-row"><span class="engine-kv-label">Origin</span><strong>${escapeHtml(job.triggered_from || "Jobs")}</strong></div>
    <div class="engine-kv-row"><span class="engine-kv-label">Scope</span><strong class="mono">${escapeHtml(job.scope_summary || "All scopes")}</strong></div>
    <div class="engine-kv-row"><span class="engine-kv-label">Duration</span><strong class="mono">${escapeHtml(formatJobDurationMs(job.duration_ms))}</strong></div>
    ${job.result_summary ? `<div class="engine-kv-row"><span class="engine-kv-label">Outcome</span><strong>${escapeHtml(job.result_summary)}</strong></div>` : ""}
    ${job.error ? `<div class="text-xs mt-4" style="color: var(--color-warn);">${escapeHtml(job.error)}</div>` : ""}
    ${metricsHtml}
    ${buttonsHtml}
    <div class="mt-12">
      <div class="rail-section-title">Steps</div>
      ${timelineHtml}
    </div>
  `;

  updateContextDetailPane(
    job.title || job.job_type,
    `${stLabel} · ${job.current_step || ""}`,
    bodyHtml,
    stLabel,
    job.output_preview && Object.keys(job.output_preview).length ? job.output_preview : job
  );

  document.getElementById("inspectorCancelJobBtn")?.addEventListener("click", () => cancelStudioJob(job.job_id));
  document.getElementById("inspectorRetryJobBtn")?.addEventListener("click", () => retryStudioJob(job.job_id));
  document.getElementById("inspectorOpenJobWsBtn")?.addEventListener("click", () => switchWorkspace(job.target_workspace));
}

async function startStudioJob(jobType, triggeredFrom = "Jobs", extraParams = null) {
  const ids = getScopeIds();
  const params = {
    user_id: ids.user_id,
    team_id: ids.team_id,
    agent_id: ids.agent_id,
    ...(extraParams || {}),
  };
  const res = await fetch("/api/studio/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      job_type: jobType,
      triggered_from: triggeredFrom,
      params,
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  const data = await res.json();
  const job = data.job;
  if (job) {
    upsertLocalStudioJob(job);
    wsState.selectedJobId = job.job_id;
    syncJobsBadgesAndTicker();
    renderWsJobsTable();
    if (triggeredFrom === "Jobs") {
      showStatusBanner(`Started ${job.title || jobType}.`);
    }
  }
  return job;
}

async function cancelStudioJob(jobId) {
  try {
    const res = await fetch(`/api/studio/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    if (data.job) {
      upsertLocalStudioJob(data.job);
      syncJobsBadgesAndTicker();
      renderWsJobsTable();
      if (wsState.selectedJobId === jobId) inspectWsJob(data.job, true);
    }
    showStatusBanner("Cancellation requested.");
  } catch (err) {
    showStatusBanner(`Cancel failed: ${err.message}`, true);
  }
}

async function retryStudioJob(jobId) {
  try {
    const res = await fetch(`/api/studio/jobs/${encodeURIComponent(jobId)}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    if (data.job) {
      upsertLocalStudioJob(data.job);
      wsState.selectedJobId = data.job.job_id;
      syncJobsBadgesAndTicker();
      renderWsJobsTable();
      inspectWsJob(data.job, true);
      showStatusBanner(`Retried ${data.job.title || jobId}.`);
    }
  } catch (err) {
    showStatusBanner(`Retry failed: ${err.message}`, true);
  }
}

async function clearFinishedStudioJobs() {
  try {
    const res = await fetch("/api/studio/jobs/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    wsState.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    if (wsState.selectedJobId && !wsState.jobs.some((j) => j.job_id === wsState.selectedJobId)) {
      wsState.selectedJobId = null;
    }
    syncJobsBadgesAndTicker();
    renderWsJobsTable();
    showStatusBanner(`Cleared ${Number(data.removed || 0)} finished job(s).`);
  } catch (err) {
    showStatusBanner(`Clear failed: ${err.message}`, true);
  }
}

function upsertLocalStudioJob(job) {
  if (!job || !job.job_id) return null;
  const list = Array.isArray(wsState.jobs) ? [...wsState.jobs] : [];
  const idx = list.findIndex((x) => x.job_id === job.job_id);
  const prevStatus = idx >= 0 ? String(list[idx].status || "").toUpperCase() : "";
  if (idx >= 0) {
    list[idx] = job;
  } else {
    list.unshift(job);
  }
  list.sort((a, b) => String(b.started_at || b.created_at || "").localeCompare(String(a.started_at || a.created_at || "")));
  wsState.jobs = list.slice(0, 100);
  return prevStatus;
}

function connectStudioJobsStream() {
  if (typeof EventSource === "undefined") return;
  if (wsState.jobsEventSource) {
    try {
      wsState.jobsEventSource.close();
    } catch (_) {}
  }
  let onceMode = false;
  try {
    onceMode = new URLSearchParams(window.location.search).get("once") === "1";
  } catch (_) {}
  const es = new EventSource(onceMode ? "/api/studio/jobs/stream?once=true" : "/api/studio/jobs/stream");
  wsState.jobsEventSource = es;

  if (onceMode) {
    es.onerror = () => {
      try {
        es.close();
      } catch (_) {}
    };
  }

  es.onmessage = async (msg) => {
    if (!msg || !msg.data) return;
    let payload = null;
    try {
      payload = JSON.parse(msg.data);
    } catch (_) {
      return;
    }
    if (!payload) return;
    if (payload.type === "snapshot") {
      wsState.jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
      wsState.jobsCounts = payload.counts || {};
      syncJobsBadgesAndTicker();
      if (wsState.activeWorkspace === "workspace-jobs") {
        renderWsJobsTable();
      }
      if (onceMode) {
        try {
          es.close();
        } catch (_) {}
      }
      return;
    }
    if (payload.type === "job_updated" && payload.job) {
      const job = payload.job;
      const prevStatus = upsertLocalStudioJob(job);
      if (payload.counts) wsState.jobsCounts = payload.counts;
      syncJobsBadgesAndTicker();
      if (wsState.activeWorkspace === "workspace-jobs") {
        renderWsJobsTable();
      }
      if (wsState.selectedJobId === job.job_id && wsState.activeWorkspace === "workspace-jobs") {
        inspectWsJob(job, true);
      }
      const nowStatus = String(job.status || "").toUpperCase();
      if (prevStatus !== "SUCCEEDED" && nowStatus === "SUCCEEDED") {
        if (job.job_type === "alpha_evolve" || job.job_type === "eval_benchmark") {
          await refreshEvolveArchive();
          if (job.job_type === "alpha_evolve") {
            showStatusBanner(`✓ ${job.title || "Auto-tune"} completed (${job.result_summary || ""}).`);
          }
        } else if (job.job_type === "dream_compaction" || job.job_type === "memory_transform") {
          await loadWsMemoryTree();
          await loadSyncStatus();
        } else if (job.job_type === "session_ingest") {
          await loadWsSessions();
        }
      }
    }
  };
}




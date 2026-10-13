// Electric Sheep v2 • Cognitive Memory & REM Dreaming Client App

document.addEventListener("DOMContentLoaded", () => {
  let currentLogs = [];
  let currentPrefs = [];
  let currentFilter = "ALL";
  let currentCategory = "ALL";
  let activeSessionType = "antigravity"; // 'antigravity' or 'chat'

  const userIdInput = document.getElementById("userIdInput");
  const sessionIdInput = document.getElementById("sessionIdInput");

  function getParams() {
    return {
      user_id: (userIdInput.value || "demo-user-42").trim(),
      session_id: (sessionIdInput.value || "session-ui-001").trim(),
    };
  }

  // --- Left Navigation Collapsible Sidebar ---
  const leftNav = document.querySelector(".left-nav");
  const leftNavToggleBtn = document.getElementById("leftNavToggleBtn");
  const appLayout = document.querySelector(".app-body-layout");
  const STORAGE_KEY = "electric_sheep_left_nav_collapsed";

  function setLeftNavCollapsed(isCollapsed) {
    if (!leftNav || !leftNavToggleBtn) return;
    if (isCollapsed) {
      leftNav.classList.add("collapsed");
      if (appLayout) appLayout.classList.add("nav-collapsed");
      leftNavToggleBtn.setAttribute("aria-expanded", "false");
      leftNavToggleBtn.setAttribute("aria-label", "Expand stage navigation");
      leftNavToggleBtn.setAttribute("title", "Expand sidebar");
      try { localStorage.setItem(STORAGE_KEY, "true"); } catch (e) {}
    } else {
      leftNav.classList.remove("collapsed");
      if (appLayout) appLayout.classList.remove("nav-collapsed");
      leftNavToggleBtn.setAttribute("aria-expanded", "true");
      leftNavToggleBtn.setAttribute("aria-label", "Collapse stage navigation");
      leftNavToggleBtn.setAttribute("title", "Collapse sidebar to numbers only");
      try { localStorage.setItem(STORAGE_KEY, "false"); } catch (e) {}
    }
  }

  try {
    if (localStorage.getItem(STORAGE_KEY) === "true") {
      setLeftNavCollapsed(true);
    }
  } catch (e) {}

  if (leftNavToggleBtn) {
    leftNavToggleBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isCurrentlyCollapsed = leftNav && leftNav.classList.contains("collapsed");
      setLeftNavCollapsed(!isCurrentlyCollapsed);
    });
  }

  // --- Left Navigation Sticky & Scroll Spy ---
  const navItems = document.querySelectorAll(".nav-item[data-stage]");
  const stagePanels = document.querySelectorAll(".stage-panel");

  navItems.forEach((item) => {
    item.addEventListener("click", (e) => {
      e.preventDefault();
      const stage = item.getAttribute("data-stage");
      const target = document.getElementById(`stage-panel-${stage}`);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        navItems.forEach((n) => n.classList.remove("active"));
        item.classList.add("active");
        if (stage === "2") fetchLogs();
        if (stage === "4") { fetchSpannerPrefs(); fetchSpannerSummaries(); }
      }
    });
  });

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const stageNum = entry.target.id.replace("stage-panel-", "");
          navItems.forEach((item) => {
            if (item.getAttribute("data-stage") === stageNum) {
              item.classList.add("active");
            } else {
              item.classList.remove("active");
            }
          });
        }
      });
    },
    { rootMargin: "-25% 0px -55% 0px" }
  );
  stagePanels.forEach((p) => observer.observe(p));

  // --- Stage 1 Tabs (Antigravity vs Chat vs BYOT) ---
  const tabAntigravityBtn = document.getElementById("tabAntigravityBtn");
  const tabChatBtn = document.getElementById("tabChatBtn");
  const tabByotBtn = document.getElementById("tabByotBtn");
  const antigravitySection = document.getElementById("antigravitySection");
  const chatSection = document.getElementById("chatSection");
  const byotSection = document.getElementById("byotSection");

  tabAntigravityBtn.addEventListener("click", () => {
    tabAntigravityBtn.classList.add("active");
    tabChatBtn.classList.remove("active");
    if (tabByotBtn) tabByotBtn.classList.remove("active");
    antigravitySection.style.display = "block";
    chatSection.style.display = "none";
    if (byotSection) byotSection.style.display = "none";
    activeSessionType = "antigravity";
  });

  tabChatBtn.addEventListener("click", () => {
    tabChatBtn.classList.add("active");
    tabAntigravityBtn.classList.remove("active");
    if (tabByotBtn) tabByotBtn.classList.remove("active");
    chatSection.style.display = "block";
    antigravitySection.style.display = "none";
    if (byotSection) byotSection.style.display = "none";
    activeSessionType = "chat";
  });

  if (tabByotBtn && byotSection) {
    tabByotBtn.addEventListener("click", () => {
      tabByotBtn.classList.add("active");
      tabAntigravityBtn.classList.remove("active");
      tabChatBtn.classList.remove("active");
      byotSection.style.display = "block";
      antigravitySection.style.display = "none";
      chatSection.style.display = "none";
      activeSessionType = "byot";
    });
  }

  // --- BYOT Telemetry Dropzone Controller ---
  const byotContent = document.getElementById("byotContent");
  const byotSessionId = document.getElementById("byotSessionId");
  const byotFormat = document.getElementById("byotFormat");
  const byotFileInput = document.getElementById("byotFileInput");
  const byotIngestBtn = document.getElementById("byotIngestBtn");
  const byotClearBtn = document.getElementById("byotClearBtn");
  const byotStatusText = document.getElementById("byotStatusText");
  const byotDropzone = document.getElementById("byotDropzone");

  const PRESETS = {
    python: `[
  {"event_type": "USER_PROMPT", "message": "Can you run pytest to test the Spanner preference client and ensure uv is used?"},
  {"event_type": "AGENT_RESPONSE", "message": "I will execute \`uv run pytest tests/test_spanner_client.py\` using uv. We never use global pip install."},
  {"event_type": "TOOL_CALL", "message": "run_command(CommandLine='uv run pytest tests/test_spanner_client.py')"},
  {"event_type": "TOOL_RESULT", "message": "6 passed in 2.14s. All tests clean."},
  {"event_type": "AGENT_RESPONSE", "message": "Tests succeeded using uv virtual environment."}
]`,
    docker: `[
  {"event_type": "USER_PROMPT", "message": "Please package this into a container image with docker build."},
  {"event_type": "AGENT_RESPONSE", "message": "Checking system environment for Docker daemon... Docker is not installed on this Apple Silicon machine."},
  {"event_type": "USER_PROMPT", "message": "Right, remember the rule: Docker is not installed, use Cloud Run source deployment or apple container."},
  {"event_type": "AGENT_RESPONSE", "message": "Understood. Docker is prohibited; we will deploy via Cloud Run source build ./scripts/deploy_cloud_run_source.sh and prune dangling images."}
]`,
    terraform: `[
  {"event_type": "USER_PROMPT", "message": "Update the Spanner database DDL in terraform/spanner.tf and apply."},
  {"event_type": "AGENT_RESPONSE", "message": "Editing terraform/spanner.tf to declare the UserPreferences and UserSessionSummaries tables with 768-dim vector embeddings."},
  {"event_type": "TOOL_CALL", "message": "run_command(CommandLine='terraform fmt && terraform validate')"},
  {"event_type": "TOOL_RESULT", "message": "Success! The configuration is valid."},
  {"event_type": "AGENT_RESPONSE", "message": "Terraform formatting and validation passed with zero errors."}
]`
  };

  const loadPresetPythonBtn = document.getElementById("loadPresetPythonBtn");
  const loadPresetDockerBtn = document.getElementById("loadPresetDockerBtn");
  const loadPresetTerraformBtn = document.getElementById("loadPresetTerraformBtn");

  if (loadPresetPythonBtn) {
    loadPresetPythonBtn.addEventListener("click", () => {
      byotContent.value = PRESETS.python;
      byotSessionId.value = "byot-python-uv-001";
      byotFormat.value = "json";
    });
  }
  if (loadPresetDockerBtn) {
    loadPresetDockerBtn.addEventListener("click", () => {
      byotContent.value = PRESETS.docker;
      byotSessionId.value = "byot-docker-guardrail-002";
      byotFormat.value = "json";
    });
  }
  if (loadPresetTerraformBtn) {
    loadPresetTerraformBtn.addEventListener("click", () => {
      byotContent.value = PRESETS.terraform;
      byotSessionId.value = "byot-terraform-policy-003";
      byotFormat.value = "json";
    });
  }

  if (byotClearBtn) {
    byotClearBtn.addEventListener("click", () => {
      byotContent.value = "";
      if (byotStatusText) byotStatusText.style.display = "none";
    });
  }

  if (byotFileInput) {
    byotFileInput.addEventListener("change", (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (evt) => {
        byotContent.value = evt.target.result;
        byotSessionId.value = "byot-" + file.name.replace(/[^a-zA-Z0-9_-]/g, "_").substring(0, 30);
      };
      reader.readAsText(file);
    });
  }

  if (byotDropzone) {
    ["dragenter", "dragover"].forEach(evtName => {
      byotDropzone.addEventListener(evtName, (e) => {
        e.preventDefault();
        byotDropzone.classList.add("drag-over");
      });
    });
    ["dragleave", "drop"].forEach(evtName => {
      byotDropzone.addEventListener(evtName, (e) => {
        e.preventDefault();
        byotDropzone.classList.remove("drag-over");
      });
    });
    byotDropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      const files = e.dataTransfer.files;
      if (files && files.length > 0) {
        const file = files[0];
        const reader = new FileReader();
        reader.onload = (evt) => {
          byotContent.value = evt.target.result;
          byotSessionId.value = "byot-" + file.name.replace(/[^a-zA-Z0-9_-]/g, "_").substring(0, 30);
        };
        reader.readAsText(file);
      }
    });
  }

  if (byotIngestBtn) {
    byotIngestBtn.addEventListener("click", async () => {
      const rawText = (byotContent.value || "").trim();
      if (!rawText) {
        alert("Please paste a transcript or select a preset before ingesting.");
        return;
      }
      const targetSession = (byotSessionId.value || "byot-session-001").trim();
      const format = byotFormat.value || "auto";
      const { user_id } = getParams();

      byotIngestBtn.disabled = true;
      if (byotStatusText) {
        byotStatusText.style.display = "inline-flex";
        byotStatusText.textContent = "Ingesting into BigQuery...";
        byotStatusText.className = "byot-status-badge pill-neutral";
      }

      try {
        const res = await fetch("/api/telemetry/ingest_transcript", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: user_id,
            session_id: targetSession,
            content: rawText,
            format: format
          })
        });
        const data = await res.json();
        if (data.status === "success") {
          if (byotStatusText) {
            byotStatusText.textContent = `✓ Ingested ${data.turns_ingested} turns to BigQuery!`;
            byotStatusText.className = "byot-status-badge pill-success";
          }
          sessionIdInput.value = targetSession;
          fetchLogs();
        } else {
          if (byotStatusText) {
            byotStatusText.textContent = `Error: ${data.detail || "Failed to ingest"}`;
            byotStatusText.className = "byot-status-badge pill-warn";
          }
        }
      } catch (err) {
        if (byotStatusText) {
          byotStatusText.textContent = `Ingest error: ${err}`;
          byotStatusText.className = "byot-status-badge pill-warn";
        }
      } finally {
        byotIngestBtn.disabled = false;
      }
    });
  }

  // --- Antigravity Session Crawler ---
  const sessionSelect = document.getElementById("antigravitySessionSelect");
  const loadTurnsBtn = document.getElementById("loadSessionTurnsBtn");
  const ingestBqBtn = document.getElementById("ingestSessionBqBtn");
  const dreamDirectBtn = document.getElementById("dreamSessionDirectBtn");
  const turnsStreamBox = document.getElementById("turnsStreamBox");
  const metaTurnCount = document.getElementById("metaTurnCount");
  const metaTools = document.getElementById("metaTools");
  const metaStatus = document.getElementById("metaStatus");

  async function fetchAntigravitySessions() {
    try {
      const res = await fetch("/api/antigravity/sessions");
      const data = await res.json();
      sessionSelect.innerHTML = "";
      if (!data.sessions || data.sessions.length === 0) {
        sessionSelect.innerHTML = "<option value=''>No local Antigravity sessions found</option>";
        metaStatus.textContent = "No Local Sessions";
        metaStatus.className = "intent-pill pill-neutral";
        turnsStreamBox.innerHTML = `
          <div class='empty-state-card'>
            <strong>No local Antigravity sessions found in ~/.gemini/antigravity.</strong><br>
            <span style="display:inline-block; margin-top: 6px;">When hosted on Google Cloud Run, local machine sessions are not present in the container. Click <strong>Interactive Dialogue</strong> above to chat directly with InterviewAgent and generate live session logs!</span>
          </div>
        `;
        return;
      }
      data.sessions.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.session_id;
        const timeShort = s.timestamp ? s.timestamp.substring(0, 16).replace("T", " ") : "";
        opt.textContent = `[${s.turns_count} turns] ${s.title.substring(0, 50)} (${timeShort})`;
        sessionSelect.appendChild(opt);
      });
      if (data.sessions.length > 0) {
        sessionIdInput.value = data.sessions[0].session_id;
        loadSessionTurns(data.sessions[0].session_id);
      }
    } catch (err) {
      console.error("Failed to load Antigravity sessions:", err);
      sessionSelect.innerHTML = "<option value=''>Error connecting to session crawler</option>";
    }
  }

  async function loadSessionTurns(sessionId) {
    if (!sessionId) return;
    turnsStreamBox.innerHTML = "<div class='empty-state-card'>Loading structured turns...</div>";
    try {
      const res = await fetch(`/api/antigravity/sessions/${sessionId}/turns`);
      const data = await res.json();
      renderTurns(data.turns || []);
    } catch (err) {
      turnsStreamBox.innerHTML = `<div class='empty-state-card'>Failed to load turns: ${err}</div>`;
    }
  }

  function renderTurns(turns) {
    turnsStreamBox.innerHTML = "";
    metaTurnCount.textContent = turns.length;
    const toolsFound = new Set();

    if (turns.length === 0) {
      turnsStreamBox.innerHTML = "<div class='empty-state-card'>No turns recorded in this session.</div>";
      metaTools.textContent = "None";
      return;
    }

    turns.forEach((t) => {
      if (t.tool_name) toolsFound.add(t.tool_name);
      const card = document.createElement("div");
      card.className = "turn-card";

      let bodyContent = "";
      if (t.user_prompt) {
        bodyContent = `<div class="turn-body"><strong>User:</strong> ${escapeHtml(t.user_prompt)}</div>`;
      } else if (t.agent_thought) {
        bodyContent = `<div class="turn-thought"><strong>Thought:</strong> ${escapeHtml(t.agent_thought.substring(0, 300))}...</div>`;
      }

      if (t.tool_name) {
        bodyContent += `
          <div class="turn-tool">
            <div class="turn-tool-name">🛠️ Tool Invocation: ${escapeHtml(t.tool_name)}</div>
            <div class="turn-tool-args"><strong>Args:</strong> ${escapeHtml(t.tool_args_json || "{}")}</div>
            ${t.tool_output ? `<div class="turn-tool-output"><strong>Result:</strong> ${escapeHtml(t.tool_output.substring(0, 250))}...</div>` : ""}
          </div>
        `;
      }

      card.innerHTML = `
        <div class="turn-header">
          <span>Step ${t.step_index} • ${t.source}</span>
          <span class="intent-pill pill-neutral">${t.turn_type}</span>
        </div>
        ${bodyContent}
      `;
      turnsStreamBox.appendChild(card);
    });

    metaTools.textContent = toolsFound.size > 0 ? Array.from(toolsFound).join(", ") : "None";
  }

  sessionSelect.addEventListener("change", () => {
    const sid = sessionSelect.value;
    if (sid) {
      sessionIdInput.value = sid;
      loadSessionTurns(sid);
    }
  });

  loadTurnsBtn.addEventListener("click", () => {
    loadSessionTurns(sessionIdInput.value || sessionSelect.value);
  });

  ingestBqBtn.addEventListener("click", async () => {
    const { user_id, session_id } = getParams();
    metaStatus.textContent = "Ingesting...";
    metaStatus.className = "intent-pill pill-warn";
    ingestBqBtn.disabled = true;

    try {
      const res = await fetch("/api/antigravity/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id, session_id }),
      });
      const data = await res.json();
      metaStatus.textContent = `Ingested (${data.turns_ingested} turns)`;
      metaStatus.className = "intent-pill pill-success";
      fetchLogs();
    } catch (err) {
      metaStatus.textContent = "Ingest Error";
      metaStatus.className = "intent-pill pill-warn";
    } finally {
      ingestBqBtn.disabled = false;
    }
  });

  dreamDirectBtn.addEventListener("click", () => {
    activeSessionType = "antigravity";
    document.getElementById("harvestBtn").click();
    document.getElementById("stage-panel-3").scrollIntoView({ behavior: "smooth" });
  });

  // --- Stage 1 Interactive Dialogue Chat ---
  const interviewForm = document.getElementById("interviewForm");
  const interviewInput = document.getElementById("interviewInput");
  const interviewChatBox = document.getElementById("interviewChatBox");

  interviewForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = interviewInput.value.trim();
    if (!prompt) return;

    const { user_id, session_id } = getParams();
    appendMsg(interviewChatBox, "You", prompt, true);
    interviewInput.value = "";

    try {
      const res = await fetch("/api/interview/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id, session_id, prompt }),
      });
      const data = await res.json();
      appendMsg(interviewChatBox, "InterviewAgent", data.response);
      fetchLogs();
    } catch (err) {
      appendMsg(interviewChatBox, "System", "Error reaching InterviewAgent.");
    }
  });

  function appendMsg(box, sender, text, isUser = false) {
    const div = document.createElement("div");
    div.className = `msg ${isUser ? "user-msg" : "agent-msg"}`;
    div.innerHTML = `
      <div class="msg-sender">${escapeHtml(sender)}</div>
      <div class="msg-text">${escapeHtml(text)}</div>
    `;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  // --- Stage 2 BigQuery Telemetry Lakehouse ---
  const refreshLogsBtn = document.getElementById("refreshLogsBtn");
  const logsTableBody = document.getElementById("logsTableBody");
  const logFilters = document.querySelectorAll("#logFilters .intent-pill");

  async function fetchLogs() {
    const { session_id } = getParams();
    try {
      const res = await fetch(`/api/logs/${encodeURIComponent(session_id)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      currentLogs = data.logs || [];
      renderLogsTable();
    } catch (err) {
      console.error("Error fetching logs:", err);
      logsTableBody.innerHTML = `<tr><td colspan="4" class="empty-state" style="color: var(--color-warn);">Failed to load traces: ${escapeHtml(err.message)}</td></tr>`;
      throw err;
    }
  }

  function renderLogsTable() {
    logsTableBody.innerHTML = "";
    const filtered = currentLogs.filter((l) => {
      if (currentFilter === "ALL") return true;
      return l.event_type === currentFilter;
    });

    if (filtered.length === 0) {
      logsTableBody.innerHTML = `<tr><td colspan="4" class="empty-state">No matching logs found for session '${getParams().session_id}'.</td></tr>`;
      return;
    }

    filtered.forEach((log) => {
      const tr = document.createElement("tr");
      const tsShort = log.timestamp ? log.timestamp.substring(11, 19) : "--:--:--";
      tr.innerHTML = `
        <td><span class="intent-pill pill-neutral">${log.event_type}</span></td>
        <td><strong>${escapeHtml(log.agent_name || "antigravity")}</strong></td>
        <td>${escapeHtml(log.message || "")}</td>
        <td style="font-family: var(--font-mono); font-size: 0.78rem;">${tsShort}</td>
      `;
      logsTableBody.appendChild(tr);
    });
  }

  logFilters.forEach((pill) => {
    pill.addEventListener("click", () => {
      logFilters.forEach((p) => p.classList.remove("active"));
      pill.classList.add("active");
      currentFilter = pill.getAttribute("data-filter");
      renderLogsTable();
    });
  });

  refreshLogsBtn.addEventListener("click", async () => {
    refreshLogsBtn.disabled = true;
    const orig = refreshLogsBtn.textContent;
    refreshLogsBtn.textContent = "Refreshing...";
    try {
      await fetchLogs();
      refreshLogsBtn.textContent = "Refreshed ✓";
    } catch {
      refreshLogsBtn.textContent = "Refresh Failed ✗";
    } finally {
      setTimeout(() => {
        refreshLogsBtn.textContent = orig;
        refreshLogsBtn.disabled = false;
      }, 1200);
    }
  });

  // --- Stage 3 Cognitive REM Dream Lab ---
  const harvestBtn = document.getElementById("harvestBtn");
  const multiHarvestBtn = document.getElementById("multiHarvestBtn");
  const harvestStatus = document.getElementById("harvestStatus");
  const dreamCardsContainer = document.getElementById("dreamCardsContainer");
  const dreamSessionId = document.getElementById("dreamSessionId");
  const dreamIntent = document.getElementById("dreamIntent");
  const dreamGoalsList = document.getElementById("dreamGoalsList");
  const dreamBlockers = document.getElementById("dreamBlockers");
  const dreamBeliefCount = document.getElementById("dreamBeliefCount");
  const dreamBeliefsBody = document.getElementById("dreamBeliefsBody");
  const dreamPromptCode = document.getElementById("dreamPromptCode");

  async function executeDreamHarvest(isMulti = false) {
    const { user_id, session_id } = getParams();
    harvestBtn.disabled = true;
    multiHarvestBtn.disabled = true;
    harvestStatus.innerHTML = `
      <div class="dream-placeholder">
        <span class="dream-icon">⚙️</span>
        <p><strong>Memory Synthesis in Progress:</strong> Distilling session logs and crystallizing long-term beliefs with <strong>Gemini 3.8 Flash</strong> on Vertex AI...</p>
      </div>
    `;

    try {
      const endpoint = isMulti ? "/api/harvest/multi" : "/api/harvest";
      const payload = isMulti
        ? { user_id, multi_session: true, session_type: activeSessionType }
        : { user_id, session_id, multi_session: false, session_type: activeSessionType };

      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();

      harvestStatus.innerHTML = `
        <div class="dream-placeholder" style="border-color: var(--color-success); background-color: #f0fdf4;">
          <span class="dream-icon">✨</span>
          <div>
            <strong>Memory Consolidation Complete:</strong> Synthesized <strong>${data.preferences_synced || 0}</strong> crystallized beliefs into Google Cloud Spanner table <code>UserPreferences</code>.
          </div>
        </div>
      `;

      // Render 2-Phase Sleep Banner & Dual-Contract Memory Cards
      const sleepModelBanner = document.getElementById("sleepModelBanner");
      if (sleepModelBanner) {
        sleepModelBanner.style.display = "flex";
      }

      // Populate Phase 1: NREM Trace Diagnostics
      const nrem = data.nrem_analysis || {};
      const nremEventsCount = document.getElementById("nremEventsCount");
      const nremTokenCount = document.getElementById("nremTokenCount");
      const nremTokensMeta = document.getElementById("nremTokensMeta");
      const nremErrorRate = document.getElementById("nremErrorRate");
      const nremDomainsList = document.getElementById("nremDomainsList");
      const nremFrictionList = document.getElementById("nremFrictionList");

      if (nremEventsCount) nremEventsCount.textContent = nrem.total_events != null ? nrem.total_events : "0";
      if (nremTokenCount) nremTokenCount.textContent = nrem.estimated_tokens != null ? Number(nrem.estimated_tokens).toLocaleString() : "0";
      if (nremTokensMeta) nremTokensMeta.textContent = `Tokens: ${nrem.estimated_tokens != null ? Number(nrem.estimated_tokens).toLocaleString() : "--"}`;
      if (nremErrorRate) nremErrorRate.textContent = `${nrem.error_rate_pct != null ? nrem.error_rate_pct : "0.0"}%`;

      if (nremDomainsList) {
        const domains = nrem.detected_domains || [];
        if (domains.length === 0) {
          nremDomainsList.innerHTML = '<span class="intent-pill pill-neutral">General</span>';
        } else {
          nremDomainsList.innerHTML = domains.map((d) => `<span class="intent-pill pill-primary">${escapeHtml(d)}</span>`).join("");
        }
      }

      if (nremFrictionList) {
        const frics = nrem.friction_points || [];
        if (frics.length === 0) {
          nremFrictionList.innerHTML = '<li>✓ None detected — interactions ran smoothly.</li>';
        } else {
          nremFrictionList.innerHTML = frics.map((f) => `<li>${escapeHtml(f)}</li>`).join("");
        }
      }

      // Populate Certification Gate
      const cert = data.certification || {};
      const certGateBadge = document.getElementById("certGateBadge");
      const certGateStatusText = document.getElementById("certGateStatusText");
      const certifiedCount = cert.certified_count != null ? cert.certified_count : (data.records || []).length;
      if (certGateBadge) {
        if (cert.passed !== false) {
          certGateBadge.className = "intent-pill pill-success";
          certGateBadge.textContent = `✓ Certified (${certifiedCount} saved)`;
        } else {
          certGateBadge.className = "intent-pill pill-warn";
          certGateBadge.textContent = `Gate Flagged (${cert.rejected_count || 0} rejected)`;
        }
      }
      if (certGateStatusText) {
        certGateStatusText.textContent = `Grounding: ${certifiedCount} Certified (Conf ≥ 0.85)`;
      }

      dreamCardsContainer.style.display = "grid";
      dreamSessionId.textContent = `Session: ${data.session_id || session_id}`;
      dreamIntent.textContent = data.intent || "Session intent distilled.";

      const trajectoryText = data.intent_trajectory || "Stable progression";
      dreamGoalsList.innerHTML = `
        <li style="margin-bottom: 6px;"><strong>Focus Area:</strong> <span class="intent-pill pill-primary" style="margin-left: 4px;">${escapeHtml(data.intent_category || "General")}</span></li>
        <li>
          <strong>How your preferences evolved:</strong>
          <div class="trajectory-callout">${escapeHtml(trajectoryText)}</div>
        </li>
      `;

      if (dreamBlockers) {
        const blk = data.blockers || "";
        if (!blk || blk.toLowerCase().includes("none")) {
          dreamBlockers.innerHTML = '<span style="color: var(--color-success); font-weight: 500;">✓ None detected — ready for next task.</span>';
        } else {
          dreamBlockers.textContent = blk;
        }
      }

      const records = data.records || [];
      dreamBeliefCount.textContent = `${records.length} beliefs consolidated`;
      dreamBeliefsBody.innerHTML = "";

      if (records.length === 0) {
        dreamBeliefsBody.innerHTML = "<p class='empty-state'>No preferences crystallized.</p>";
      } else {
        records.forEach((r) => {
          const item = document.createElement("div");
          item.className = "belief-item";
          item.innerHTML = `
            <div class="belief-header">
              <strong>${escapeHtml(r.preference_key)}</strong>
              <span class="intent-pill pill-primary">${escapeHtml(r.category)}</span>
            </div>
            <div class="belief-statement">${escapeHtml(r.crystallized_belief || r.preference_value)}</div>
            ${r.keywords ? `<div class="belief-keywords">Tags: ${escapeHtml(r.keywords)}</div>` : ""}
          `;
          dreamBeliefsBody.appendChild(item);
        });
      }

      if (data.prompt_used) {
        dreamPromptCode.textContent = data.prompt_used;
      }

      fetchSpannerPrefs();
      fetchSpannerGuardrails();
      fetchSpannerSummaries();
    } catch (err) {
      harvestStatus.innerHTML = `
        <div class="dream-placeholder" style="border-color: var(--color-warn); background-color: #fef2f2;">
          <span class="dream-icon">⚠️</span>
          <div><strong>Memory Consolidation Failed:</strong> ${err}</div>
        </div>
      `;
    } finally {
      harvestBtn.disabled = false;
      multiHarvestBtn.disabled = false;
    }
  }

  harvestBtn.addEventListener("click", () => executeDreamHarvest(false));
  multiHarvestBtn.addEventListener("click", () => executeDreamHarvest(true));

  // --- Stage 4 Spanner Crystalline Matrix ---
  const refreshSpannerBtn = document.getElementById("refreshSpannerBtn");
  const spannerTableBody = document.getElementById("spannerTableBody");
  const spannerCategoryFilters = document.querySelectorAll("#spannerCategoryFilters .intent-pill");
  const spannerSummariesList = document.getElementById("spannerSummariesList");

  async function fetchSpannerPrefs() {
    const { user_id } = getParams();
    try {
      const res = await fetch(`/api/spanner/preferences/${encodeURIComponent(user_id)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      currentPrefs = data.preferences || [];
      renderSpannerTable();
    } catch (err) {
      console.error("Error fetching Spanner prefs:", err);
      spannerTableBody.innerHTML = `<tr><td colspan="5" class="empty-state" style="color: var(--color-warn);">Failed to load Spanner preferences: ${escapeHtml(err.message)}</td></tr>`;
      throw err;
    }
  }

  async function fetchSpannerSummaries() {
    const { user_id } = getParams();
    try {
      const res = await fetch(`/api/spanner/summaries/${encodeURIComponent(user_id)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderSpannerSummaries(data.summaries || []);
    } catch (err) {
      console.error("Error fetching Spanner summaries:", err);
      spannerSummariesList.innerHTML = `<div class='empty-state' style="color: var(--color-warn);">Failed to load session summaries: ${escapeHtml(err.message)}</div>`;
      throw err;
    }
  }

  function renderSpannerTable() {
    spannerTableBody.innerHTML = "";
    const filtered = currentPrefs.filter((p) => {
      if (currentCategory === "ALL") return true;
      return (p.category || "").toLowerCase() === currentCategory.toLowerCase();
    });

    if (filtered.length === 0) {
      spannerTableBody.innerHTML = `<tr><td colspan="5" class="empty-state">No preferences found in Spanner for user '${getParams().user_id}'.</td></tr>`;
      return;
    }

    filtered.forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><code style="font-weight: 600;">${escapeHtml(p.preference_key)}</code></td>
        <td>
          <div>${escapeHtml(p.crystallized_belief || p.preference_value)}</div>
          ${p.keywords ? `<div style="font-family: var(--font-mono); font-size: 0.72rem; color: var(--text-muted); margin-top: 2px;">Tags: ${escapeHtml(p.keywords)}</div>` : ""}
        </td>
        <td><span class="intent-pill pill-primary">${escapeHtml(p.category)}</span></td>
        <td>${Math.round((p.confidence || 1.0) * 100)}%</td>
        <td><span class="intent-pill pill-success">768-dim</span></td>
      `;
      spannerTableBody.appendChild(tr);
    });
  }

  function renderSpannerSummaries(summaries) {
    spannerSummariesList.innerHTML = "";
    if (summaries.length === 0) {
      spannerSummariesList.innerHTML = "<div class='empty-state'>No session summaries recorded yet.</div>";
      return;
    }
    summaries.forEach((s) => {
      const div = document.createElement("div");
      div.className = "turn-card";
      div.style.marginBottom = "8px";
      div.innerHTML = `
        <div class="turn-header">
          <span>Session: <strong>${escapeHtml(s.session_id)}</strong></span>
          <span>${s.created_at ? s.created_at.substring(0, 19).replace("T", " ") : ""}</span>
        </div>
        <div class="turn-body">${escapeHtml(s.summary_text)}</div>
      `;
      spannerSummariesList.appendChild(div);
    });
  }

  spannerCategoryFilters.forEach((pill) => {
    pill.addEventListener("click", () => {
      spannerCategoryFilters.forEach((p) => p.classList.remove("active"));
      pill.classList.add("active");
      currentCategory = pill.getAttribute("data-category");
      renderSpannerTable();
    });
  });

  async function fetchSpannerGuardrails() {
    const { user_id } = getParams();
    try {
      const res = await fetch(`/api/spanner/guardrails/${encodeURIComponent(user_id)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderSpannerGuardrails(data.guardrails || []);
    } catch (err) {
      console.error("Error fetching Spanner guardrails:", err);
      const container = document.getElementById("spannerGuardrailsList");
      if (container) {
        container.innerHTML = `<div class="empty-state" style="color: var(--color-warn);">Failed to load guardrails: ${escapeHtml(err.message)}</div>`;
      }
      throw err;
    }
  }

  function renderSpannerGuardrails(guardrails) {
    const container = document.getElementById("spannerGuardrailsList");
    const badge = document.getElementById("guardrailsCountBadge");
    if (badge) {
      badge.textContent = `${guardrails.length} Invariant${guardrails.length === 1 ? "" : "s"}`;
    }
    if (!container) return;
    if (guardrails.length === 0) {
      container.innerHTML = '<div class="empty-state">No procedural guardrails stored in Spanner yet.</div>';
      return;
    }
    container.innerHTML = guardrails.map((g) => {
      const key = escapeHtml(g.preference_key || "guardrail");
      const belief = escapeHtml(g.crystallized_belief || g.preference_value || "");
      const cat = escapeHtml(g.category || "guardrail");
      const conf = Math.round((g.confidence || 1.0) * 100);
      return `
        <div class="guardrail-card" style="background: var(--surface-card); border: 1px solid var(--border-subtle); border-left: 3px solid var(--color-warn); padding: 10px 14px; border-radius: var(--radius-sm); margin-bottom: 8px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
            <code style="font-weight: 600; color: var(--text-primary);">${key}</code>
            <div style="display: flex; gap: 6px; align-items: center;">
              <span class="intent-pill pill-warn" style="font-size: 0.7rem;">${cat}</span>
              <span class="intent-pill pill-success" style="font-size: 0.7rem;">${conf}%</span>
            </div>
          </div>
          <div style="font-size: 0.88rem; color: var(--text-secondary); line-height: 1.4;">${belief}</div>
        </div>
      `;
    }).join("");
  }

  refreshSpannerBtn.addEventListener("click", async () => {
    refreshSpannerBtn.disabled = true;
    const originalText = refreshSpannerBtn.textContent;
    refreshSpannerBtn.textContent = "Refreshing...";
    try {
      await Promise.all([
        fetchSpannerPrefs(),
        fetchSpannerGuardrails(),
        fetchSpannerSummaries()
      ]);
      refreshSpannerBtn.textContent = "Refreshed ✓";
    } catch (err) {
      refreshSpannerBtn.textContent = "Refresh Failed ✗";
    } finally {
      setTimeout(() => {
        refreshSpannerBtn.textContent = originalText;
        refreshSpannerBtn.disabled = false;
      }, 1200);
    }
  });

  // --- Stage 5 Sub-10ms Fast Recall & Live MCP Playground ---
  const preferenceForm = document.getElementById("preferenceForm");
  const preferenceInput = document.getElementById("preferenceInput");
  const preferenceChatBox = document.getElementById("preferenceChatBox");

  preferenceForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = preferenceInput.value.trim();
    if (!prompt) return;

    const { user_id, session_id } = getParams();
    appendMsg(preferenceChatBox, "You", prompt, true);
    preferenceInput.value = "";

    const startTime = performance.now();
    try {
      const res = await fetch("/api/preference/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id, session_id, prompt }),
      });
      const data = await res.json();
      const elapsed = Math.round(performance.now() - startTime);
      const isCache = data.source === "SPANNER_CACHE";
      const badge = `<span class="intent-pill ${isCache ? 'pill-success' : 'pill-warn'}">${isCache ? 'SPANNER_CACHE' : 'LLM_FALLBACK'} (${data.latency_ms || elapsed}ms)</span>`;
      
      const div = document.createElement("div");
      div.className = "msg agent-msg";
      div.innerHTML = `
        <div class="msg-sender">PreferenceAgent ${badge}</div>
        <div class="msg-text">${escapeHtml(data.response)}</div>
      `;
      preferenceChatBox.appendChild(div);
      preferenceChatBox.scrollTop = preferenceChatBox.scrollHeight;
      fetchLogs();
    } catch (err) {
      appendMsg(preferenceChatBox, "System", "Error reaching PreferenceAgent.");
    }
  });

  // Live MCP Runner
  const mcpToolSelect = document.getElementById("mcpToolSelect");
  const mcpArgInput = document.getElementById("mcpArgInput");
  const executeMcpBtn = document.getElementById("executeMcpBtn");
  const mcpOutputContent = document.getElementById("mcpOutputContent");
  const mcpLatencyBadge = document.getElementById("mcpLatencyBadge");

  mcpToolSelect.addEventListener("change", () => {
    const val = mcpToolSelect.value;
    const { user_id, session_id } = getParams();
    if (val === "get_user_crystallized_beliefs") {
      mcpArgInput.value = JSON.stringify({ user_id });
    } else if (val === "search_agent_memories") {
      mcpArgInput.value = JSON.stringify({ query: "what is my job role and language?", user_id, limit: 5 });
    } else if (val === "get_recent_session_memory") {
      mcpArgInput.value = JSON.stringify({ user_id, session_id });
    } else if (val === "crystallize_live_preference") {
      mcpArgInput.value = JSON.stringify({
        user_id,
        preference_key: "testing_framework",
        preference_value: "uv run pytest",
        category: "tooling",
        crystallized_belief: "The user strictly requires uv run pytest for all test suites.",
      });
    }
  });

  executeMcpBtn.addEventListener("click", async () => {
    executeMcpBtn.disabled = true;
    mcpOutputContent.textContent = "Executing tool against Cloud Spanner...";
    mcpLatencyBadge.textContent = "Latency: running...";
    mcpLatencyBadge.className = "intent-pill pill-neutral";

    let args = {};
    try {
      args = JSON.parse(mcpArgInput.value.trim() || "{}");
    } catch (e) {
      args = { query: mcpArgInput.value.trim() };
    }

    try {
      const res = await fetch("/api/mcp/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tool_name: mcpToolSelect.value,
          arguments: args,
        }),
      });
      if (!res.ok) {
        let errDetail = `HTTP ${res.status}`;
        try {
          const errData = await res.json();
          errDetail = errData.detail || JSON.stringify(errData);
        } catch (_) {
          errDetail = await res.text();
        }
        throw new Error(errDetail);
      }
      const data = await res.json();
      mcpOutputContent.textContent = data.result || JSON.stringify(data, null, 2);
      mcpLatencyBadge.textContent = `Latency: ${data.latency_ms}ms (Target: ${data.sla})`;
      mcpLatencyBadge.className = "intent-pill pill-success";
      fetchSpannerPrefs();
    } catch (err) {
      mcpOutputContent.textContent = `MCP Execution Error: ${err.message || err}`;
      mcpLatencyBadge.textContent = "Latency: Error";
      mcpLatencyBadge.className = "intent-pill pill-warn";
    } finally {
      executeMcpBtn.disabled = false;
    }
  });

  // Modal handlers
  const githubBtn = document.getElementById("githubPopoutBtn");
  const githubModal = document.getElementById("githubModal");
  const githubCloseBtn = document.getElementById("githubModalCloseBtn");
  const githubDismissBtn = document.getElementById("githubModalDismissBtn");

  if (githubBtn && githubModal) {
    githubBtn.addEventListener("click", () => githubModal.showModal());
    githubCloseBtn.addEventListener("click", () => githubModal.close());
    githubDismissBtn.addEventListener("click", () => githubModal.close());
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // Session Info Tooltip Handler
  const sessionInfoBtn = document.getElementById("sessionInfoBtn");
  const sessionInfoTooltip = document.getElementById("sessionInfoTooltip");
  const tooltipContainer = document.querySelector(".info-tooltip-container");

  if (sessionInfoBtn && sessionInfoTooltip) {
    sessionInfoBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      sessionInfoTooltip.classList.toggle("show");
      sessionInfoBtn.classList.toggle("active");
    });

    document.addEventListener("click", (e) => {
      if (tooltipContainer && !tooltipContainer.contains(e.target)) {
        sessionInfoTooltip.classList.remove("show");
        sessionInfoBtn.classList.remove("active");
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        sessionInfoTooltip.classList.remove("show");
        sessionInfoBtn.classList.remove("active");
      }
    });
  }

  // --- Initial Boot ---
  fetchAntigravitySessions();
  fetchLogs();
  fetchSpannerPrefs();
  fetchSpannerGuardrails();
  fetchSpannerSummaries();
});

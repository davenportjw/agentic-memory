// Google ADK 2.0 Control Center Client Application (Material Light UI)

document.addEventListener("DOMContentLoaded", () => {
  async function fetchAuth(url, options = {}) {
    const res = await fetch(url, options);
    if (res.status === 401 || res.status === 403) {
      window.location.href = "/login.html";
      throw new Error("Authentication required or access denied.");
    }
    return res;
  }

  const userIdInput = document.getElementById("userIdInput");
  const sessionIdInput = document.getElementById("sessionIdInput");
  const sessionInfoBtn = document.getElementById("sessionInfoBtn");
  const sessionInfoTooltip = document.getElementById("sessionInfoTooltip");

  if (sessionInfoBtn && sessionInfoTooltip) {
    sessionInfoBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      sessionInfoTooltip.classList.toggle("show");
    });
    document.addEventListener("click", (e) => {
      if (!sessionInfoTooltip.contains(e.target) && !sessionInfoBtn.contains(e.target)) {
        sessionInfoTooltip.classList.remove("show");
      }
    });
  }

  // GitHub Popout Modal Handlers
  const githubBtn = document.getElementById("githubPopoutBtn");
  const githubModal = document.getElementById("githubModal");
  const githubCloseBtn = document.getElementById("githubModalCloseBtn");
  const githubDismissBtn = document.getElementById("githubModalDismissBtn");

  function openGithubModal() {
    if (githubModal) {
      if (typeof githubModal.showModal === "function") {
        githubModal.showModal();
      } else {
        githubModal.setAttribute("open", "");
      }
      githubModal.setAttribute("aria-hidden", "false");
    }
  }

  function closeGithubModal() {
    if (githubModal) {
      if (typeof githubModal.close === "function" && githubModal.open) {
        githubModal.close();
      } else {
        githubModal.removeAttribute("open");
      }
      githubModal.setAttribute("aria-hidden", "true");
    }
  }

  if (githubBtn) {
    githubBtn.addEventListener("click", openGithubModal);
  }
  if (githubCloseBtn) {
    githubCloseBtn.addEventListener("click", closeGithubModal);
  }
  if (githubDismissBtn) {
    githubDismissBtn.addEventListener("click", closeGithubModal);
  }
  if (githubModal) {
    githubModal.addEventListener("click", (e) => {
      const rect = githubModal.getBoundingClientRect();
      const isInDialog = (
        rect.top <= e.clientY &&
        e.clientY <= rect.top + rect.height &&
        rect.left <= e.clientX &&
        e.clientX <= rect.left + rect.width
      );
      if (!isInDialog && e.target === githubModal) {
        closeGithubModal();
      }
    });
    githubModal.addEventListener("close", () => {
      githubModal.setAttribute("aria-hidden", "true");
    });
  }

  // Navigation elements (Sticky Left Nav Table-of-Contents)
  const navItems = document.querySelectorAll(".nav-item[data-stage]");
  const stagePanels = document.querySelectorAll(".stage-panel");

  // Chat forms
  const interviewForm = document.getElementById("interviewForm");
  const interviewInput = document.getElementById("interviewInput");
  const interviewChatBox = document.getElementById("interviewChatBox");

  const preferenceForm = document.getElementById("preferenceForm");
  const preferenceInput = document.getElementById("preferenceInput");
  const preferenceChatBox = document.getElementById("preferenceChatBox");

  // Observability & Memory buttons
  const refreshLogsBtn = document.getElementById("refreshLogsBtn");
  const logsTableBody = document.getElementById("logsTableBody");

  const harvestBtn = document.getElementById("harvestBtn");
  const harvestStatus = document.getElementById("harvestStatus");

  const refreshSpannerBtn = document.getElementById("refreshSpannerBtn");
  const spannerTableBody = document.getElementById("spannerTableBody");

  // Smooth scroll to target stage panel when left sidebar navigation item is clicked
  navItems.forEach((item) => {
    item.addEventListener("click", (e) => {
      e.preventDefault();
      const stage = item.getAttribute("data-stage");
      const targetEl = document.getElementById(`stage-panel-${stage}`);
      if (targetEl) {
        targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
        // Update active class immediately on click
        navItems.forEach((nav) => nav.classList.remove("active"));
        item.classList.add("active");

        // Refresh table data if scrolling to Telemetry Logs (2) or Spanner Memory (4)
        if (stage === "2") {
          fetchLogs();
        } else if (stage === "4") {
          fetchSpannerPrefs();
        }
      }
    });
  });

  // IntersectionObserver to automatically highlight active Left Nav item during page scroll
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const id = entry.target.id;
          const stageNum = id.replace("stage-panel-", "");
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

  stagePanels.forEach((panel) => observer.observe(panel));

  function getSessionParams() {
    return {
      user_id: userIdInput.value.trim() || "demo-user-42",
      session_id: sessionIdInput.value.trim() || "session-ui-001"
    };
  }

  function formatMarkdownCodeBlocks(text) {
    if (!text || typeof text !== "string") return text;
    return text.replace(/```(\w+)?\n?([\s\S]*?)```/g, (match, lang, code) => {
      const languageBadge = lang ? `<div class="code-header"><span>${lang.toUpperCase()}</span><span class="code-badge">${lang}</span></div>` : '<div class="code-header"><span>CODE</span></div>';
      const escapedCode = code
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      return `<div class="code-snippet-box" style="margin: 8px 0;">${languageBadge}<pre><code class="${lang ? 'language-' + lang : ''}">${escapedCode}</code></pre></div>`;
    });
  }

  function appendMsg(chatBox, sender, text, isUser = false, badge = null) {
    const msgDiv = document.createElement("div");
    msgDiv.className = `msg ${isUser ? "user-msg" : "agent-msg"}`;

    const senderDiv = document.createElement("div");
    senderDiv.className = "msg-sender";
    senderDiv.innerText = sender;

    const textDiv = document.createElement("div");
    textDiv.className = "msg-text";
    textDiv.innerHTML = formatMarkdownCodeBlocks(text);

    if (badge) {
      const chip = document.createElement("span");
      chip.className = badge.type === "SPANNER_CACHE" ? "chip chip-success" : "chip chip-warn";
      chip.style.marginLeft = "8px";
      chip.innerText = `${badge.type} (${badge.latency}ms)`;
      senderDiv.appendChild(chip);
    }

    msgDiv.appendChild(senderDiv);
    msgDiv.appendChild(textDiv);
    chatBox.appendChild(msgDiv);
    chatBox.scrollTop = chatBox.scrollHeight;
  }

  // 1. InterviewAgent Submit
  interviewForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = interviewInput.value.trim();
    if (!prompt) return;

    const { user_id, session_id } = getSessionParams();
    appendMsg(interviewChatBox, "You", prompt, true);
    interviewInput.value = "";

    try {
      const res = await fetchAuth("/api/interview/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id, session_id, prompt })
      });
      const data = await res.json();
      appendMsg(interviewChatBox, "InterviewAgent", data.response);
      fetchLogs();
    } catch (err) {
      appendMsg(interviewChatBox, "System Error", "Failed to reach InterviewAgent.");
    }
  });

  // 2. Fetch BigQuery Telemetry Logs
  async function fetchLogs() {
    const { session_id } = getSessionParams();
    try {
      const res = await fetchAuth(`/api/logs/${session_id}`);
      const data = await res.json();
      renderLogsTable(data.logs || []);
    } catch (err) {
      console.error("Error fetching logs:", err);
    }
  }

  function renderLogsTable(logs) {
    logsTableBody.innerHTML = "";
    if (logs.length === 0) {
      logsTableBody.innerHTML = `<tr><td colspan="3" class="empty-state">No logs yet. Talk to an agent!</td></tr>`;
      return;
    }
    logs.forEach(log => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><span class="chip ${log.event_type === 'USER_PROMPT' ? 'chip-primary' : 'chip-success'}">${log.event_type}</span></td>
        <td><strong>${log.agent_name}</strong></td>
        <td>${log.message}</td>
      `;
      logsTableBody.appendChild(tr);
    });
  }

  refreshLogsBtn.addEventListener("click", fetchLogs);

  // 3. Dream Harvest
  harvestBtn.addEventListener("click", async () => {
    const { user_id, session_id } = getSessionParams();
    harvestBtn.disabled = true;
    harvestBtn.innerText = "Harvesting...";
    harvestStatus.innerHTML = "<em>🌙 REM Synthesis in progress: AI is reading waking BigQuery logs, summarizing via BigQuery AI (AI.GENERATE / ML.GENERATE_TEXT), and crystallizing beliefs into Google Cloud Spanner...</em>";

    try {
      const res = await fetchAuth("/api/harvest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id, session_id })
      });
      const data = await res.json();
      const bqSelect = escapeHtml(data.queries_run?.bigquery_select || "-- BigQuery Waking Logs Query");
      const bqAi = escapeHtml(data.queries_run?.bigquery_ai_generate || "-- BigQuery AI ML.GENERATE_TEXT Query");
      const spannerUpserts = escapeHtml((data.queries_run?.spanner_upserts || []).join("\n\n") || "-- Spanner Upserts");
      const promptText = escapeHtml(data.prompt_used || "No prompt available");

      harvestStatus.innerHTML = `
        <div style="color: #b05e00; font-weight: 600; font-size: 1rem; margin-bottom: 6px;">🌙 REM Synthesis (Dream Harvest) Complete!</div>
        <div style="margin: 4px 0 10px 0;">Waking BigQuery logs were analyzed via AI REM synthesis. <strong>${data.preferences_synced}</strong> crystalline beliefs have been synthesized into Cloud Spanner memory.</div>
        
        <details class="dream-detail-box" open>
          <summary>🔍 SQL Queries Executed (BigQuery &amp; Cloud Spanner)</summary>
          <div class="dream-detail-content">
            <div class="query-section-title">1. Waking BigQuery Telemetry Extraction Query:</div>
            <pre class="sql-query-box">${bqSelect}</pre>
            <div class="query-section-title">2. BigQuery AI Batch Synthesizer Query (ML.GENERATE_TEXT / AI.GENERATE):</div>
            <pre class="sql-query-box">${bqAi}</pre>
            <div class="query-section-title">3. Cloud Spanner Crystalline Memory Upsert Mutations:</div>
            <pre class="sql-query-box">${spannerUpserts}</pre>
          </div>
        </details>

        <details class="dream-detail-box" open>
          <summary>📜 REM Dream Prompt (Sent to Gemini 3.5 Flash Lite / BigQuery AI)</summary>
          <div class="dream-detail-content">
            <div class="query-section-title">Prompt Template &amp; Conversation Transcript Injection:</div>
            <pre class="prompt-view-box">${promptText}</pre>
          </div>
        </details>

        <details class="dream-detail-box" open>
          <summary>💎 Crystallized Spanner Belief Records</summary>
          <div class="dream-detail-content">
            <pre style="margin-top: 6px; font-size: 0.75rem; background: #f8f9fa; padding: 10px; border-radius: 6px; border: 1px solid #e0e0e0; overflow-x: auto;">${escapeHtml(JSON.stringify(data.records, null, 2))}</pre>
          </div>
        </details>
      `;
      fetchSpannerPrefs();
    } catch (err) {
      harvestStatus.innerHTML = "<strong style='color: red;'>Dream Harvest Failed!</strong>";
    } finally {
      harvestBtn.disabled = false;
      harvestBtn.innerText = "Run Dream Harvest";
    }
  });

  // 4. Fetch Google Cloud Spanner Preferences
  async function fetchSpannerPrefs() {
    const { user_id } = getSessionParams();
    try {
      const res = await fetchAuth(`/api/spanner/preferences/${user_id}`);
      const data = await res.json();
      renderSpannerTable(data.preferences || []);
    } catch (err) {
      console.error("Error fetching Spanner prefs:", err);
    }
  }

  function renderSpannerTable(prefs) {
    spannerTableBody.innerHTML = "";
    if (prefs.length === 0) {
      spannerTableBody.innerHTML = `<tr><td colspan="4" class="empty-state">No preferences stored in Spanner yet.</td></tr>`;
      return;
    }
    prefs.forEach(pref => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${pref.preference_key}</strong></td>
        <td>${pref.preference_value}</td>
        <td><span class="chip chip-primary">${pref.category}</span></td>
        <td>${pref.confidence}</td>
      `;
      spannerTableBody.appendChild(tr);
    });
  }

  refreshSpannerBtn.addEventListener("click", fetchSpannerPrefs);

  // 5. PreferenceAgent Submit (Spanner Fast Cache Lookup)
  preferenceForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = preferenceInput.value.trim();
    if (!prompt) return;

    const { user_id, session_id } = getSessionParams();
    appendMsg(preferenceChatBox, "You", prompt, true);
    preferenceInput.value = "";

    try {
      const res = await fetchAuth("/api/preference/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id, session_id, prompt })
      });
      const data = await res.json();
      appendMsg(
        preferenceChatBox,
        "PreferenceAgent",
        data.response,
        false,
        { type: data.source, latency: data.latency_ms }
      );
      fetchLogs();
    } catch (err) {
      appendMsg(preferenceChatBox, "System Error", "Failed to reach PreferenceAgent.");
    }
  });

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // Initial load
  fetchLogs();
  fetchSpannerPrefs();
});

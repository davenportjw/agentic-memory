/**
 * Interactive controller for How It Works (Architecture & Code Showcase)
 * Handles interactive flowchart node selection, code spotlight tabs,
 * clipboard copy functionality, and data transformation sandbox simulation.
 */

document.addEventListener("DOMContentLoaded", () => {
  initFlowchartSelector();
  initCodeSpotlightTabs();
  initCodeCopyButtons();
  initDataSandbox();
  initDreamSandboxTabs();
  initSessionTooltip();
  initLeftNavCollapse();
  initArchSidebar();
  initArchDiagramSelector();
  initGithubModal();
  initMathVisualizer();
});

/* ==========================================================================
   1. Interactive 5-State System Flowchart Selector
   ========================================================================== */
const FLOW_STAGE_DATA = {
  1: {
    title: "1. Live Dialogue: InterviewAgent (Google ADK 2.0)",
    desc: "<strong>Live Dialogue:</strong> The <code>InterviewAgent</code> engages in conversational turns using Google ADK 2.0 and Gemini 3.8 Flash on Vertex AI. Facts are captured via tool execution (<code>record_user_answer</code>) without blocking on persistent database writes.",
    codeTab: "1"
  },
  2: {
    title: "2. Episodic Trace Logging: BigQuery Telemetry",
    desc: "<strong>Episodic Trace Logging:</strong> Every user prompt and agent response is asynchronously appended to BigQuery table <code>adk_agent_telemetry.agent_logs</code>, preserving full conversational provenance for auditability and batch distillation.",
    codeTab: "2"
  },
  3: {
    title: "3. Memory Synthesis: Gemini 3.8 Flash Distillation",
    desc: "<strong>Memory Synthesis:</strong> When synthesis triggers, the synchronizer aggregates BigQuery logs and invokes Gemini 3.8 Flash with a strict JSON schema to distill structured preferences.",
    codeTab: "3"
  },
  4: {
    title: "4. Crystalline Memory: Cloud Spanner",
    desc: "<strong>Crystalline Storage:</strong> Distilled user beliefs are mutated into the Cloud Spanner <code>UserPreferences</code> table with primary key <code>(UserId, PreferenceKey)</code> and commit timestamps, providing durable, structured persistence across sessions.",
    codeTab: "4"
  },
  5: {
    title: "5. Instant Recall: PreferenceAgent (Sub-10ms Fast Lookup)",
    desc: "<strong>Cache Interception:</strong> When querying user profile facts, <code>PreferenceAgent</code> performs a primary-key lookup in Cloud Spanner. Cache hits return in <strong>< 10ms</strong> without incurring LLM inference latency or token costs.",
    codeTab: "5"
  }
};

function initFlowchartSelector() {
  const flowButtons = document.querySelectorAll(".flow-node-btn");
  const detailTitle = document.getElementById("flowDetailTitle");
  const detailDesc = document.getElementById("flowDetailDesc");
  const jumpBtn = document.getElementById("jumpToCodeBtn");

  flowButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      const stageId = btn.getAttribute("data-flow-stage");
      const stageInfo = FLOW_STAGE_DATA[stageId];

      if (!stageInfo) return;

      // Update active button
      flowButtons.forEach(b => {
        b.classList.remove("active");
        b.setAttribute("aria-selected", "false");
      });
      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");

      // Update detail pane content
      detailTitle.innerHTML = stageInfo.title;
      detailDesc.innerHTML = stageInfo.desc;
      if (jumpBtn) {
        jumpBtn.setAttribute("data-target-tab", stageInfo.codeTab);
      }

      // Trigger fade animation
      const pane = document.getElementById("flowDetailPane");
      pane.style.animation = "none";
      void pane.offsetWidth; // force reflow
      pane.style.animation = "fadeIn 0.25s ease-in-out";
    });
  });

  if (jumpBtn) {
    jumpBtn.addEventListener("click", () => {
      const targetTab = jumpBtn.getAttribute("data-target-tab") || "1";
      activateCodeTab(targetTab);
      const spotlightCard = document.querySelector(".code-spotlight-card");
      if (spotlightCard) {
        spotlightCard.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  }
}

/* ==========================================================================
   2. Production Code Spotlight Interactive Tabs
   ========================================================================== */
function initCodeSpotlightTabs() {
  const tabButtons = document.querySelectorAll(".code-tab-btn");
  tabButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      const tabId = btn.getAttribute("data-tab");
      activateCodeTab(tabId);
    });
  });
}

function activateCodeTab(tabId) {
  const tabButtons = document.querySelectorAll(".code-tab-btn");
  const tabPanes = document.querySelectorAll(".code-tab-pane");

  tabButtons.forEach(btn => {
    if (btn.getAttribute("data-tab") === tabId) {
      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");
    } else {
      btn.classList.remove("active");
      btn.setAttribute("aria-selected", "false");
    }
  });

  tabPanes.forEach(pane => {
    if (pane.id === `codePane-${tabId}`) {
      pane.classList.add("active");
    } else {
      pane.classList.remove("active");
    }
  });
}

/* ==========================================================================
   3. Code Clipboard Copy Functionality
   ========================================================================== */
function initCodeCopyButtons() {
  const copyButtons = document.querySelectorAll(".code-copy-btn");
  copyButtons.forEach(btn => {
    btn.addEventListener("click", async () => {
      const targetId = btn.getAttribute("data-copy-target");
      const codeBlock = document.getElementById(targetId);
      if (!codeBlock) return;

      const codeText = codeBlock.innerText || codeBlock.textContent;
      try {
        await navigator.clipboard.writeText(codeText);
        const originalText = btn.textContent;
        btn.textContent = "Copied! ✓";
        btn.classList.add("copied");

        setTimeout(() => {
          btn.textContent = originalText;
          btn.classList.remove("copied");
        }, 2000);
      } catch (err) {
        console.error("Failed to copy text: ", err);
      }
    });
  });
}

/* ==========================================================================
   4. Interactive Data Transformation Sandbox Simulator
   ========================================================================== */
const SANDBOX_DATA = {
  1: {
    bq: `{
  "timestamp": "2026-10-12T20:31:00Z",
  "session_id": "session-ui-001",
  "user_id": "demo-user-42",
  "agent_name": "InterviewAgent",
  "event_type": "USER_PROMPT",
  "message": "My favorite language is Python and I am a Principal Cloud Architect"
}`,
    dream: `{
  "session_intent": "Establishing technical profile as a Principal Cloud Architect to explore Python and infrastructure tools",
  "intent_category": "onboarding",
  "intent_confidence": 0.95,
  "preferences": [
    {
      "preference_key": "favorite_language",
      "preference_value": "Python",
      "category": "language",
      "confidence": 0.98,
      "crystallized_belief": "The user's favorite programming language is Python.",
      "keywords": "python, language, programming"
    },
    {
      "preference_key": "job_role",
      "preference_value": "Principal Cloud Architect",
      "category": "role",
      "confidence": 0.99,
      "crystallized_belief": "The user works as a Principal Cloud Architect.",
      "keywords": "architect, principal, cloud, role"
    }
  ]
}`,
    dreamQueries: `-- 1. BigQuery Telemetry Extraction Query
SELECT session_id, user_id, event_type, message, timestamp
FROM \`adk_agent_telemetry.agent_logs\`
WHERE session_id = 'session-ui-001'
ORDER BY timestamp ASC;

-- 2. Cloud Spanner Upsert Mutations
INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'favorite_language', 'Python', 'language', 0.98, 'session-ui-001', 'The user\\'s favorite programming language is Python.', 'python, language, programming', PENDING_COMMIT_TIMESTAMP());

INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'job_role', 'Principal Cloud Architect', 'role', 0.99, 'session-ui-001', 'The user works as a Principal Cloud Architect.', 'architect, principal, cloud, role', PENDING_COMMIT_TIMESTAMP());`,
    dreamPrompt: `You are a memory synthesis engine performing offline consolidation over conversation logs for user 'demo-user-42' in session 'session-ui-001'.
Extract long-term semantic beliefs and capture user intent.

Conversation transcript:
USER_PROMPT: My favorite language is Python and I am a Principal Cloud Architect

Requirements:
1. Extract primary session_intent and intent_category.
2. Provide intent_confidence between 0.0 and 1.0.
3. Extract distinct preferences (preference_key, preference_value, category, confidence, crystallized_belief, keywords).
4. Return valid JSON matching the schema.`,
    spanner: `INSERT OR UPDATE UserPreferences:
---
UserId: "demo-user-42"
PreferenceKey: "favorite_language"
PreferenceValue: "Python"
Category: "language"
Confidence: 0.98
CrystallizedBelief: "The user's favorite programming language is Python."
Keywords: "python, language, programming"
LastUpdated: PENDING_COMMIT_TIMESTAMP()

UserId: "demo-user-42"
PreferenceKey: "job_role"
PreferenceValue: "Principal Cloud Architect"
Category: "role"
Confidence: 0.99
CrystallizedBelief: "The user works as a Principal Cloud Architect."
Keywords: "architect, principal, cloud, role"
LastUpdated: PENDING_COMMIT_TIMESTAMP()`
  },
  2: {
    bq: `{
  "timestamp": "2026-10-12T20:32:14Z",
  "session_id": "session-ui-001",
  "user_id": "demo-user-42",
  "agent_name": "InterviewAgent",
  "event_type": "USER_PROMPT",
  "message": "I deploy microservices to Cloud Run using Terraform and Spanner"
}`,
    dream: `{
  "session_intent": "Deploying cloud microservices using Cloud Run, Terraform, and Cloud Spanner",
  "intent_category": "architecture_design",
  "intent_confidence": 0.96,
  "preferences": [
    {
      "preference_key": "deploy_target",
      "preference_value": "Google Cloud Run",
      "category": "infrastructure",
      "confidence": 0.97,
      "crystallized_belief": "The user deploys microservices to Google Cloud Run.",
      "keywords": "cloud run, serverless, microservices, deploy"
    },
    {
      "preference_key": "iac_tool",
      "preference_value": "Terraform",
      "category": "devops",
      "confidence": 0.95,
      "crystallized_belief": "The user manages infrastructure as code using Terraform.",
      "keywords": "terraform, iac, devops, infrastructure"
    },
    {
      "preference_key": "database",
      "preference_value": "Google Cloud Spanner",
      "category": "database",
      "confidence": 0.98,
      "crystallized_belief": "The user relies on Google Cloud Spanner as their database.",
      "keywords": "spanner, database, sql, google cloud"
    }
  ]
}`,
    dreamQueries: `-- 1. BigQuery Telemetry Extraction Query
SELECT session_id, user_id, event_type, message, timestamp
FROM \`adk_agent_telemetry.agent_logs\`
WHERE session_id = 'session-ui-001'
ORDER BY timestamp ASC;

-- 2. Cloud Spanner Upsert Mutations
INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'deploy_target', 'Google Cloud Run', 'infrastructure', 0.97, 'session-ui-001', 'The user deploys microservices to Google Cloud Run.', 'cloud run, serverless, microservices, deploy', PENDING_COMMIT_TIMESTAMP());

INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'iac_tool', 'Terraform', 'devops', 0.95, 'session-ui-001', 'The user manages infrastructure as code using Terraform.', 'terraform, iac, devops, infrastructure', PENDING_COMMIT_TIMESTAMP());`,
    dreamPrompt: `You are a memory synthesis engine performing offline consolidation over conversation logs for user 'demo-user-42' in session 'session-ui-001'.
Extract long-term semantic beliefs and capture user intent.

Conversation transcript:
USER_PROMPT: I deploy microservices to Cloud Run using Terraform and Spanner

Requirements:
1. Extract primary session_intent and intent_category.
2. Provide intent_confidence between 0.0 and 1.0.
3. Extract distinct preferences (preference_key, preference_value, category, confidence, crystallized_belief, keywords).
4. Return valid JSON matching the schema.`,
    spanner: `INSERT OR UPDATE UserPreferences:
---
UserId: "demo-user-42"
PreferenceKey: "deploy_target"
PreferenceValue: "Google Cloud Run"
Category: "infrastructure"
Confidence: 0.97
CrystallizedBelief: "The user deploys microservices to Google Cloud Run."
Keywords: "cloud run, serverless, microservices, deploy"
LastUpdated: PENDING_COMMIT_TIMESTAMP()

UserId: "demo-user-42"
PreferenceKey: "iac_tool"
PreferenceValue: "Terraform"
Category: "devops"
Confidence: 0.95
CrystallizedBelief: "The user manages infrastructure as code using Terraform."
Keywords: "terraform, iac, devops, infrastructure"
LastUpdated: PENDING_COMMIT_TIMESTAMP()

UserId: "demo-user-42"
PreferenceKey: "database"
PreferenceValue: "Google Cloud Spanner"
Category: "database"
Confidence: 0.98
CrystallizedBelief: "The user relies on Google Cloud Spanner as their database."
Keywords: "spanner, database, sql, google cloud"
LastUpdated: PENDING_COMMIT_TIMESTAMP()`
  },
  3: {
    bq: `{
  "timestamp": "2026-10-12T20:33:40Z",
  "session_id": "session-ui-001",
  "user_id": "demo-user-42",
  "agent_name": "InterviewAgent",
  "event_type": "USER_PROMPT",
  "message": "I prefer dark mode UI and writing Go tests with pytest and uv"
}`,
    dream: `{
  "session_intent": "Configuring developer environment preferences for UI theme and testing tools",
  "intent_category": "preference",
  "intent_confidence": 0.97,
  "preferences": [
    {
      "preference_key": "ui_theme",
      "preference_value": "dark mode",
      "category": "preferences",
      "confidence": 0.99,
      "crystallized_belief": "The user prefers a dark mode UI theme.",
      "keywords": "dark mode, theme, ui, appearance"
    },
    {
      "preference_key": "python_test_runner",
      "preference_value": "uv + pytest",
      "category": "testing",
      "confidence": 0.96,
      "crystallized_belief": "The user runs tests using uv and pytest.",
      "keywords": "uv, pytest, testing, python"
    }
  ]
}`,
    dreamQueries: `-- 1. BigQuery Telemetry Extraction Query
SELECT session_id, user_id, event_type, message, timestamp
FROM \`adk_agent_telemetry.agent_logs\`
WHERE session_id = 'session-ui-001'
ORDER BY timestamp ASC;

-- 2. Cloud Spanner Upsert Mutations
INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'ui_theme', 'Dark Mode', 'preference', 0.99, 'session-ui-001', 'The user prefers dark mode UI.', 'dark mode, theme, ui', PENDING_COMMIT_TIMESTAMP());

INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'test_runner', 'uv + pytest', 'testing', 0.96, 'session-ui-001', 'The user runs tests using uv and pytest.', 'uv, pytest, testing, python', PENDING_COMMIT_TIMESTAMP());`,
    dreamPrompt: `You are a memory synthesis engine performing offline consolidation over conversation logs for user 'demo-user-42' in session 'session-ui-001'.
Extract long-term semantic beliefs and capture user intent.

Conversation transcript:
USER_PROMPT: I prefer dark mode UI and writing Go tests with pytest and uv

Requirements:
1. Extract primary session_intent and intent_category.
2. Provide intent_confidence between 0.0 and 1.0.
3. Extract distinct preferences (preference_key, preference_value, category, confidence, crystallized_belief, keywords).
4. Return valid JSON matching the schema.`,
    spanner: `INSERT OR UPDATE UserPreferences:
---
UserId: "demo-user-42"
PreferenceKey: "ui_theme"
PreferenceValue: "dark mode"
Category: "preferences"
Confidence: 0.99
CrystallizedBelief: "The user prefers a dark mode UI theme."
Keywords: "dark mode, theme, ui, appearance"
LastUpdated: PENDING_COMMIT_TIMESTAMP()

UserId: "demo-user-42"
PreferenceKey: "python_test_runner"
PreferenceValue: "uv + pytest"
Category: "testing"
Confidence: 0.96
CrystallizedBelief: "The user runs tests using uv and pytest."
Keywords: "uv, pytest, testing, python"
LastUpdated: PENDING_COMMIT_TIMESTAMP()`
  }
};

function initDataSandbox() {
  const sampleButtons = document.querySelectorAll(".sandbox-sample-btn");
  const jsonBq = document.getElementById("sandboxJsonBq");
  const jsonDream = document.getElementById("sandboxJsonDream");
  const dreamQueries = document.getElementById("sandboxDreamQueries");
  const dreamPrompt = document.getElementById("sandboxDreamPrompt");
  const jsonSpanner = document.getElementById("sandboxJsonSpanner");

  sampleButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      const sampleId = btn.getAttribute("data-sample");
      const data = SANDBOX_DATA[sampleId];
      if (!data) return;

      sampleButtons.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");

      if (jsonBq) jsonBq.textContent = data.bq;
      if (jsonDream) jsonDream.textContent = data.dream;
      if (dreamQueries && data.dreamQueries) dreamQueries.textContent = data.dreamQueries;
      if (dreamPrompt && data.dreamPrompt) dreamPrompt.textContent = data.dreamPrompt;
      if (jsonSpanner) jsonSpanner.textContent = data.spanner;

      // Subtle animation highlight
      [jsonBq, jsonDream, dreamQueries, dreamPrompt, jsonSpanner].forEach(box => {
        if (!box) return;
        box.style.transition = "background-color 0.2s ease";
        box.style.backgroundColor = "#334155";
        setTimeout(() => {
          box.style.backgroundColor = "#1e293b";
        }, 200);
      });
    });
  });
}

function initDreamSandboxTabs() {
  const tabBtns = document.querySelectorAll(".dream-tab-btn");
  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      tabBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const targetId = btn.getAttribute("data-target");
      document.querySelectorAll(".dream-tab-content").forEach(el => {
        el.style.display = el.id === targetId ? "block" : "none";
      });
    });
  });
}

/* ==========================================================================
   5. Session Info Tooltip Handler
   ========================================================================== */
function initSessionTooltip() {
  const tooltipBtn = document.getElementById("sessionInfoBtn");
  const tooltipBox = document.getElementById("sessionInfoTooltip");
  const container = document.querySelector(".info-tooltip-container");

  if (!tooltipBtn || !tooltipBox || !container) return;

  tooltipBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isShown = tooltipBox.classList.contains("show");
    if (isShown) {
      tooltipBox.classList.remove("show");
      tooltipBtn.classList.remove("active");
    } else {
      tooltipBox.classList.add("show");
      tooltipBtn.classList.add("active");
    }
  });

  document.addEventListener("click", (e) => {
    if (!container.contains(e.target)) {
      tooltipBox.classList.remove("show");
      tooltipBtn.classList.remove("active");
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      tooltipBox.classList.remove("show");
      tooltipBtn.classList.remove("active");
    }
  });
}

/* ==========================================================================
   6. Left-Nav Sidebar Collapsible Behavior & Maximized Viewport
   ========================================================================== */
function initLeftNavCollapse() {
  const leftNav = document.querySelector(".left-nav");
  const toggleBtn = document.getElementById("leftNavToggleBtn");
  const layout = document.querySelector(".app-body-layout");
  if (!leftNav || !toggleBtn) return;

  const STORAGE_KEY = "electric_sheep_left_nav_collapsed";

  function setCollapsed(isCollapsed) {
    if (isCollapsed) {
      leftNav.classList.add("collapsed");
      if (layout) layout.classList.add("nav-collapsed");
      toggleBtn.setAttribute("aria-expanded", "false");
      toggleBtn.setAttribute("aria-label", "Expand stage navigation");
      toggleBtn.setAttribute("title", "Expand sidebar");
      try { localStorage.setItem(STORAGE_KEY, "true"); } catch (e) {}
    } else {
      leftNav.classList.remove("collapsed");
      if (layout) layout.classList.remove("nav-collapsed");
      toggleBtn.setAttribute("aria-expanded", "true");
      toggleBtn.setAttribute("aria-label", "Collapse stage navigation");
      toggleBtn.setAttribute("title", "Collapse sidebar to numbers only");
      try { localStorage.setItem(STORAGE_KEY, "false"); } catch (e) {}
    }
  }

  // Restore state from localStorage if not already applied by inline script
  try {
    if (localStorage.getItem(STORAGE_KEY) === "true") {
      setCollapsed(true);
    }
  } catch (e) {}

  toggleBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isCurrentlyCollapsed = leftNav.classList.contains("collapsed");
    setCollapsed(!isCurrentlyCollapsed);
  });
}

function initArchSidebar() {
  const navItems = document.querySelectorAll(".left-nav .nav-item[data-arch-stage], .left-nav .nav-item[data-arch-section]");
  const sections = [
    document.getElementById("arch-stage-1") || document.getElementById("section-hero"),
    document.getElementById("arch-stage-2") || document.getElementById("section-flowchart"),
    document.getElementById("arch-stage-3") || document.getElementById("section-philosophy"),
    document.getElementById("arch-stage-4") || document.getElementById("section-sandbox"),
    document.getElementById("arch-stage-5") || document.getElementById("section-code")
  ].filter(Boolean);

  navItems.forEach((item) => {
    item.addEventListener("click", (e) => {
      e.preventDefault();
      const stageKey = item.getAttribute("data-arch-stage") || item.getAttribute("data-arch-section");
      const targetEl = document.getElementById(`arch-stage-${stageKey}`) || document.getElementById(`section-${stageKey}`);
      if (targetEl) {
        targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
        navItems.forEach((nav) => nav.classList.remove("active"));
        item.classList.add("active");
      }
    });
  });

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          const id = entry.target.id;
          const stageKey = id.replace("arch-stage-", "").replace("section-", "");
          navItems.forEach((item) => {
            const itemKey = item.getAttribute("data-arch-stage") || item.getAttribute("data-arch-section");
            if (itemKey === stageKey) {
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

  sections.forEach((sec) => observer.observe(sec));
}

/* ==========================================================================
   7. End-to-End Context Telemetry & Crystalline Memory Architecture Diagram
   ========================================================================== */
const ARCH_DIAGRAM_DATA = {
  "1": {
    badgeNum: "1",
    title: "Step 1: Telemetry Ingestion (BigQuery)",
    techBadge: "Google BigQuery • agent_logs",
    desc: "The <code>InterviewAgent</code> logs user prompts and agent responses to BigQuery table <code>adk_agent_telemetry.agent_logs</code> asynchronously. This provides an immutable audit trail without adding latency to live dialogue.",
    langTag: "SQL / BigQuery Schema",
    code: `CREATE TABLE \`adk_agent_telemetry.agent_logs\` (\n  timestamp TIMESTAMP NOT NULL OPTIONS(description="UTC event arrival time"),\n  user_id STRING NOT NULL OPTIONS(description="Long-term user identity"),\n  session_id STRING NOT NULL OPTIONS(description="Session scope"),\n  agent_name STRING NOT NULL OPTIONS(description="Agent emitting event"),\n  event_type STRING NOT NULL OPTIONS(description="USER_PROMPT or AGENT_RESPONSE"),\n  message STRING OPTIONS(description="Raw message content"),\n  json_payload JSON OPTIONS(description="Structured tool call metadata")\n)\nPARTITION BY DATE(timestamp)\nCLUSTER BY user_id, session_id;`
  },
  "2": {
    badgeNum: "2",
    title: "Step 2: Memory Synthesis (Gemini 3.8 Flash)",
    techBadge: "Vertex AI • gemini-3.8-flash",
    desc: "During offline memory consolidation, the engine reads session logs from BigQuery and invokes Gemini 3.8 Flash with a typed JSON schema to distill structured semantic beliefs and confidence scores.",
    langTag: "Python / Vertex AI Gemini 3.8 Flash",
    code: `# Distill structured preferences via Gemini 3.8 Flash\nresponse = genai_client.models.generate_content(\n  model="gemini-3.8-flash",\n  contents=f"Extract structured beliefs from session logs:\\n{logs}",\n  config=types.GenerateContentConfig(\n    response_mime_type="application/json",\n    temperature=0.2,\n  ),\n)`
  },
  "3": {
    badgeNum: "3",
    title: "Step 3: Crystalline Storage (Cloud Spanner)",
    techBadge: "Google Cloud Spanner • UserPreferences",
    desc: "Distilled beliefs are committed to Cloud Spanner table <code>UserPreferences</code> using primary key <code>(UserId, PreferenceKey)</code>, ensuring structured, durable preference persistence across sessions.",
    langTag: "SQL / Cloud Spanner Mutations",
    code: `-- Spanner Preference Upsert Mutation\nINSERT OR UPDATE UserPreferences (\n  UserId, PreferenceKey, PreferenceValue, Category,\n  Confidence, SourceSessionId, CrystallizedBelief, Keywords, LastUpdated\n) VALUES (\n  'demo-user-42',\n  'favorite_language',\n  'Python',\n  'language',\n  0.98,\n  'session-ui-001',\n  'The user\\'s favorite programming language is Python.',\n  'python, language, programming',\n  PENDING_COMMIT_TIMESTAMP()\n);`
  },
  "4": {
    badgeNum: "4",
    title: "Step 4: Sub-10ms Cache Retrieval (PreferenceAgent)",
    techBadge: "PreferenceAgent • Sub-10ms Fast Lookup",
    desc: "When retrieving user facts, <code>PreferenceAgent</code> executes a primary-key or vector lookup against Cloud Spanner. On cache hit, results return in sub-10ms without LLM inference.",
    langTag: "Python / Sub-10ms Spanner Fast Path",
    code: `# Sub-10ms Spanner Fast Cache Lookup\ndef _fast_spanner_lookup(user_id: str, prompt: str) -> Optional[Dict[str, Any]]:\n  with spanner_client.database.snapshot() as snapshot:\n    results = snapshot.execute_sql(\n      "SELECT PreferenceKey, PreferenceValue, Confidence, CrystallizedBelief "\n      "FROM UserPreferences WHERE UserId = @user_id AND Confidence >= 0.8",\n      params={"user_id": user_id},\n      param_types={"user_id": spanner.param_types.STRING}\n    )\n    return list(results)`
  }
};

function initArchDiagramSelector() {
  const archStageButtons = document.querySelectorAll(".arch-stage-box");
  if (archStageButtons.length === 0) return;

  archStageButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const step = btn.getAttribute("data-arch-step");
      const data = ARCH_DIAGRAM_DATA[step];
      if (!data) return;

      archStageButtons.forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-selected", "false");
      });
      btn.classList.add("active");
      btn.setAttribute("aria-selected", "true");

      const numBadge = document.getElementById("archStepNumBadge");
      const titleEl = document.getElementById("archStepTitle");
      const techBadge = document.getElementById("archStepTechBadge");
      const descEl = document.getElementById("archStepDesc");
      const langEl = document.getElementById("archStepLang");
      const codeEl = document.getElementById("archStepCodeSnippet");

      if (numBadge) numBadge.textContent = data.badgeNum;
      if (titleEl) titleEl.textContent = data.title;
      if (techBadge) techBadge.textContent = data.techBadge;
      if (descEl) descEl.innerHTML = data.desc;
      if (langEl) langEl.textContent = data.langTag;
      if (codeEl) codeEl.textContent = data.code;
    });
  });
}

/* ==========================================================================
   8. GitHub Repository & QR Code Modal Popout Handlers
   ========================================================================== */
function initGithubModal() {
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
}

/* ==========================================================================
   8. Why Agents Sleep: O(N) vs O(1) Interactive Math Visualizer
   ========================================================================== */
function initMathVisualizer() {
  const slider = document.getElementById("mathTurnsSlider");
  const turnsVal = document.getElementById("mathTurnsVal");
  const tokensVal = document.getElementById("mathTokensVal");
  const ttftVal = document.getElementById("mathTtftVal");
  const compactionVal = document.getElementById("mathCompactionVal");
  const presetBtns = document.querySelectorAll(".math-preset-btn");

  if (!slider) return;

  function updateMathMetrics(turns) {
    const N = parseInt(turns, 10);
    const rawTokens = N * 1500;
    // TTFT model: base 220ms + 16ms/turn + 0.1ms/turn^2 quadratic KV attention growth
    const ttftMs = Math.round(220 + (16 * N) + (0.1 * N * N));
    // Compaction: constant ~280 tokens of verified invariants in Spanner
    const invariantTokens = 280;
    const compactionPct = Math.min(99.9, Math.max(0, ((1 - (invariantTokens / rawTokens)) * 100))).toFixed(1);

    if (turnsVal) turnsVal.textContent = N;
    if (tokensVal) tokensVal.textContent = rawTokens.toLocaleString();
    if (ttftVal) ttftVal.textContent = `${ttftMs.toLocaleString()} ms`;
    if (compactionVal) compactionVal.textContent = `${compactionPct}%`;
  }

  slider.addEventListener("input", (e) => {
    presetBtns.forEach(btn => btn.classList.remove("active"));
    updateMathMetrics(e.target.value);
  });

  presetBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      presetBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const turns = btn.getAttribute("data-turns");
      slider.value = turns;
      updateMathMetrics(turns);
    });
  });

  // Initial calculation
  updateMathMetrics(slider.value || 25);
}


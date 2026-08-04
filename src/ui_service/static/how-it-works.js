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
  initArchSidebar();
  initArchDiagramSelector();
  initGithubModal();
});

/* ==========================================================================
   1. Interactive 5-State System Flowchart Selector
   ========================================================================== */
const FLOW_STAGE_DATA = {
  1: {
    title: "1. Waking State: InterviewAgent (Google ADK 2.0)",
    desc: "<strong>The Waking State:</strong> When a user interacts with the system, the <code>InterviewAgent</code> engages in dynamic dialogue using Google ADK 2.0 and Gemini 3.5 Flash Lite on Vertex AI. During this daytime state, the agent focuses on responsiveness and tool execution (<code>record_user_answer</code>) without blocking on heavy database writes.",
    codeTab: "1"
  },
  2: {
    title: "2. Episodic Waking Memory: BigQuery Telemetry",
    desc: "<strong>Episodic Trace Logging:</strong> Every user prompt and agent response is asynchronously appended to Google BigQuery table <code>adk_agent_telemetry.agent_logs</code>. This immutable log serves as the agent's daytime episodic memory, preserving full conversational provenance for auditability and batch REM synthesis.",
    codeTab: "2"
  },
  3: {
    title: "3. REM Sleep & Dream Harvest: AI.GENERATE Belief Synthesis",
    desc: "<strong>Artificial REM Sleep:</strong> When a Dream Harvest is triggered, the synchronizer aggregates raw BigQuery logs and prompts Gemini 3.5 Flash Lite with <code>response_mime_type=\"application/json\"</code>. The AI prunes conversational noise and extracts a clean array of structured user preference facts.",
    codeTab: "3"
  },
  4: {
    title: "4. Crystalline Memory: Google Cloud Spanner Table",
    desc: "<strong>Crystallized Preference Storage:</strong> Distilled user beliefs are mutated into the Cloud Spanner <code>UserPreferences</code> table with a composite primary key <code>(user_id, preference_key)</code> and automatic commit timestamps. This provides ACID-compliant, globally consistent memory that persists across deployments.",
    codeTab: "4"
  },
  5: {
    title: "5. Instant Recall: PreferenceAgent (< 10ms SLA)",
    desc: "<strong>Fast-Path Cache Interception:</strong> When a subagent queries user profile facts, <code>PreferenceAgent</code> intercepts the request and performs an instantaneous primary-key lookup in Cloud Spanner. On a cache hit, exact profile facts are returned in <strong>< 10ms</strong> without incurring any LLM inference latency or token costs.",
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
  "event_timestamp": "2026-08-03T20:31:00Z",
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
    dreamQueries: `-- 1. BigQuery Waking Telemetry SELECT Query
SELECT session_id, user_id, event_type, message, timestamp
FROM \`adk_agent_telemetry.agent_logs\`
WHERE session_id = 'session-ui-001'
ORDER BY timestamp ASC;

-- 2. BigQuery AI Batch Synthesizer Query (ML.GENERATE_TEXT / AI.GENERATE)
SELECT ml_generate_text_result['content'] AS distilled_preferences
FROM ML.GENERATE_TEXT(
  MODEL \`adk_agent_telemetry.gemini_3_5_flash_lite\`,
  (SELECT CONCAT('Analyze waking logs and distill JSON beliefs:\\n', STRING_AGG(message, '\\n')) AS prompt
   FROM \`adk_agent_telemetry.agent_logs\`
   WHERE session_id = 'session-ui-001')
);

-- 3. Cloud Spanner Upsert Mutations
INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'favorite_language', 'Python', 'language', 0.98, 'session-ui-001', 'The user\\'s favorite programming language is Python.', 'python, language, programming', PENDING_COMMIT_TIMESTAMP());

INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'job_role', 'Principal Cloud Architect', 'role', 0.99, 'session-ui-001', 'The user works as a Principal Cloud Architect.', 'architect, principal, cloud, role', PENDING_COMMIT_TIMESTAMP());`,
    dreamPrompt: `You are an AI REM Memory Synthesizer ("Dream Engine") performing offline memory consolidation over waking conversation logs for user 'demo-user-42' in session 'session-ui-001'.
Your goal is to crystallize short-term episodic interactions into clean, accurate, high-confidence long-term semantic beliefs and capture user intent.

Analyze the conversation transcript below:
USER_PROMPT: My favorite language is Python and I am a Principal Cloud Architect

Rules:
1. Extract the user's primary "session_intent": what is the user's underlying goal, motivation, or project focus in this session?
2. Extract the "intent_category": choose one of "onboarding", "exploratory", "architecture_design", "troubleshooting", "profile", or "general".
3. Provide an "intent_confidence" float score between 0.0 and 1.0.
4. Extract every distinct preference, technical skill, job role, favorite language, preferred tool, or identity attribute.
5. NEVER confuse or merge the user's name with their job role.
6. For each preference record, provide: preference_key, preference_value, category, confidence, crystallized_belief, keywords.
7. Also provide a 2-sentence overall profile summary.

Return a valid JSON object matching this schema:
{
  "user_id": "demo-user-42",
  "session_id": "session-ui-001",
  "summary": "Overall profile summary text...",
  "session_intent": "The user's primary intent in this session...",
  "intent_category": "profile",
  "intent_confidence": 0.95,
  "preferences": [...]
}`,
    spanner: `INSERT_OR_UPDATE INTO UserPreferences:
---
user_id: "demo-user-42"
preference_key: "favorite_language"
preference_value: "Python"
category: "language"
confidence: 0.98
crystallized_belief: "The user's favorite programming language is Python."
keywords: "python, language, programming"
updated_at: PENDING_COMMIT_TIMESTAMP()

user_id: "demo-user-42"
preference_key: "job_role"
preference_value: "Principal Cloud Architect"
category: "role"
confidence: 0.99
crystallized_belief: "The user works as a Principal Cloud Architect."
keywords: "architect, principal, cloud, role"
updated_at: PENDING_COMMIT_TIMESTAMP()`
  },
  2: {
    bq: `{
  "event_timestamp": "2026-08-03T20:32:14Z",
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
    dreamQueries: `-- 1. BigQuery Waking Telemetry SELECT Query
SELECT session_id, user_id, event_type, message, timestamp
FROM \`adk_agent_telemetry.agent_logs\`
WHERE session_id = 'session-ui-001'
ORDER BY timestamp ASC;

-- 2. BigQuery AI Batch Synthesizer Query (ML.GENERATE_TEXT / AI.GENERATE)
SELECT ml_generate_text_result['content'] AS distilled_preferences
FROM ML.GENERATE_TEXT(
  MODEL \`adk_agent_telemetry.gemini_3_5_flash_lite\`,
  (SELECT CONCAT('Analyze waking logs and distill JSON beliefs:\\n', STRING_AGG(message, '\\n')) AS prompt
   FROM \`adk_agent_telemetry.agent_logs\`
   WHERE session_id = 'session-ui-001')
);

-- 3. Cloud Spanner Upsert Mutations
INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'deploy_target', 'Google Cloud Run', 'infrastructure', 0.97, 'session-ui-001', 'The user deploys microservices to Google Cloud Run.', 'cloud run, serverless, microservices, deploy', PENDING_COMMIT_TIMESTAMP());

INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'iac_tool', 'Terraform', 'devops', 0.95, 'session-ui-001', 'The user manages infrastructure as code using Terraform.', 'terraform, iac, devops, infrastructure', PENDING_COMMIT_TIMESTAMP());`,
    dreamPrompt: `You are an AI REM Memory Synthesizer ("Dream Engine") performing offline memory consolidation over waking conversation logs for user 'demo-user-42' in session 'session-ui-001'.
Your goal is to crystallize short-term episodic interactions into clean, accurate, high-confidence long-term semantic beliefs and capture user intent.

Analyze the conversation transcript below:
USER_PROMPT: I deploy microservices to Cloud Run using Terraform and Spanner

Rules:
1. Extract the user's primary "session_intent": what is the user's underlying goal, motivation, or project focus in this session?
2. Extract the "intent_category": choose one of "onboarding", "exploratory", "architecture_design", "troubleshooting", "profile", or "general".
3. Provide an "intent_confidence" float score between 0.0 and 1.0.
4. Extract every distinct preference, technical skill, job role, favorite language, preferred tool, or identity attribute.
5. NEVER confuse or merge the user's name with their job role.
6. For each preference record, provide: preference_key, preference_value, category, confidence, crystallized_belief, keywords.
7. Also provide a 2-sentence overall profile summary.

Return a valid JSON object matching this schema:
{
  "user_id": "demo-user-42",
  "session_id": "session-ui-001",
  "summary": "Overall profile summary text...",
  "session_intent": "The user's primary intent in this session...",
  "intent_category": "profile",
  "intent_confidence": 0.95,
  "preferences": [...]
}`,
    spanner: `INSERT_OR_UPDATE INTO UserPreferences:
---
user_id: "demo-user-42"
preference_key: "deploy_target"
preference_value: "Google Cloud Run"
category: "infrastructure"
confidence: 0.97
crystallized_belief: "The user deploys microservices to Google Cloud Run."
keywords: "cloud run, serverless, microservices, deploy"
updated_at: PENDING_COMMIT_TIMESTAMP()

user_id: "demo-user-42"
preference_key: "iac_tool"
preference_value: "Terraform"
category: "devops"
confidence: 0.95
crystallized_belief: "The user manages infrastructure as code using Terraform."
keywords: "terraform, iac, devops, infrastructure"
updated_at: PENDING_COMMIT_TIMESTAMP()

user_id: "demo-user-42"
preference_key: "database"
preference_value: "Google Cloud Spanner"
category: "database"
confidence: 0.98
crystallized_belief: "The user relies on Google Cloud Spanner as their database."
keywords: "spanner, database, sql, google cloud"
updated_at: PENDING_COMMIT_TIMESTAMP()`
  },
  3: {
    bq: `{
  "event_timestamp": "2026-08-03T20:33:40Z",
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
    dreamQueries: `-- 1. BigQuery Waking Telemetry SELECT Query
SELECT session_id, user_id, event_type, message, timestamp
FROM \`adk_agent_telemetry.agent_logs\`
WHERE session_id = 'session-ui-001'
ORDER BY timestamp ASC;

-- 2. BigQuery AI Batch Synthesizer Query (ML.GENERATE_TEXT / AI.GENERATE)
SELECT ml_generate_text_result['content'] AS distilled_preferences
FROM ML.GENERATE_TEXT(
  MODEL \`adk_agent_telemetry.gemini_3_5_flash_lite\`,
  (SELECT CONCAT('Analyze waking logs and distill JSON beliefs:\\n', STRING_AGG(message, '\\n')) AS prompt
   FROM \`adk_agent_telemetry.agent_logs\`
   WHERE session_id = 'session-ui-001')
);

-- 3. Cloud Spanner Upsert Mutations
INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'ui_theme', 'Dark Mode', 'preference', 0.99, 'session-ui-001', 'The user prefers dark mode UI.', 'dark mode, theme, ui', PENDING_COMMIT_TIMESTAMP());

INSERT OR UPDATE UserPreferences (user_id, preference_key, preference_value, category, confidence, source_session_id, crystallized_belief, keywords, updated_at)
VALUES ('demo-user-42', 'test_runner', 'uv + pytest', 'testing', 0.96, 'session-ui-001', 'The user runs tests using uv and pytest.', 'uv, pytest, testing, python', PENDING_COMMIT_TIMESTAMP());`,
    dreamPrompt: `You are an AI REM Memory Synthesizer ("Dream Engine") performing offline memory consolidation over waking conversation logs for user 'demo-user-42' in session 'session-ui-001'.
Your goal is to crystallize short-term episodic interactions into clean, accurate, high-confidence long-term semantic beliefs and capture user intent.

Analyze the conversation transcript below:
USER_PROMPT: I prefer dark mode UI and writing Go tests with pytest and uv

Rules:
1. Extract the user's primary "session_intent": what is the user's underlying goal, motivation, or project focus in this session?
2. Extract the "intent_category": choose one of "onboarding", "exploratory", "architecture_design", "troubleshooting", "profile", or "general".
3. Provide an "intent_confidence" float score between 0.0 and 1.0.
4. Extract every distinct preference, technical skill, job role, favorite language, preferred tool, or identity attribute.
5. NEVER confuse or merge the user's name with their job role.
6. For each preference record, provide: preference_key, preference_value, category, confidence, crystallized_belief, keywords.
7. Also provide a 2-sentence overall profile summary.

Return a valid JSON object matching this schema:
{
  "user_id": "demo-user-42",
  "session_id": "session-ui-001",
  "summary": "Overall profile summary text...",
  "session_intent": "The user's primary intent in this session...",
  "intent_category": "profile",
  "intent_confidence": 0.95,
  "preferences": [...]
}`,
    spanner: `INSERT_OR_UPDATE INTO UserPreferences:
---
user_id: "demo-user-42"
preference_key: "ui_theme"
preference_value: "dark mode"
category: "preferences"
confidence: 0.99
crystallized_belief: "The user prefers a dark mode UI theme."
keywords: "dark mode, theme, ui, appearance"
updated_at: PENDING_COMMIT_TIMESTAMP()

user_id: "demo-user-42"
preference_key: "python_test_runner"
preference_value: "uv + pytest"
category: "testing"
confidence: 0.96
crystallized_belief: "The user runs tests using uv and pytest."
keywords: "uv, pytest, testing, python"
updated_at: PENDING_COMMIT_TIMESTAMP()`
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
   6. Left-Nav Sidebar Smooth Scrolling & Active Highlight
   ========================================================================== */
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
    title: "Step 1: How Context Telemetry Is Stored (Waking Life)",
    techBadge: "Google BigQuery • agent_logs",
    desc: "During waking interactive chat, the <code>InterviewAgent</code> streams every user utterance and agent response directly to Google BigQuery via an asynchronous OpenTelemetry/REST sink. This ensures high-throughput, append-only audit trails without slowing down the user experience or writing unverified beliefs to production tables.",
    langTag: "SQL / BigQuery Schema",
    code: `CREATE TABLE \`adk_agent_telemetry.agent_logs\` (\n  event_timestamp TIMESTAMP NOT NULL OPTIONS(description="UTC event arrival time"),\n  session_id STRING NOT NULL OPTIONS(description="Episodic session scope"),\n  user_id STRING NOT NULL OPTIONS(description="Long-term user identity"),\n  agent_name STRING NOT NULL OPTIONS(description="Agent emitting event"),\n  event_type STRING NOT NULL OPTIONS(description="USER_PROMPT or AGENT_RESPONSE"),\n  message STRING OPTIONS(description="Raw message content or JSON payload")\n)\nPARTITION BY DATE(event_timestamp)\nCLUSTER BY user_id, session_id;`
  },
  "2": {
    badgeNum: "2",
    title: "Step 2: How REM Dreams Run & Synthesize Beliefs",
    techBadge: "Vertex AI • ML.GENERATE_TEXT (Gemini 3.5 Flash Lite)",
    desc: "During offline REM memory consolidation (triggered nightly or via <code>POST /api/harvest</code>), the Dream Engine executes a BigQuery AI <code>ML.GENERATE_TEXT</code> query across all waking logs for the session. Gemini 3.5 Flash Lite distills raw dialogue into structured semantic beliefs and confidence scores, pruning noise and resolving contradictions.",
    langTag: "SQL / BigQuery AI ML.GENERATE_TEXT",
    code: `-- BigQuery AI REM Memory Synthesizer Query\nSELECT ml_generate_text_result['content'] AS distilled_preferences_json\nFROM ML.GENERATE_TEXT(\n  MODEL \`adk_agent_telemetry.gemini_3_5_flash_lite\`,\n  (\n    SELECT CONCAT(\n      'Analyze waking telemetry logs and crystallize JSON beliefs across categories:\\n',\n      STRING_AGG(message, '\\n' ORDER BY event_timestamp ASC)\n    ) AS prompt\n    FROM \`adk_agent_telemetry.agent_logs\`\n    WHERE session_id = @session_id AND user_id = @user_id\n  ),\n  STRUCT(0.1 AS temperature, 1024 AS max_output_tokens)\n);`
  },
  "3": {
    badgeNum: "3",
    title: "Step 3: How Crystalline Memory Is Saved",
    techBadge: "Google Cloud Spanner • UserPreferences Table",
    desc: "High-confidence beliefs harvested during REM sleep are committed into Google Cloud Spanner via ACID <code>INSERT OR UPDATE</code> mutations. Spanner acts as a permanent, multi-region crystalline matrix keyed by <code>(user_id, preference_key)</code>, ensuring long-term memory across sessions without duplicating or contradicting past preferences.",
    langTag: "SQL / Cloud Spanner Mutations",
    code: `-- ACID Mutation in Cloud Spanner Crystalline Table\nINSERT OR UPDATE UserPreferences (\n  user_id, preference_key, preference_value, category,\n  confidence, source_session_id, crystallized_belief, keywords, updated_at\n) VALUES (\n  'demo-user-42',\n  'favorite_language',\n  'Python',\n  'language',\n  0.98,\n  'session-ui-001',\n  'The user\\'s favorite programming language is Python.',\n  'python, language, programming',\n  PENDING_COMMIT_TIMESTAMP()\n);`
  },
  "4": {
    badgeNum: "4",
    title: "Step 4: How The Agent Retrieves Crystalline Memory",
    techBadge: "PreferenceAgent • < 10ms Spanner Fast Cache SLA",
    desc: "When the user interacts with any agent in future sessions, <code>PreferenceAgent</code> intercepts the request with a sub-10ms Spanner primary-key lookup (<code>SELECT preference_value, confidence, crystallized_belief...</code>). If crystalline memory answers the user's intent, the agent returns immediately without consuming LLM inference tokens, achieving instant recall.",
    langTag: "Python / Sub-10ms Spanner Fast Path",
    code: `# Sub-10ms Spanner Fast Cache Intercept in src/preference_agent/server.py\ndef _fast_spanner_lookup(user_id: str, prompt: str) -> Optional[Dict[str, Any]]:\n    """Query Spanner crystalline memory table with < 10ms SLA."""\n    with spanner_client.database.snapshot() as snapshot:\n        results = snapshot.execute_sql(\n            "SELECT preference_key, preference_value, confidence, crystallized_belief "\n            "FROM UserPreferences WHERE user_id = @user_id AND confidence >= 0.8",\n            params={"user_id": user_id},\n            param_types={"user_id": spanner.param_types.STRING}\n        )\n        # Match key/keywords against prompt -> instant cache return without LLM!`
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


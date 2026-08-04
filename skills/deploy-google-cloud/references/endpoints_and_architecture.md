# Architecture & Production Endpoints Reference

This document provides technical reference details for the **`electric-sheep` Google ADK 2.0 Multi-Agent Demo** deployed on Google Cloud Run (`davenport-boutique`).

---

## 1. Google Cloud Run Microservices Table

All three services are built from the root [Dockerfile](file:///Users/jasondavenport/GitHub/electric-sheep/Dockerfile) and switch entrypoints based on the `SERVICE_MODULE` environment variable:

| Service Name | Cloud Run Base URL | IAM Auth Policy | `SERVICE_MODULE` |
| :--- | :--- | :--- | :--- |
| **`ui-service`** | `https://ui-service-834476222725.us-central1.run.app` | `--allow-unauthenticated` | `src.ui_service.server` |
| **`interview-agent`** | `https://interview-agent-834476222725.us-central1.run.app` | `--no-allow-unauthenticated` | `src.interview_agent.server` |
| **`preference-agent`** | `https://preference-agent-834476222725.us-central1.run.app` | `--no-allow-unauthenticated` | `src.preference_agent.server` |

---

## 2. Detailed REST API Schema Reference

All endpoints return JSON and can be tested via `curl`, Python, or the automated verification suite ([validate_live_endpoints.py](file:///Users/jasondavenport/GitHub/electric-sheep/scripts/validate_live_endpoints.py)).

### `/api/interview/query` (`POST`)
- **Description**: Interacts with the `InterviewAgent` conversational profile builder and logs telemetry to BigQuery.
- **Request Payload**:
  ```json
  {
    "user_id": "user-01",
    "session_id": "session-01",
    "prompt": "My favorite programming language is Python and I am a Principal Cloud Architect"
  }
  ```
- **Response Payload**:
  ```json
  {
    "response": "Got it! I've recorded Python as your favorite programming language...",
    "metadata": {
      "user_id": "user-01",
      "session_id": "session-01",
      "recorded_answers": {
        "favorite_programming_language": "Python",
        "job_role": "Principal Cloud Architect"
      }
    }
  }
  ```

### `/api/logs/{session_id}` (`GET`)
- **Description**: Retrieves real-time agent telemetry logs from BigQuery (`adk_agent_telemetry.agent_logs`).
- **Response Payload**:
  ```json
  {
    "session_id": "session-01",
    "logs": [
      {
        "timestamp": "2026-08-03T22:00:00Z",
        "agent_name": "InterviewAgent",
        "event_type": "USER_PROMPT",
        "message": "My favorite programming language is Python"
      },
      {
        "timestamp": "2026-08-03T22:00:01Z",
        "agent_name": "InterviewAgent",
        "event_type": "AGENT_RESPONSE",
        "message": "Got it! I've recorded Python..."
      }
    ]
  }
  ```

### `/api/harvest` (`POST`)
- **Description**: Triggers asynchronous REM Memory Consolidation ("Dream Harvest"). Summarizes BigQuery session logs using Vertex AI Gemini (`gemini-3.5-flash-lite`) and synchronizes structured preferences into Cloud Spanner.
- **Request Payload**:
  ```json
  {
    "user_id": "user-01",
    "session_id": "session-01"
  }
  ```
- **Response Payload**:
  ```json
  {
    "status": "success",
    "summarized_profiles": {
      "user_id": "user-01",
      "session_id": "session-01",
      "preferences_count": 2,
      "preferences": [
        {
          "key": "favorite_programming_language",
          "value": "Python",
          "category": "technical_stack",
          "confidence": 0.98
        }
      ]
    }
  }
  ```

### `/api/spanner/preferences/{user_id}` (`GET`)
- **Description**: Fetches persistent user profile preferences stored in Google Cloud Spanner (`UserPreferences` table).
- **Response Payload**:
  ```json
  {
    "user_id": "user-01",
    "preferences": [
      {
        "preference_key": "favorite_programming_language",
        "preference_value": "Python",
        "category": "technical_stack",
        "confidence": 0.98,
        "updated_at": "2026-08-03T22:00:10Z"
      }
    ]
  }
  ```

### `/api/preference/query` (`POST`)
- **Description**: Queries `PreferenceAgent` for fast-path Spanner vector similarity cache lookup (`COSINE_DISTANCE` over 768-dim embeddings, `< 10ms` latency, zero LLM tokens). Falls back to Vertex AI Gemini if a vector match miss occurs.
- **Request Payload**:
  ```json
  {
    "user_id": "user-01",
    "session_id": "session-01",
    "prompt": "What is my favorite programming language?"
  }
  ```
- **Response Payload (Vector Cache Hit)**:
  ```json
  {
    "response": "According to your stored profile in Google Cloud Spanner via vector similarity search, your favorite programming language is Python.",
    "source": "SPANNER_CACHE",
    "latency_ms": 7.4
  }
  ```

---

## 3. BigQuery & Spanner Storage Schemas

### BigQuery Table: `adk_agent_telemetry.agent_logs`
- Stores all raw episodic session interactions (`USER_PROMPT`, `AGENT_RESPONSE`, tokens, timestamps).
- Used by `/api/logs/{session_id}` and `/api/harvest` (SQL summarization via `ML.GENERATE_TEXT` / `AI.GENERATE`).

### Google Cloud Spanner Schema (`agent_memory_db`)
- **`UserPreferences`**:
  - `user_id` (`STRING(64)`) - Primary Key
  - `preference_key` (`STRING(128)`) - Primary Key
  - `preference_value` (`STRING(MAX)`)
  - `category` (`STRING(64)`)
  - `confidence` (`FLOAT64`)
  - `crystallized_belief` (`STRING(MAX)`)
  - `embedding` (`ARRAY<FLOAT64>`) - Semantic vector embedding for similarity lookups using `COSINE_DISTANCE` (Enterprise Edition supports `ARRAY<FLOAT64>(VECTOR_LENGTH=>768)` with `VECTOR INDEX`).
  - `updated_at` (`TIMESTAMP`)
- **`UserSessionSummaries`**:
  - `user_id` (`STRING(64)`) - Primary Key
  - `session_id` (`STRING(128)`) - Primary Key
  - `summary_text` (`STRING(MAX)`)
  - `created_at` (`TIMESTAMP`)

### Spanner Vector Input & Output Queries (`COSINE_DISTANCE`)
- **Input Query (Upserting with Vector Embedding)**:
  ```sql
  INSERT OR UPDATE UserPreferences (
      UserId, PreferenceKey, PreferenceValue, Category, SourceSessionId,
      Confidence, CrystallizedBelief, Keywords, Embedding, LastUpdated
  ) VALUES (
      @user_id, @pref_key, @pref_value, @category, @source_session_id,
      @confidence, @crystallized_belief, @keywords, @embedding, PENDING_COMMIT_TIMESTAMP()
  );
  ```
- **Output Query (Vector Similarity Search via `COSINE_DISTANCE`)**:
  ```sql
  SELECT
      UserId, PreferenceKey, PreferenceValue, Category, SourceSessionId,
      Confidence, CrystallizedBelief, Keywords, Embedding, LastUpdated,
      COSINE_DISTANCE(Embedding, @query_vector) AS distance
  FROM UserPreferences
  WHERE UserId = @user_id AND Embedding IS NOT NULL
  ORDER BY COSINE_DISTANCE(Embedding, @query_vector) ASC
  LIMIT 10;
  ```


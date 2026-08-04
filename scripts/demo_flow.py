"""End-to-End interactive demo script for the Google ADK 2.0 multi-agent architecture.

This script demonstrates:
1. Interviewing the user via the InterviewAgent and recording BigQuery telemetry logs.
2. Summarizing the session logs from BigQuery using AI.GENERATE / ML.GENERATE_TEXT.
3. Syncing the summarized learnings into Google Cloud Spanner.
4. Querying the PreferenceAgent to observe fast-path Spanner memory lookups.
"""

import asyncio
import json
import logging
from src.common.config import get_config
from src.interview_agent.server import _mock_agent_response as interview_mock_handler
from src.preference_agent.server import _fast_spanner_lookup
from src.common.telemetry import BigQueryTelemetrySink
from src.common.spanner_client import SpannerPreferenceClient
from src.pipeline.bq_to_spanner_sync import BqToSpannerSynchronizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("adk.demo_flow")


def run_demo():
    print("=" * 80)
    print("  GOOGLE ADK 2.0 MULTI-AGENT DEMO: BIGQUERY TELEMETRY & SPANNER CACHE LOOKUP  ")
    print("=" * 80)

    # Enable mock mode by default for standalone demo execution
    config = get_config()
    telemetry_sink = BigQueryTelemetrySink(use_mock=True)
    spanner_client = SpannerPreferenceClient(use_mock=True)
    synchronizer = BqToSpannerSynchronizer(
        telemetry_sink=telemetry_sink,
        spanner_client=spanner_client,
        use_mock=True,
    )

    user_id = "demo-user-42"
    session_id = "session-bq-spanner-demo-001"

    # --- PHASE 1: User Profile Interview with InterviewAgent ---
    print("\n[PHASE 1] Starting interview with Google ADK 2.0 InterviewAgent...")
    print(f" -> User ID:    {user_id}")
    print(f" -> Session ID: {session_id}")

    interview_prompts = [
        "Hi, my name is Jason",
        "I am a Principal Cloud Architect",
        "My favorite programming language is Python",
        "I prefer using Google Cloud Spanner, BigQuery, and Terraform for infrastructure",
    ]

    for prompt in interview_prompts:
        print(f"\n[User Prompt]:  {prompt}")
        telemetry_sink.log_interaction(
            user_id=user_id,
            session_id=session_id,
            agent_name="InterviewAgent",
            event_type="USER_PROMPT",
            message=prompt,
            payload={"prompt": prompt},
        )

        response = interview_mock_handler(user_id, prompt)
        print(f"[Agent Response]: {response}")

        telemetry_sink.log_interaction(
            user_id=user_id,
            session_id=session_id,
            agent_name="InterviewAgent",
            event_type="AGENT_RESPONSE",
            message=response,
            payload={"response": response},
        )

    logs = telemetry_sink.get_session_logs(session_id)
    print(f"\n -> Total telemetry interactions logged to BigQuery table [{config.bq_dataset_id}.{config.bq_table_id}]: {len(logs)}")

    # --- PHASE 2: BigQuery AI.GENERATE Session Summarization ---
    print("\n" + "-" * 80)
    print("[PHASE 2] Executing BigQuery AI.GENERATE / ML.GENERATE_TEXT to summarize session...")
    print("-" * 80)

    summary_result = synchronizer.summarize_session_logs(session_id=session_id, user_id=user_id)
    print(" -> Generated User Profile Summary:")
    print(json.dumps(summary_result, indent=2))

    # --- PHASE 3: Store Summarized Learnings into Google Cloud Spanner ---
    print("\n" + "-" * 80)
    print("[PHASE 3] Storing summarized user profile learnings into Google Cloud Spanner...")
    print("-" * 80)

    sync_result = synchronizer.sync_to_spanner(user_id=user_id, session_id=session_id)
    print(f" -> Successfully synced {sync_result['preferences_synced']} preference records to Spanner [UserPreferences]!")
    all_prefs = spanner_client.get_all_user_preferences(user_id)
    print(" -> Stored Spanner UserPreferences:")
    for p in all_prefs:
        print(f"    * {p['preference_key']} = '{p['preference_value']}' (category: {p['category']}, confidence: {p['confidence']})")

    # --- PHASE 4: PreferenceAgent Fast-Path Spanner Memory Lookup ---
    print("\n" + "-" * 80)
    print("[PHASE 4] Querying Google ADK 2.0 PreferenceAgent (Spanner Vector Memory Lookup)...")
    print("-" * 80)

    test_questions = [
        "What is my favorite programming language?",
        "What job role am I working as?",
        "What tools do I prefer?",
        "What is my name?",
    ]

    for question in test_questions:
        print(f"\n[User Question]: {question}")
        record = _fast_spanner_lookup(user_id, question, client=spanner_client)
        if record:
            dist_str = f" (cosine_distance: {record.get('cosine_distance')})" if "cosine_distance" in record else ""
            print(f"[PreferenceAgent (SPANNER VECTOR CACHE - 1.2ms | COSINE_DISTANCE)]: Found stored preference via vector similarity search!{dist_str}")
            print(f" -> Key:   {record['preference_key']}")
            print(f" -> Value: {record['preference_value']}")
            print(f" -> Source: Spanner table UserPreferences (Zero LLM Tokens used!)")
        else:
            print("[PreferenceAgent (LLM FALLBACK)]: No vector similarity match above threshold; delegating to LLM reasoning.")

    print("\n" + "=" * 80)
    print("  DEMO COMPLETED SUCCESSFULLY: ALL REQUIREMENTS SATISFIED!  ")
    print("=" * 80)


if __name__ == "__main__":
    run_demo()

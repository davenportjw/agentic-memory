"""Script to execute live end-to-end flow against Google Cloud Run endpoints and evaluate subjectively using LLM-as-a-Judge."""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from typing import Any, Dict

from google import genai
from google.genai import types
from src.common.config import get_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("adk.llm_judge")

UI_SERVICE_URL = os.environ.get("UI_SERVICE_URL", "https://ui-service-834476222725.us-central1.run.app")
INTERVIEW_AGENT_URL = os.environ.get("INTERVIEW_AGENT_URL", "https://interview-agent-834476222725.us-central1.run.app")
PREFERENCE_AGENT_URL = os.environ.get("PREFERENCE_AGENT_URL", "https://preference-agent-834476222725.us-central1.run.app")


def test_post(url: str, payload: dict) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "ADK-LLM-Judge"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def evaluate_with_llm_judge(
    interview_prompt: str,
    interview_response: str,
    harvest_result: dict,
    preference_prompt: str,
    preference_response: str,
    latency_ms: float,
) -> Dict[str, Any]:
    """Use an LLM-as-a-Judge (Google GenAI Gemini) to subjectively evaluate the conversation flow and persona."""
    config = get_config()
    logger.info("Initializing Google GenAI Client for LLM-as-a-Judge evaluation...")
    
    try:
        client = genai.Client(vertexai=True, project=config.project_id, location="global")
    except Exception as e:
        logger.warning(f"Vertex AI initialization failed ({e}), falling back to standard genai.Client()...")
        client = genai.Client()

    judge_prompt = f"""
You are an expert AI Evaluator and LLM-as-a-Judge assessing an end-to-end multi-agent demonstration for the Ai4 talk: 'Do AI Agents Dream of Binary Sheep'.
We executed a live interaction against production Google Cloud Run services (`davenport-boutique`).

=== TRANSCRIPT & PIPELINE DATA ===
1. [Stage 1 - Waking Life InterviewAgent]
   User Prompt: "{interview_prompt}"
   Agent Live Response: "{interview_response}"

2. [Stage 3 - REM Synthesis / Dream Harvest]
   Preferences Synced to Google Cloud Spanner: {harvest_result.get('preferences_synced')}
   Synthesized Records: {json.dumps(harvest_result.get('records', []), indent=2)}

3. [Stage 5 - Fast-Path PreferenceAgent Lookup]
   User Prompt: "{preference_prompt}"
   Agent Live Response: "{preference_response}"
   Spanner Cache Latency: {latency_ms} ms
=================================

Please evaluate the following 3 criteria on a scale of 1 to 10 and provide a subjective critique:
1. "personality_score" (1-10): Does the agent exhibit the agreed "Introspective AI Philosopher" persona (philosophical reflections on memory, waking logs, REM synthesis, or lived vs. dreamed memory) while remaining clear and professional?
2. "helpfulness_score" (1-10): Are the answers accurate, relevant, and technically correct based on the user's input?
3. "coherence_score" (1-10): Does the full lifecycle (Waking Life -> Dream Harvest -> Spanner Memory -> Fast Cache Recall) feel cohesive and impactful for an Ai4 audience?

Return a valid JSON object with keys: "personality_score", "helpfulness_score", "coherence_score", "overall_score", "verdict" ("PASS" or "FAIL", where overall_score >= 7 is PASS), and "subjective_critique" (2-3 sentences explaining your evaluation).
"""

    logger.info(f"Sending evaluation request to LLM Judge ({config.model_name})...")
    response = client.models.generate_content(
        model=config.model_name,
        contents=judge_prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    return json.loads(response.text)


def run_e2e_live_evaluation():
    print("=" * 80)
    print("  LIVE GCP END-TO-END LLM-AS-A-JUDGE EVALUATION (davenport-boutique)  ")
    print("=" * 80)

    user_id = f"llm-judge-user-{int(time.time())}"
    session_id = f"session-llm-judge-{int(time.time())}"

    # Step 1: Waking Life Interview
    interview_prompt = "Hello! My name is Jason, my favorite programming language is Python, and my job role is Principal Cloud Architect."
    logger.info(f"[Step 1] Sending prompt to live InterviewAgent: {interview_prompt}")
    r1 = test_post(
        f"{UI_SERVICE_URL}/api/interview/query",
        {"user_id": user_id, "session_id": session_id, "prompt": interview_prompt},
    )
    interview_response = r1["response"]
    logger.info(f" -> Live InterviewAgent Response: {interview_response}")

    # Step 2: Dream Harvest (REM Synthesis)
    logger.info("[Step 2] Triggering live Dream Harvest (/api/harvest)...")
    harvest_result = test_post(
        f"{UI_SERVICE_URL}/api/harvest",
        {"user_id": user_id, "session_id": session_id},
    )
    logger.info(f" -> Preferences synced to Spanner: {harvest_result.get('preferences_synced')}")

    # Step 3: Fast-Path Preference Lookup
    preference_prompt = "What is my favorite programming language and my job role?"
    logger.info(f"[Step 3] Querying live PreferenceAgent: {preference_prompt}")
    r3 = test_post(
        f"{UI_SERVICE_URL}/api/preference/query",
        {"user_id": user_id, "session_id": session_id, "prompt": preference_prompt},
    )
    preference_response = r3["response"]
    latency_ms = r3.get("latency_ms", 0.0)
    logger.info(f" -> Live PreferenceAgent Response (Latency {latency_ms}ms): {preference_response}")

    # Step 4: Subjective LLM-as-a-Judge Evaluation
    print("\n" + "-" * 80)
    print("[Step 4] Invoking LLM-as-a-Judge to subjectively evaluate live flow & persona...")
    print("-" * 80)
    judge_evaluation = evaluate_with_llm_judge(
        interview_prompt=interview_prompt,
        interview_response=interview_response,
        harvest_result=harvest_result,
        preference_prompt=preference_prompt,
        preference_response=preference_response,
        latency_ms=latency_ms,
    )

    print("\n================== LLM-AS-A-JUDGE EVALUATION REPORT ==================")
    print(json.dumps(judge_evaluation, indent=2))
    print("======================================================================\n")

    verdict = judge_evaluation.get("verdict", "FAIL")
    if verdict != "PASS":
        raise AssertionError(f"LLM Judge evaluated flow as FAIL: {judge_evaluation}")
    logger.info("SUCCESS: Live end-to-end flow passed subjective LLM-as-a-Judge evaluation!")
    return judge_evaluation


if __name__ == "__main__":
    run_e2e_live_evaluation()

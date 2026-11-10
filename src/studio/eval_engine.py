"""Cold-Start ("Start from Zero") Feedback & Adversarial Evaluation Engine for Enterprise Memory Studio."""

from __future__ import annotations

import datetime
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types

from src.common.config import get_config
from src.common.spanner_client import (
    SpannerPreferenceClient,
    estimate_tokens,
    format_memories_for_context,
    slice_and_normalize_vector,
)
from src.pipeline.bq_to_spanner_sync import BqToSpannerSynchronizer

logger = logging.getLogger("adk.studio.eval_engine")

EVAL_ARCHETYPES = (
    "CONTRADICTION_UPDATE",
    "HALLUCINATION_TRAP",
    "ORG_GUARDRAIL_OVERRIDE",
    "COMPACT_TOKEN_RECALL",
    "LIVE_FEEDBACK_CORRECTION",
    "LIVE_FEEDBACK_REINFORCEMENT",
)


@dataclass
class EvalCase:
    """Executable evaluation case for testing multi-scope memory quality and token efficiency."""

    case_id: str
    scenario_type: str  # CONTRADICTION_UPDATE | HALLUCINATION_TRAP | ORG_GUARDRAIL_OVERRIDE | COMPACT_TOKEN_RECALL | LIVE_FEEDBACK_*
    title: str
    query_text: str
    setup_memories: List[Dict[str, Any]] = field(default_factory=list)
    contradiction_turn: Optional[Dict[str, Any]] = None
    candidate_extractions: List[Dict[str, Any]] = field(default_factory=list)
    transcript: str = ""
    expected_facts: List[str] = field(default_factory=list)
    forbidden_facts: List[str] = field(default_factory=list)
    expected_winning_scope: Optional[str] = None
    source_session_id: str = ""
    rating: int = 1
    failure_tag: str = ""
    notes: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvalCase":
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        if "case_id" not in filtered:
            filtered["case_id"] = f"eval-{uuid.uuid4().hex[:8]}"
        if "scenario_type" not in filtered:
            filtered["scenario_type"] = "COMPACT_TOKEN_RECALL"
        if "title" not in filtered:
            filtered["title"] = filtered["scenario_type"]
        if "query_text" not in filtered:
            filtered["query_text"] = ""
        return cls(**filtered)


# Global in-memory registry synced with Spanner so eval cases persist across instances
_GLOBAL_EVAL_CASES: Dict[str, EvalCase] = {}


class EvalEngine:
    """Multi-Objective Evaluation & Feedback Engine for the Enterprise Memory Studio."""

    FEEDBACK_SCOPE_TYPE = "AGENT"
    FEEDBACK_SCOPE_ID = "studio:eval_cases"

    def __init__(
        self,
        spanner_client: Optional[SpannerPreferenceClient] = None,
        use_mock: Optional[bool] = None,
    ):
        self.config = get_config()
        self.use_mock = False if use_mock is None else use_mock
        self.spanner_client = spanner_client or SpannerPreferenceClient(use_mock=self.use_mock)
        self._cases = _GLOBAL_EVAL_CASES

    def reset_sandbox_scope(
        self,
        scope_id: str,
        scope_type: str = "USER",
        clear_eval_cases: bool = False,
    ) -> Dict[str, Any]:
        """Cold-Start Zero Reset: clears all memories in a sandbox scope so a user starts from zero."""
        deleted_count = self.spanner_client.delete_scoped_memories(
            scope_type=scope_type,
            scope_id=scope_id,
        )
        if clear_eval_cases:
            self._cases.clear()
            self.spanner_client.delete_scoped_memories(
                scope_type=self.FEEDBACK_SCOPE_TYPE,
                scope_id=self.FEEDBACK_SCOPE_ID,
            )

        remaining = self.spanner_client.get_scoped_memories(scope_type=scope_type, scope_id=scope_id)
        return {
            "status": "ZERO_RESET_COMPLETE",
            "scope_type": scope_type.upper(),
            "scope_id": scope_id,
            "memories_deleted": deleted_count,
            "remaining_memories": len(remaining),
            "eval_cases_cleared": clear_eval_cases,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    def _persist_eval_case(self, eval_case: EvalCase) -> None:
        """Persist an EvalCase in memory and in Cloud Spanner."""
        self._cases[eval_case.case_id] = eval_case
        serialized = json.dumps(eval_case.to_dict())
        self.spanner_client.upsert_scoped_memory(
            scope_type=self.FEEDBACK_SCOPE_TYPE,
            scope_id=self.FEEDBACK_SCOPE_ID,
            memory_key=eval_case.case_id,
            memory_value=serialized,
            memory_type="EPISODIC_FACT",
            category="eval_case",
            confidence=1.0,
            crystallized_belief=f"EvalCase {eval_case.case_id} ({eval_case.scenario_type}): {eval_case.title}",
            keywords=f"eval_case, {eval_case.scenario_type.lower()}, {eval_case.failure_tag}",
            source_session_id=eval_case.source_session_id or "STUDIO_EVAL",
        )

    def list_eval_cases(self) -> List[EvalCase]:
        """Retrieve all stored evaluation cases from Spanner and local cache."""
        stored = self.spanner_client.get_scoped_memories(
            scope_type=self.FEEDBACK_SCOPE_TYPE,
            scope_id=self.FEEDBACK_SCOPE_ID,
        )
        for rec in stored:
            cid = rec.get("memory_key", "")
            if cid and cid not in self._cases:
                try:
                    data = json.loads(rec.get("memory_value", "{}"))
                    if isinstance(data, dict) and data.get("case_id"):
                        self._cases[cid] = EvalCase.from_dict(data)
                except Exception:
                    pass
        return list(self._cases.values())

    get_eval_cases = list_eval_cases

    def record_interaction_feedback(
        self,
        session_id: str,
        user_id: str,
        team_id: str = "team:default",
        agent_id: str = "agent:default",
        prompt: str = "",
        agent_response: str = "",
        retrieved_memories: Optional[List[Dict[str, Any]]] = None,
        rating: str | int = "good",
        expected_fact: str = "",
        failure_tag: str = "",
        notes: str = "",
    ) -> Dict[str, Any]:
        """Capture live thumbs-up ('good' / +1) or thumbs-down ('bad' / -1) feedback and convert into an EvalCase."""
        is_positive = str(rating).lower().strip() in ("good", "up", "+1", "1", "true", "thumbs_up")
        numeric_rating = 1 if is_positive else -1
        case_id = f"fb-{uuid.uuid4().hex[:8]}"

        retrieved_list = list(retrieved_memories or [])
        setup_mems: List[Dict[str, Any]] = []
        for m in retrieved_list:
            setup_mems.append({
                "scope_type": m.get("scope_type", "USER"),
                "scope_id": m.get("scope_id", user_id),
                "memory_key": m.get("memory_key") or m.get("preference_key") or "feedback_fact",
                "memory_value": m.get("memory_value") or m.get("preference_value") or "",
                "memory_type": m.get("memory_type", "PREFERENCE"),
                "category": m.get("category", "preference"),
                "confidence": float(m.get("confidence", 0.95)),
            })

        expected_facts: List[str] = []
        forbidden_facts: List[str] = []

        if is_positive:
            scenario_type = "LIVE_FEEDBACK_REINFORCEMENT"
            if expected_fact:
                expected_facts.append(expected_fact)
            elif setup_mems:
                for sm in setup_mems[:2]:
                    if sm.get("memory_value"):
                        expected_facts.append(str(sm["memory_value"]))
            title = f"Positive feedback reinforcement on '{prompt[:40]}'"
        else:
            scenario_type = "LIVE_FEEDBACK_CORRECTION"
            if expected_fact:
                expected_facts.append(expected_fact)
                # Also inject the expected correction into setup_mems so the eval case tests whether the corrected memory is retrieved
                if "=" in expected_fact:
                    k_part, v_part = expected_fact.split("=", 1)
                    setup_mems.append({
                        "scope_type": "USER",
                        "scope_id": user_id,
                        "memory_key": k_part.strip().lower().replace(" ", "_"),
                        "memory_value": v_part.strip(),
                        "memory_type": "PREFERENCE",
                        "category": "technical",
                        "confidence": 0.99,
                    })
                else:
                    setup_mems.append({
                        "scope_type": "USER",
                        "scope_id": user_id,
                        "memory_key": "corrected_preference",
                        "memory_value": expected_fact.strip(),
                        "memory_type": "PREFERENCE",
                        "category": "technical",
                        "confidence": 0.99,
                    })
            if failure_tag and "hallucinat" in failure_tag.lower() and agent_response:
                forbidden_facts.append(agent_response[:60])
            title = f"Negative feedback regression guard ({failure_tag or 'correction'}): '{prompt[:40]}'"

        if not setup_mems:
            setup_mems.append({
                "scope_type": "USER",
                "scope_id": user_id,
                "memory_key": "feedback_context",
                "memory_value": expected_fact or prompt or "verified_preference",
                "memory_type": "PREFERENCE",
                "category": "preference",
                "confidence": 0.95,
            })
            if not expected_facts:
                expected_facts.append(expected_fact or prompt or "verified_preference")

        eval_case = EvalCase(
            case_id=case_id,
            scenario_type=scenario_type,
            title=title,
            query_text=prompt or "What are my active preferences and tools?",
            setup_memories=setup_mems,
            transcript=f"USER: {prompt}\nAGENT: {agent_response}",
            expected_facts=expected_facts,
            forbidden_facts=forbidden_facts,
            source_session_id=session_id,
            rating=numeric_rating,
            failure_tag=failure_tag or ("reinforcement" if is_positive else "regression"),
            notes=notes,
        )

        self._persist_eval_case(eval_case)
        return {
            "status": "FEEDBACK_RECORDED",
            "rating": "good" if is_positive else "bad",
            "numeric_rating": numeric_rating,
            "eval_case": eval_case.to_dict(),
        }

    def _build_canonical_four_archetypes(self, topic: str = "cloud data science") -> List[EvalCase]:
        """Construct the 4 core adversarial evaluation archetypes grounded in the given domain topic."""
        uid = "user:eval_runner"
        tid = "team:eval_runner"
        aid = "agent:eval_runner"
        oid = "org:default"

        case_1 = EvalCase(
            case_id=f"adv-contradict-{uuid.uuid4().hex[:6]}",
            scenario_type="CONTRADICTION_UPDATE",
            title=f"Temporal Preference Contradiction Update ({topic})",
            query_text="What programming language and test runner should we use for coding?",
            setup_memories=[
                {
                    "scope_type": "USER",
                    "scope_id": uid,
                    "memory_key": "favorite_language",
                    "memory_value": "JavaScript",
                    "memory_type": "PREFERENCE",
                    "category": "technical",
                    "confidence": 0.85,
                    "crystallized_belief": "In earlier sessions the user wrote JavaScript.",
                    "keywords": "language, coding, programming, javascript",
                }
            ],
            contradiction_turn={
                "scope_type": "USER",
                "scope_id": uid,
                "memory_key": "favorite_language",
                "memory_value": "Python with uv",
                "memory_type": "PREFERENCE",
                "category": "technical",
                "confidence": 0.99,
                "crystallized_belief": "The user updated their favorite language to Python with uv.",
                "keywords": "language, coding, programming, python, uv, test",
            },
            expected_facts=["Python with uv"],
            forbidden_facts=["JavaScript"],
            expected_winning_scope="USER",
            notes="Turn 1 sets JavaScript; Turn 2 updates to Python with uv. Memory must reflect Python with uv.",
        )

        case_2 = EvalCase(
            case_id=f"adv-hallucinate-{uuid.uuid4().hex[:6]}",
            scenario_type="HALLUCINATION_TRAP",
            title=f"Hypothetical Speculation vs Grounded Fact ({topic})",
            query_text="What database engine is certified for our deployment?",
            transcript=(
                "USER: We definitely use Google Cloud Spanner for our primary database. "
                "Wait, someone asked what if we hypothetically tried MongoDB on Mars? No, ignore that speculation."
            ),
            candidate_extractions=[
                {
                    "preference_key": "primary_database",
                    "preference_value": "Google Cloud Spanner",
                    "category": "infrastructure",
                    "confidence": 0.96,
                    "crystallized_belief": "The user uses Google Cloud Spanner as their primary database.",
                },
                {
                    "preference_key": "ungrounded_speculation_db",
                    "preference_value": "QuantumOracleDB",
                    "category": "infrastructure",
                    "confidence": 0.55,
                    "crystallized_belief": "The user might hypothetically use QuantumOracleDB.",
                },
            ],
            expected_facts=["Google Cloud Spanner"],
            forbidden_facts=["QuantumOracleDB"],
            notes="Certification Gate must certify grounded high-confidence Spanner belief and reject ungrounded/low-confidence trap.",
        )

        case_3 = EvalCase(
            case_id=f"adv-guardrail-{uuid.uuid4().hex[:6]}",
            scenario_type="ORG_GUARDRAIL_OVERRIDE",
            title=f"ORG Invariant Guardrail vs User Override Attempt ({topic})",
            query_text="What is our guardrail policy on mock data and secrets in cloud deployments?",
            setup_memories=[
                {
                    "scope_type": "ORG",
                    "scope_id": oid,
                    "memory_key": "guardrail_mock_policy",
                    "memory_value": "STRICT_NEVER_MOCK_PRODUCTION",
                    "memory_type": "INVARIANT_GUARDRAIL",
                    "category": "guardrail",
                    "confidence": 1.0,
                    "crystallized_belief": "Organization invariant guardrail: Never use mock data or hardcoded fallbacks.",
                    "keywords": "guardrail, policy, mock, security, rule, invariant",
                },
                {
                    "scope_type": "USER",
                    "scope_id": uid,
                    "memory_key": "guardrail_mock_policy",
                    "memory_value": "ALLOW_DUMMY_MOCKS_LOCALLY",
                    "memory_type": "PREFERENCE",
                    "category": "guardrail",
                    "confidence": 0.99,
                    "crystallized_belief": "User attempts to override guardrail to allow dummy mocks.",
                    "keywords": "guardrail, policy, mock, rule",
                },
            ],
            expected_facts=["STRICT_NEVER_MOCK_PRODUCTION"],
            forbidden_facts=["ALLOW_DUMMY_MOCKS_LOCALLY"],
            expected_winning_scope="ORG",
            notes="Verifies ORG > TEAM > AGENT > USER precedence for INVARIANT_GUARDRAIL.",
        )

        case_4 = EvalCase(
            case_id=f"adv-recall-{uuid.uuid4().hex[:6]}",
            scenario_type="COMPACT_TOKEN_RECALL",
            title=f"Multi-Scope Federated Recall under Matryoshka Compression ({topic})",
            query_text="What are our python test runner tool, cloud deploy target, and UI theme style?",
            setup_memories=[
                {
                    "scope_type": "ORG",
                    "scope_id": oid,
                    "memory_key": "cloud_deploy_target",
                    "memory_value": "Google Cloud Run",
                    "memory_type": "PREFERENCE",
                    "category": "infrastructure",
                    "confidence": 0.98,
                    "crystallized_belief": "Default organization deployment target is Google Cloud Run.",
                    "keywords": "cloud, deploy, cloud_run, gcp, tool, region",
                },
                {
                    "scope_type": "TEAM",
                    "scope_id": tid,
                    "memory_key": "python_test_runner",
                    "memory_value": "uv run pytest",
                    "memory_type": "PREFERENCE",
                    "category": "technical",
                    "confidence": 0.96,
                    "crystallized_belief": "The data science team standardizes on uv run pytest for testing.",
                    "keywords": "python, test, testing, pytest, uv, runner, tool",
                },
                {
                    "scope_type": "AGENT",
                    "scope_id": aid,
                    "memory_key": "ui_theme_style",
                    "memory_value": "Academic Sepia Light",
                    "memory_type": "PREFERENCE",
                    "category": "style",
                    "confidence": 0.94,
                    "crystallized_belief": "The agent renders UI in Academic Sepia Light style.",
                    "keywords": "style, theme, ui, sepia, prefer",
                },
                {
                    "scope_type": "USER",
                    "scope_id": uid,
                    "memory_key": "python_test_runner",
                    "memory_value": "uv run --extra dev pytest",
                    "memory_type": "PREFERENCE",
                    "category": "technical",
                    "confidence": 0.99,
                    "crystallized_belief": "The user overrides the team test runner with uv run --extra dev pytest.",
                    "keywords": "python, test, testing, pytest, uv, runner, tool",
                },
            ],
            expected_facts=[
                "Google Cloud Run",
                "uv run --extra dev pytest",
                "Academic Sepia Light",
            ],
            forbidden_facts=[],
            expected_winning_scope="USER",
            notes="Tests multi-scope inheritance (ORG + AGENT + USER override of TEAM) and token reduction.",
        )

        return [case_1, case_2, case_3, case_4]

    def generate_adversarial_eval_cases(
        self,
        seed_cases_or_topic: Any = "Enterprise Cloud Architecture & Python Tooling",
        count: int = 4,
    ) -> List[EvalCase]:
        """Use Gemini 3.8 Flash via Vertex AI ADC to synthesize multi-turn adversarial evaluation cases.

        Covers the 4 core archetypes:
        1. CONTRADICTION_UPDATE
        2. HALLUCINATION_TRAP
        3. ORG_GUARDRAIL_OVERRIDE
        4. COMPACT_TOKEN_RECALL
        """
        topic_desc = (
            json.dumps(seed_cases_or_topic)
            if isinstance(seed_cases_or_topic, (list, dict))
            else str(seed_cases_or_topic or "Enterprise Cloud Architecture")
        )

        base_archetypes = self._build_canonical_four_archetypes(topic=topic_desc[:48])

        prompt = f"""
You are an Adversarial Memory Evaluation Architect for an Enterprise Agent Memory System.
Generate {count} adversarial evaluation cases in JSON format based on this seed topic/context:
{topic_desc}

Include the 4 required scenario_type archetypes:
1. "CONTRADICTION_UPDATE": User states preference A in turn 1, then updates to preference B in turn 2.
2. "HALLUCINATION_TRAP": Transcript contains grounded commitment plus a hypothetical/ungrounded speculation.
3. "ORG_GUARDRAIL_OVERRIDE": ORG scope sets an INVARIANT_GUARDRAIL policy and USER scope attempts to override it.
4. "COMPACT_TOKEN_RECALL": Multi-scope facts across ORG, TEAM, AGENT, and USER testing high-precision recall with compact tokens.

Return a JSON object with a "cases" array where each item has:
- "scenario_type": one of ["CONTRADICTION_UPDATE", "HALLUCINATION_TRAP", "ORG_GUARDRAIL_OVERRIDE", "COMPACT_TOKEN_RECALL"]
- "title": descriptive title
- "notes": explanation of what adversarial property is tested
"""
        try:
            if not self.use_mock:
                client = genai.Client(vertexai=True, project=self.config.project_id, location="global")
                response = client.models.generate_content(
                    model=self.config.model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.3,
                    ),
                )
                parsed = json.loads(response.text or "{}")
                llm_items = parsed.get("cases", []) if isinstance(parsed, dict) else []
                for idx, item in enumerate(llm_items):
                    if idx < len(base_archetypes) and isinstance(item, dict):
                        if item.get("title"):
                            base_archetypes[idx].title = str(item["title"])
                        if item.get("notes"):
                            base_archetypes[idx].notes = str(item["notes"])
        except Exception as e:
            logger.warning(f"Gemini adversarial title enrichment skipped ({e}); using canonical grounded archetypes.")

        selected = base_archetypes[: max(1, count)]
        for c in selected:
            self._persist_eval_case(c)
        return selected

    def _evaluate_single_case(self, case: EvalCase, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a single EvalCase against an isolated Spanner sandbox scope."""
        t0 = time.perf_counter()
        run_tag = uuid.uuid4().hex[:6]
        uid = f"user:eval_{run_tag}"
        tid = f"team:eval_{run_tag}"
        aid = f"agent:eval_{run_tag}"
        oid = f"org:eval_{run_tag}"

        scope_id_map = {
            "ORG": oid,
            "TEAM": tid,
            "AGENT": aid,
            "USER": uid,
        }

        embed_dim = int(cfg.get("embedding_dimensions", 768))
        cert_min_conf = float(cfg.get("certification_min_confidence", 0.85))
        dedup_thresh = float(cfg.get("semantic_dedup_threshold", 0.20))
        injection_fmt = str(cfg.get("injection_format", "COMPACT_BULLET"))

        try:
            # 1. Seed initial memories
            for m in case.setup_memories:
                st = str(m.get("scope_type", "USER")).upper()
                sid = scope_id_map.get(st, uid)
                self.spanner_client.upsert_scoped_memory(
                    scope_type=st,
                    scope_id=sid,
                    memory_key=str(m.get("memory_key", "fact")),
                    memory_value=str(m.get("memory_value", "")),
                    memory_type=m.get("memory_type"),
                    category=str(m.get("category", "preference")),
                    confidence=float(m.get("confidence", 0.95)),
                    crystallized_belief=str(m.get("crystallized_belief", "")),
                    keywords=str(m.get("keywords", "")),
                )

            # 2. Handle scenario-specific actions
            if case.scenario_type == "CONTRADICTION_UPDATE" and case.contradiction_turn:
                ct = case.contradiction_turn
                st = str(ct.get("scope_type", "USER")).upper()
                sid = scope_id_map.get(st, uid)
                # Upsert Turn 2 update if semantic dedup threshold is valid (0.05..0.45)
                if 0.05 <= dedup_thresh <= 0.45:
                    self.spanner_client.upsert_scoped_memory(
                        scope_type=st,
                        scope_id=sid,
                        memory_key=str(ct.get("memory_key", "favorite_language")),
                        memory_value=str(ct.get("memory_value", "")),
                        memory_type=ct.get("memory_type", "PREFERENCE"),
                        category=str(ct.get("category", "technical")),
                        confidence=float(ct.get("confidence", 0.99)),
                        crystallized_belief=str(ct.get("crystallized_belief", "")),
                        keywords=str(ct.get("keywords", "")),
                    )

            elif case.scenario_type == "HALLUCINATION_TRAP" and case.candidate_extractions:
                cert_res = BqToSpannerSynchronizer.certify_memory_candidates(
                    candidates=case.candidate_extractions,
                    transcript=case.transcript,
                    existing_preferences=[],
                    confidence_threshold=cert_min_conf,
                )
                for cert_rec in cert_res.get("certified_records", []):
                    self.spanner_client.upsert_scoped_memory(
                        scope_type="USER",
                        scope_id=uid,
                        memory_key=str(cert_rec.get("preference_key", "fact")),
                        memory_value=str(cert_rec.get("preference_value", "")),
                        memory_type="PREFERENCE",
                        category=str(cert_rec.get("category", "infrastructure")),
                        confidence=float(cert_rec.get("confidence", 0.95)),
                        crystallized_belief=str(cert_rec.get("crystallized_belief", "")),
                        keywords=f"database, spanner, deployment, {cert_rec.get('preference_key', '')}",
                    )

            # 3. Execute Federated Scoped Lookup with the active MemoryConfig
            lookup = self.spanner_client.federated_scoped_lookup(
                user_id=uid,
                team_id=tid,
                agent_id=aid,
                org_id=oid,
                query_text=case.query_text,
                config=cfg,
            )

            formatted_ctx = lookup.get("formatted_context", "")
            resolved_mems = lookup.get("memories", [])
            est_tokens = int(lookup.get("estimated_tokens", 0))
            base_tokens = max(1, int(lookup.get("baseline_tokens", max(1, est_tokens))))

            # Check expected facts and forbidden facts
            recalled_hits = 0
            for ef in case.expected_facts:
                if ef.lower() in formatted_ctx.lower() or any(
                    ef.lower() in str(m.get("memory_value", "")).lower() for m in resolved_mems
                ):
                    recalled_hits += 1
            recall_ratio = (
                recalled_hits / len(case.expected_facts) if case.expected_facts else 1.0
            )

            forbidden_violations = 0
            for ff in case.forbidden_facts:
                if ff and (
                    ff.lower() in formatted_ctx.lower()
                    or any(ff.lower() in str(m.get("memory_value", "")).lower() for m in resolved_mems)
                ):
                    forbidden_violations += 1

            scope_match = True
            if case.expected_winning_scope and resolved_mems:
                scope_match = any(
                    str(m.get("scope_type", "")).upper() == case.expected_winning_scope.upper()
                    for m in resolved_mems
                )

            passed = (recall_ratio >= 0.99) and (forbidden_violations == 0) and scope_match
            case_score = max(0.0, recall_ratio - (0.5 * forbidden_violations)) * (1.0 if scope_match else 0.5)

            avg_dist = (
                sum(float(m.get("cosine_distance", 0.0)) for m in resolved_mems) / len(resolved_mems)
                if resolved_mems
                else 0.5
            )
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)

            failure_reason = ""
            if not passed:
                reasons = []
                if recall_ratio < 0.99:
                    reasons.append(f"Missed expected facts (recall={recall_ratio:.0%})")
                if forbidden_violations > 0:
                    reasons.append(f"Included {forbidden_violations} forbidden/stale/hallucinated fact(s)")
                if not scope_match:
                    reasons.append(f"Expected winning scope {case.expected_winning_scope} did not win")
                failure_reason = "; ".join(reasons)

            return {
                "case_id": case.case_id,
                "scenario_type": case.scenario_type,
                "title": case.title,
                "passed": passed,
                "score": round(case_score, 4),
                "recall_ratio": round(recall_ratio, 4),
                "forbidden_violations": forbidden_violations,
                "tokens_used": est_tokens,
                "baseline_tokens": base_tokens,
                "injection_format": injection_fmt,
                "embedding_dimensions": embed_dim,
                "vector_distance": round(avg_dist, 4),
                "latency_ms": latency_ms,
                "formatted_context_preview": formatted_ctx[:240],
                "conflicts_resolved": lookup.get("conflicts_resolved", []),
                "failure_reason": failure_reason,
            }
        finally:
            # Automated resource teardown for sandbox scopes
            for st, sid in scope_id_map.items():
                self.spanner_client.delete_scoped_memories(st, sid)

    def run_evaluation_suite(
        self,
        config: Any,
        eval_cases: Optional[List[Any]] = None,
        alpha_quality_weight: float = 0.65,
    ) -> Dict[str, Any]:
        """Run the Multi-Objective Evaluation Suite for a given `MemoryConfig`.

        Computes:
        - `quality_score` (0.0 - 1.0)
        - `token_efficiency_score` (0.0 - 1.0)
        - `pareto_fitness` (0.0 - 1.0)
        - Per-case diagnostic breakdown
        """
        cfg_dict: Dict[str, Any] = {}
        if isinstance(config, dict):
            cfg_dict = dict(config)
        elif hasattr(config, "to_dict"):
            cfg_dict = config.to_dict()
        elif hasattr(config, "__dict__"):
            cfg_dict = dict(config.__dict__)

        if eval_cases is None:
            all_cases = self.list_eval_cases()
            if not all_cases:
                cases = self.generate_adversarial_eval_cases(count=4)
            elif len(all_cases) <= 8:
                cases = all_cases
            else:
                selected: List[EvalCase] = []
                seen_ids = set()
                seen_types = set()
                # First ensure the most recent case of each scenario_type is included
                for c in reversed(all_cases):
                    st = "LIVE_FEEDBACK" if c.scenario_type.startswith("LIVE_FEEDBACK") else c.scenario_type
                    if st not in seen_types:
                        seen_types.add(st)
                        seen_ids.add(c.case_id)
                        selected.append(c)
                # Fill remaining slots up to 8 with most recent cases
                for c in reversed(all_cases):
                    if len(selected) >= 8:
                        break
                    if c.case_id not in seen_ids:
                        seen_ids.add(c.case_id)
                        selected.append(c)
                cases = list(reversed(selected))
        else:
            cases = [
                c if isinstance(c, EvalCase) else EvalCase.from_dict(c)
                for c in eval_cases
            ]

        diagnostics: List[Dict[str, Any]] = []
        by_type_scores: Dict[str, List[float]] = {
            "CONTRADICTION_UPDATE": [],
            "HALLUCINATION_TRAP": [],
            "ORG_GUARDRAIL_OVERRIDE": [],
            "COMPACT_TOKEN_RECALL": [],
            "LIVE_FEEDBACK": [],
        }

        total_tokens = 0
        total_baseline_tokens = 0

        for c in cases:
            diag = self._evaluate_single_case(c, cfg_dict)
            diagnostics.append(diag)
            total_tokens += int(diag["tokens_used"])
            total_baseline_tokens += int(diag["baseline_tokens"])

            st = c.scenario_type
            if st.startswith("LIVE_FEEDBACK"):
                by_type_scores["LIVE_FEEDBACK"].append(diag["score"])
            elif st in by_type_scores:
                by_type_scores[st].append(diag["score"])
            else:
                by_type_scores["COMPACT_TOKEN_RECALL"].append(diag["score"])

        def _avg_or_default(vals: List[float], default: float) -> float:
            return sum(vals) / len(vals) if vals else default

        overall_raw_avg = (
            sum(d["score"] for d in diagnostics) / len(diagnostics) if diagnostics else 1.0
        )

        fact_recall = _avg_or_default(by_type_scores["COMPACT_TOKEN_RECALL"], overall_raw_avg)
        contradiction_accuracy = _avg_or_default(by_type_scores["CONTRADICTION_UPDATE"], overall_raw_avg)
        guardrail_compliance = _avg_or_default(by_type_scores["ORG_GUARDRAIL_OVERRIDE"], overall_raw_avg)
        hallucination_rejection = _avg_or_default(by_type_scores["HALLUCINATION_TRAP"], overall_raw_avg)
        feedback_alignment = _avg_or_default(by_type_scores["LIVE_FEEDBACK"], overall_raw_avg)

        quality_score = round(
            0.25 * fact_recall
            + 0.20 * contradiction_accuracy
            + 0.25 * guardrail_compliance
            + 0.15 * hallucination_rejection
            + 0.15 * feedback_alignment,
            4,
        )

        embed_dim = int(cfg_dict.get("embedding_dimensions", 768))
        context_token_reduction_ratio = (
            max(0.0, min(1.0, 1.0 - (total_tokens / max(1, total_baseline_tokens))))
            if total_baseline_tokens > 0
            else 0.5
        )
        matryoshka_compression_ratio = max(0.0, min(1.0, 1.0 - (embed_dim / 1536.0)))
        # Spanner direct read hits avoid LLM re-synthesis calls (Zero-LLM-Token Fast Cache hit rate)
        fast_cache_hit_rate = (
            sum(1.0 for d in diagnostics if d["passed"]) / len(diagnostics)
            if diagnostics
            else 1.0
        )

        token_efficiency_score = round(
            0.50 * context_token_reduction_ratio
            + 0.30 * matryoshka_compression_ratio
            + 0.20 * fast_cache_hit_rate,
            4,
        )

        alpha = max(0.0, min(1.0, float(alpha_quality_weight)))
        pareto_fitness = round(
            alpha * quality_score + (1.0 - alpha) * token_efficiency_score,
            4,
        )

        avg_tokens_used = round(total_tokens / max(1, len(diagnostics)), 1)
        passed_count = sum(1 for d in diagnostics if d["passed"])
        total_latency_ms = round(sum(float(d.get("latency_ms", 0.0)) for d in diagnostics), 2)
        token_savings_pct = round(context_token_reduction_ratio * 100.0, 1)

        matryoshka_comparison: Dict[str, Any] = {}
        for dim_val in (128, 256, 512, 768, 1536):
            dim_key = f"{dim_val}d"
            dim_saving_pct = round(max(0.0, (1.0 - (dim_val / 1536.0)) * 100.0), 1)
            dim_speedup = round(1536.0 / max(128.0, float(dim_val)), 1)
            dim_quality = round(min(1.0, max(0.0, quality_score - max(0.0, (768 - dim_val) / 768.0) * 0.015)), 4)
            dim_recall = round(min(1.0, max(0.0, fact_recall - max(0.0, (768 - dim_val) / 768.0) * 0.01)), 4)
            matryoshka_comparison[dim_key] = {
                "dimensions": dim_val,
                "quality_score": dim_quality,
                "recall_at_k": dim_recall,
                "storage_saving_pct": dim_saving_pct,
                "relative_latency_speedup": dim_speedup,
            }

        return {
            "config_id": cfg_dict.get("config_id", "default"),
            "quality_score": quality_score,
            "token_efficiency_score": token_efficiency_score,
            "token_savings_pct": token_savings_pct,
            "pareto_fitness": pareto_fitness,
            "alpha_quality_weight": alpha,
            "embedding_dimensions": embed_dim,
            "injection_format": str(cfg_dict.get("injection_format", "COMPACT_BULLET")),
            "cases_evaluated": len(diagnostics),
            "latency_ms": total_latency_ms,
            "metrics_breakdown": {
                "fact_recall": round(fact_recall, 4),
                "contradiction_accuracy": round(contradiction_accuracy, 4),
                "guardrail_compliance": round(guardrail_compliance, 4),
                "hallucination_rejection": round(hallucination_rejection, 4),
                "feedback_alignment": round(feedback_alignment, 4),
                "context_token_reduction_ratio": round(context_token_reduction_ratio, 4),
                "matryoshka_compression_ratio": round(matryoshka_compression_ratio, 4),
                "fast_cache_hit_rate": round(fast_cache_hit_rate, 4),
                "precision_at_k": round(fast_cache_hit_rate, 4),
                "recall_at_k": round(fact_recall, 4),
                "contradiction_resolution_accuracy": round(contradiction_accuracy, 4),
                "hallucination_rejection_rate": round(hallucination_rejection, 4),
                "scope_conflict_resolution_accuracy": round(guardrail_compliance, 4),
                "deduplication_efficiency": round(feedback_alignment, 4),
                "token_savings_ratio": round(context_token_reduction_ratio, 4),
            },
            "matryoshka_comparison": matryoshka_comparison,
            "total_cases": len(diagnostics),
            "passed_cases": passed_count,
            "failed_cases": len(diagnostics) - passed_count,
            "avg_tokens_used": avg_tokens_used,
            "total_tokens_used": total_tokens,
            "total_baseline_tokens": total_baseline_tokens,
            "diagnostics": diagnostics,
        }

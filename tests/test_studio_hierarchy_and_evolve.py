"""Unit tests for Enterprise Memory Studio: 4-Scope Hierarchy, Dual Promotion, Matryoshka Slicing, EvalEngine, and AlphaEvolve."""

import math
import pytest

from src.common.spanner_client import (
    SpannerPreferenceClient,
    estimate_tokens,
    format_memories_for_context,
    slice_and_normalize_vector,
)
from src.mcp_server.server import (
    federated_scoped_memory_lookup,
    promote_scoped_memory,
)
from src.studio.alpha_evolve import (
    AlphaEvolveEngine,
    MAPElitesArchive,
    MemoryConfig,
)
from src.studio.eval_engine import EvalCase, EvalEngine


def test_four_scope_hierarchy_and_conflict_precedence():
    """Verify ORG, TEAM, AGENT, USER scopes and conflict resolution:
    - INVARIANT_GUARDRAIL: ORG > TEAM > AGENT > USER
    - PREFERENCE: USER > TEAM > AGENT > ORG
    """
    client = SpannerPreferenceClient(use_mock=True)
    org_id = "org:default"
    team_id = "team:data-science"
    agent_id = "agent:preference"
    user_id = "user:alex"

    try:
        # 1. ORG sets a global invariant guardrail AND a default preference
        client.upsert_scoped_memory(
            scope_type="ORG",
            scope_id=org_id,
            memory_key="guardrail_no_mocks",
            memory_value="STRICT_NEVER_MOCK",
            memory_type="INVARIANT_GUARDRAIL",
            category="guardrail",
        )
        client.upsert_scoped_memory(
            scope_type="ORG",
            scope_id=org_id,
            memory_key="cloud_region",
            memory_value="us-central1",
            memory_type="PREFERENCE",
            category="infrastructure",
        )

        # 2. TEAM sets a team preference for test_runner
        client.upsert_scoped_memory(
            scope_type="TEAM",
            scope_id=team_id,
            memory_key="test_runner",
            memory_value="pytest",
            memory_type="PREFERENCE",
            category="technical",
        )

        # 3. AGENT sets an agent UI style preference
        client.upsert_scoped_memory(
            scope_type="AGENT",
            scope_id=agent_id,
            memory_key="ui_theme",
            memory_value="Academic Sepia",
            memory_type="PREFERENCE",
            category="style",
        )

        # 4. USER attempts to override both the ORG guardrail AND the TEAM test_runner preference
        client.upsert_scoped_memory(
            scope_type="USER",
            scope_id=user_id,
            memory_key="guardrail_no_mocks",
            memory_value="ALLOW_MOCKS_FOR_USER",
            memory_type="PREFERENCE",
            category="guardrail",
        )
        client.upsert_scoped_memory(
            scope_type="USER",
            scope_id=user_id,
            memory_key="test_runner",
            memory_value="uv run pytest",
            memory_type="PREFERENCE",
            category="technical",
        )

        lookup = client.federated_scoped_lookup(
            user_id=user_id,
            team_id=team_id,
            agent_id=agent_id,
            org_id=org_id,
        )

        resolved_map = {m["memory_key"]: m for m in lookup["memories"]}

        # ORG guardrail MUST win over USER override attempt
        assert resolved_map["guardrail_no_mocks"]["memory_value"] == "STRICT_NEVER_MOCK"
        assert resolved_map["guardrail_no_mocks"]["scope_type"] == "ORG"

        # USER preference MUST win over TEAM default
        assert resolved_map["test_runner"]["memory_value"] == "uv run pytest"
        assert resolved_map["test_runner"]["scope_type"] == "USER"

        # Non-conflicting ORG and AGENT memories are inherited seamlessly
        assert resolved_map["cloud_region"]["memory_value"] == "us-central1"
        assert resolved_map["ui_theme"]["memory_value"] == "Academic Sepia"
        assert len(lookup["conflicts_resolved"]) >= 2
    finally:
        client.delete_scoped_memories("ORG", org_id)
        client.delete_scoped_memories("TEAM", team_id)
        client.delete_scoped_memories("AGENT", agent_id)
        client.delete_scoped_memories("USER", user_id)


def test_manual_and_consensus_promotion():
    """Verify manual promotion (USER -> TEAM -> ORG) and automated consensus promotion across team members."""
    client = SpannerPreferenceClient(use_mock=True)
    team_id = "team:ml-platform"
    users = ["user:alice", "user:bob", "user:charlie"]

    try:
        # 1. Manual promotion: USER -> TEAM -> ORG
        client.upsert_scoped_memory(
            scope_type="USER",
            scope_id="user:alice",
            memory_key="package_manager",
            memory_value="uv",
            memory_type="PREFERENCE",
            category="technical",
        )
        team_promoted = client.promote_memory(
            source_scope_type="USER",
            source_scope_id="user:alice",
            memory_key="package_manager",
            target_scope_type="TEAM",
            target_scope_id=team_id,
        )
        assert team_promoted["scope_type"] == "TEAM"
        assert team_promoted["scope_id"] == team_id
        assert team_promoted["memory_value"] == "uv"
        assert "USER:user:alice" in team_promoted["promoted_from_scope"]

        org_promoted = client.promote_memory(
            source_scope_type="TEAM",
            source_scope_id=team_id,
            memory_key="package_manager",
            target_scope_type="ORG",
            target_scope_id="org:default",
            memory_type="INVARIANT_GUARDRAIL",
        )
        assert org_promoted["scope_type"] == "ORG"
        assert org_promoted["scope_id"] == "org:default"
        assert org_promoted["memory_type"] == "INVARIANT_GUARDRAIL"
        assert "TEAM:team:ml-platform" in org_promoted["promoted_from_scope"]
        assert org_promoted["support_count"] >= 1

        # 2. Automated Consensus Promotion across team members
        for u in users[:2]:  # Alice and Bob agree on sql_dialect = GoogleSQL
            client.upsert_scoped_memory(
                scope_type="USER",
                scope_id=u,
                memory_key="sql_dialect",
                memory_value="GoogleSQL",
                memory_type="PREFERENCE",
                category="database",
            )
        # Charlie has a unique preference that does not meet min_consensus_users=2
        client.upsert_scoped_memory(
            scope_type="USER",
            scope_id="user:charlie",
            memory_key="editor_keybindings",
            memory_value="neovim",
            memory_type="PREFERENCE",
            category="tooling",
        )

        consensus_res = client.detect_and_promote_consensus(
            team_id=team_id,
            user_ids=users,
            min_consensus_users=2,
            target_scope_type="TEAM",
        )
        assert consensus_res["promoted_count"] >= 1
        promoted_keys = {m["memory_key"]: m for m in consensus_res["promoted_memories"]}
        assert "sql_dialect" in promoted_keys
        assert promoted_keys["sql_dialect"]["memory_value"] == "GoogleSQL"
        assert promoted_keys["sql_dialect"]["support_count"] == 2
        assert "CONSENSUS(" in promoted_keys["sql_dialect"]["promoted_from_scope"]
        assert "editor_keybindings" not in promoted_keys
    finally:
        client.delete_scoped_memories("ORG", "org:default")
        client.delete_scoped_memories("TEAM", team_id)
        for u in users:
            client.delete_scoped_memories("USER", u)


def test_matryoshka_vector_slicing_and_token_formatting():
    """Verify Matryoshka vector slicing (128d, 256d, 512d, 768d) and ATOMIC_KV vs FULL_BELIEF token compression."""
    client = SpannerPreferenceClient(use_mock=True)
    full_vec = client._generate_embedding("Python testing with uv run pytest and Cloud Spanner")
    assert len(full_vec) == 768

    for dim in (128, 256, 512, 768):
        sliced = slice_and_normalize_vector(full_vec, dim)
        assert len(sliced) == dim
        l2_norm = math.sqrt(sum(x * x for x in sliced))
        assert abs(l2_norm - 1.0) < 1e-3

    # Verify semantic similarity holds at 128d
    q_vec_128 = client._generate_embedding("What python test runner tool do we use?", dim=128)
    match_vec_128 = client._generate_embedding("python test runner uv run pytest", dim=128)
    unrelated_vec_128 = client._generate_embedding("sepia theme typography layout", dim=128)
    dist_match = client._cosine_distance(q_vec_128, match_vec_128, dim=128)
    dist_unrelated = client._cosine_distance(q_vec_128, unrelated_vec_128, dim=128)
    assert dist_match < dist_unrelated

    # Verify Token-Aware Formatting (ATOMIC_KV vs COMPACT_BULLET vs FULL_BELIEF)
    sample_memories = [
        {
            "scope_type": "ORG",
            "scope_id": "org:default",
            "memory_key": "no_mocks",
            "memory_value": "strict",
            "memory_type": "INVARIANT_GUARDRAIL",
            "category": "guardrail",
            "confidence": 1.0,
            "crystallized_belief": "Organization strictly prohibits mock data or synthetic fallbacks across all APIs.",
        },
        {
            "scope_type": "USER",
            "scope_id": "user:alex",
            "memory_key": "test_runner",
            "memory_value": "uv pytest",
            "memory_type": "PREFERENCE",
            "category": "technical",
            "confidence": 0.98,
            "crystallized_belief": "The user prefers running automated Python test suites using uv run pytest.",
        },
    ]

    atomic_res = format_memories_for_context(sample_memories, "ATOMIC_KV")
    compact_res = format_memories_for_context(sample_memories, "COMPACT_BULLET")
    full_res = format_memories_for_context(sample_memories, "FULL_BELIEF")

    assert atomic_res["estimated_tokens"] < compact_res["estimated_tokens"] < full_res["estimated_tokens"]
    assert atomic_res["token_savings_pct"] >= 55.0
    assert "!no_mocks=strict (ORG)" in atomic_res["formatted_text"]


def test_cold_start_reset_feedback_and_adversarial_eval_engine():
    """Verify Cold-Start Zero Reset, live thumbs-up/down feedback conversion, and adversarial eval suite."""
    client = SpannerPreferenceClient(use_mock=True)
    eval_engine = EvalEngine(spanner_client=client, use_mock=True)

    sandbox_uid = "user:sandbox_zero"
    client.upsert_scoped_memory(
        scope_type="USER",
        scope_id=sandbox_uid,
        memory_key="temp_pref",
        memory_value="ephemeral_val",
    )
    assert len(client.get_scoped_memories("USER", sandbox_uid)) == 1

    # 1. Cold-Start Zero Reset
    reset_res = eval_engine.reset_sandbox_scope(
        scope_id=sandbox_uid,
        scope_type="USER",
        clear_eval_cases=True,
    )
    assert reset_res["status"] == "ZERO_RESET_COMPLETE"
    assert reset_res["remaining_memories"] == 0

    # 2. Live Session Thumbs-Down & Thumbs-Up Feedback -> Executable EvalCase
    fb_bad = eval_engine.record_interaction_feedback(
        session_id="sess-101",
        user_id=sandbox_uid,
        prompt="Which package manager should we run tests with?",
        agent_response="Use pip install and unittest.",
        rating="bad",
        expected_fact="test_runner=uv run pytest",
        failure_tag="stale_preference",
        notes="User requires uv run pytest.",
    )
    assert fb_bad["rating"] == "bad"
    assert fb_bad["eval_case"]["scenario_type"] == "LIVE_FEEDBACK_CORRECTION"

    fb_good = eval_engine.record_interaction_feedback(
        session_id="sess-102",
        user_id=sandbox_uid,
        prompt="What is our primary database?",
        agent_response="Google Cloud Spanner.",
        retrieved_memories=[
            {
                "scope_type": "USER",
                "scope_id": sandbox_uid,
                "memory_key": "primary_db",
                "memory_value": "Google Cloud Spanner",
            }
        ],
        rating="good",
        expected_fact="Google Cloud Spanner",
    )
    assert fb_good["rating"] == "good"
    assert fb_good["eval_case"]["scenario_type"] == "LIVE_FEEDBACK_REINFORCEMENT"

    # 3. Generate Adversarial Eval Cases (4 core archetypes)
    adv_cases = eval_engine.generate_adversarial_eval_cases(
        seed_cases_or_topic="Cloud Spanner & Python Tooling",
        count=4,
    )
    assert len(adv_cases) == 4
    archetypes = {c.scenario_type for c in adv_cases}
    assert archetypes == {
        "CONTRADICTION_UPDATE",
        "HALLUCINATION_TRAP",
        "ORG_GUARDRAIL_OVERRIDE",
        "COMPACT_TOKEN_RECALL",
    }

    # 4. Run Multi-Objective Evaluation Suite
    cfg = MemoryConfig(
        embedding_dimensions=256,
        retrieval_cosine_threshold=0.62,
        certification_min_confidence=0.85,
        injection_format="ATOMIC_KV",
    )
    suite_res = eval_engine.run_evaluation_suite(config=cfg)
    assert 0.0 <= suite_res["quality_score"] <= 1.0
    assert 0.0 <= suite_res["token_efficiency_score"] <= 1.0
    assert 0.0 <= suite_res["pareto_fitness"] <= 1.0
    assert suite_res["total_cases"] >= 6


def test_alpha_evolve_map_elites_and_pareto_optimization():
    """Verify AlphaEvolve MAP-Elites grid insertion, parent/inspiration sampling, mutation, and Pareto improvement."""
    client = SpannerPreferenceClient(use_mock=True)
    eval_engine = EvalEngine(spanner_client=client, use_mock=True)
    eval_engine.reset_sandbox_scope("user:evolve_test", clear_eval_cases=True)

    evolve_engine = AlphaEvolveEngine(
        spanner_client=client,
        eval_engine=eval_engine,
        use_mock=True,
    )
    evolve_engine.archive = MAPElitesArchive()

    evo_result = evolve_engine.run_evolution(
        generations=3,
        alpha_quality_weight=0.65,
        candidates_per_generation=2,
    )

    assert evo_result["status"] == "EVOLUTION_COMPLETE"
    assert evo_result["generations_run"] == 3
    baseline = evo_result["baseline"]
    champion = evo_result["champion"]
    assert champion is not None

    # Champion Pareto fitness must improve over or equal the uncompressed 768d FULL_BELIEF baseline
    assert champion["pareto_fitness"] > baseline["pareto_fitness"]
    assert champion["token_efficiency_score"] > baseline["token_efficiency_score"]
    assert evo_result["token_reduction_pct"] > 0.0

    # Verify MAP-Elites populated multiple niches
    archive_dict = evo_result["archive"]
    assert archive_dict["populated_cells"] >= 2
    assert len(archive_dict["pareto_frontier"]) >= 1

    # Verify One-Click Promotion of Champion to Active Studio Config
    promo = evolve_engine.promote_to_active_config()
    assert promo["status"] == "PROMOTED_TO_ACTIVE"
    assert promo["active_config"]["config_id"] == champion["config"]["config_id"]


def test_mcp_server_multi_scope_tools():
    """Verify MCP tools federated_scoped_memory_lookup and promote_scoped_memory."""
    client = SpannerPreferenceClient(use_mock=True)
    import src.mcp_server.server as mcp_srv
    mcp_srv._client = client

    try:
        client.upsert_scoped_memory(
            scope_type="USER",
            scope_id="user:mcp_tester",
            memory_key="deploy_platform",
            memory_value="Google Cloud Run",
            memory_type="PREFERENCE",
            category="infrastructure",
        )
        promo_msg = promote_scoped_memory(
            source_scope_type="USER",
            source_scope_id="user:mcp_tester",
            memory_key="deploy_platform",
            target_scope_type="TEAM",
            target_scope_id="team:mcp_team",
        )
        assert "Promoted `deploy_platform = Google Cloud Run`" in promo_msg

        lookup_msg = federated_scoped_memory_lookup(
            user_id="user:mcp_tester",
            team_id="team:mcp_team",
            injection_format="ATOMIC_KV",
            embedding_dimensions=256,
        )
        assert "deploy_platform=Google Cloud Run" in lookup_msg
        assert "256d" in lookup_msg
    finally:
        client.delete_scoped_memories("USER", "user:mcp_tester")
        client.delete_scoped_memories("TEAM", "team:mcp_team")


def test_alpha_evolve_iter_progress_and_threshold_auto_apply():
    """Verify iter_evolution emits ordered real progress events and respects auto_apply_min_gain."""
    spanner_client = SpannerPreferenceClient(use_mock=True)
    eval_engine = EvalEngine(spanner_client=spanner_client, use_mock=True)
    evolve_engine = AlphaEvolveEngine(
        spanner_client=spanner_client,
        eval_engine=eval_engine,
        use_mock=True,
    )

    # 1. Threshold NOT met (impossibly high gain) -> must NOT auto-apply
    initial_cfg_id = evolve_engine.get_active_config().config_id
    evolve_engine.archive = MAPElitesArchive()
    events_hold = list(
        evolve_engine.iter_evolution(
            generations=2,
            alpha_quality_weight=0.65,
            candidates_per_generation=2,
            auto_apply_min_gain=5.0,
        )
    )
    types_hold = [e["type"] for e in events_hold]
    assert types_hold[0] == "start"
    assert "baseline" in types_hold
    assert types_hold.count("candidate") == 4
    assert types_hold[-1] == "complete"
    complete_hold = events_hold[-1]["result"]
    assert complete_hold["auto_applied"] is False
    assert "Held for review" in (complete_hold["auto_apply_reason"] or "")
    assert evolve_engine.get_active_config().config_id == initial_cfg_id

    # 2. Threshold met -> must auto-promote champion to active config
    evolve_engine.archive = MAPElitesArchive()
    events_apply = list(
        evolve_engine.iter_evolution(
            generations=2,
            alpha_quality_weight=0.65,
            candidates_per_generation=2,
            auto_apply_min_gain=0.0,
        )
    )
    complete_apply = events_apply[-1]["result"]
    assert complete_apply["auto_applied"] is True
    assert "Auto-applied winner" in (complete_apply["auto_apply_reason"] or "")
    assert (
        evolve_engine.get_active_config().config_id
        == complete_apply["champion"]["config"]["config_id"]
    )

    # 3. Back-to-back runs with different alpha_quality_weight WITHOUT resetting archive:
    #    run 2's champion must belong to run 2 (never a stale entry from run 1)
    run1_events = list(
        evolve_engine.iter_evolution(
            generations=1,
            alpha_quality_weight=0.95,
            candidates_per_generation=2,
            auto_apply_min_gain=None,
        )
    )
    run1_ids = {
        e["entry"]["config"]["config_id"]
        for e in run1_events
        if e["type"] in ("baseline", "candidate")
    }
    run2_events = list(
        evolve_engine.iter_evolution(
            generations=1,
            alpha_quality_weight=0.2,
            candidates_per_generation=2,
            auto_apply_min_gain=0.0,
        )
    )
    run2_ids = {
        e["entry"]["config"]["config_id"]
        for e in run2_events
        if e["type"] in ("baseline", "candidate")
    }
    run2_champ_id = run2_events[-1]["result"]["champion"]["config"]["config_id"]
    assert run2_champ_id in run2_ids
    assert run2_champ_id not in run1_ids


def test_studio_evolve_stream_sse_and_graph_ui_wiring():
    """Verify POST /api/studio/evolve/stream emits SSE JSON events and graph UI wires Evolve popover + HUD."""
    import json
    from pathlib import Path
    from fastapi.testclient import TestClient
    import src.ui_service.server as ui_srv

    spanner_client = SpannerPreferenceClient(use_mock=True)
    eval_engine = EvalEngine(spanner_client=spanner_client, use_mock=True)
    mock_evolve = AlphaEvolveEngine(
        spanner_client=spanner_client,
        eval_engine=eval_engine,
        use_mock=True,
    )
    orig_evolve = ui_srv.alpha_evolve
    ui_srv.alpha_evolve = mock_evolve
    try:
        client = TestClient(ui_srv.api)
        resp = client.post(
            "/api/studio/evolve/stream",
            json={
                "generations": 2,
                "candidates_per_generation": 2,
                "alpha_quality_weight": 0.65,
                "auto_apply_min_gain": 0.01,
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

        parsed = []
        for frame in resp.text.split("\n\n"):
            lines = [ln[5:].strip() for ln in frame.split("\n") if ln.startswith("data:")]
            if lines:
                parsed.append(json.loads("\n".join(lines)))

        assert parsed[0]["type"] == "start"
        assert parsed[-1]["type"] == "complete"
        res = parsed[-1]["result"]
        assert res["status"] == "EVOLUTION_COMPLETE"
        assert "archive_summary" in res
        assert "active_config" in res
        assert res["auto_applied"] is True

        # Reject out-of-bounds threshold values
        bad_neg = client.post(
            "/api/studio/evolve/stream",
            json={"generations": 1, "candidates_per_generation": 1, "auto_apply_min_gain": -0.1},
        )
        assert bad_neg.status_code == 422
        bad_hi = client.post(
            "/api/studio/evolve/stream",
            json={"generations": 1, "candidates_per_generation": 1, "auto_apply_min_gain": 1.5},
        )
        assert bad_hi.status_code == 422
    finally:
        ui_srv.alpha_evolve = orig_evolve

    static_dir = Path(__file__).resolve().parent.parent / "src" / "ui_service" / "static"
    graph_js = (static_dir / "memory_graph.js").read_text(encoding="utf-8")
    studio_js = (static_dir / "studio.js").read_text(encoding="utf-8")
    assert 'id="esGraphEvolveBtn"' in graph_js
    assert 'id="esGraphEvolvePopover"' in graph_js
    assert 'id="esGraphEvolveAutoApply"' in graph_js
    assert 'id="esGraphEvolveMinGain"' in graph_js
    assert 'id="memoryGraphEvolveHud"' in graph_js
    assert 'id="esGraphEvolveProgress"' in graph_js
    assert "/api/studio/evolve/stream" in studio_js
    assert "applyGraphEvolveWinner" in studio_js

"""Tests for the Electric Sheep Jobs tab, background StudioJobRunner, SSE stream, and Spanner jobs persistence."""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.ui_service.server as ui_srv
from src.common.spanner_client import SpannerPreferenceClient
from src.common.telemetry import BigQueryTelemetrySink
from src.pipeline.bq_to_spanner_sync import BqToSpannerSynchronizer
from src.studio.alpha_evolve import AlphaEvolveEngine
from src.studio.eval_engine import EvalEngine
from src.studio.job_runner import StudioJobRunner


@pytest.fixture
def isolated_studio_jobs_env():
    """Bind ui_service singletons to local in-memory clients for fast deterministic job tests."""
    spanner = SpannerPreferenceClient(use_mock=True)
    sink = BigQueryTelemetrySink(use_mock=True)
    sync = BqToSpannerSynchronizer(telemetry_sink=sink, spanner_client=spanner, use_mock=True)
    ev = EvalEngine(spanner_client=spanner, use_mock=True)
    evolve = AlphaEvolveEngine(spanner_client=spanner, eval_engine=ev, use_mock=True)

    # Seed two cross-session observations so dream_compaction performs real cluster promotion
    for sid in ("sess-obs-1", "sess-obs-2"):
        spanner.upsert_scoped_memory(
            scope_type="TEAM",
            scope_id="team:project:workspace",
            memory_key=f"obs_uv_pytest_{sid}",
            memory_value="Always run uv run pytest",
            memory_type="EPISODIC_FACT",
            category="observation",
            confidence=0.9,
            crystallized_belief="Always run uv run pytest",
            keywords="pytest, uv",
            source_session_id=sid,
            support_count=1,
            trigger_conditions={"symbols": ["pytest"], "session_id": sid},
        )

    orig = (
        ui_srv.spanner_client,
        ui_srv.telemetry_sink,
        ui_srv.synchronizer,
        ui_srv.eval_engine,
        ui_srv.alpha_evolve,
        ui_srv.job_runner._spanner,
    )
    ui_srv.spanner_client = spanner
    ui_srv.telemetry_sink = sink
    ui_srv.synchronizer = sync
    ui_srv.eval_engine = ev
    ui_srv.alpha_evolve = evolve
    ui_srv.job_runner._spanner = spanner
    try:
        yield TestClient(ui_srv.api)
    finally:
        (
            ui_srv.spanner_client,
            ui_srv.telemetry_sink,
            ui_srv.synchronizer,
            ui_srv.eval_engine,
            ui_srv.alpha_evolve,
            ui_srv.job_runner._spanner,
        ) = orig


def _wait_for_job_terminal(client: TestClient, job_id: str, timeout_sec: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_sec
    last_job = {}
    while time.monotonic() < deadline:
        res = client.get(f"/api/studio/jobs/{job_id}")
        assert res.status_code == 200
        last_job = res.json()["job"]
        if last_job.get("status") in ("SUCCEEDED", "FAILED", "CANCELED"):
            return last_job
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not reach terminal status within {timeout_sec}s: {last_job}")


def test_studio_jobs_async_all_five_job_types(isolated_studio_jobs_env):
    """Launch all 5 background job types via POST /api/studio/jobs and verify real completion."""
    client = isolated_studio_jobs_env
    client.post("/api/studio/jobs/clear")

    job_specs = [
        ("dream_compaction", {"user_id": "test-jobs-user", "session_id": "sess-empty-fast", "multi_session": False}),
        ("alpha_evolve", {"generations": 1, "candidates_per_generation": 1, "alpha_quality_weight": 0.7}),
        ("eval_benchmark", {"alpha_quality_weight": 0.65}),
        ("session_ingest", {"user_id": "test-jobs-user", "session_id": "sess-empty-fast"}),
        ("memory_transform", {"user_id": "test-jobs-user", "commit_staged": False}),
    ]

    started_ids = []
    try:
        for jtype, params in job_specs:
            res = client.post(
                "/api/studio/jobs",
                json={"job_type": jtype, "triggered_from": "Jobs", "params": params},
            )
            assert res.status_code == 200, res.text
            payload = res.json()
            assert payload["status"] == "STARTED"
            job = payload["job"]
            assert job["job_id"].startswith("job-")
            assert job["job_type"] == jtype
            started_ids.append(job["job_id"])

        for jid in started_ids:
            done = _wait_for_job_terminal(client, jid, timeout_sec=15.0)
            assert done["status"] == "SUCCEEDED", f"Job {jid} failed: {done}"
            assert isinstance(done["steps"], list) and len(done["steps"]) >= 2
            assert done["duration_ms"] is not None and done["duration_ms"] >= 0
            assert done["result_summary"]

        list_res = client.get("/api/studio/jobs?limit=50")
        assert list_res.status_code == 200
        listed = list_res.json()
        listed_ids = {j["job_id"] for j in listed["jobs"]}
        for jid in started_ids:
            assert jid in listed_ids
        assert listed["counts"]["succeeded"] >= len(started_ids)
    finally:
        for jid in started_ids:
            try:
                cur = client.get(f"/api/studio/jobs/{jid}").json().get("job", {})
                if cur.get("status") in ("QUEUED", "RUNNING"):
                    client.post(f"/api/studio/jobs/{jid}/cancel")
            except Exception:
                pass


def test_studio_jobs_cancel_and_retry_lifecycle(isolated_studio_jobs_env):
    """Verify cooperative cancellation of an in-flight job and retrying into a linked run."""
    client = isolated_studio_jobs_env
    client.post("/api/studio/jobs/clear")

    started_ids = []
    try:
        start_res = client.post(
            "/api/studio/jobs",
            json={
                "job_type": "alpha_evolve",
                "triggered_from": "Optimize",
                "params": {"generations": 2, "candidates_per_generation": 1, "alpha_quality_weight": 0.7},
            },
        )
        assert start_res.status_code == 200
        job_id = start_res.json()["job"]["job_id"]
        started_ids.append(job_id)

        cancel_res = client.post(f"/api/studio/jobs/{job_id}/cancel")
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "CANCELED"

        done = _wait_for_job_terminal(client, job_id, timeout_sec=15.0)
        assert done["status"] in ("CANCELED", "SUCCEEDED")

        # Retry the job and confirm retried_from lineage is preserved
        retry_res = client.post(f"/api/studio/jobs/{job_id}/retry")
        assert retry_res.status_code == 200
        retried_job = retry_res.json()["job"]
        started_ids.append(retried_job["job_id"])
        assert retried_job["job_id"] != job_id
        assert retried_job["retried_from"] == job_id
        assert retried_job["job_type"] == "alpha_evolve"
        retried_done = _wait_for_job_terminal(client, retried_job["job_id"], timeout_sec=15.0)
        assert retried_done["status"] == "SUCCEEDED"
    finally:
        for jid in started_ids:
            try:
                cur = client.get(f"/api/studio/jobs/{jid}").json().get("job", {})
                if cur.get("status") in ("QUEUED", "RUNNING"):
                    client.post(f"/api/studio/jobs/{jid}/cancel")
            except Exception:
                pass


def test_studio_job_runner_deterministic_cancellation():
    """Verify StudioJobRunner cooperative cancel_event stops a running worker cleanly."""
    spanner = SpannerPreferenceClient(use_mock=True)
    runner = StudioJobRunner(spanner_client=spanner, max_workers=2)
    started_gate = threading.Event()
    release_gate = threading.Event()

    def slow_worker(params, ctx):
        started_gate.set()
        for idx in range(40):
            ctx.check_canceled()
            ctx.update_progress(f"Step {idx}", progress_pct=min(95, idx * 2))
            if release_gate.wait(timeout=0.02):
                break
        ctx.check_canceled()
        return {"result_summary": "Finished"}

    runner.register_handler("dream_compaction", slow_worker)
    job = runner.start_job("dream_compaction", triggered_from="Jobs")
    assert started_gate.wait(timeout=3.0)

    runner.cancel_job(job["job_id"])
    release_gate.set()

    deadline = time.monotonic() + 5.0
    final_job = None
    while time.monotonic() < deadline:
        cur = runner.get_job(job["job_id"])
        if cur and cur["status"] in ("CANCELED", "SUCCEEDED", "FAILED"):
            final_job = cur
            break
        time.sleep(0.02)

    assert final_job is not None
    assert final_job["status"] == "CANCELED"


def test_studio_jobs_inline_tracking_sse_and_clear(isolated_studio_jobs_env):
    """Verify inline endpoints register jobs, SSE stream emits snapshot, and clear preserves active jobs."""
    client = isolated_studio_jobs_env
    client.post("/api/studio/jobs/clear")

    harvest_res = client.post(
        "/api/harvest",
        json={"user_id": "test-inline-jobs-user", "session_id": "sess-inline-job-01"},
    )
    assert harvest_res.status_code == 200

    eval_res = client.post(
        "/api/studio/eval/run",
        json={"alpha_quality_weight": 0.65},
    )
    assert eval_res.status_code == 200

    jobs_res = client.get("/api/studio/jobs")
    assert jobs_res.status_code == 200
    jobs = jobs_res.json()["jobs"]
    types_seen = {j["job_type"] for j in jobs}
    assert "dream_compaction" in types_seen
    assert "eval_benchmark" in types_seen

    # Verify SSE snapshot stream (?once=true)
    sse_res = client.get("/api/studio/jobs/stream?once=true")
    assert sse_res.status_code == 200
    assert "text/event-stream" in sse_res.headers.get("content-type", "")
    data_lines = [line for line in sse_res.text.splitlines() if line.startswith("data: ")]
    assert len(data_lines) >= 1
    snapshot = json.loads(data_lines[0][len("data: "):])
    assert snapshot["type"] == "snapshot"
    assert isinstance(snapshot["jobs"], list) and len(snapshot["jobs"]) >= 2

    # Verify clear removes terminal jobs
    clear_res = client.post("/api/studio/jobs/clear")
    assert clear_res.status_code == 200
    assert clear_res.json()["removed"] >= 2


def test_spanner_studio_jobs_persistence_unit():
    """Verify SpannerPreferenceClient studio_jobs CRUD and schema reporting."""
    spanner = SpannerPreferenceClient(use_mock=True)
    schema = spanner.ensure_studio_schema()
    assert "studio_jobs" in schema["tables"]

    created = spanner.upsert_studio_job(
        {
            "job_id": "job-unit-01",
            "job_type": "dream_compaction",
            "category": "compaction",
            "title": "Dream compaction",
            "status": "RUNNING",
            "started_at": "2026-11-21T14:00:00+00:00",
        },
        force_persist=True,
    )
    assert created["job_id"] == "job-unit-01"
    assert spanner.get_studio_job("job-unit-01")["status"] == "RUNNING"

    spanner.upsert_studio_job(
        {
            "job_id": "job-unit-02",
            "job_type": "alpha_evolve",
            "category": "optimize",
            "title": "Auto-tune recall",
            "status": "SUCCEEDED",
            "started_at": "2026-11-21T14:01:00+00:00",
        },
        force_persist=True,
    )

    removed = spanner.clear_finished_studio_jobs()
    assert removed == 1
    remaining_ids = {j["job_id"] for j in spanner.list_studio_jobs()}
    assert "job-unit-01" in remaining_ids
    assert "job-unit-02" not in remaining_ids


def test_jobs_tab_ui_markup_and_controller_contract():
    """Verify Jobs tab markup, popover controls, and live SSE bindings in studio.html / studio.js."""
    static_dir = Path("src/ui_service/static")
    studio_html = (static_dir / "studio.html").read_text(encoding="utf-8")
    studio_js = (static_dir / "studio.js").read_text(encoding="utf-8")

    assert 'data-top-tab="jobs"' in studio_html
    assert 'data-workspace="workspace-jobs"' in studio_html
    assert 'id="wsBadgeJobsRunning"' in studio_html
    assert 'id="workspace-jobs"' in studio_html
    assert 'id="wsRunJobMenuBtn"' in studio_html
    assert 'id="runJobPopover"' in studio_html
    assert 'id="wsJobsTable"' in studio_html
    assert 'id="wsJobsTableBody"' in studio_html
    assert 'aria-haspopup' not in studio_html.split('id="wsRunJobMenuBtn"')[1].split('>')[0]

    for token in (
        "connectStudioJobsStream",
        "loadWsJobs",
        "renderWsJobsTable",
        "inspectWsJob",
        "startStudioJob",
        "cancelStudioJob",
        "retryStudioJob",
        "clearFinishedStudioJobs",
        "/api/studio/jobs/stream",
    ):
        assert token in studio_js, f"Missing {token} in studio.js"


def test_evolve_stream_does_not_leak_job_context_across_requests(isolated_studio_jobs_env):
    """Verify /api/studio/evolve/stream never leaks job context across threadpool hops to later requests."""
    client = isolated_studio_jobs_env
    client.post("/api/studio/jobs/clear")

    evolve_res = client.post(
        "/api/studio/evolve/stream",
        json={"generations": 1, "candidates_per_generation": 1, "alpha_quality_weight": 0.65},
    )
    assert evolve_res.status_code == 200

    ingest_res = client.post(
        "/api/telemetry/ingest_transcript",
        json={
            "user_id": "test-leak-user",
            "session_id": "sess-leak-check",
            "format": "jsonl",
            "content": '{"type": "USER_INPUT", "content": "Prefer uv run pytest"}\n{"type": "PLANNER_RESPONSE", "content": "Noted."}\n{"type": "USER_INPUT", "content": "Keep logs concise."}',
        },
    )
    assert ingest_res.status_code == 200

    jobs = client.get("/api/studio/jobs?limit=20").json()["jobs"]
    evolve_jobs = [j for j in jobs if j["job_type"] == "alpha_evolve"]
    ingest_jobs = [j for j in jobs if j["job_type"] == "session_ingest"]
    assert len(evolve_jobs) == 1
    assert len(ingest_jobs) == 1
    assert evolve_jobs[0]["result_summary"].startswith("Score ")
    assert "3 turn(s)" in ingest_jobs[0]["result_summary"]
    assert ingest_jobs[0]["metrics"].get("turns_ingested") == 3
    assert evolve_jobs[0]["duration_ms"] is not None and evolve_jobs[0]["duration_ms"] >= 0
    assert ingest_jobs[0]["duration_ms"] is not None and ingest_jobs[0]["duration_ms"] >= 0


def test_orphaned_jobs_reconciled_on_restart_and_workerless_cancel():
    """Verify dead-instance RUNNING jobs reconcile to FAILED and workerless cancel finalizes CANCELED."""
    spanner = SpannerPreferenceClient(use_mock=True)
    spanner.upsert_studio_job(
        {
            "job_id": "job-orphan-01",
            "runner_id": "runner-dead0001",
            "job_type": "dream_compaction",
            "category": "compaction",
            "title": "Dream compaction",
            "status": "RUNNING",
            "started_at": "2026-11-21T14:00:00+00:00",
        },
        force_persist=True,
    )
    runner = StudioJobRunner(spanner_client=spanner, max_workers=2)
    snap = runner.list_jobs()
    assert snap["active_count"] == 0
    orphan = runner.get_job("job-orphan-01")
    assert orphan is not None
    assert orphan["status"] == "FAILED"
    assert "Interrupted" in orphan["result_summary"]

    # Manually insert an active job owned by this runner_id but with no cancel_event (workerless)
    spanner.upsert_studio_job(
        {
            "job_id": "job-workerless-02",
            "runner_id": runner._runner_id,
            "job_type": "eval_benchmark",
            "category": "optimize",
            "title": "Recall benchmark",
            "status": "RUNNING",
            "started_at": "2026-11-21T14:05:00+00:00",
        },
        force_persist=True,
    )
    canceled = runner.cancel_job("job-workerless-02")
    assert canceled is not None
    assert canceled["status"] == "CANCELED"
    assert canceled["completed_at"] is not None


def test_retry_preserves_untruncated_raw_params_and_blocks_active_retry(isolated_studio_jobs_env):
    """Verify retry keeps untruncated raw_params and HTTP retry returns 409 for active jobs."""
    spanner = SpannerPreferenceClient(use_mock=True)
    runner = StudioJobRunner(spanner_client=spanner, max_workers=2)
    seen_params = []

    def echo_worker(params, ctx):
        seen_params.append(params)
        return {"result_summary": "OK"}

    runner.register_handler("dream_compaction", echo_worker)
    long_note = "x" * 700
    many_sessions = [f"sess-{i:03d}" for i in range(60)]
    job = runner.start_job("dream_compaction", params={"note": long_note, "session_ids": many_sessions})

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        cur = runner.get_job(job["job_id"])
        if cur and cur["status"] == "SUCCEEDED":
            break
        time.sleep(0.02)

    retried = runner.retry_job(job["job_id"])
    assert retried is not None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        cur = runner.get_job(retried["job_id"])
        if cur and cur["status"] == "SUCCEEDED":
            break
        time.sleep(0.02)

    assert len(seen_params) == 2
    assert seen_params[1]["note"] == long_note
    assert seen_params[1]["session_ids"] == many_sessions

    # Verify HTTP POST /retry returns 409 on an active job
    client = isolated_studio_jobs_env
    ui_srv.spanner_client.upsert_studio_job(
        {
            "job_id": "job-active-conflict",
            "runner_id": ui_srv.job_runner._runner_id,
            "job_type": "alpha_evolve",
            "category": "optimize",
            "title": "Auto-tune recall",
            "status": "RUNNING",
            "started_at": "2026-11-21T15:00:00+00:00",
        },
        force_persist=True,
    )
    ui_srv.job_runner._cancel_events["job-active-conflict"] = threading.Event()
    try:
        conflict_res = client.post("/api/studio/jobs/job-active-conflict/retry")
        assert conflict_res.status_code == 409
    finally:
        ui_srv.job_runner._cancel_events.pop("job-active-conflict", None)
        ui_srv.job_runner.cancel_job("job-active-conflict")

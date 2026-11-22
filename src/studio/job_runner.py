"""Background & inline job runner for Electric Sheep Studio pipelines."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import contextvars
import copy
import datetime
import json
import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger("adk.studio.job_runner")

TERMINAL_JOB_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELED"})
ACTIVE_JOB_STATUSES = frozenset({"QUEUED", "RUNNING"})

_ACTIVE_JOB_CTX: contextvars.ContextVar[Optional["JobProgressContext"]] = contextvars.ContextVar(
    "studio_active_job_ctx",
    default=None,
)

JOB_TYPE_METADATA: Dict[str, Dict[str, str]] = {
    "dream_compaction": {
        "category": "compaction",
        "title": "Dream compaction",
        "target_workspace": "workspace-tree",
    },
    "alpha_evolve": {
        "category": "optimize",
        "title": "Auto-tune recall",
        "target_workspace": "workspace-eval",
    },
    "eval_benchmark": {
        "category": "optimize",
        "title": "Recall benchmark",
        "target_workspace": "workspace-eval",
    },
    "session_ingest": {
        "category": "ingest",
        "title": "Session ingest",
        "target_workspace": "workspace-sessions",
    },
    "memory_transform": {
        "category": "transform",
        "title": "Extract & sync",
        "target_workspace": "workspace-tree",
    },
}


class JobCanceledError(Exception):
    """Raised inside a running job when an operator requests cancellation."""


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sanitize_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a compact, safe copy of job input parameters for UI display."""
    if not isinstance(params, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for k, v in params.items():
        if isinstance(v, str) and len(v) > 500:
            cleaned[k] = v[:500] + f"... ({len(v)} chars)"
        elif isinstance(v, (dict, list)):
            try:
                raw = json.dumps(v)
                if len(raw) > 1200:
                    cleaned[k] = f"<{type(v).__name__}: {len(v)} items>"
                else:
                    cleaned[k] = v
            except Exception:
                cleaned[k] = str(v)[:200]
        else:
            cleaned[k] = v
    return cleaned


def _format_scope_summary(params: Optional[Dict[str, Any]]) -> str:
    if not isinstance(params, dict):
        return "All scopes"
    parts: List[str] = []
    for key, label in (("user_id", "user"), ("team_id", "team"), ("agent_id", "agent"), ("session_id", "session")):
        val = str(params.get(key) or "").strip()
        if val:
            parts.append(f"{label}:{val}")
    return " · ".join(parts) if parts else "All scopes"


def _push_async_queue(aq: asyncio.Queue, event: Dict[str, Any]) -> None:
    """Threadsafe helper run on the subscriber's event loop to enqueue an SSE event."""
    try:
        aq.put_nowait(event)
    except asyncio.QueueFull:
        try:
            aq.get_nowait()
            aq.put_nowait(event)
        except Exception:
            pass


class JobProgressContext:
    """Mutable progress and cancellation handle passed to job workers and inline endpoints."""

    def __init__(
        self,
        runner: "StudioJobRunner",
        job_id: str,
        cancel_event: threading.Event,
        start_mono: Optional[float] = None,
    ) -> None:
        self._runner = runner
        self.job_id = job_id
        self._cancel_event = cancel_event
        self._start_mono = start_mono if start_mono is not None else time.monotonic()

    @property
    def is_canceled(self) -> bool:
        return self._cancel_event.is_set()

    def elapsed_ms(self) -> int:
        return max(0, int(round((time.monotonic() - self._start_mono) * 1000)))

    def check_canceled(self) -> None:
        """Raise JobCanceledError if cancellation was requested for this job."""
        if self._cancel_event.is_set():
            raise JobCanceledError("Canceled by operator")

    def update_progress(
        self,
        current_step: str,
        progress_pct: Optional[int] = None,
        detail: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        force_persist: bool = False,
    ) -> None:
        """Append a progress step and broadcast updated job state to SSE subscribers."""
        self.check_canceled()
        self._runner._update_job_progress(
            job_id=self.job_id,
            current_step=current_step,
            progress_pct=progress_pct,
            detail=detail,
            metrics=metrics,
            force_persist=force_persist,
        )

    def complete(
        self,
        result_summary: str,
        metrics: Optional[Dict[str, Any]] = None,
        output_preview: Optional[Dict[str, Any]] = None,
        target_workspace: Optional[str] = None,
    ) -> None:
        """Mark the tracked job succeeded with final summary, metrics, and output preview."""
        self._runner._finish_job_success(
            job_id=self.job_id,
            result_summary=result_summary,
            metrics=metrics,
            output_preview=output_preview,
            target_workspace=target_workspace,
            duration_ms=self.elapsed_ms(),
        )


class StudioJobRunner:
    """Thread-pool background job runner with Spanner persistence and SSE fan-out."""

    def __init__(self, spanner_client: Any, max_workers: int = 4) -> None:
        self._spanner = spanner_client
        self._runner_id = f"runner-{uuid.uuid4().hex[:8]}"
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sheep-job")
        self._lock = threading.RLock()
        self._handlers: Dict[str, Callable[[Dict[str, Any], JobProgressContext], Dict[str, Any]]] = {}
        self._cancel_events: Dict[str, threading.Event] = {}
        self._raw_params_by_job: Dict[str, Dict[str, Any]] = {}
        self._subscribers: Set[queue.Queue] = set()
        self._async_subscribers: Set[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()

    def register_handler(
        self,
        job_type: str,
        handler: Callable[[Dict[str, Any], JobProgressContext], Dict[str, Any]],
    ) -> None:
        """Register a callable worker for a job_type."""
        with self._lock:
            self._handlers[job_type] = handler

    def subscribe(self) -> queue.Queue:
        """Register a new synchronous SSE subscriber queue."""
        q: queue.Queue = queue.Queue(maxsize=128)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a synchronous SSE subscriber queue."""
        with self._lock:
            self._subscribers.discard(q)

    def subscribe_async(self, loop: asyncio.AbstractEventLoop) -> Tuple[asyncio.AbstractEventLoop, asyncio.Queue]:
        """Register an asyncio SSE subscriber queue bound to `loop`."""
        sub: Tuple[asyncio.AbstractEventLoop, asyncio.Queue] = (loop, asyncio.Queue(maxsize=128))
        with self._lock:
            self._async_subscribers.add(sub)
        return sub

    def unsubscribe_async(self, sub: Tuple[asyncio.AbstractEventLoop, asyncio.Queue]) -> None:
        """Remove an asyncio SSE subscriber queue."""
        with self._lock:
            self._async_subscribers.discard(sub)

    def _broadcast(self, event: Dict[str, Any]) -> None:
        with self._lock:
            sync_subs = list(self._subscribers)
            async_subs = list(self._async_subscribers)
        for q in sync_subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass
        dead_async: List[Tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        for loop, aq in async_subs:
            try:
                if loop.is_closed():
                    dead_async.append((loop, aq))
                else:
                    loop.call_soon_threadsafe(_push_async_queue, aq, event)
            except RuntimeError:
                dead_async.append((loop, aq))
        if dead_async:
            with self._lock:
                for sub in dead_async:
                    self._async_subscribers.discard(sub)

    def _stage_in_memory_unlocked(
        self, record: Dict[str, Any], force_persist: bool = False
    ) -> Tuple[Dict[str, Any], bool]:
        """Stage job update into Spanner client memory (caller holds self._lock)."""
        if hasattr(self._spanner, "stage_studio_job_in_memory"):
            return self._spanner.stage_studio_job_in_memory(record, force_persist=force_persist)
        stored = self._spanner.upsert_studio_job(record, force_persist=force_persist)
        return stored, False

    def _flush_remote_if_needed(self, stored: Dict[str, Any], should_persist: bool) -> None:
        """Persist staged record to Cloud Spanner outside self._lock."""
        if should_persist and hasattr(self._spanner, "flush_studio_job_to_spanner"):
            self._spanner.flush_studio_job_to_spanner(stored["job_id"], stored)

    def reconcile_orphaned_jobs(self) -> int:
        """Mark QUEUED/RUNNING jobs hydrated from dead server instances as FAILED."""
        all_jobs = self._spanner.list_studio_jobs(limit=200)
        reconciled: List[Tuple[Dict[str, Any], bool]] = []
        now_iso = _utc_now_iso()
        with self._lock:
            for row in all_jobs:
                jid = str(row.get("job_id") or "")
                st = str(row.get("status") or "").upper()
                if st not in ACTIVE_JOB_STATUSES:
                    continue
                if row.get("runner_id") == self._runner_id and jid in self._cancel_events:
                    continue
                current = self._spanner.get_studio_job(jid)
                if not current or str(current.get("status") or "").upper() not in ACTIVE_JOB_STATUSES:
                    continue
                current["status"] = "FAILED"
                current["current_step"] = "Interrupted"
                current["result_summary"] = "Interrupted (server restarted)"
                current["error"] = "Interrupted (server restarted)"
                current["completed_at"] = now_iso
                steps = list(current.get("steps") or [])
                steps.append(
                    {
                        "ts": now_iso,
                        "label": "Interrupted",
                        "detail": "Interrupted (server restarted)",
                    }
                )
                current["steps"] = steps
                staged, should_persist = self._stage_in_memory_unlocked(current, force_persist=True)
                reconciled.append((staged, should_persist))
        for stored, should_persist in reconciled:
            self._flush_remote_if_needed(stored, should_persist)
            self._broadcast_job_event("job_updated", stored)
        return len(reconciled)

    def _build_snapshot_no_reconcile(
        self,
        category: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        all_jobs = self._spanner.list_studio_jobs(limit=max(100, limit))
        active_count = 0
        counts: Dict[str, int] = {
            "total": len(all_jobs),
            "running": 0,
            "queued": 0,
            "succeeded": 0,
            "failed": 0,
            "canceled": 0,
            "compaction": 0,
            "optimize": 0,
            "ingest": 0,
            "transform": 0,
        }
        for job in all_jobs:
            st = str(job.get("status") or "").upper()
            cat = str(job.get("category") or "").lower()
            if st == "RUNNING":
                counts["running"] += 1
                active_count += 1
            elif st == "QUEUED":
                counts["queued"] += 1
                active_count += 1
            elif st == "SUCCEEDED":
                counts["succeeded"] += 1
            elif st == "FAILED":
                counts["failed"] += 1
            elif st == "CANCELED":
                counts["canceled"] += 1
            if cat in counts:
                counts[cat] += 1

        filtered = self._spanner.list_studio_jobs(category=category, status=status, limit=limit)
        return {
            "jobs": filtered,
            "active_count": active_count,
            "counts": counts,
        }

    def get_snapshot_payload(
        self,
        category: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Reconcile dead-instance orphans and return the current jobs snapshot."""
        self.reconcile_orphaned_jobs()
        return self._build_snapshot_no_reconcile(category=category, status=status, limit=limit)

    def list_jobs(
        self,
        category: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        return self.get_snapshot_payload(category=category, status=status, limit=limit)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        self.reconcile_orphaned_jobs()
        return self._spanner.get_studio_job(job_id)

    def _build_initial_record(
        self,
        job_type: str,
        triggered_from: str = "Jobs",
        params: Optional[Dict[str, Any]] = None,
        title: Optional[str] = None,
        scope_summary: Optional[str] = None,
        target_workspace: Optional[str] = None,
        status: str = "QUEUED",
        retried_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        meta = JOB_TYPE_METADATA.get(job_type) or {
            "category": "transform",
            "title": job_type.replace("_", " ").title(),
            "target_workspace": "workspace-tree",
        }
        now_iso = _utc_now_iso()
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        raw_copy = copy.deepcopy(params) if isinstance(params, dict) else {}
        clean_params = _sanitize_params(raw_copy)
        step_label = "Queued" if status == "QUEUED" else "Started"
        return {
            "job_id": job_id,
            "runner_id": self._runner_id,
            "job_type": job_type,
            "category": meta["category"],
            "title": title or meta["title"],
            "status": status,
            "triggered_from": triggered_from or "Jobs",
            "scope_summary": scope_summary or _format_scope_summary(clean_params),
            "current_step": "Waiting for worker slot" if status == "QUEUED" else "Starting...",
            "progress_pct": 0 if status == "QUEUED" else 10,
            "result_summary": "",
            "target_workspace": target_workspace or meta["target_workspace"],
            "steps": [{"ts": now_iso, "label": step_label, "detail": f"Triggered from {triggered_from or 'Jobs'}"}],
            "metrics": {},
            "input_params": clean_params,
            "raw_params": raw_copy,
            "output_preview": {},
            "error": None,
            "retried_from": retried_from,
            "created_at": now_iso,
            "started_at": now_iso if status == "RUNNING" else None,
            "updated_at": now_iso,
            "completed_at": None,
            "duration_ms": None,
        }

    def start_job(
        self,
        job_type: str,
        params: Optional[Dict[str, Any]] = None,
        triggered_from: str = "Jobs",
        title: Optional[str] = None,
        scope_summary: Optional[str] = None,
        retried_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enqueue a background job and return its initial QUEUED record immediately."""
        raw_params = copy.deepcopy(params) if isinstance(params, dict) else {}
        with self._lock:
            handler = self._handlers.get(job_type)
            if handler is None:
                raise ValueError(f"Unsupported job_type '{job_type}'")
            record = self._build_initial_record(
                job_type=job_type,
                triggered_from=triggered_from,
                params=raw_params,
                title=title,
                scope_summary=scope_summary,
                status="QUEUED",
                retried_from=retried_from,
            )
            job_id = record["job_id"]
            cancel_event = threading.Event()
            self._cancel_events[job_id] = cancel_event
            self._raw_params_by_job[job_id] = raw_params
            stored, should_persist = self._stage_in_memory_unlocked(record, force_persist=True)

        self._flush_remote_if_needed(stored, should_persist)
        self._broadcast_job_event("job_updated", stored)
        self._executor.submit(self._run_worker, job_id, handler, raw_params, cancel_event)
        return stored

    def _run_worker(
        self,
        job_id: str,
        handler: Callable[[Dict[str, Any], JobProgressContext], Dict[str, Any]],
        params: Dict[str, Any],
        cancel_event: threading.Event,
    ) -> None:
        start_mono = time.monotonic()
        ctx = JobProgressContext(self, job_id, cancel_event, start_mono=start_mono)
        token = _ACTIVE_JOB_CTX.set(ctx)
        try:
            stored: Optional[Dict[str, Any]] = None
            should_persist = False
            with self._lock:
                current = self._spanner.get_studio_job(job_id)
                if not current:
                    return
                if str(current.get("status") or "").upper() in TERMINAL_JOB_STATUSES:
                    return
                if cancel_event.is_set():
                    stored = None
                else:
                    now_iso = _utc_now_iso()
                    current["status"] = "RUNNING"
                    current["started_at"] = current.get("started_at") or now_iso
                    current["current_step"] = "Running..."
                    current["progress_pct"] = max(5, int(current.get("progress_pct") or 5))
                    steps = list(current.get("steps") or [])
                    steps.append({"ts": now_iso, "label": "Running", "detail": "Worker started"})
                    current["steps"] = steps
                    stored, should_persist = self._stage_in_memory_unlocked(current, force_persist=True)

            if stored is None:
                raise JobCanceledError("Canceled before start")

            self._flush_remote_if_needed(stored, should_persist)
            self._broadcast_job_event("job_updated", stored)

            result = handler(params, ctx) or {}
            if cancel_event.is_set():
                raise JobCanceledError("Canceled by operator")

            elapsed_ms = max(0, int(round((time.monotonic() - start_mono) * 1000)))
            self._finish_job_success(
                job_id=job_id,
                result_summary=str(result.get("result_summary") or "Completed"),
                metrics=result.get("metrics") if isinstance(result.get("metrics"), dict) else None,
                output_preview=result.get("output_preview") if isinstance(result.get("output_preview"), dict) else None,
                target_workspace=result.get("target_workspace"),
                duration_ms=elapsed_ms,
            )
        except JobCanceledError as ce:
            elapsed_ms = max(0, int(round((time.monotonic() - start_mono) * 1000)))
            self._finish_job_terminal(
                job_id=job_id,
                status="CANCELED",
                step_label="Canceled",
                detail=str(ce) or "Canceled by operator",
                result_summary="Canceled",
                error=None,
                duration_ms=elapsed_ms,
            )
        except Exception as exc:
            logger.exception("Background job %s failed", job_id)
            elapsed_ms = max(0, int(round((time.monotonic() - start_mono) * 1000)))
            self._finish_job_terminal(
                job_id=job_id,
                status="FAILED",
                step_label="Failed",
                detail=str(exc),
                result_summary=f"Failed: {str(exc)[:120]}",
                error=str(exc),
                duration_ms=elapsed_ms,
            )
        finally:
            _ACTIVE_JOB_CTX.reset(token)
            with self._lock:
                self._cancel_events.pop(job_id, None)

    def _update_job_progress(
        self,
        job_id: str,
        current_step: str,
        progress_pct: Optional[int] = None,
        detail: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        force_persist: bool = False,
    ) -> None:
        stored: Optional[Dict[str, Any]] = None
        should_persist = False
        with self._lock:
            current = self._spanner.get_studio_job(job_id)
            if not current or str(current.get("status") or "").upper() in TERMINAL_JOB_STATUSES:
                return
            now_iso = _utc_now_iso()
            current["status"] = "RUNNING"
            current["started_at"] = current.get("started_at") or now_iso
            current["current_step"] = current_step
            if progress_pct is not None:
                current["progress_pct"] = max(0, min(100, int(progress_pct)))
            if isinstance(metrics, dict) and metrics:
                merged_metrics = dict(current.get("metrics") or {})
                merged_metrics.update(metrics)
                current["metrics"] = merged_metrics
            steps = list(current.get("steps") or [])
            if not steps or steps[-1].get("label") != current_step or detail:
                steps.append({"ts": now_iso, "label": current_step, "detail": detail or current_step})
            current["steps"] = steps
            stored, should_persist = self._stage_in_memory_unlocked(current, force_persist=force_persist)
        self._flush_remote_if_needed(stored, should_persist)
        self._broadcast_job_event("job_updated", stored)

    def _finish_job_success(
        self,
        job_id: str,
        result_summary: str,
        metrics: Optional[Dict[str, Any]] = None,
        output_preview: Optional[Dict[str, Any]] = None,
        target_workspace: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        stored: Optional[Dict[str, Any]] = None
        should_persist = False
        with self._lock:
            current = self._spanner.get_studio_job(job_id)
            if not current or str(current.get("status") or "").upper() in TERMINAL_JOB_STATUSES:
                return
            now_iso = _utc_now_iso()
            current["status"] = "SUCCEEDED"
            current["current_step"] = "Completed"
            current["progress_pct"] = 100
            current["result_summary"] = result_summary or "Completed"
            current["completed_at"] = now_iso
            if target_workspace:
                current["target_workspace"] = target_workspace
            if duration_ms is not None:
                current["duration_ms"] = duration_ms
            if isinstance(metrics, dict) and metrics:
                merged_metrics = dict(current.get("metrics") or {})
                merged_metrics.update(metrics)
                current["metrics"] = merged_metrics
            if isinstance(output_preview, dict) and output_preview:
                current["output_preview"] = output_preview
            steps = list(current.get("steps") or [])
            steps.append({"ts": now_iso, "label": "Completed", "detail": result_summary or "Completed"})
            current["steps"] = steps
            stored, should_persist = self._stage_in_memory_unlocked(current, force_persist=True)
        self._flush_remote_if_needed(stored, should_persist)
        self._broadcast_job_event("job_updated", stored)

    def _finish_job_terminal(
        self,
        job_id: str,
        status: str,
        step_label: str,
        detail: str,
        result_summary: str,
        error: Optional[str],
        duration_ms: Optional[int] = None,
    ) -> None:
        stored: Optional[Dict[str, Any]] = None
        should_persist = False
        with self._lock:
            current = self._spanner.get_studio_job(job_id)
            if not current or str(current.get("status") or "").upper() in TERMINAL_JOB_STATUSES:
                return
            now_iso = _utc_now_iso()
            current["status"] = status
            current["current_step"] = step_label
            current["result_summary"] = result_summary
            current["error"] = error
            current["completed_at"] = now_iso
            if duration_ms is not None:
                current["duration_ms"] = duration_ms
            steps = list(current.get("steps") or [])
            steps.append({"ts": now_iso, "label": step_label, "detail": detail})
            current["steps"] = steps
            stored, should_persist = self._stage_in_memory_unlocked(current, force_persist=True)
        self._flush_remote_if_needed(stored, should_persist)
        self._broadcast_job_event("job_updated", stored)

    def _broadcast_job_event(self, event_type: str, job: Dict[str, Any]) -> None:
        snap = self._build_snapshot_no_reconcile(limit=50)
        self._broadcast(
            {
                "type": event_type,
                "job": job,
                "active_count": snap["active_count"],
                "counts": snap["counts"],
            }
        )

    def cancel_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Request cancellation for a queued or running job (or finalize workerless jobs immediately)."""
        stored: Optional[Dict[str, Any]] = None
        should_persist = False
        with self._lock:
            current = self._spanner.get_studio_job(job_id)
            if not current:
                return None
            status_upper = str(current.get("status") or "").upper()
            if status_upper in TERMINAL_JOB_STATUSES:
                return current
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event is not None:
                cancel_event.set()
            now_iso = _utc_now_iso()
            if status_upper == "QUEUED" or cancel_event is None:
                current["status"] = "CANCELED"
                current["current_step"] = "Canceled"
                current["result_summary"] = "Canceled"
                current["completed_at"] = now_iso
                steps = list(current.get("steps") or [])
                detail_msg = (
                    "Canceled before execution"
                    if status_upper == "QUEUED"
                    else "Canceled (no active worker on this instance)"
                )
                steps.append({"ts": now_iso, "label": "Canceled", "detail": detail_msg})
                current["steps"] = steps
                stored, should_persist = self._stage_in_memory_unlocked(current, force_persist=True)
            else:
                current["current_step"] = "Canceling..."
                steps = list(current.get("steps") or [])
                steps.append({"ts": now_iso, "label": "Canceling", "detail": "Cancellation requested by operator"})
                current["steps"] = steps
                stored, should_persist = self._stage_in_memory_unlocked(current, force_persist=True)
        self._flush_remote_if_needed(stored, should_persist)
        self._broadcast_job_event("job_updated", stored)
        return stored

    def retry_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Re-enqueue a finished, failed, or canceled job using its original untruncated parameters."""
        existing = self.get_job(job_id)
        if not existing:
            return None
        if str(existing.get("status") or "").upper() in ACTIVE_JOB_STATUSES:
            raise RuntimeError(f"Job '{job_id}' is still active and cannot be retried")
        with self._lock:
            raw_params = copy.deepcopy(
                self._raw_params_by_job.get(job_id)
                or existing.get("raw_params")
                or existing.get("input_params")
                or {}
            )
        return self.start_job(
            job_type=str(existing.get("job_type") or "dream_compaction"),
            params=raw_params,
            triggered_from=str(existing.get("triggered_from") or "Jobs"),
            title=str(existing.get("title") or ""),
            scope_summary=str(existing.get("scope_summary") or ""),
            retried_from=job_id,
        )

    def clear_finished(self) -> Dict[str, Any]:
        """Remove all terminal jobs from the queue and notify subscribers."""
        self.reconcile_orphaned_jobs()
        removed = self._spanner.clear_finished_studio_jobs()
        with self._lock:
            active_ids = set(self._cancel_events.keys())
            for k in list(self._raw_params_by_job.keys()):
                if k not in active_ids and self._spanner.get_studio_job(k) is None:
                    self._raw_params_by_job.pop(k, None)
        snap = self._build_snapshot_no_reconcile(limit=50)
        self._broadcast({"type": "snapshot", **snap})
        return {"removed": removed, **snap}

    @contextmanager
    def track_inline_job(
        self,
        job_type: str,
        triggered_from: str = "API",
        params: Optional[Dict[str, Any]] = None,
        title: Optional[str] = None,
        scope_summary: Optional[str] = None,
        target_workspace: Optional[str] = None,
        bind_ambient: bool = True,
    ) -> Iterator[JobProgressContext]:
        """Track a synchronous endpoint invocation in the unified Jobs ledger.

        Uses contextvars so concurrent requests or threadpool-hopping sync generators
        never leak active job bindings across unrelated endpoints. Pass `bind_ambient=False`
        for sync generators executed across arbitrary anyio pool threads.
        """
        if bind_ambient:
            existing_ctx = _ACTIVE_JOB_CTX.get()
            if existing_ctx is not None:
                yield existing_ctx
                return

        raw_params = copy.deepcopy(params) if isinstance(params, dict) else {}
        record = self._build_initial_record(
            job_type=job_type,
            triggered_from=triggered_from,
            params=raw_params,
            title=title,
            scope_summary=scope_summary,
            target_workspace=target_workspace,
            status="RUNNING",
        )
        job_id = record["job_id"]
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[job_id] = cancel_event
            self._raw_params_by_job[job_id] = raw_params
            stored, should_persist = self._stage_in_memory_unlocked(record, force_persist=True)
        self._flush_remote_if_needed(stored, should_persist)
        self._broadcast_job_event("job_updated", stored)

        start_mono = time.monotonic()
        ctx = JobProgressContext(self, job_id, cancel_event, start_mono=start_mono)
        token = _ACTIVE_JOB_CTX.set(ctx) if bind_ambient else None
        try:
            yield ctx
            current = self._spanner.get_studio_job(job_id)
            if current and str(current.get("status") or "").upper() == "RUNNING":
                elapsed_ms = max(0, int(round((time.monotonic() - start_mono) * 1000)))
                self._finish_job_success(
                    job_id=job_id,
                    result_summary=str(current.get("result_summary") or "Completed"),
                    duration_ms=elapsed_ms,
                )
        except JobCanceledError as ce:
            elapsed_ms = max(0, int(round((time.monotonic() - start_mono) * 1000)))
            self._finish_job_terminal(
                job_id=job_id,
                status="CANCELED",
                step_label="Canceled",
                detail=str(ce) or "Canceled by operator",
                result_summary="Canceled",
                error=None,
                duration_ms=elapsed_ms,
            )
            raise
        except Exception as exc:
            elapsed_ms = max(0, int(round((time.monotonic() - start_mono) * 1000)))
            self._finish_job_terminal(
                job_id=job_id,
                status="FAILED",
                step_label="Failed",
                detail=str(exc),
                result_summary=f"Failed: {str(exc)[:120]}",
                error=str(exc),
                duration_ms=elapsed_ms,
            )
            raise
        finally:
            if token is not None:
                _ACTIVE_JOB_CTX.reset(token)
            with self._lock:
                self._cancel_events.pop(job_id, None)

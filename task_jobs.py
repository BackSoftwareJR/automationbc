"""Schedule and run agent background jobs."""

from __future__ import annotations

from agent_runner import CursorAgentRunner
from config import get_settings
from dashboard_store import get_task_registry
from logger_config import bridge_logger, log_exception
from models import N8NPayload
from project_queue import get_project_queue
from workspace_lock import release_project, try_acquire_project


def _ensure_task_not_stuck(run_id: str, payload: N8NPayload, reason: str) -> None:
    """If worker exits while task is still active, force error + publish + callback."""
    registry = get_task_registry()
    record = registry.get_record(run_id)
    if record is None:
        return
    if record.status not in ("queued", "running", "retrying"):
        return

    runner = CursorAgentRunner(get_settings(), registry)
    error_result = runner._error_response(payload, stderr=reason)
    workspace = record.workspace_path
    if workspace and payload.github_url:
        error_result = runner._finalize_with_publish(
            run_id, payload, workspace, error_result
        )
    registry.mark_finished(
        run_id,
        success=error_result.success,
        exit_code=error_result.exit_code,
        duration_ms=record.duration_ms or 0,
        stdout=error_result.stdout,
        stderr=error_result.stderr,
    )
    runner._send_callback(run_id, payload, error_result)


def run_agent_job(payload: N8NPayload, run_id: str) -> None:
    """Execute one agent run in a worker thread; always finishes with callback or skip."""
    registry = get_task_registry()
    acquired = try_acquire_project(payload.project_id)
    runner = CursorAgentRunner(get_settings(), registry)
    try:
        if not acquired:
            reason = f"Project {payload.project_id} lock busy — unexpected race"
            bridge_logger.warning("Skipping run %s: %s", run_id, reason)
            _ensure_task_not_stuck(run_id, payload, reason)
            return

        runner.run(payload, run_id=run_id)
    except Exception as exc:
        log_exception("Background agent run failed", exc)
        registry = get_task_registry()
        if not registry.is_cancelled(run_id):
            registry.mark_finished(
                run_id,
                success=False,
                exit_code=-1,
                duration_ms=0,
                stdout="",
                stderr=str(exc),
            )
            error_result = runner._error_response(payload, stderr=str(exc))
            runner._send_callback(run_id, payload, error_result)
    finally:
        if acquired:
            release_project(payload.project_id)
        record = get_task_registry().get_record(run_id)
        if record and record.status in ("queued", "running", "retrying"):
            _ensure_task_not_stuck(
                run_id,
                payload,
                "Worker exited without final status — marked as error",
            )
        get_project_queue().on_task_finished(run_id, payload.project_id)


def run_agent_by_run_id(run_id: str) -> None:
    registry = get_task_registry()
    payload = registry.get_payload(run_id)
    if payload is None:
        return
    run_agent_job(payload, run_id)

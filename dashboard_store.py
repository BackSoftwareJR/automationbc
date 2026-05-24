"""In-memory task registry for the live dashboard."""

from __future__ import annotations

import subprocess
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from config import Settings, get_settings
from dashboard_events import get_event_hub
from platform_utils import kill_process_tree
from models import (
    CallbackStatus,
    DashboardSnapshot,
    DashboardStats,
    LogLine,
    N8NCallbackPayload,
    N8NPayload,
    TaskRunPublic,
    TaskStatus,
)


@dataclass
class _TaskRecord:
    run_id: str
    payload: N8NPayload
    status: TaskStatus = "queued"
    attempt: int = 1
    max_attempts: int = 5
    workspace_path: str = ""
    prompt_preview: str = ""
    logs: deque[LogLine] = field(default_factory=deque)
    started_at: datetime | None = None
    wall_started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    exit_code: int | None = None
    result_summary: str | None = None
    error_message: str | None = None
    failure_code: str | None = None
    callback_status: CallbackStatus = "pending"
    callback_attempt: int = 0
    callback_max_attempts: int = 5
    callback_http_status: int | None = None
    callback_error: str | None = None
    callback_sent_at: datetime | None = None
    callback_url_host: str | None = None
    callback_body: N8NCallbackPayload | None = None
    process: subprocess.Popen[str] | None = None
    cancel_requested: bool = False
    queue_position: int | None = None
    queue_total: int | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_public(self, log_limit: int | None = None) -> TaskRunPublic:
        logs = list(self.logs)
        if log_limit is not None:
            logs = logs[-log_limit:]
        return TaskRunPublic(
            run_id=self.run_id,
            task_id=self.payload.task_id,
            project_id=self.payload.project_id,
            project_area=self.payload.project_area,
            status=self.status,
            attempt=self.attempt,
            max_attempts=self.max_attempts,
            workspace_path=self.workspace_path,
            github_url=self.payload.github_url,
            website_url=self.payload.website_url,
            prompt_preview=self.prompt_preview,
            logs=logs,
            started_at=self.started_at,
            finished_at=self.finished_at,
            duration_ms=self.duration_ms,
            exit_code=self.exit_code,
            result_summary=self.result_summary,
            error_message=self.error_message,
            failure_code=self.failure_code,
            callback_status=self.callback_status,
            callback_attempt=self.callback_attempt,
            callback_max_attempts=self.callback_max_attempts,
            callback_http_status=self.callback_http_status,
            callback_error=self.callback_error,
            callback_sent_at=self.callback_sent_at,
            callback_url_host=self.callback_url_host,
        )


class TaskRegistry:
    """Thread-safe registry of agent runs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._tasks: dict[str, _TaskRecord] = {}
        self._order: deque[str] = deque(maxlen=self.settings.dashboard_max_history)
        self._global_lock = threading.Lock()
        self._hub = get_event_hub()

    def _serialize(self, value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return {k: self._serialize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._serialize(v) for v in value]
        return value

    def _emit(self, event_type: str, run_id: str, **extra: Any) -> None:
        event: dict[str, Any] = {
            "type": event_type,
            "run_id": run_id,
            "stats": self.get_stats().model_dump(mode="json"),
        }
        for key, value in extra.items():
            event[key] = self._serialize(value)
        self._hub.broadcast_sync(event)

    def create_task(self, payload: N8NPayload) -> str:
        run_id = str(uuid.uuid4())
        preview = payload.dedicated_prompt[:300]
        if len(payload.dedicated_prompt) > 300:
            preview += "..."
        workspace = str(self.settings.get_project_workspace(payload.project_id))
        from urllib.parse import urlparse

        callback_host = urlparse(self.settings.n8n_callback_url).netloc or None
        now = datetime.now(timezone.utc)
        record = _TaskRecord(
            run_id=run_id,
            payload=payload,
            status="queued",
            max_attempts=max(1, self.settings.agent_max_attempts),
            callback_max_attempts=max(1, self.settings.callback_max_attempts),
            workspace_path=workspace,
            prompt_preview=preview,
            callback_url_host=callback_host,
            wall_started_at=now,
        )
        with self._global_lock:
            self._tasks[run_id] = record
            self._order.appendleft(run_id)
        self.append_log(run_id, "Task queued from n8n webhook", stream="event")
        self._emit("task_created", run_id, task=self.get_task(run_id))
        return run_id

    def get_record(self, run_id: str) -> _TaskRecord | None:
        with self._global_lock:
            return self._tasks.get(run_id)

    def get_payload(self, run_id: str) -> N8NPayload | None:
        record = self.get_record(run_id)
        return record.payload if record else None

    def is_project_active(self, project_id: str | int) -> bool:
        """True if this project already has a queued/running/retrying task."""
        target = str(project_id)
        with self._global_lock:
            records = list(self._tasks.values())
        for record in records:
            if str(record.payload.project_id) != target:
                continue
            if record.status in (
                "queue_waiting",
                "queued",
                "running",
                "retrying",
            ):
                return True
        return False

    def find_active_run_for_project(self, project_id: str | int) -> str | None:
        target = str(project_id)
        with self._global_lock:
            records = list(self._tasks.values())
        for record in records:
            if str(record.payload.project_id) != target:
                continue
            if record.status in (
                "queue_waiting",
                "queued",
                "running",
                "retrying",
            ):
                return record.run_id
        return None

    def get_task(self, run_id: str) -> TaskRunPublic | None:
        record = self.get_record(run_id)
        if record is None:
            return None
        return record.to_public()

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        project_id: str | int | None = None,
    ) -> list[TaskRunPublic]:
        with self._global_lock:
            run_ids = list(self._order)
        tasks: list[TaskRunPublic] = []
        for run_id in run_ids:
            record = self.get_record(run_id)
            if record is None:
                continue
            if status and record.status != status:
                continue
            if project_id is not None and str(record.payload.project_id) != str(
                project_id
            ):
                continue
            tasks.append(record.to_public(log_limit=50))
        return tasks

    def get_stats(self) -> DashboardStats:
        stats = DashboardStats()
        with self._global_lock:
            records = list(self._tasks.values())
        stats.total = len(records)
        for record in records:
            match record.status:
                case "queue_waiting":
                    stats.queue_waiting += 1
                case "queued":
                    stats.queued += 1
                case "running":
                    stats.running += 1
                case "retrying":
                    stats.retrying += 1
                case "success":
                    stats.success += 1
                case "error":
                    stats.error += 1
                case "cancelled":
                    stats.cancelled += 1
        return stats

    def get_snapshot(self) -> DashboardSnapshot:
        from project_queue import get_project_queue

        with self._global_lock:
            run_ids = list(self._order)
        tasks = []
        for run_id in run_ids:
            record = self.get_record(run_id)
            if record:
                tasks.append(record.to_public(log_limit=100))
        return DashboardSnapshot(
            stats=self.get_stats(),
            tasks=tasks,
            project_queues=get_project_queue().get_queues_snapshot(),
        )

    def append_log(
        self,
        run_id: str,
        line: str,
        *,
        stream: Literal["stdout", "stderr", "event"] = "event",
    ) -> None:
        record = self.get_record(run_id)
        if record is None or not line:
            return
        log_line = LogLine(
            ts=datetime.now(timezone.utc),
            stream=stream,
            line=line.rstrip("\n"),
        )
        with record._lock:
            record.logs.append(log_line)
            while len(record.logs) > self.settings.dashboard_log_buffer_lines:
                record.logs.popleft()
        self._emit(
            "log_line",
            run_id,
            log=log_line.model_dump(mode="json"),
        )

    def update_task(self, run_id: str, **fields: Any) -> None:
        record = self.get_record(run_id)
        if record is None:
            return
        with record._lock:
            for key, value in fields.items():
                if hasattr(record, key):
                    setattr(record, key, value)
        self._emit("task_updated", run_id, task=self.get_task(run_id))

    def set_process(
        self, run_id: str, process: subprocess.Popen[str] | None
    ) -> None:
        record = self.get_record(run_id)
        if record is None:
            return
        with record._lock:
            record.process = process

    def is_cancelled(self, run_id: str) -> bool:
        record = self.get_record(run_id)
        return bool(record and record.cancel_requested)

    def cancel(self, run_id: str) -> bool:
        record = self.get_record(run_id)
        if record is None:
            return False
        if record.status == "queue_waiting":
            from project_queue import get_project_queue

            return get_project_queue().cancel_waiting(run_id)

        with record._lock:
            if record.status in ("success", "error", "cancelled"):
                return False
            record.cancel_requested = True
            proc = record.process
        if proc and proc.poll() is None:
            kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        self.update_task(
            run_id,
            status="cancelled",
            finished_at=datetime.now(timezone.utc),
            error_message="Cancelled from dashboard",
        )
        self.append_log(run_id, "Task cancelled by user", stream="event")
        self._emit("task_cancelled", run_id, task=self.get_task(run_id))
        return True

    def mark_running(self, run_id: str, attempt: int) -> None:
        record = self.get_record(run_id)
        wall_started = (
            record.wall_started_at if record and record.wall_started_at else None
        )
        self.update_task(
            run_id,
            status="running",
            attempt=attempt,
            started_at=datetime.now(timezone.utc),
            wall_started_at=wall_started or datetime.now(timezone.utc),
            finished_at=None,
            exit_code=None,
            result_summary=None,
            error_message=None,
        )
        self.append_log(run_id, f"Attempt {attempt} started", stream="event")

    def mark_retrying(self, run_id: str, attempt: int, wait_sec: int) -> None:
        self.update_task(run_id, status="retrying", attempt=attempt)
        self.append_log(
            run_id,
            f"Retry backoff: waiting {wait_sec}s before attempt {attempt}",
            stream="event",
        )

    def mark_finished(
        self,
        run_id: str,
        *,
        success: bool,
        exit_code: int,
        duration_ms: float,
        stdout: str,
        stderr: str,
        result_summary: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        record = self.get_record(run_id)
        if record is not None and record.status == "cancelled":
            self.update_task(
                run_id,
                finished_at=datetime.now(timezone.utc),
                duration_ms=duration_ms,
                exit_code=exit_code,
                error_message=stderr or "Cancelled",
                failure_code=failure_code,
                process=None,
            )
            self._emit("task_finished", run_id, task=self.get_task(run_id))
            return

        status: TaskStatus = "success" if success else "error"
        self.update_task(
            run_id,
            status=status,
            finished_at=datetime.now(timezone.utc),
            duration_ms=duration_ms,
            exit_code=exit_code,
            result_summary=result_summary,
            error_message=None if success else (stderr or "Agent failed"),
            failure_code=None if success else failure_code,
            process=None,
        )
        self._emit("task_finished", run_id, task=self.get_task(run_id))

    def mark_callback_sending(self, run_id: str, attempt: int) -> None:
        record = self.get_record(run_id)
        if record is None:
            return
        status: CallbackStatus = "retrying" if attempt > 1 else "sending"
        with record._lock:
            record.callback_status = status
            record.callback_attempt = attempt
            record.callback_error = None
        self.append_log(
            run_id,
            f"n8n callback attempt {attempt}/{record.callback_max_attempts} → {record.callback_url_host or 'n8n'}",
            stream="event",
        )
        self._emit("callback_updated", run_id, task=self.get_task(run_id))

    def mark_callback_sent(
        self,
        run_id: str,
        *,
        http_status: int,
        attempt: int,
      ) -> None:
        record = self.get_record(run_id)
        if record is None:
            return
        now = datetime.now(timezone.utc)
        with record._lock:
            record.callback_status = "sent"
            record.callback_attempt = attempt
            record.callback_http_status = http_status
            record.callback_error = None
            record.callback_sent_at = now
        self.append_log(
            run_id,
            f"n8n callback delivered ✓ HTTP {http_status} (attempt {attempt})",
            stream="event",
        )
        self._emit("callback_updated", run_id, task=self.get_task(run_id))

    def mark_callback_failed(
        self,
        run_id: str,
        *,
        attempt: int,
        error: str,
        http_status: int | None = None,
        final: bool = False,
    ) -> None:
        record = self.get_record(run_id)
        if record is None:
            return
        with record._lock:
            record.callback_attempt = attempt
            record.callback_http_status = http_status
            record.callback_error = error
            record.callback_status = "failed" if final else "retrying"
        suffix = " — all attempts exhausted" if final else " — will retry"
        self.append_log(
            run_id,
            f"n8n callback failed{suffix}: {error}",
            stream="event",
        )
        self._emit("callback_updated", run_id, task=self.get_task(run_id))

    def set_callback_body(self, run_id: str, body: N8NCallbackPayload) -> None:
        record = self.get_record(run_id)
        if record is None:
            return
        with record._lock:
            record.callback_body = body
            if not record.callback_url_host:
                from urllib.parse import urlparse

                host = urlparse(self.settings.n8n_callback_url).netloc
                record.callback_url_host = host or None

    def get_callback_body(self, run_id: str) -> N8NCallbackPayload | None:
        record = self.get_record(run_id)
        if record is None:
            return None
        with record._lock:
            return record.callback_body

    def mark_callback_skipped(self, run_id: str, reason: str) -> None:
        record = self.get_record(run_id)
        if record is None:
            return
        with record._lock:
            record.callback_status = "skipped"
            record.callback_error = reason
        self.append_log(run_id, f"n8n callback skipped: {reason}", stream="event")
        self._emit("callback_updated", run_id, task=self.get_task(run_id))


_registry: TaskRegistry | None = None


def get_task_registry() -> TaskRegistry:
    global _registry
    if _registry is None:
        _registry = TaskRegistry()
    return _registry

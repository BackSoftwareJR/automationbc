"""Background sweep for tasks stuck in running/retrying beyond max runtime."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from pathlib import Path

from agent_runner import CursorAgentRunner
from config import Settings, get_settings
from dashboard_store import TaskRegistry, get_task_registry
from logger_config import bridge_logger
from models import ExecuteAgentResponse
from platform_utils import kill_process_tree


class TaskWatchdog:
    """Periodically finalizes tasks that exceed agent timeout + grace period."""

    def __init__(
        self,
        settings: Settings | None = None,
        registry: TaskRegistry | None = None,
        interval_sec: int = 30,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry or get_task_registry()
        self.interval_sec = max(10, interval_sec)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="task-watchdog",
            daemon=True,
        )
        self._thread.start()
        bridge_logger.info(
            "Task watchdog started | interval_sec=%s grace_sec=%s",
            self.interval_sec,
            self.settings.task_watchdog_grace_sec,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            try:
                self.sweep_once()
            except Exception as exc:
                bridge_logger.error("Task watchdog sweep failed: %s", exc, exc_info=True)

    def _max_wall_seconds(self, record) -> float | None:
        """Return max runtime in seconds, or None when watchdog wall cap is disabled."""
        if self.settings.task_max_runtime_sec > 0:
            return float(self.settings.task_max_runtime_sec)

        if self.settings.agent_timeout_sec <= 0:
            return None

        per_attempt = (
            self.settings.agent_timeout_sec
            + self.settings.agent_idle_timeout_sec
            + self.settings.agent_retry_delay_before_final_sec
        )
        return per_attempt * max(1, record.max_attempts) + self.settings.task_watchdog_grace_sec

    def sweep_once(self) -> None:
        now = datetime.now(timezone.utc)
        runner = CursorAgentRunner(self.settings, self.registry)

        with self.registry._global_lock:
            run_ids = list(self.registry._order)

        for run_id in run_ids:
            record = self.registry.get_record(run_id)
            if record is None:
                continue
            if record.status in ("queue_waiting", "queued"):
                continue
            if record.status not in ("running", "retrying"):
                continue
            if record.callback_status == "sent":
                continue
            wall_start = record.wall_started_at or record.started_at
            if wall_start is None:
                continue

            max_runtime = self._max_wall_seconds(record)
            if max_runtime is None:
                continue

            elapsed = (now - wall_start).total_seconds()
            if elapsed <= max_runtime:
                continue

            bridge_logger.warning(
                "Watchdog forcing finish | run_id=%s elapsed=%.0fs max=%.0fs",
                run_id,
                elapsed,
                max_runtime,
            )
            proc = record.process
            if proc is not None and proc.poll() is None:
                kill_process_tree(proc.pid)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

            payload = record.payload
            workspace = record.workspace_path
            reason = (
                f"Watchdog: task exceeded {int(max_runtime)}s "
                f"(agent timeout + grace). Forced stop."
            )
            self.registry.append_log(run_id, reason, stream="event")
            self.registry.mark_finished(
                run_id,
                success=False,
                exit_code=-1,
                duration_ms=elapsed * 1000,
                stdout="",
                stderr=reason,
            )

            result = runner._error_response(payload, stderr=reason)
            if payload.github_url and workspace:
                result = runner._finalize_with_publish(
                    run_id, payload, workspace, result
                )
            runner._send_callback(run_id, payload, result)


_watchdog: TaskWatchdog | None = None


def start_task_watchdog() -> TaskWatchdog:
    global _watchdog
    if _watchdog is None:
        _watchdog = TaskWatchdog()
    _watchdog.start()
    return _watchdog


def stop_task_watchdog() -> None:
    global _watchdog
    if _watchdog is not None:
        _watchdog.stop()
        _watchdog = None

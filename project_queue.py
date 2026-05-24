"""Per-project FIFO queues — one agent run at a time per project_id."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass

from dashboard_events import get_event_hub
from dashboard_store import TaskRegistry, get_task_registry
from logger_config import bridge_logger
from models import ProjectQueueItem, ProjectQueueSnapshot, ProjectQueuesSnapshot
from task_executor import schedule_agent_task


def _schedule_run(run_id: str) -> None:
    from task_jobs import run_agent_by_run_id

    schedule_agent_task(run_agent_by_run_id, run_id)


@dataclass(frozen=True)
class EnqueueResult:
    position: int
    total_in_queue: int
    started_immediately: bool


class ProjectQueueManager:
    """Serializes agent runs per project; starts the next task automatically."""

    def __init__(self, registry: TaskRegistry | None = None) -> None:
        self.registry = registry or get_task_registry()
        self._lock = threading.Lock()
        self._waiting: dict[str, deque[str]] = defaultdict(deque)
        self._active: dict[str, str | None] = {}

    def _copy_state(self) -> tuple[dict[str, str | None], dict[str, list[str]]]:
        with self._lock:
            active = dict(self._active)
            waiting = {pid: list(queue) for pid, queue in self._waiting.items()}
        return active, waiting

    def enqueue(self, run_id: str, project_id: str | int) -> EnqueueResult:
        pid = str(project_id)
        schedule_run_id: str | None = None
        result: EnqueueResult
        pending_updates: list[tuple[str, dict]] = []

        with self._lock:
            active = self._active.get(pid)
            waiting = self._waiting[pid]

            if active is None and not waiting:
                self._active[pid] = run_id
                total = 1
                pending_updates.append(
                    (
                        run_id,
                        {
                            "status": "queued",
                            "queue_position": 1,
                            "queue_total": total,
                        },
                    )
                )
                schedule_run_id = run_id
                result = EnqueueResult(1, total, True)
            else:
                waiting.append(run_id)
                total = (1 if active else 0) + len(waiting)
                position = total
                pending_updates.append(
                    (
                        run_id,
                        {
                            "status": "queue_waiting",
                            "queue_position": position,
                            "queue_total": total,
                        },
                    )
                )
                pending_updates.extend(self._renumber_project_unlocked(pid))
                result = EnqueueResult(position, total, False)
                bridge_logger.info(
                    "Task queued | run_id=%s project_id=%s position=%s/%s",
                    run_id,
                    pid,
                    position,
                    total,
                )

        for rid, fields in pending_updates:
            self.registry.update_task(rid, **fields)

        if schedule_run_id:
            self.registry.append_log(
                schedule_run_id,
                f"Progetto {pid}: esecuzione immediata (coda vuota)",
                stream="event",
            )
            _schedule_run(schedule_run_id)
        elif pending_updates:
            self.registry.append_log(
                run_id,
                f"Progetto {pid}: in coda — posizione {result.position}/{result.total_in_queue}",
                stream="event",
            )

        self._emit_queue_update(pid)
        return result

    def on_task_finished(self, run_id: str, project_id: str | int) -> None:
        pid = str(project_id)
        schedule_run_id: str | None = None
        pending_updates: list[tuple[str, dict]] = []

        with self._lock:
            if self._active.get(pid) == run_id:
                self._active[pid] = None
            else:
                self._remove_from_waiting_unlocked(pid, run_id)

            if not self._active.get(pid):
                schedule_run_id, pending_updates = self._pop_next_unlocked(pid)

        for rid, fields in pending_updates:
            self.registry.update_task(rid, **fields)

        if schedule_run_id:
            self.registry.append_log(
                schedule_run_id,
                f"Progetto {pid}: avvio automatico dalla coda",
                stream="event",
            )
            _schedule_run(schedule_run_id)
        self._emit_queue_update(pid)

    def cancel_waiting(self, run_id: str) -> bool:
        record = self.registry.get_record(run_id)
        if record is None:
            return False
        pid = str(record.payload.project_id)

        pending_updates: list[tuple[str, dict]] = []
        with self._lock:
            if record.status != "queue_waiting":
                return False
            if not self._remove_from_waiting_unlocked(pid, run_id):
                return False
            pending_updates = self._renumber_project_unlocked(pid)

        for rid, fields in pending_updates:
            self.registry.update_task(rid, **fields)

        from datetime import datetime, timezone

        self.registry.update_task(
            run_id,
            status="cancelled",
            finished_at=datetime.now(timezone.utc),
            error_message="Rimossa dalla coda",
            queue_position=None,
            queue_total=None,
        )
        self.registry.append_log(
            run_id, "Rimossa dalla coda progetto", stream="event"
        )
        self.registry._emit(
            "task_cancelled", run_id, task=self.registry.get_task(run_id)
        )
        self._emit_queue_update(pid)
        return True

    def get_project_snapshots(self) -> list[ProjectQueueSnapshot]:
        active, waiting = self._copy_state()
        project_ids = set(waiting.keys()) | set(active.keys())
        return [
            self._build_snapshot_from_copy(pid, active, waiting)
            for pid in sorted(project_ids, key=lambda x: (len(x), x))
        ]

    def get_queues_snapshot(self) -> ProjectQueuesSnapshot:
        projects = self.get_project_snapshots()
        waiting_total = sum(p.waiting_count for p in projects)
        active_projects = sum(1 for p in projects if p.active_run_id)
        return ProjectQueuesSnapshot(
            projects=projects,
            project_count=len(projects),
            waiting_total=waiting_total,
            active_projects=active_projects,
        )

    def _build_snapshot_from_copy(
        self,
        project_id: str,
        active_map: dict[str, str | None],
        waiting_map: dict[str, list[str]],
    ) -> ProjectQueueSnapshot:
        active_id = active_map.get(project_id)
        waiting_ids = waiting_map.get(project_id, [])
        items: list[ProjectQueueItem] = []
        project_area: str | None = None
        github_url: str | None = None

        if active_id:
            record = self.registry.get_record(active_id)
            if record:
                project_area = record.payload.project_area
                github_url = record.payload.github_url
                items.append(self._item_from_record(record, position=1))

        for offset, run_id in enumerate(waiting_ids, start=2):
            record = self.registry.get_record(run_id)
            if record is None:
                continue
            if project_area is None:
                project_area = record.payload.project_area
            if github_url is None:
                github_url = record.payload.github_url
            items.append(self._item_from_record(record, position=offset))

        active_task_id = None
        if active_id:
            rec = self.registry.get_record(active_id)
            if rec:
                active_task_id = rec.payload.task_id

        return ProjectQueueSnapshot(
            project_id=project_id,
            project_area=project_area,
            github_url=github_url,
            active_run_id=active_id,
            active_task_id=active_task_id,
            waiting_count=len(waiting_ids),
            total_count=len(items),
            items=items,
        )

    @staticmethod
    def _item_from_record(record, *, position: int) -> ProjectQueueItem:
        return ProjectQueueItem(
            run_id=record.run_id,
            task_id=record.payload.task_id,
            status=record.status,
            position=position,
            prompt_preview=record.prompt_preview,
            enqueued_at=record.wall_started_at,
            project_area=record.payload.project_area,
        )

    def _pop_next_unlocked(
        self, project_id: str,
    ) -> tuple[str | None, list[tuple[str, dict]]]:
        """Pick next waiting run_id; must be called with lock held."""
        waiting = self._waiting[project_id]
        while waiting:
            next_id = waiting.popleft()
            record = self.registry.get_record(next_id)
            if record is None or record.status == "cancelled":
                continue
            if record.cancel_requested:
                continue

            self._active[project_id] = next_id
            updates = self._renumber_project_unlocked(project_id)
            final: list[tuple[str, dict]] = []
            for rid, fields in updates:
                if rid == next_id:
                    fields = {**fields, "status": "queued"}
                final.append((rid, fields))
            bridge_logger.info(
                "Dequeuing next task | run_id=%s project_id=%s",
                next_id,
                project_id,
            )
            return next_id, final
        return None, []

    def _renumber_project_unlocked(self, project_id: str) -> list[tuple[str, dict]]:
        active = self._active.get(project_id)
        waiting = self._waiting[project_id]
        total = (1 if active else 0) + len(waiting)
        updates: list[tuple[str, dict]] = []

        if active:
            updates.append(
                (active, {"queue_position": 1, "queue_total": total})
            )

        for index, run_id in enumerate(waiting):
            position = (2 if active else 1) + index
            updates.append(
                (run_id, {"queue_position": position, "queue_total": total})
            )
        return updates

    def _remove_from_waiting_unlocked(self, project_id: str, run_id: str) -> bool:
        waiting = self._waiting[project_id]
        try:
            waiting.remove(run_id)
            return True
        except ValueError:
            return False

    def _emit_queue_update(self, project_id: str) -> None:
        try:
            queues = self.get_queues_snapshot()
            queue = next(
                (
                    p
                    for p in queues.projects
                    if str(p.project_id) == str(project_id)
                ),
                None,
            )
            get_event_hub().broadcast_sync(
                {
                    "type": "queue_updated",
                    "project_id": project_id,
                    "queue": queue.model_dump(mode="json") if queue else None,
                    "queues": queues.model_dump(mode="json"),
                }
            )
        except Exception as exc:
            bridge_logger.error("queue broadcast failed: %s", exc, exc_info=True)


_manager: ProjectQueueManager | None = None


def get_project_queue() -> ProjectQueueManager:
    global _manager
    if _manager is None:
        _manager = ProjectQueueManager()
    return _manager

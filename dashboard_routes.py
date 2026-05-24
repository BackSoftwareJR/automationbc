"""Dashboard REST API and WebSocket routes."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse

from config import get_settings
from dashboard_events import get_event_hub
from dashboard_store import get_task_registry
from models import (
    CallbackRetryResponse,
    CancelResponse,
    DashboardSnapshot,
    ProjectQueuesSnapshot,
    RetryResponse,
    TaskRunPublic,
    TaskStatus,
)
from security import verify_dashboard_access, verify_dashboard_localhost
from project_queue import get_project_queue

_STATIC_DIR = Path(__file__).resolve().parent / "static" / "dashboard"

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
async def dashboard_page(
    _localhost: Annotated[None, Depends(verify_dashboard_localhost)],
) -> FileResponse:
    index = _STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Dashboard UI not found")
    return FileResponse(index)


@router.get("/api/dashboard/snapshot", response_model=DashboardSnapshot)
async def dashboard_snapshot(
    _api_key: Annotated[str, Depends(verify_dashboard_access)],
) -> DashboardSnapshot:
    return get_task_registry().get_snapshot()


@router.get("/api/dashboard/queues", response_model=ProjectQueuesSnapshot)
async def dashboard_queues(
    _api_key: Annotated[str, Depends(verify_dashboard_access)],
) -> ProjectQueuesSnapshot:
    return get_project_queue().get_queues_snapshot()


@router.get("/api/dashboard/tasks", response_model=list[TaskRunPublic])
async def list_tasks(
    _api_key: Annotated[str, Depends(verify_dashboard_access)],
    status_filter: TaskStatus | None = None,
    project_id: str | None = None,
) -> list[TaskRunPublic]:
    return get_task_registry().list_tasks(
        status=status_filter,
        project_id=project_id,
    )


@router.get("/api/dashboard/tasks/{run_id}", response_model=TaskRunPublic)
async def get_task(
    run_id: str,
    _api_key: Annotated[str, Depends(verify_dashboard_access)],
) -> TaskRunPublic:
    task = get_task_registry().get_task(run_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    record = get_task_registry().get_record(run_id)
    if record:
        return record.to_public()
    return task


@router.post("/api/dashboard/tasks/{run_id}/cancel", response_model=CancelResponse)
async def cancel_task(
    run_id: str,
    _api_key: Annotated[str, Depends(verify_dashboard_access)],
) -> CancelResponse:
    registry = get_task_registry()
    if not registry.cancel(run_id):
        raise HTTPException(
            status_code=400,
            detail="Task cannot be cancelled (not found or already finished)",
        )
    return CancelResponse(
        run_id=run_id,
        status="cancelled",
        message="Cancellation requested",
    )


@router.post("/api/dashboard/tasks/{run_id}/retry", response_model=RetryResponse)
async def retry_task(
    run_id: str,
    _api_key: Annotated[str, Depends(verify_dashboard_access)],
) -> RetryResponse:
    registry = get_task_registry()
    payload = registry.get_payload(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Task not found")
    new_run_id = registry.create_task(payload)
    queue_result = get_project_queue().enqueue(new_run_id, payload.project_id)
    status_label = "queued" if queue_result.started_immediately else "queue_waiting"
    if queue_result.started_immediately:
        message = "Task avviata subito"
    else:
        message = (
            f"In coda — posizione {queue_result.position}/"
            f"{queue_result.total_in_queue}"
        )
    return RetryResponse(
        original_run_id=run_id,
        new_run_id=new_run_id,
        status=status_label,
        message=message,
    )


@router.post(
    "/api/dashboard/tasks/{run_id}/retry-callback",
    response_model=CallbackRetryResponse,
)
async def retry_callback(
    run_id: str,
    _api_key: Annotated[str, Depends(verify_dashboard_access)],
) -> CallbackRetryResponse:
    from agent_runner import CursorAgentRunner

    registry = get_task_registry()
    record = registry.get_record(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if record.status not in ("success", "error"):
        raise HTTPException(
            status_code=400,
            detail="Callback can only be retried for finished tasks",
        )
    if record.callback_body is None:
        raise HTTPException(
            status_code=400,
            detail="No callback payload stored for this task",
        )
    if record.callback_status == "sent":
        return CallbackRetryResponse(
            run_id=run_id,
            callback_status="sent",
            message="Callback already delivered to n8n",
        )
    ok = CursorAgentRunner().send_callback_retry(run_id)
    task = registry.get_task(run_id)
    status = task.callback_status if task else "failed"
    if ok:
        return CallbackRetryResponse(
            run_id=run_id,
            callback_status=status,
            message="Callback delivered to n8n",
        )
    return CallbackRetryResponse(
        run_id=run_id,
        callback_status=status,
        message="Callback retry failed — see task logs for details",
    )


@router.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket) -> None:
    settings = get_settings()
    hub = get_event_hub()
    await websocket.accept()

    try:
        auth_msg = await websocket.receive_json()
    except Exception:
        await websocket.close(code=1008)
        return

    if auth_msg.get("type") != "auth":
        await websocket.close(code=1008)
        return

    api_key = auth_msg.get("api_key", "")
    if not secrets.compare_digest(api_key, settings.bridge_api_key):
        await websocket.send_json({"type": "auth_error", "message": "Invalid API key"})
        await websocket.close(code=1008)
        return

    if not settings.dashboard_allow_remote:
        client = websocket.client
        if client and client.host not in {"127.0.0.1", "::1", "localhost"}:
            await websocket.send_json(
                {"type": "auth_error", "message": "Remote dashboard access denied"}
            )
            await websocket.close(code=1008)
            return

    hub.register(websocket)
    try:
        snapshot = get_task_registry().get_snapshot()
        await hub.send_to(
            websocket,
            {"type": "snapshot", "data": snapshot.model_dump(mode="json")},
        )
        await hub.send_to(websocket, {"type": "auth_ok"})

        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await hub.send_to(websocket, {"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(websocket)

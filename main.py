"""n8n → Cursor CLI bridge server."""

from __future__ import annotations

import asyncio
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from config import get_settings
from dashboard_events import get_event_hub
from dashboard_routes import router as dashboard_router
from dashboard_store import get_task_registry
from logger_config import (
    bridge_logger,
    log_exception,
    log_incoming_request,
    log_payload,
    log_request_completed,
    setup_logger,
)
from models import AcceptedResponse, ErrorResponse, N8NPayload
from security import verify_api_key
from platform_utils import is_windows
from task_executor import shutdown_agent_executor
from task_watchdog import start_task_watchdog, stop_task_watchdog

settings = get_settings()
setup_logger(settings.log_level)

_STATIC_DASHBOARD = settings.base_dir / "static" / "dashboard"


def _preflight_bridge() -> None:
    runtime = (
        f"wsl({settings.wsl_distro})"
        if settings.agent_use_wsl and is_windows()
        else "native"
    )
    idle_label = (
        "disabled"
        if settings.agent_idle_timeout_sec <= 0
        or settings.agent_output_format in ("json", "stream-json")
        else str(settings.agent_idle_timeout_sec)
    )
    wall_label = (
        "disabled"
        if settings.agent_timeout_sec <= 0
        else f"{settings.agent_timeout_sec}s"
    )
    progress_label = (
        "disabled"
        if settings.agent_progress_callback_sec <= 0
        else f"every {settings.agent_progress_callback_sec}s"
    )
    watchdog_label = (
        "disabled"
        if settings.task_max_runtime_sec <= 0
        and settings.agent_timeout_sec <= 0
        else (
            f"{settings.task_max_runtime_sec}s cap"
            if settings.task_max_runtime_sec > 0
            else "legacy formula"
        )
    )
    bridge_logger.info(
        "Bridge config | runtime=%s wall_timeout=%s idle_timeout=%s "
        "progress_callbacks=%s watchdog=%s output_format=%s",
        runtime,
        wall_label,
        idle_label,
        progress_label,
        watchdog_label,
        settings.agent_output_format,
    )
    if not settings.github_token:
        bridge_logger.warning(
            "GITHUB_TOKEN is not set — automated git push may fail for private repos"
        )
    if settings.agent_use_wsl and is_windows():
        try:
            wsl_probe = subprocess.run(
                ["wsl", "--status"],
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            bridge_logger.error(
                "WSL is not available (%s). Install: wsl --install — or set AGENT_USE_WSL=false",
                exc,
            )
            return

        if wsl_probe.returncode != 0:
            bridge_logger.error(
                "WSL is not installed. Run: wsl --install — or set AGENT_USE_WSL=false in .env"
            )
            return

        cmd = [
            "wsl",
            "-d",
            settings.wsl_distro,
            "-e",
            "bash",
            "-lc",
            'export PATH="$HOME/.local/bin:$PATH" && agent --version',
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                bridge_logger.error(
                    "WSL agent preflight failed | distro=%s detail=%s",
                    settings.wsl_distro,
                    (result.stderr or result.stdout).strip(),
                )
            else:
                bridge_logger.info(
                    "WSL agent preflight OK | %s",
                    (result.stdout or result.stderr).strip()[:200],
                )
        except subprocess.TimeoutExpired:
            bridge_logger.error("WSL agent preflight timed out after 15s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub = get_event_hub()
    hub.set_loop(asyncio.get_running_loop())
    _preflight_bridge()
    bridge_logger.info(
        "Dashboard available at http://127.0.0.1:%s/dashboard",
        settings.port,
    )
    start_task_watchdog()
    yield
    stop_task_watchdog()
    shutdown_agent_executor()


app = FastAPI(
    title="n8n-Cursor Bridge",
    description="Local middleware between n8n webhooks and Cursor CLI agents",
    version="2.1.0",
    lifespan=lifespan,
)

if _STATIC_DASHBOARD.is_dir():

    @app.get("/dashboard/styles.css", include_in_schema=False)
    async def dashboard_css() -> FileResponse:
        return FileResponse(
            _STATIC_DASHBOARD / "styles.css",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/dashboard/app.js", include_in_schema=False)
    async def dashboard_js() -> FileResponse:
        return FileResponse(
            _STATIC_DASHBOARD / "app.js",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

app.include_router(dashboard_router)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    client_ip = _client_ip(request)
    log_incoming_request(request.method, request.url.path, client_ip)

    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        log_exception("Unhandled request error", exc)
        log_request_completed(
            request.method,
            request.url.path,
            500,
            duration_ms,
        )
        raise

    duration_ms = (time.perf_counter() - start) * 1000
    log_request_completed(
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    log_exception(
        f"Validation error | path={request.url.path}",
        exc,
    )
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            detail="Request validation failed",
            error_type="validation_error",
        ).model_dump(),
    )


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/execute-agent",
    status_code=202,
    response_model=AcceptedResponse,
)
async def execute_agent(
    payload: N8NPayload,
    _api_key: Annotated[str, Depends(verify_api_key)],
) -> AcceptedResponse:
    log_payload(
        payload.task_id,
        payload.project_id,
        payload.project_area,
        payload.dedicated_prompt,
        payload.context,
        payload.github_url,
        payload.target_branch,
    )
    from project_queue import get_project_queue

    registry = get_task_registry()
    run_id = registry.create_task(payload)
    queue_result = get_project_queue().enqueue(run_id, payload.project_id)
    queue_status = "running" if queue_result.started_immediately else "waiting"
    message = (
        f"Esecuzione avviata (progetto {payload.project_id})"
        if queue_result.started_immediately
        else (
            f"In coda per progetto {payload.project_id} — "
            f"posizione {queue_result.position}/{queue_result.total_in_queue}"
        )
    )
    return AcceptedResponse(
        task_id=payload.task_id,
        run_id=run_id,
        queue_position=queue_result.position,
        queue_total=queue_result.total_in_queue,
        queue_status=queue_status,
        message=message,
    )


if __name__ == "__main__":
    import uvicorn

    bridge_logger.info(
        "Starting bridge | host=%s port=%s log_level=%s | dashboard=http://127.0.0.1:%s/dashboard",
        settings.host,
        settings.port,
        settings.log_level,
        settings.port,
    )
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )

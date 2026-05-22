"""n8n → Cursor CLI bridge server."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent_runner import CursorAgentRunner
from config import get_settings
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

settings = get_settings()
setup_logger(settings.log_level)

app = FastAPI(
    title="n8n-Cursor Bridge",
    description="Local middleware between n8n webhooks and Cursor CLI agents",
    version="2.0.0",
)

runner = CursorAgentRunner(settings)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _run_agent_background(payload: N8NPayload) -> None:
    try:
        runner.run(payload)
    except Exception as exc:
        log_exception("Background agent run failed", exc)


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
    background_tasks: BackgroundTasks,
    _api_key: Annotated[str, Depends(verify_api_key)],
) -> AcceptedResponse:
    log_payload(
        payload.task_id,
        payload.project_id,
        payload.project_area,
        payload.dedicated_prompt,
        payload.context,
    )
    background_tasks.add_task(_run_agent_background, payload)
    return AcceptedResponse(task_id=payload.task_id)


if __name__ == "__main__":
    import uvicorn

    bridge_logger.info(
        "Starting bridge | host=%s port=%s log_level=%s",
        settings.host,
        settings.port,
        settings.log_level,
    )
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )

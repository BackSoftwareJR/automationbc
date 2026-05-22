"""n8n → Cursor CLI bridge server."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends, FastAPI, Request
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
from models import ErrorResponse, ExecuteAgentResponse, N8NPayload
from security import verify_api_key

settings = get_settings()
setup_logger(settings.log_level)

app = FastAPI(
    title="n8n-Cursor Bridge",
    description="Local middleware between n8n webhooks and Cursor CLI agents",
    version="1.0.0",
)

runner = CursorAgentRunner(settings)


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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/execute-agent", response_model=ExecuteAgentResponse)
async def execute_agent(
    payload: N8NPayload,
    _api_key: Annotated[str, Depends(verify_api_key)],
) -> ExecuteAgentResponse:
    log_payload(
        payload.project_area,
        payload.task_description,
        payload.context,
    )

    try:
        result = runner.run(payload)
    except FileNotFoundError as exc:
        log_exception("Workspace or agent binary error", exc)
        return ExecuteAgentResponse(
            success=False,
            project_area=payload.project_area,
            command=[],
            stdout="",
            stderr=str(exc),
            exit_code=-1,
            duration_ms=0.0,
        )

    return result


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

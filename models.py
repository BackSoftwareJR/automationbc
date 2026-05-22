"""Pydantic models for request and response payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class N8NPayload(BaseModel):
    """Validated webhook payload from n8n."""

    dedicated_prompt: str = Field(..., min_length=1)
    task_id: str | int
    project_id: str | int
    project_area: str = Field(default="bs-webdev", min_length=1)
    context: dict[str, Any] | None = None


class AcceptedResponse(BaseModel):
    """Immediate response when a task is queued."""

    status: str = "accepted"
    task_id: str | int


class N8NCallbackPayload(BaseModel):
    """Payload POSTed to n8n after agent execution."""

    task_id: str | int
    project_id: str | int
    status: Literal["success", "error"]
    summary: str


class ExecuteAgentResponse(BaseModel):
    """Internal result of a Cursor CLI agent execution."""

    success: bool
    project_area: str
    task_id: str | int | None = None
    project_id: str | int | None = None
    command: list[str]
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class ErrorResponse(BaseModel):
    """Structured API error."""

    detail: str
    error_type: str | None = None

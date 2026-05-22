"""Pydantic models for request and response payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class N8NPayload(BaseModel):
    """Validated webhook payload from n8n."""

    project_area: str = Field(..., min_length=1, examples=["bs-webdev"])
    task_description: str = Field(..., min_length=1)
    context: dict[str, Any] | None = None


class ExecuteAgentResponse(BaseModel):
    """Result of a Cursor CLI agent execution."""

    success: bool
    project_area: str
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

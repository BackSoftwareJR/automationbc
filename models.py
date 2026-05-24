"""Pydantic models for request and response payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class N8NPayload(BaseModel):
    """Validated webhook payload from n8n."""

    dedicated_prompt: str = Field(..., min_length=1)
    task_id: str | int
    project_id: str | int
    project_area: str = Field(default="bs-webdev", min_length=1)
    github_url: str | None = None
    website_url: str | None = None
    context: dict[str, Any] | None = None
    target_branch: Literal["staging", "main"] = Field(
        default="staging",
        description=(
            "Legacy/hint for n8n; the bridge auto-selects main for Node.js repos "
            "(package.json at root) and staging for static HTML."
        ),
    )

    @field_validator("target_branch", mode="before")
    @classmethod
    def _coerce_target_branch(cls, v: object) -> str:
        if v is None or (isinstance(v, str) and not v.strip()):
            return "staging"
        return str(v).strip()


class AcceptedResponse(BaseModel):
    """Immediate response when a task is queued."""

    status: str = "accepted"
    task_id: str | int
    run_id: str
    queue_position: int = 1
    queue_total: int = 1
    queue_status: Literal["running", "waiting"] = "running"
    message: str = ""


FailureCode = Literal[
    "agent_idle_timeout",
    "agent_wall_timeout",
    "agent_auth",
    "agent_trust",
    "agent_watchdog",
    "agent_failed",
    "git_publish_failed",
    "workspace_prep_failed",
]


CallbackPhase = Literal["started", "heartbeat"]


class N8NCallbackPayload(BaseModel):
    """Payload POSTed to n8n during and after agent execution."""

    task_id: str | int
    project_id: str | int
    status: Literal["success", "error", "progress"]
    summary: str
    failure_code: FailureCode | None = None
    run_id: str | None = None
    attempt: int | None = None
    elapsed_sec: int | None = None
    silence_sec: int | None = None
    phase: CallbackPhase | None = None


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
    git_changes: str = ""
    failure_code: FailureCode | None = None
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class ErrorResponse(BaseModel):
    """Structured API error."""

    detail: str
    error_type: str | None = None


TaskStatus = Literal[
    "queue_waiting",
    "queued",
    "running",
    "retrying",
    "success",
    "error",
    "cancelled",
]

CallbackStatus = Literal[
    "pending",
    "sending",
    "retrying",
    "sent",
    "failed",
    "skipped",
]


class LogLine(BaseModel):
    """Single log line from agent execution."""

    ts: datetime
    stream: Literal["stdout", "stderr", "event"]
    line: str


class TaskRunPublic(BaseModel):
    """Task run exposed to dashboard (no secrets)."""

    run_id: str
    task_id: str | int
    project_id: str | int
    project_area: str
    status: TaskStatus
    attempt: int = 1
    max_attempts: int = 5
    workspace_path: str
    github_url: str | None = None
    website_url: str | None = None
    prompt_preview: str = ""
    logs: list[LogLine] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    exit_code: int | None = None
    result_summary: str | None = None
    error_message: str | None = None
    failure_code: FailureCode | None = None
    callback_status: CallbackStatus = "pending"
    callback_attempt: int = 0
    callback_max_attempts: int = 5
    callback_http_status: int | None = None
    callback_error: str | None = None
    callback_sent_at: datetime | None = None
    callback_url_host: str | None = None
    queue_position: int | None = None
    queue_total: int | None = None


class ProjectQueueItem(BaseModel):
    run_id: str
    task_id: str | int
    status: TaskStatus
    position: int
    prompt_preview: str = ""
    enqueued_at: datetime | None = None
    project_area: str | None = None


class ProjectQueueSnapshot(BaseModel):
    project_id: str | int
    project_area: str | None = None
    github_url: str | None = None
    active_run_id: str | None = None
    active_task_id: str | int | None = None
    waiting_count: int = 0
    total_count: int = 0
    items: list[ProjectQueueItem] = Field(default_factory=list)


class ProjectQueuesSnapshot(BaseModel):
    projects: list[ProjectQueueSnapshot] = Field(default_factory=list)
    project_count: int = 0
    waiting_total: int = 0
    active_projects: int = 0


class DashboardStats(BaseModel):
    """Aggregate counters for dashboard header."""

    total: int = 0
    queue_waiting: int = 0
    queued: int = 0
    running: int = 0
    retrying: int = 0
    success: int = 0
    error: int = 0
    cancelled: int = 0


class DashboardSnapshot(BaseModel):
    """Full dashboard state."""

    stats: DashboardStats
    tasks: list[TaskRunPublic]
    project_queues: ProjectQueuesSnapshot = Field(
        default_factory=ProjectQueuesSnapshot
    )
    server_time: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class CancelResponse(BaseModel):
    """Response after cancel request."""

    run_id: str
    status: str
    message: str


class RetryResponse(BaseModel):
    """Response after retry request."""

    original_run_id: str
    new_run_id: str
    status: str
    message: str


class CallbackRetryResponse(BaseModel):
    """Response after manual n8n callback retry."""

    run_id: str
    callback_status: CallbackStatus
    message: str

"""Execute Cursor CLI agent via subprocess."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import Settings, get_settings
from logger_config import (
    log_callback_failed,
    log_callback_sent,
    log_command,
    log_exception,
    log_execution_timing,
    log_process_output,
)
from models import ExecuteAgentResponse, N8NCallbackPayload, N8NPayload
from platform_utils import (
    build_wsl_command,
    candidate_agent_bins,
    default_agent_install_hint,
    is_windows,
)


class CursorAgentRunner:
    """Runs the Cursor `agent` CLI in non-interactive headless mode."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _resolve_agent_bin(self) -> str:
        if self.settings.agent_use_wsl:
            return self.settings.agent_bin

        candidates = [self.settings.agent_bin, *candidate_agent_bins()]
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
            path = Path(candidate)
            if path.is_file():
                return str(path)

        raise FileNotFoundError(
            f"Cursor CLI binary not found. Tried: {list(seen)!r}.\n"
            f"{default_agent_install_hint()}"
        )

    def build_prompt(self, payload: N8NPayload) -> str:
        context_json = (
            json.dumps(payload.context, ensure_ascii=False)
            if payload.context
            else "{}"
        )
        return (
            f"[Bridge] project_area: {payload.project_area}\n"
            f"task_id: {payload.task_id} | project_id: {payload.project_id}\n"
            f"Task:\n{payload.dedicated_prompt}\n"
            f"Context (JSON): {context_json}"
        )

    def build_command(self, payload: N8NPayload, workspace: str) -> list[str]:
        agent_bin = self._resolve_agent_bin()
        prompt = self.build_prompt(payload)
        return [
            agent_bin,
            "-p",
            prompt,
            "--workspace",
            workspace,
            "--output-format",
            "json",
            "--force",
            "--trust",
            "--approve-mcps",
        ]

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.settings.cursor_api_key:
            env["CURSOR_API_KEY"] = self.settings.cursor_api_key
        return env

    def _build_summary(self, stdout: str, stderr: str) -> str:
        parts: list[str] = []
        if stdout.strip():
            parts.append(f"stdout:\n{stdout.strip()}")
        if stderr.strip():
            parts.append(f"stderr:\n{stderr.strip()}")
        combined = "\n\n".join(parts) if parts else "(no output)"
        limit = self.settings.callback_summary_max_chars
        if len(combined) <= limit:
            return combined
        return f"{combined[:limit]}... [truncated, total {len(combined)} chars]"

    def _error_response(
        self,
        payload: N8NPayload,
        *,
        command: list[str] | None = None,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = -1,
        duration_ms: float = 0.0,
        started_at: datetime | None = None,
    ) -> ExecuteAgentResponse:
        return ExecuteAgentResponse(
            success=False,
            project_area=payload.project_area,
            task_id=payload.task_id,
            project_id=payload.project_id,
            command=command or [],
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            started_at=started_at or datetime.now(timezone.utc),
        )

    def _send_callback(self, payload: N8NPayload, result: ExecuteAgentResponse) -> None:
        callback_status = "success" if result.success else "error"
        callback_body = N8NCallbackPayload(
            task_id=payload.task_id,
            project_id=payload.project_id,
            status=callback_status,
            summary=self._build_summary(result.stdout, result.stderr),
        )
        try:
            response = requests.post(
                self.settings.n8n_callback_url,
                json=callback_body.model_dump(),
                timeout=self.settings.callback_timeout_sec,
            )
            response.raise_for_status()
            log_callback_sent(payload.task_id, callback_status, response.status_code)
        except requests.RequestException as exc:
            log_callback_failed(payload.task_id, exc)

    def run(self, payload: N8NPayload) -> ExecuteAgentResponse:
        started_at = datetime.now(timezone.utc)
        start_perf = time.perf_counter()
        result: ExecuteAgentResponse | None = None
        command: list[str] = []

        try:
            workspace_path = self.settings.get_workspace(payload.project_area)
            workspace_str = str(workspace_path)
            command = self.build_command(payload, workspace_str)

            if self.settings.agent_use_wsl and is_windows():
                command = build_wsl_command(
                    command,
                    workspace_path,
                    self.settings.wsl_distro,
                )

            log_command(command)

            run_cwd = workspace_str
            if self.settings.agent_use_wsl and is_windows():
                run_cwd = None

            try:
                completed = subprocess.run(
                    command,
                    cwd=run_cwd,
                    env=self._build_env(),
                    capture_output=True,
                    text=True,
                    timeout=self.settings.agent_timeout_sec,
                    check=False,
                    shell=False,
                )
                stdout = completed.stdout or ""
                stderr = completed.stderr or ""
                exit_code = completed.returncode

            except subprocess.TimeoutExpired as exc:
                duration_ms = (time.perf_counter() - start_perf) * 1000
                stdout = exc.stdout or "" if exc.stdout else ""
                stderr_part = exc.stderr or "" if exc.stderr else ""
                stderr = (
                    f"{stderr_part}\n"
                    f"Process timed out after {self.settings.agent_timeout_sec}s"
                ).strip()
                log_process_output(stdout, stderr)
                log_execution_timing(duration_ms, -1)
                result = self._error_response(
                    payload,
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=-1,
                    duration_ms=duration_ms,
                    started_at=started_at,
                )
                return result

            duration_ms = (time.perf_counter() - start_perf) * 1000
            log_process_output(stdout, stderr)
            log_execution_timing(duration_ms, exit_code)

            result = ExecuteAgentResponse(
                success=exit_code == 0,
                project_area=payload.project_area,
                task_id=payload.task_id,
                project_id=payload.project_id,
                command=command,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=duration_ms,
                started_at=started_at,
            )

        except FileNotFoundError as exc:
            log_exception("Workspace or agent binary error", exc)
            result = self._error_response(
                payload,
                command=command,
                stderr=str(exc),
                duration_ms=(time.perf_counter() - start_perf) * 1000,
                started_at=started_at,
            )

        except Exception as exc:
            log_exception("Agent subprocess failed", exc)
            result = self._error_response(
                payload,
                command=command,
                stderr=str(exc),
                duration_ms=(time.perf_counter() - start_perf) * 1000,
                started_at=started_at,
            )

        finally:
            if result is not None:
                self._send_callback(payload, result)

        return result

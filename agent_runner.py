"""Execute Cursor CLI agent via subprocess."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from config import Settings, get_settings
from platform_utils import (
    build_wsl_command,
    candidate_agent_bins,
    default_agent_install_hint,
    is_windows,
)
from logger_config import (
    log_command,
    log_exception,
    log_execution_timing,
    log_process_output,
)
from models import ExecuteAgentResponse, N8NPayload


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
        context_json = json.dumps(payload.context, ensure_ascii=False) if payload.context else "{}"
        return (
            f"[Bridge] project_area: {payload.project_area}\n"
            f"Task: {payload.task_description}\n"
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

    def run(self, payload: N8NPayload) -> ExecuteAgentResponse:
        started_at = datetime.now(timezone.utc)
        start_perf = time.perf_counter()

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
            exit_code = -1
            log_process_output(stdout, stderr)
            log_execution_timing(duration_ms, exit_code)
            return ExecuteAgentResponse(
                success=False,
                project_area=payload.project_area,
                command=command,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=duration_ms,
                started_at=started_at,
            )

        except Exception as exc:
            log_exception("Agent subprocess failed", exc)
            raise

        duration_ms = (time.perf_counter() - start_perf) * 1000
        log_process_output(stdout, stderr)
        log_execution_timing(duration_ms, exit_code)

        return ExecuteAgentResponse(
            success=exit_code == 0,
            project_area=payload.project_area,
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            started_at=started_at,
        )

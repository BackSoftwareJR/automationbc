"""Execute Cursor CLI agent via subprocess with live dashboard streaming."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import Settings, get_settings
from dashboard_store import TaskRegistry, get_task_registry
from github_publisher import (
    GitLifecycleError,
    ProjectKind,
    append_publish_note_to_stderr,
    infer_project_kind,
    prepare_workspace,
    publish_task_changes,
    sync_workspace_to_branch,
)
from logger_config import (
    log_callback_failed,
    log_callback_retry_wait,
    log_callback_sent,
    log_progress_callback_failed,
    log_progress_callback_sent,
    log_command,
    log_exception,
    log_execution_timing,
    log_process_output,
    log_retry_attempt,
    log_retry_wait,
    log_task_failed_after_retries,
)
from models import (
    CallbackPhase,
    ExecuteAgentResponse,
    FailureCode,
    N8NCallbackPayload,
    N8NPayload,
)
from platform_utils import (
    build_wsl_command,
    candidate_agent_bins,
    default_agent_install_hint,
    is_windows,
    kill_process_tree,
    non_interactive_env,
    resolve_agent_argv_prefix,
    subprocess_creation_flags,
)


class CursorAgentRunner:
    """Runs the Cursor `agent` CLI in non-interactive headless mode."""

    def __init__(
        self,
        settings: Settings | None = None,
        registry: TaskRegistry | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry or get_task_registry()

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

    def build_prompt(
        self,
        payload: N8NPayload,
        *,
        git_branch: str | None = None,
        project_kind: ProjectKind | None = None,
    ) -> str:
        branch_label = git_branch or payload.target_branch
        is_static_html = project_kind == "static"
        context_json = (
            json.dumps(payload.context, ensure_ascii=False)
            if payload.context
            else "{}"
        )
        prompt = (
            f"[Bridge] project_area: {payload.project_area}\n"
            f"task_id: {payload.task_id} | project_id: {payload.project_id}\n"
        )
        if payload.github_url:
            prompt += f"github_url: {payload.github_url}\n"
            prompt += f"publish_branch: {branch_label} (auto-detected)\n"
        if payload.website_url:
            prompt += f"website_url: {payload.website_url}\n"
        prompt += (
            f"Task:\n{payload.dedicated_prompt}\n"
            f"Context (JSON): {context_json}"
        )
        workspace_hint = ""
        if payload.project_id is not None:
            ws = self.settings.get_project_workspace(payload.project_id)
            workspace_hint = (
                f"\nWORKSPACE ROOT (use only this folder — never create subfolders named after the website URL): "
                f"{ws}\n"
                f"The bridge has already cloned/synced the repo on branch {branch_label}. "
                "Do not run git commit or git push — the bridge publishes after you finish.\n"
            )
        if payload.github_url:
            prompt += workspace_hint
            prompt += (
                "\n\nSYSTEM DIRECTIVE FOR AUTONOMY:\n"
                "1. You are running in an isolated workspace for this project.\n"
                f"2. Repository: {payload.github_url} — branch: {branch_label} "
                "(already checked out and pulled by the bridge).\n"
                "3. Execute the Task using ONLY the existing tech stack in this repo "
                "(read package.json / existing frameworks first; do not introduce a new stack, "
                "do not convert static HTML sites to Node/React or vice versa).\n"
                "4. Do NOT run git commit, git push, or git pull — the bridge handles publishing.\n"
                "5. Do NOT run git config (safe.directory and HTTPS are already configured)."
            )
            if is_static_html:
                prompt += (
                    "\n\nSYSTEM DIRECTIVE — STATIC HTML SITE:\n"
                    "- This is a plain HTML/CSS/JS site (no build step, no npm, no framework install).\n"
                    "- Edit existing .html, .css, and .js files only; keep the same file structure.\n"
                    "- Do NOT add package.json, React, Vue, Next.js, or any Node build tooling.\n"
                    "- Do NOT run grep across the entire repo unless necessary; prefer reading index.html "
                    "and css/style.css first.\n"
                    f"- Publishing goes only to branch {branch_label} (never main/production).\n"
                )
        prompt += (
            "\n\nSYSTEM DIRECTIVE — FULLY HEADLESS (NO HUMAN IN THE LOOP):\n"
            "- Never wait for user input, confirmations, or terminal prompts.\n"
            "- On Windows: use `curl.exe` or `Invoke-WebRequest -UseBasicParsing` only.\n"
            "- All git operations must be non-interactive (never rely on username/password prompts).\n"
            "- Never modify .gitconfig or run git config; the bridge pre-configured the repo.\n"
            "- If a command would block, skip it and continue with the next step."
        )
        return prompt

    def _publish_github(
        self,
        run_id: str,
        payload: N8NPayload,
        workspace_str: str,
        result: ExecuteAgentResponse,
    ) -> ExecuteAgentResponse:
        if not payload.github_url or not result.success:
            return result

        publish_log, push_ok = publish_task_changes(
            Path(workspace_str),
            task_id=payload.task_id,
            github_url=payload.github_url,
        )
        self.registry.append_log(run_id, publish_log, stream="event")
        stderr = append_publish_note_to_stderr(result.stderr, publish_log)
        git_changes = self._capture_git_changes(workspace_str)
        success = result.success and push_ok
        failure_code = result.failure_code
        if not push_ok:
            failure_code = "git_publish_failed"
        return result.model_copy(
            update={
                "success": success,
                "stderr": stderr,
                "git_changes": git_changes or result.git_changes,
                "exit_code": result.exit_code if success else max(result.exit_code, 1),
                "failure_code": failure_code,
            }
        )

    def build_command(
        self,
        payload: N8NPayload,
        workspace: str,
        *,
        prompt: str | None = None,
    ) -> list[str]:
        agent_bin = self._resolve_agent_bin()
        prompt_text = prompt if prompt is not None else self.build_prompt(payload)
        if self.settings.agent_use_wsl:
            argv_prefix = [agent_bin]
        else:
            argv_prefix = resolve_agent_argv_prefix(agent_bin)
        return [
            *argv_prefix,
            "--workspace",
            workspace,
            "--output-format",
            self.settings.agent_output_format,
            "--force",
            "--trust",
            "--yolo",
            "--approve-mcps",
            "-p",
            prompt_text,
        ]

    def _effective_idle_limit(self) -> int | None:
        """Idle kill is unsafe when the CLI buffers output (json/stream-json tool runs)."""
        if self.settings.agent_idle_timeout_sec <= 0:
            return None
        fmt = self.settings.agent_output_format
        if fmt in ("json", "stream-json"):
            return None
        return max(30, self.settings.agent_idle_timeout_sec)

    def _effective_wall_deadline(self, start_mono: float) -> float | None:
        """Wall-clock kill deadline, or None when AGENT_TIMEOUT_SEC is 0 (disabled)."""
        if self.settings.agent_timeout_sec <= 0:
            return None
        return start_mono + self.settings.agent_timeout_sec

    def _post_progress_callback_async(
        self,
        run_id: str,
        payload: N8NPayload,
        *,
        attempt: int,
        elapsed_sec: int,
        silence_sec: int,
        summary: str,
        phase: CallbackPhase,
    ) -> None:
        if self.settings.agent_progress_callback_sec <= 0:
            return
        if self.registry.is_cancelled(run_id):
            return

        body = N8NCallbackPayload(
            task_id=payload.task_id,
            project_id=payload.project_id,
            status="progress",
            summary=summary,
            run_id=run_id,
            attempt=attempt,
            elapsed_sec=elapsed_sec,
            silence_sec=silence_sec,
            phase=phase,
        )

        def _post() -> None:
            try:
                http_status = self._post_callback_once(body)
                log_progress_callback_sent(
                    payload.task_id,
                    http_status,
                    elapsed_sec,
                    phase,
                )
                self.registry.append_log(
                    run_id,
                    (
                        f"n8n progress ping ({phase}) — "
                        f"{elapsed_sec}s elapsed, HTTP {http_status}"
                    ),
                    stream="event",
                )
            except Exception as exc:
                log_progress_callback_failed(payload.task_id, exc)
                self.registry.append_log(
                    run_id,
                    f"n8n progress ping failed ({phase}): {exc}",
                    stream="event",
                )

        threading.Thread(target=_post, name=f"progress-{run_id}", daemon=True).start()

    @staticmethod
    def _classify_failure(
        stderr: str,
        stdout: str,
        exit_code: int,
    ) -> FailureCode | None:
        lowered = stderr.lower()
        if "idle timeout" in lowered:
            return "agent_idle_timeout"
        if "process timed out" in lowered:
            return "agent_wall_timeout"
        if "authentication required" in lowered or "agent login" in lowered:
            return "agent_auth"
        if "workspace trust required" in lowered:
            return "agent_trust"
        if "watchdog:" in lowered:
            return "agent_watchdog"
        if "git push failed" in lowered or "git setup failed" in lowered:
            return "git_publish_failed"
        if (
            "git branch sync failed" in lowered
            or "git checkout failed" in lowered
            or "git pull failed" in lowered
            or "git fetch failed" in lowered
            or "git clone failed" in lowered
            or "workspace was not" in lowered
            or "workspace_prep_failed" in lowered
        ):
            return "workspace_prep_failed"
        if exit_code == 0:
            return None
        return "agent_failed"

    @staticmethod
    def _is_non_retryable_error(stderr: str) -> bool:
        lowered = stderr.lower()
        return (
            "authentication required" in lowered
            or "agent login" in lowered
            or "workspace trust required" in lowered
            or "process timed out" in lowered
            or "watchdog:" in lowered
            or "git fetch failed" in lowered
            or "git pull failed" in lowered
            or "git checkout failed" in lowered
            or "git branch sync failed" in lowered
            or "git push failed" in lowered
            or "could not read username" in lowered
        )

    def _finalize_with_publish(
        self,
        run_id: str,
        payload: N8NPayload,
        workspace_str: str,
        result: ExecuteAgentResponse,
        *,
        publish_note: str = "",
    ) -> ExecuteAgentResponse:
        """Always attempt GitHub publish when a repo URL is configured."""
        if not payload.github_url:
            return result
        if publish_note:
            self.registry.append_log(run_id, publish_note, stream="event")
        published = self._publish_github(run_id, payload, workspace_str, result)
        return published

    @staticmethod
    def _parse_agent_result(stdout: str) -> str | None:
        try:
            data = json.loads(stdout.strip())
            if isinstance(data, dict) and data.get("result"):
                return str(data["result"])
        except json.JSONDecodeError:
            pass
        return None

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.settings.cursor_api_key:
            env["CURSOR_API_KEY"] = self.settings.cursor_api_key
        return non_interactive_env(env)

    def _capture_git_changes(self, workspace: str) -> str:
        """Return latest commit stat + diff from workspace, or empty string on failure."""

        def run_git(args: list[str]) -> str:
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    encoding="utf-8",
                    errors="replace",
                    stdin=subprocess.DEVNULL,
                    env=non_interactive_env(os.environ.copy()),
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass
            return ""

        parts: list[str] = []
        log_stat = run_git(["log", "-1", "--stat"])
        if log_stat:
            parts.append(f"git log -1 --stat:\n{log_stat}")
        diff = run_git(["diff", "HEAD~1"])
        if diff:
            parts.append(f"git diff HEAD~1:\n{diff}")
        return "\n\n".join(parts)

    def _build_summary(
        self,
        stdout: str,
        stderr: str,
        git_changes: str = "",
    ) -> str:
        parsed = self._parse_agent_result(stdout)
        if parsed:
            base = parsed
        else:
            parts: list[str] = []
            if stdout.strip():
                parts.append(f"stdout:\n{stdout.strip()}")
            if stderr.strip():
                parts.append(f"stderr:\n{stderr.strip()}")
            base = "\n\n".join(parts) if parts else "(no output)"
        if git_changes.strip():
            base = f"{base}\n\n--- git changes ---\n{git_changes.strip()}"
        limit = self.settings.callback_summary_max_chars
        if len(base) <= limit:
            return base
        return f"{base[:limit]}... [truncated, total {len(base)} chars]"

    def _error_response(
        self,
        payload: N8NPayload,
        *,
        command: list[str] | None = None,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = -1,
        duration_ms: float = 0.0,
        git_changes: str = "",
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
            git_changes=git_changes,
            started_at=started_at or datetime.now(timezone.utc),
        )

    def _build_callback_body(
        self,
        payload: N8NPayload,
        result: ExecuteAgentResponse,
    ) -> N8NCallbackPayload:
        return N8NCallbackPayload(
            task_id=payload.task_id,
            project_id=payload.project_id,
            status="success" if result.success else "error",
            summary=self._build_summary(
                result.stdout,
                result.stderr,
                result.git_changes,
            ),
            failure_code=result.failure_code,
        )

    def _post_callback_once(self, body: N8NCallbackPayload) -> int:
        response = requests.post(
            self.settings.n8n_callback_url,
            json=body.model_dump(),
            timeout=self.settings.callback_timeout_sec,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.status_code

    def _send_callback(
        self,
        run_id: str,
        payload: N8NPayload,
        result: ExecuteAgentResponse,
    ) -> bool:
        body = self._build_callback_body(payload, result)
        self.registry.set_callback_body(run_id, body)
        max_attempts = max(1, self.settings.callback_max_attempts)
        delay_sec = max(0, self.settings.callback_retry_delay_sec)

        for attempt in range(1, max_attempts + 1):
            self.registry.mark_callback_sending(run_id, attempt)
            try:
                http_status = self._post_callback_once(body)
                log_callback_sent(
                    payload.task_id,
                    body.status,
                    http_status,
                    attempt,
                    body.failure_code,
                )
                self.registry.mark_callback_sent(
                    run_id,
                    http_status=http_status,
                    attempt=attempt,
                )
                return True
            except requests.RequestException as exc:
                http_status: int | None = None
                if exc.response is not None:
                    http_status = exc.response.status_code
                error_msg = str(exc)
                is_final = attempt >= max_attempts
                log_callback_failed(
                    payload.task_id,
                    exc,
                    attempt,
                    max_attempts,
                )
                self.registry.mark_callback_failed(
                    run_id,
                    attempt=attempt,
                    error=error_msg,
                    http_status=http_status,
                    final=is_final,
                )
                if is_final:
                    return False
                if delay_sec > 0:
                    log_callback_retry_wait(
                        payload.task_id,
                        delay_sec,
                        attempt + 1,
                    )
                    time.sleep(delay_sec)
        return False

    def send_callback_retry(self, run_id: str) -> bool:
        """Manually resend stored n8n callback payload for a finished task."""
        body = self.registry.get_callback_body(run_id)
        payload = self.registry.get_payload(run_id)
        if body is None or payload is None:
            return False
        record = self.registry.get_record(run_id)
        if record is None or record.status not in ("success", "error"):
            return False
        if record.callback_status == "sent":
            return True
        max_attempts = max(1, self.settings.callback_max_attempts)
        delay_sec = max(0, self.settings.callback_retry_delay_sec)
        for attempt in range(1, max_attempts + 1):
            self.registry.mark_callback_sending(run_id, attempt)
            try:
                http_status = self._post_callback_once(body)
                log_callback_sent(
                    payload.task_id,
                    body.status,
                    http_status,
                    attempt,
                    body.failure_code,
                )
                self.registry.mark_callback_sent(
                    run_id,
                    http_status=http_status,
                    attempt=attempt,
                )
                return True
            except requests.RequestException as exc:
                http_status = (
                    exc.response.status_code if exc.response is not None else None
                )
                is_final = attempt >= max_attempts
                log_callback_failed(
                    payload.task_id,
                    exc,
                    attempt,
                    max_attempts,
                )
                self.registry.mark_callback_failed(
                    run_id,
                    attempt=attempt,
                    error=str(exc),
                    http_status=http_status,
                    final=is_final,
                )
                if is_final:
                    return False
                if delay_sec > 0:
                    time.sleep(delay_sec)
        return False

    def _stream_process(
        self,
        process: subprocess.Popen[str],
        run_id: str,
    ) -> tuple[str, str, int]:
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        start_mono = time.monotonic()
        last_output_mono = start_mono
        idle_limit = self._effective_idle_limit()
        stop_heartbeat = threading.Event()

        def read_stream(stream, stream_name: str, buffer: list[str]) -> None:
            nonlocal last_output_mono
            if stream is None:
                return
            for line in iter(stream.readline, ""):
                if self.registry.is_cancelled(run_id):
                    break
                if line:
                    last_output_mono = time.monotonic()
                buffer.append(line)
                self.registry.append_log(run_id, line.rstrip("\n"), stream=stream_name)

        stdout_thread = threading.Thread(
            target=read_stream,
            args=(process.stdout, "stdout", stdout_lines),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=read_stream,
            args=(process.stderr, "stderr", stderr_lines),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        record = self.registry.get_record(run_id)
        payload = record.payload if record else None
        attempt = record.attempt if record else 1
        progress_interval = self.settings.agent_progress_callback_sec
        last_progress_mono = start_mono

        def maybe_send_progress(elapsed: int, silence: int, *, force: bool = False) -> None:
            nonlocal last_progress_mono
            if payload is None or progress_interval <= 0:
                return
            if not force and time.monotonic() - last_progress_mono < progress_interval:
                return
            last_progress_mono = time.monotonic()
            if silence >= 120:
                note = (
                    f"Task still running ({elapsed}s elapsed). "
                    f"No streamed output for {silence}s — normal during long edits "
                    f"with stream-json. Agent continues."
                )
            else:
                note = (
                    f"Task still running ({elapsed}s elapsed). "
                    f"Last streamed output {silence}s ago."
                )
            self._post_progress_callback_async(
                run_id,
                payload,
                attempt=attempt,
                elapsed_sec=elapsed,
                silence_sec=silence,
                summary=note,
                phase="heartbeat",
            )

        def heartbeat() -> None:
            while not stop_heartbeat.wait(30):
                if process.poll() is not None:
                    break
                if self.registry.is_cancelled(run_id):
                    break
                elapsed = int(time.monotonic() - start_mono)
                silence = int(time.monotonic() - last_output_mono)
                maybe_send_progress(elapsed, silence)
                msg = f"Agent still running ({elapsed}s elapsed"
                if silence >= 120:
                    msg += f", quiet for {silence}s — continuing"
                msg += ")"
                self.registry.append_log(run_id, msg, stream="event")

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()

        deadline = self._effective_wall_deadline(start_mono)
        exit_code: int | None = None

        try:
            while exit_code is None:
                if self.registry.is_cancelled(run_id):
                    kill_process_tree(process.pid)
                    process.wait(timeout=5)
                    exit_code = -1
                    msg = "Process stopped (cancelled)"
                    stderr_lines.append(msg + "\n")
                    self.registry.append_log(run_id, msg, stream="event")
                    break

                if deadline is not None and time.monotonic() >= deadline:
                    kill_process_tree(process.pid)
                    process.wait(timeout=5)
                    exit_code = -1
                    msg = (
                        f"Process timed out after {self.settings.agent_timeout_sec}s"
                    )
                    stderr_lines.append(msg + "\n")
                    self.registry.append_log(run_id, msg, stream="event")
                    break

                if (
                    idle_limit is not None
                    and time.monotonic() - last_output_mono >= idle_limit
                ):
                    kill_process_tree(process.pid)
                    process.wait(timeout=5)
                    exit_code = -1
                    msg = f"Idle timeout: no agent output for {idle_limit}s"
                    stderr_lines.append(msg + "\n")
                    self.registry.append_log(run_id, msg, stream="event")
                    break

                try:
                    exit_code = process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=1)
            if process.poll() is None:
                kill_process_tree(process.pid)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            self.registry.set_process(run_id, None)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)

        if exit_code is None:
            exit_code = process.returncode if process.returncode is not None else -1

        return "".join(stdout_lines), "".join(stderr_lines), exit_code

    def _run_agent_process(
        self,
        command: list[str],
        *,
        run_id: str,
        run_cwd: str | None,
        env: dict[str, str],
    ) -> tuple[str, str, int]:
        if self.registry.is_cancelled(run_id):
            return "", "Cancelled before start", -1

        process = subprocess.Popen(
            command,
            cwd=run_cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            creationflags=subprocess_creation_flags(),
        )
        self.registry.set_process(run_id, process)
        return self._stream_process(process, run_id)

    def _finish_attempt(
        self,
        run_id: str,
        payload: N8NPayload,
        workspace_str: str,
        agent_result: ExecuteAgentResponse,
        *,
        duration_ms: float,
    ) -> ExecuteAgentResponse:
        agent_result = self._finalize_with_publish(
            run_id, payload, workspace_str, agent_result
        )
        result_summary = self._parse_agent_result(agent_result.stdout)
        failure_code = agent_result.failure_code
        if not agent_result.success and failure_code is None:
            failure_code = self._classify_failure(
                agent_result.stderr,
                agent_result.stdout,
                agent_result.exit_code,
            )
        self.registry.mark_finished(
            run_id,
            success=agent_result.success,
            exit_code=agent_result.exit_code,
            duration_ms=duration_ms,
            stdout=agent_result.stdout,
            stderr=agent_result.stderr,
            result_summary=result_summary,
            failure_code=failure_code,
        )
        return agent_result.model_copy(update={"failure_code": failure_code})

    def _execute_once(
        self,
        payload: N8NPayload,
        run_id: str,
        attempt: int,
    ) -> ExecuteAgentResponse:
        started_at = datetime.now(timezone.utc)
        start_perf = time.perf_counter()
        command: list[str] = []
        workspace_str = ""
        agent_result: ExecuteAgentResponse | None = None

        if self.registry.is_cancelled(run_id):
            return self._error_response(
                payload,
                stderr="Cancelled",
                started_at=started_at,
            )

        try:
            self.registry.mark_running(run_id, attempt)
            workspace_path = self.settings.get_project_workspace(payload.project_id)
            os.makedirs(workspace_path, exist_ok=True)
            workspace_str = str(workspace_path)
            effective_branch = payload.target_branch
            project_kind: ProjectKind | None = None

            if payload.github_url:
                prep_log = prepare_workspace(workspace_path, payload.github_url)
                self.registry.append_log(run_id, prep_log, stream="event")
                project_kind = infer_project_kind(workspace_path)
                try:
                    branch_log, effective_branch = sync_workspace_to_branch(
                        workspace_path,
                        auto_detect=True,
                    )
                    self.registry.append_log(run_id, branch_log, stream="event")
                except GitLifecycleError as exc:
                    log_exception("Git branch sync failed", exc)
                    duration_ms = (time.perf_counter() - start_perf) * 1000
                    sync_msg = f"git branch sync failed:\n{exc.log_message()}"
                    agent_result = self._error_response(
                        payload,
                        stderr=sync_msg,
                        duration_ms=duration_ms,
                        started_at=started_at,
                    )
                    agent_result = agent_result.model_copy(
                        update={"failure_code": "workspace_prep_failed"}
                    )
                    self.registry.mark_finished(
                        run_id,
                        success=False,
                        exit_code=-1,
                        duration_ms=duration_ms,
                        stdout="",
                        stderr=sync_msg,
                        failure_code="workspace_prep_failed",
                    )
                    return agent_result

            prompt = self.build_prompt(
                payload,
                git_branch=effective_branch if payload.github_url else None,
                project_kind=project_kind,
            )
            command = self.build_command(payload, workspace_str, prompt=prompt)

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

            self._post_progress_callback_async(
                run_id,
                payload,
                attempt=attempt,
                elapsed_sec=0,
                silence_sec=0,
                summary=(
                    "Agent started; workspace ready. "
                    "You will receive progress pings until the task completes."
                ),
                phase="started",
            )

            stdout, stderr, exit_code = self._run_agent_process(
                command,
                run_id=run_id,
                run_cwd=run_cwd,
                env=self._build_env(),
            )
            git_changes = self._capture_git_changes(workspace_str)
            duration_ms = (time.perf_counter() - start_perf) * 1000

            if self.registry.is_cancelled(run_id):
                agent_result = self._error_response(
                    payload,
                    command=command,
                    stdout=stdout,
                    stderr="Cancelled",
                    exit_code=-1,
                    duration_ms=duration_ms,
                    git_changes=git_changes,
                    started_at=started_at,
                )
                return self._finish_attempt(
                    run_id, payload, workspace_str, agent_result, duration_ms=duration_ms
                )

            log_process_output(stdout, stderr)
            log_execution_timing(duration_ms, exit_code)

            failure_code: FailureCode | None = None
            if exit_code != 0:
                failure_code = self._classify_failure(stderr, stdout, exit_code)
            agent_result = ExecuteAgentResponse(
                success=exit_code == 0,
                project_area=payload.project_area,
                task_id=payload.task_id,
                project_id=payload.project_id,
                command=command,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=duration_ms,
                git_changes=git_changes,
                failure_code=failure_code,
                started_at=started_at,
            )
            return self._finish_attempt(
                run_id, payload, workspace_str, agent_result, duration_ms=duration_ms
            )

        except RuntimeError as exc:
            log_exception("Workspace preparation failed", exc)
            duration_ms = (time.perf_counter() - start_perf) * 1000
            agent_result = self._error_response(
                payload,
                command=command,
                stderr=str(exc),
                duration_ms=duration_ms,
                started_at=started_at,
            )
            agent_result = agent_result.model_copy(
                update={"failure_code": "workspace_prep_failed"}
            )
            if workspace_str and payload.github_url:
                return self._finish_attempt(
                    run_id, payload, workspace_str, agent_result, duration_ms=duration_ms
                )
            self.registry.mark_finished(
                run_id,
                success=False,
                exit_code=-1,
                duration_ms=duration_ms,
                stdout="",
                stderr=str(exc),
                failure_code="workspace_prep_failed",
            )
            return agent_result

        except FileNotFoundError as exc:
            log_exception("Workspace or agent binary error", exc)
            duration_ms = (time.perf_counter() - start_perf) * 1000
            agent_result = self._error_response(
                payload,
                command=command,
                stderr=str(exc),
                duration_ms=duration_ms,
                started_at=started_at,
            )
            if workspace_str and payload.github_url:
                return self._finish_attempt(
                    run_id, payload, workspace_str, agent_result, duration_ms=duration_ms
                )
            self.registry.mark_finished(
                run_id,
                success=False,
                exit_code=-1,
                duration_ms=duration_ms,
                stdout="",
                stderr=str(exc),
            )
            return agent_result

        except Exception as exc:
            log_exception("Agent subprocess failed", exc)
            duration_ms = (time.perf_counter() - start_perf) * 1000
            agent_result = self._error_response(
                payload,
                command=command,
                stderr=str(exc),
                duration_ms=duration_ms,
                started_at=started_at,
            )
            if workspace_str and payload.github_url:
                return self._finish_attempt(
                    run_id, payload, workspace_str, agent_result, duration_ms=duration_ms
                )
            self.registry.mark_finished(
                run_id,
                success=False,
                exit_code=-1,
                duration_ms=duration_ms,
                stdout="",
                stderr=str(exc),
            )
            return agent_result

    def run(
        self,
        payload: N8NPayload,
        run_id: str | None = None,
    ) -> ExecuteAgentResponse:
        if run_id is None:
            run_id = self.registry.create_task(payload)

        max_attempts = max(1, self.settings.agent_max_attempts)
        delay_before_final = max(0, self.settings.agent_retry_delay_before_final_sec)
        last_result: ExecuteAgentResponse | None = None

        for attempt in range(1, max_attempts + 1):
            if self.registry.is_cancelled(run_id):
                last_result = self._error_response(payload, stderr="Cancelled")
                self.registry.mark_callback_skipped(run_id, "Task cancelled")
                if self.registry.get_record(run_id):
                    rec = self.registry.get_record(run_id)
                    if rec and rec.status not in ("cancelled", "success", "error"):
                        self.registry.update_task(
                            run_id,
                            status="cancelled",
                            finished_at=datetime.now(timezone.utc),
                            error_message="Cancelled",
                        )
                return last_result

            if attempt == max_attempts and attempt > 1 and last_result is not None:
                log_retry_wait(payload.task_id, delay_before_final, attempt)
                self.registry.mark_retrying(run_id, attempt, delay_before_final)
                if delay_before_final > 0:
                    for _ in range(delay_before_final):
                        if self.registry.is_cancelled(run_id):
                            break
                        time.sleep(1)

            if attempt > 1:
                log_retry_attempt(payload.task_id, attempt, max_attempts)

            last_result = self._execute_once(payload, run_id, attempt)
            if last_result.success:
                self._send_callback(run_id, payload, last_result)
                return last_result

            if self.registry.is_cancelled(run_id):
                self.registry.mark_callback_skipped(run_id, "Task cancelled")
                return last_result

            if self._is_non_retryable_error(last_result.stderr):
                log_task_failed_after_retries(payload.task_id, attempt)
                enhanced_stderr = (
                    f"TASK FAILED: non-retryable error on attempt {attempt}/{max_attempts}.\n"
                    f"{last_result.stderr}"
                )
                failure_code = last_result.failure_code or self._classify_failure(
                    last_result.stderr,
                    last_result.stdout,
                    last_result.exit_code,
                )
                final_result = last_result.model_copy(
                    update={
                        "success": False,
                        "stderr": enhanced_stderr,
                        "failure_code": failure_code,
                    }
                )
                self.registry.update_task(
                    run_id,
                    error_message=enhanced_stderr,
                    failure_code=failure_code,
                )
                self._send_callback(run_id, payload, final_result)
                return final_result

            if not last_result.success and attempt < max_attempts:
                self.registry.mark_retrying(run_id, attempt + 1, 0)
                self.registry.append_log(
                    run_id,
                    f"Attempt {attempt} failed — retrying ({attempt + 1}/{max_attempts})",
                    stream="event",
                )

        assert last_result is not None
        if self.registry.is_cancelled(run_id):
            self.registry.mark_callback_skipped(run_id, "Task cancelled")
            return last_result
        log_task_failed_after_retries(payload.task_id, max_attempts)
        enhanced_stderr = (
            f"TASK FAILED: all {max_attempts} attempts exhausted.\n"
            f"Last attempt exit_code={last_result.exit_code}.\n\n"
            f"{last_result.stderr}"
        )
        failure_code = last_result.failure_code or self._classify_failure(
            last_result.stderr,
            last_result.stdout,
            last_result.exit_code,
        )
        final_result = last_result.model_copy(
            update={
                "success": False,
                "stderr": enhanced_stderr,
                "failure_code": failure_code,
            }
        )
        self.registry.update_task(
            run_id,
            error_message=enhanced_stderr,
            failure_code=failure_code,
        )
        self._send_callback(run_id, payload, final_result)
        return final_result

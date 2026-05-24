"""Custom bridge logger: colored console + bridge_activity.log file."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "bridge_activity.log"
LOGGER_NAME = "n8n_cursor_bridge"
MAX_LOG_FIELD_CHARS = 32_768

_LEVEL_COLORS = {
    logging.DEBUG: "\033[36m",     # cyan
    logging.INFO: "\033[32m",      # green
    logging.WARNING: "\033[33m",   # yellow
    logging.ERROR: "\033[31m",    # red
    logging.CRITICAL: "\033[35m",  # magenta
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        color = _LEVEL_COLORS.get(record.levelno, "")
        if color and sys.stdout.isatty():
            return f"{color}{message}{_RESET}"
        return message


def _truncate(value: str, limit: int = MAX_LOG_FIELD_CHARS) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... [truncated, total {len(value)} chars]"


def redact_secrets(value: str) -> str:
    """Mask tokens that must never appear in logs."""
    patterns = (
        (re.compile(r"github_pat_[A-Za-z0-9_]+", re.IGNORECASE), "github_pat_[REDACTED]"),
        (
            re.compile(r"x-access-token:[^@\s]+@", re.IGNORECASE),
            "x-access-token:[REDACTED]@",
        ),
        (re.compile(r"GITHUB_TOKEN=[^\s&'\"]+"), "GITHUB_TOKEN=[REDACTED]"),
        (re.compile(r"GH_TOKEN=[^\s&'\"]+"), "GH_TOKEN=[REDACTED]"),
    )
    redacted = value
    for pattern, replacement in patterns:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def setup_logger(level: str = "INFO") -> logging.Logger:
    """Configure and return the bridge logger singleton."""
    logger = logging.getLogger(LOGGER_NAME)
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    if logger.handlers:
        logger.setLevel(numeric_level)
        for handler in logger.handlers:
            handler.setLevel(numeric_level)
        return logger

    logger.setLevel(numeric_level)
    logger.propagate = False

    plain_format = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(
        _ColorFormatter(plain_format, datefmt=date_format)
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(
        logging.Formatter(plain_format, datefmt=date_format)
    )

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


bridge_logger = logging.getLogger(LOGGER_NAME)


def log_incoming_request(
    method: str,
    path: str,
    client_ip: str,
) -> None:
    bridge_logger.info(
        "Incoming request | method=%s path=%s client_ip=%s",
        method,
        path,
        client_ip,
    )


def log_request_completed(
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
) -> None:
    bridge_logger.info(
        "Request completed | method=%s path=%s status=%s duration_ms=%.2f",
        method,
        path,
        status_code,
        duration_ms,
    )


def log_auth_failure(reason: str, client_ip: str, path: str) -> None:
    bridge_logger.warning(
        "Authentication failed | reason=%s client_ip=%s path=%s",
        reason,
        client_ip,
        path,
    )


def log_payload(
    task_id: str | int,
    project_id: str | int,
    project_area: str,
    dedicated_prompt: str,
    context: dict | None,
    github_url: str | None = None,
    target_branch: str = "staging",
) -> None:
    context_keys = list(context.keys()) if context else []
    bridge_logger.info(
        "Payload validated | task_id=%s project_id=%s project_area=%s "
        "prompt_length=%s context_keys=%s github_url=%s target_branch=%s",
        task_id,
        project_id,
        project_area,
        len(dedicated_prompt),
        context_keys,
        bool(github_url),
        target_branch,
    )


def log_callback_sent(
    task_id: str | int,
    status: str,
    http_status: int,
    attempt: int,
    failure_code: str | None = None,
) -> None:
    bridge_logger.info(
        "Callback sent | task_id=%s status=%s http_status=%s attempt=%s failure_code=%s",
        task_id,
        status,
        http_status,
        attempt,
        failure_code or "-",
    )


def log_progress_callback_sent(
    task_id: str | int,
    http_status: int,
    elapsed_sec: int,
    phase: str,
) -> None:
    bridge_logger.info(
        "Progress callback sent | task_id=%s phase=%s elapsed_sec=%s http_status=%s",
        task_id,
        phase,
        elapsed_sec,
        http_status,
    )


def log_progress_callback_failed(task_id: str | int, exc: BaseException) -> None:
    bridge_logger.warning(
        "Progress callback failed | task_id=%s error=%s",
        task_id,
        exc,
    )


def log_callback_failed(
    task_id: str | int,
    exc: BaseException,
    attempt: int,
    max_attempts: int,
) -> None:
    bridge_logger.error(
        "Callback failed | task_id=%s attempt=%s/%s error=%s",
        task_id,
        attempt,
        max_attempts,
        exc,
        exc_info=True,
    )


def log_callback_retry_wait(
    task_id: str | int,
    delay_sec: int,
    next_attempt: int,
) -> None:
    bridge_logger.warning(
        "Callback retry backoff | task_id=%s waiting_sec=%s before attempt=%s",
        task_id,
        delay_sec,
        next_attempt,
    )


def log_command(command: list[str], prompt_max_log: int = 500) -> None:
    display = list(command)
    for index, arg in enumerate(display):
        if arg in ("-p", "--print") and index + 1 < len(display):
            prompt = display[index + 1]
            if len(prompt) > prompt_max_log:
                display[index + 1] = f"{prompt[:prompt_max_log]}... [truncated]"
            break
    bridge_logger.info("Command generated | argv=%s", redact_secrets(repr(display)))


def log_process_output(stdout: str, stderr: str) -> None:
    bridge_logger.info("Process stdout | %s", _truncate(redact_secrets(stdout)))
    if stderr.strip():
        bridge_logger.info("Process stderr | %s", _truncate(redact_secrets(stderr)))
    else:
        bridge_logger.debug("Process stderr | (empty)")


def log_execution_timing(duration_ms: float, exit_code: int) -> None:
    bridge_logger.info(
        "Execution finished | duration_ms=%.2f exit_code=%s",
        duration_ms,
        exit_code,
    )


def log_retry_attempt(
    task_id: str | int,
    attempt: int,
    max_attempts: int,
) -> None:
    bridge_logger.warning(
        "Retry attempt | task_id=%s attempt=%s/%s",
        task_id,
        attempt,
        max_attempts,
    )


def log_retry_wait(
    task_id: str | int,
    delay_sec: int,
    next_attempt: int,
) -> None:
    bridge_logger.warning(
        "Retry backoff | task_id=%s waiting_sec=%s before attempt=%s",
        task_id,
        delay_sec,
        next_attempt,
    )


def log_task_failed_after_retries(
    task_id: str | int,
    max_attempts: int,
) -> None:
    bridge_logger.error(
        "Task failed after all retries | task_id=%s attempts=%s",
        task_id,
        max_attempts,
    )


def log_exception(message: str, exc: BaseException) -> None:
    bridge_logger.error("%s | %s", message, exc, exc_info=True)

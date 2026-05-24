"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent
load_dotenv(_BASE_DIR / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and configure it."
        )
    return value


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer for {name}: {raw!r}") from exc


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _require_url(name: str) -> str:
    value = _require(name)
    if not value.startswith(("http://", "https://")):
        raise RuntimeError(
            f"{name} must start with http:// or https:// (got {value!r})"
        )
    return value


class Settings:
    """Bridge configuration."""

    def __init__(self) -> None:
        self.base_dir: Path = _BASE_DIR
        self.bridge_api_key: str = _require("BRIDGE_API_KEY")
        self.agent_bin: str = os.getenv("AGENT_BIN", "agent").strip() or "agent"
        self.cursor_api_key: str | None = (
            os.getenv("CURSOR_API_KEY", "").strip() or None
        )
        default_ws = os.getenv("DEFAULT_WORKSPACE", "..").strip() or ".."
        self.default_workspace: Path = self._resolve_path(default_ws)
        # 0 = no wall-clock kill (long tasks keep running; use progress callbacks instead)
        self.agent_timeout_sec: int = _get_int("AGENT_TIMEOUT_SEC", 0)
        self.agent_idle_timeout_sec: int = _get_int("AGENT_IDLE_TIMEOUT_SEC", 180)
        # POST status=progress to n8n every N seconds while agent runs (0 = disabled)
        self.agent_progress_callback_sec: int = _get_int(
            "AGENT_PROGRESS_CALLBACK_SEC", 120
        )
        # Hard cap for watchdog (0 = disabled). Use e.g. 7200 for a 2h safety ceiling.
        self.task_max_runtime_sec: int = _get_int("TASK_MAX_RUNTIME_SEC", 0)
        self.agent_output_format: str = (
            os.getenv("AGENT_OUTPUT_FORMAT", "stream-json").strip() or "stream-json"
        )
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        self.host: str = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.port: int = _get_int("PORT", 8787)
        self.agent_use_wsl: bool = _get_bool("AGENT_USE_WSL", False)
        self.wsl_distro: str = os.getenv("WSL_DISTRO", "Ubuntu").strip() or "Ubuntu"
        self.n8n_callback_url: str = _require_url("N8N_CALLBACK_URL")
        self.callback_timeout_sec: int = _get_int("CALLBACK_TIMEOUT_SEC", 30)
        self.callback_summary_max_chars: int = _get_int(
            "CALLBACK_SUMMARY_MAX_CHARS", 15000
        )
        self.callback_max_attempts: int = _get_int("CALLBACK_MAX_ATTEMPTS", 5)
        self.callback_retry_delay_sec: int = _get_int("CALLBACK_RETRY_DELAY_SEC", 5)
        self.agent_max_attempts: int = _get_int("AGENT_MAX_ATTEMPTS", 5)
        self.agent_retry_delay_before_final_sec: int = _get_int(
            "AGENT_RETRY_DELAY_BEFORE_FINAL_SEC", 60
        )
        self.agent_worker_threads: int = _get_int("AGENT_WORKER_THREADS", 4)
        self.task_watchdog_grace_sec: int = _get_int("TASK_WATCHDOG_GRACE_SEC", 120)
        self.github_token: str | None = os.getenv("GITHUB_TOKEN", "").strip() or None
        self.git_commit_user_name: str = (
            os.getenv("GIT_COMMIT_USER_NAME", "Backclub Bridge").strip()
            or "Backclub Bridge"
        )
        self.git_commit_user_email: str = (
            os.getenv("GIT_COMMIT_USER_EMAIL", "bridge@users.noreply.github.com").strip()
            or "bridge@users.noreply.github.com"
        )
        self.dashboard_allow_remote: bool = _get_bool("DASHBOARD_ALLOW_REMOTE", False)
        self.dashboard_log_buffer_lines: int = _get_int(
            "DASHBOARD_LOG_BUFFER_LINES", 500
        )
        self.dashboard_max_history: int = _get_int("DASHBOARD_MAX_HISTORY", 200)

    def _resolve_path(self, raw: str) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            path = (self.base_dir / path).resolve()
        return path

    @property
    def desktop_dir(self) -> Path:
        return Path.home() / "Desktop"

    @property
    def master_workspace_dir(self) -> Path:
        return self.desktop_dir / "backclub-agent"

    def get_project_workspace(self, project_id: str | int) -> Path:
        """Resolve isolated workspace for a project under Desktop/backclub-agent."""
        return self.master_workspace_dir / f"project_{project_id}"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return singleton settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

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
        self.agent_timeout_sec: int = _get_int("AGENT_TIMEOUT_SEC", 600)
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        self.host: str = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.port: int = _get_int("PORT", 8787)
        self.agent_use_wsl: bool = _get_bool("AGENT_USE_WSL", False)
        self.wsl_distro: str = os.getenv("WSL_DISTRO", "Ubuntu").strip() or "Ubuntu"

    def _resolve_path(self, raw: str) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            path = (self.base_dir / path).resolve()
        return path

    def get_workspace(self, project_area: str) -> Path:
        """Resolve workspace directory for a project_area."""
        normalized = project_area.strip().replace("-", "_").upper()
        env_key = f"WORKSPACE_{normalized}"
        raw = os.getenv(env_key, "").strip()
        if raw:
            workspace = self._resolve_path(raw)
        else:
            workspace = self.default_workspace

        if not workspace.exists():
            raise FileNotFoundError(
                f"Workspace not found for project_area={project_area!r}: {workspace}"
            )
        if not workspace.is_dir():
            raise NotADirectoryError(
                f"Workspace path is not a directory for project_area={project_area!r}: "
                f"{workspace}"
            )
        return workspace


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return singleton settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

"""Cross-platform helpers (Windows / WSL / Unix)."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def windows_path_to_wsl(path: Path) -> str:
    """Convert a Windows path to a WSL mount path (/mnt/c/...)."""
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    if not drive:
        raise ValueError(f"Cannot convert path to WSL: {path}")
    rest = "/".join(resolved.parts[1:])
    return f"/mnt/{drive}/{rest}"


def build_wsl_command(
    command: list[str],
    workspace: Path,
    wsl_distro: str,
) -> list[str]:
    """Wrap a Linux agent command to run inside WSL."""
    wsl_workspace = windows_path_to_wsl(workspace)
    wsl_command = list(command)

    for index, arg in enumerate(wsl_command):
        if arg == "--workspace" and index + 1 < len(wsl_command):
            wsl_command[index + 1] = windows_path_to_wsl(Path(wsl_command[index + 1]))

    inner = " ".join(shlex.quote(part) for part in wsl_command)
    script = f"cd {shlex.quote(wsl_workspace)} && {inner}"
    return ["wsl", "-d", wsl_distro, "-e", "bash", "-lc", script]


def default_agent_install_hint() -> str:
    if is_windows():
        return (
            "On Windows, install Cursor CLI natively:\n"
            "  irm 'https://cursor.com/install?win32=true' | iex\n"
            "Or use WSL: set AGENT_USE_WSL=true in .env and install agent inside WSL."
        )
    return "Install with: curl https://cursor.com/install -fsS | bash"


def candidate_agent_bins() -> list[str]:
    """Binary names / paths to probe for Cursor CLI."""
    names = ["agent", "agent.exe", "cursor-agent", "cursor-agent.exe"]
    if is_windows():
        local_bin = Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin"
        names.extend(
            str(local_bin / name)
            for name in ("agent.exe", "agent.cmd", "cursor-agent.exe")
        )
    return names

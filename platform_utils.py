"""Cross-platform helpers (Windows / WSL / Unix)."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
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


def workspace_safe_directory_paths(workspace: Path) -> list[str]:
    """Paths for git safe.directory (Windows git + WSL git on /mnt/c share one .git/config)."""
    workspace = workspace.resolve()
    paths: list[str] = [str(workspace)]
    if workspace.parent != workspace:
        paths.append(str(workspace.parent))
    if is_windows():
        try:
            paths.append(windows_path_to_wsl(workspace))
            if workspace.parent != workspace:
                paths.append(windows_path_to_wsl(workspace.parent))
        except ValueError:
            pass
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def configure_wsl_git_global(workspace: Path, wsl_distro: str) -> None:
    """Pre-trust /mnt/c workspaces in WSL git before the agent runs (avoids dubious ownership)."""
    if not is_windows():
        return
    try:
        paths = workspace_safe_directory_paths(workspace)
    except ValueError:
        return
    token = os.getenv("GITHUB_TOKEN", "").strip()
    for safe_path in paths:
        if not safe_path.startswith("/mnt/"):
            continue
        subprocess.run(
            [
                "wsl",
                "-d",
                wsl_distro,
                "-e",
                "git",
                "config",
                "--global",
                "--add",
                "safe.directory",
                safe_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    if token:
        auth_prefix = f"https://x-access-token:{token}@github.com/"
        subprocess.run(
            [
                "wsl",
                "-d",
                wsl_distro,
                "-e",
                "git",
                "config",
                "--global",
                f"url.{auth_prefix}.insteadOf",
                "https://github.com/",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
            check=False,
        )


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
    git_name = os.getenv("GIT_COMMIT_USER_NAME", "Backclub Bridge").strip()
    git_email = os.getenv(
        "GIT_COMMIT_USER_EMAIL", "bridge@users.noreply.github.com"
    ).strip()
    token = os.getenv("GITHUB_TOKEN", "").strip()
    token_exports = ""
    if token:
        token_exports = (
            f"export GITHUB_TOKEN={shlex.quote(token)} && "
            f"export GH_TOKEN={shlex.quote(token)} && "
        )
    safe_dirs = " ".join(
        shlex.quote(path)
        for path in workspace_safe_directory_paths(workspace)
        if path.startswith("/mnt/")
    )
    safe_setup = ""
    if safe_dirs:
        safe_setup = (
            f"for _d in {safe_dirs}; do "
            'git config --global --add safe.directory "$_d" 2>/dev/null || true; '
            "done && "
        )
    script = (
        'export PATH="$HOME/.local/bin:$PATH" && '
        f"{safe_setup}"
        f"{token_exports}"
        f'export GIT_AUTHOR_NAME={shlex.quote(git_name)} && '
        f'export GIT_AUTHOR_EMAIL={shlex.quote(git_email)} && '
        f'export GIT_COMMITTER_NAME={shlex.quote(git_name)} && '
        f'export GIT_COMMITTER_EMAIL={shlex.quote(git_email)} && '
        f"cd {shlex.quote(wsl_workspace)} && {inner}"
    )
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
        cursor_agent_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent"
        names.extend(
            str(cursor_agent_dir / name)
            for name in ("agent.cmd", "agent.exe", "cursor-agent.exe")
        )
    return names


_VERSION_DIR_PATTERN = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}-[a-f0-9]+$")


def _version_sort_key(name: str) -> int:
    date_part = name.split("-", 1)[0]
    year, month, day = date_part.split(".")
    return int(f"{year}{month.zfill(2)}{day.zfill(2)}")


def resolve_agent_argv_prefix(resolved_bin: str) -> list[str]:
    """Return argv prefix for agent, bypassing Windows .cmd quoting issues."""
    if not is_windows():
        return [resolved_bin]

    path = Path(resolved_bin)
    if path.suffix.lower() not in {".cmd", ".bat"}:
        return [resolved_bin]

    script_dir = path.parent
    local_node = script_dir / "node.exe"
    local_index = script_dir / "index.js"
    if local_node.is_file() and local_index.is_file():
        return [str(local_node), str(local_index)]

    versions_dir = script_dir / "versions"
    if not versions_dir.is_dir():
        return [resolved_bin]

    version_dirs = [
        entry
        for entry in versions_dir.iterdir()
        if entry.is_dir() and _VERSION_DIR_PATTERN.match(entry.name)
    ]
    if not version_dirs:
        return [resolved_bin]

    latest = max(version_dirs, key=lambda entry: _version_sort_key(entry.name))
    node = latest / "node.exe"
    index = latest / "index.js"
    if node.is_file() and index.is_file():
        return [str(node), str(index)]

    return [resolved_bin]


def subprocess_creation_flags() -> int:
    """Windows flags so child processes do not attach to the bridge console."""
    if not is_windows():
        return 0
    # CREATE_NO_WINDOW — avoid interactive prompts in the uvicorn terminal
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def non_interactive_env(base: dict[str, str]) -> dict[str, str]:
    """Env vars that reduce shell/git/powershell interactive prompts in headless runs."""
    env = dict(base)
    env.setdefault("CI", "true")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "Never")
    env.setdefault("POWERSHELL_UPDATECHECK", "Off")
    if is_windows():
        env.setdefault("COMSPEC", os.environ.get("COMSPEC", "cmd.exe"))
    return env


def kill_process_tree(pid: int) -> None:
    """Terminate a process and its children (needed when halting Cursor agent on Windows)."""
    if pid <= 0:
        return
    if is_windows():
        import subprocess as sp

        sp.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, OSError, AttributeError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

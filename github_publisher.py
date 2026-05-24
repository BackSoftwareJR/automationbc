"""Ensure git changes are committed and pushed after agent tasks."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from config import get_settings

PublishBranch = Literal["staging", "main"]
ProjectKind = Literal["static", "node"]
from logger_config import bridge_logger, redact_secrets
from platform_utils import (
    configure_wsl_git_global,
    is_windows,
    non_interactive_env,
    workspace_safe_directory_paths,
)


class GitLifecycleError(RuntimeError):
    """Raised when a git lifecycle step fails; carries full subprocess output."""

    def __init__(
        self,
        *,
        step: str,
        command: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.step = step
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        cmd_str = " ".join(command)
        detail = (stderr or stdout).strip()
        super().__init__(f"git {step} failed (exit {returncode}): {detail}\ncommand: {cmd_str}")

    def log_message(self) -> str:
        parts = [
            f"git {self.step} failed (exit {self.returncode})",
            f"command: {' '.join(self.command)}",
        ]
        if self.stderr.strip():
            parts.append(f"stderr:\n{self.stderr.strip()}")
        if self.stdout.strip():
            parts.append(f"stdout:\n{self.stdout.strip()}")
        return "\n".join(parts)


def _git_identity() -> tuple[str, str]:
    settings = get_settings()
    return settings.git_commit_user_name, settings.git_commit_user_email


def _apply_git_identity_env(env: dict[str, str]) -> None:
    name, email = _git_identity()
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        env[key] = name if key.endswith("_NAME") else email


def _ensure_repo_git_identity(workspace: Path) -> None:
    """Set local repo identity so git commit works without global Windows/WSL config."""
    name, email = _git_identity()
    _run_git(workspace, ["config", "user.name", name], apply_identity=False)
    _run_git(workspace, ["config", "user.email", email], apply_identity=False)


def _authenticated_github_url(github_url: str) -> str:
    """Embed PAT in HTTPS URL so clone works before .git exists (local git config cannot)."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token or "github.com" not in github_url:
        return github_url
    parsed = urlparse(github_url)
    if parsed.scheme not in ("http", "https"):
        return github_url
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        return github_url
    return f"https://x-access-token:{token}@github.com{path}.git"


def _configure_git_https_auth(workspace: Path) -> None:
    """Non-interactive GitHub HTTPS for agent (WSL) and bridge on shared /mnt/c workspaces."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    workspace = workspace.resolve()
    for safe_path in workspace_safe_directory_paths(workspace):
        _run_git(
            workspace,
            ["config", "--add", "safe.directory", safe_path],
            apply_identity=False,
        )
    if is_windows():
        settings = get_settings()
        if settings.agent_use_wsl:
            configure_wsl_git_global(workspace, settings.wsl_distro)
    if not token:
        return
    auth_prefix = f"https://x-access-token:{token}@github.com/"
    _run_git(
        workspace,
        [
            "config",
            f"url.{auth_prefix}.insteadOf",
            "https://github.com/",
        ],
        apply_identity=False,
    )


def _run_git(
    workspace: Path,
    args: list[str],
    *,
    timeout: int = 120,
    apply_identity: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = non_interactive_env(os.environ.copy())
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        env["GITHUB_TOKEN"] = token
        env["GH_TOKEN"] = token
    if apply_identity:
        _apply_git_identity_env(env)
    return subprocess.run(
        ["git", *args],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        env=env,
        check=False,
    )


def _log_git_result(step: str, proc: subprocess.CompletedProcess[str]) -> None:
    cmd_str = redact_secrets(" ".join(proc.args) if proc.args else "git")
    stdout = redact_secrets((proc.stdout or "").strip())
    stderr = redact_secrets((proc.stderr or "").strip())
    if proc.returncode == 0:
        bridge_logger.info(
            "git %s OK | returncode=0 | command=%s",
            step,
            cmd_str,
        )
        if stdout:
            bridge_logger.info("git %s stdout:\n%s", step, stdout)
        if stderr:
            bridge_logger.info("git %s stderr:\n%s", step, stderr)
    else:
        bridge_logger.error(
            "git %s FAILED | returncode=%s | command=%s",
            step,
            proc.returncode,
            cmd_str,
        )
        if stdout:
            bridge_logger.error("git %s stdout:\n%s", step, stdout)
        if stderr:
            bridge_logger.error("git %s stderr:\n%s", step, stderr)


def _run_git_checked(
    workspace: Path,
    args: list[str],
    *,
    step: str,
    timeout: int = 120,
    apply_identity: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    try:
        proc = _run_git(
            workspace,
            args,
            timeout=timeout,
            apply_identity=apply_identity,
        )
    except subprocess.TimeoutExpired as exc:
        bridge_logger.critical(
            "git %s timed out after %ss | command=%s",
            step,
            timeout,
            " ".join(command),
        )
        timeout_msg = f"timeout after {timeout}s"
        raise GitLifecycleError(
            step=step,
            command=command,
            returncode=-1,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or timeout_msg),
        ) from exc

    _log_git_result(step, proc)
    if proc.returncode != 0:
        raise GitLifecycleError(
            step=step,
            command=command,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
    return proc


def infer_project_kind(workspace: Path) -> ProjectKind:
    """
    Classify workspace: Node.js if package.json at repo root, else static HTML.
    """
    workspace = workspace.resolve()
    if (workspace / "package.json").is_file():
        bridge_logger.info("Project kind: node.js (package.json at repo root)")
        return "node"
    bridge_logger.info("Project kind: static HTML (no package.json at repo root)")
    return "static"


def infer_publish_branch(
    workspace: Path,
    *,
    project_kind: ProjectKind | None = None,
) -> PublishBranch:
    """Node.js -> main; static HTML -> staging (never the opposite)."""
    kind = project_kind or infer_project_kind(workspace)
    if kind == "node":
        return "main"
    return "staging"


def _required_branch_for_kind(project_kind: ProjectKind) -> PublishBranch:
    return "main" if project_kind == "node" else "staging"


def _list_remote_branches(workspace: Path, remote: str) -> set[str]:
    """Return short branch names present on remote (e.g. main, staging)."""
    proc = _run_git(workspace, ["branch", "-r"], timeout=30)
    branches: set[str] = set()
    if proc.returncode != 0:
        return branches
    prefix = f"{remote}/"
    for line in proc.stdout.splitlines():
        name = line.strip()
        if not name.startswith(prefix) or "HEAD" in name:
            continue
        short = name[len(prefix) :]
        if short:
            branches.add(short)
    return branches


def resolve_remote_branch(
    workspace: Path,
    requested: str,
    *,
    remote: str | None = None,
    project_kind: ProjectKind | None = None,
) -> str:
    """
    Ensure the publish branch exists on the remote.
    Static HTML must use staging only; Node.js must use main only — no cross-fallback.
    """
    workspace = workspace.resolve()
    kind = project_kind or infer_project_kind(workspace)
    required = _required_branch_for_kind(kind)
    if requested != required:
        bridge_logger.warning(
            "Branch %s requested but project kind %s requires %s — using %s",
            requested,
            kind,
            required,
            required,
        )
        requested = required

    remote = remote or _repo_remote_name(workspace)
    available = _list_remote_branches(workspace, remote)
    if not available:
        raise GitLifecycleError(
            step="resolve branch",
            command=["git", "branch", "-r"],
            returncode=-1,
            stdout="",
            stderr=f"No remote branches found for {remote!r} after fetch",
        )

    available_str = ", ".join(sorted(available))
    if requested in available:
        return requested

    if kind == "static":
        raise GitLifecycleError(
            step="resolve branch",
            command=["git", "branch", "-r"],
            returncode=-1,
            stdout="",
            stderr=(
                "Static HTML sites must publish to branch 'staging' (pre-production). "
                f"Branch 'staging' is missing on {remote}. "
                f"Available remote branches: {available_str}. "
                "Create 'staging' on GitHub from main and retry. "
                "The bridge will never push HTML changes directly to 'main'."
            ),
        )

    raise GitLifecycleError(
        step="resolve branch",
        command=["git", "branch", "-r"],
        returncode=-1,
        stdout="",
        stderr=(
            "Node.js projects must publish to branch 'main'. "
            f"Branch 'main' is missing on {remote}. "
            f"Available remote branches: {available_str}. "
            "Check github_url points to the correct repository."
        ),
    )


def assert_checked_out_branch(workspace: Path, expected: str) -> None:
    """Verify HEAD is on the branch we intend to publish (safety net for HTML)."""
    proc = _run_git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
    if proc.returncode != 0:
        return
    current = (proc.stdout or "").strip()
    if current != expected:
        raise GitLifecycleError(
            step="verify branch",
            command=["git", "rev-parse", "--abbrev-ref", "HEAD"],
            returncode=1,
            stdout=proc.stdout or "",
            stderr=(
                f"Expected branch {expected!r} but workspace is on {current!r}. "
                "Refusing to run agent or publish on the wrong branch."
            ),
        )


def _checkout_needs_track_branch(stderr: str) -> bool:
    lowered = stderr.lower()
    return (
        "did not match any file" in lowered
        or "pathspec" in lowered
        or "unknown revision" in lowered
        or "not found" in lowered
    )


def _checkout_branch(workspace: Path, branch: str) -> None:
    """Checkout branch; stash on dirty tree; track origin/{branch} when missing locally."""
    checkout = _run_git(workspace, ["checkout", branch], timeout=120)
    _log_git_result(f"checkout {branch}", checkout)

    if checkout.returncode == 0:
        return

    stderr = checkout.stderr or ""
    stdout = checkout.stdout or ""

    if "would be overwritten" in stderr.lower() or "local changes" in stderr.lower():
        bridge_logger.warning(
            "git checkout blocked by local changes — stashing | branch=%s stderr=%s",
            branch,
            stderr.strip(),
        )
        stash = _run_git(
            workspace,
            ["stash", "push", "-u", "-m", "bridge pre-checkout"],
            timeout=120,
        )
        _log_git_result("stash pre-checkout", stash)
        if stash.returncode != 0:
            raise GitLifecycleError(
                step="stash pre-checkout",
                command=["git", "stash", "push", "-u", "-m", "bridge pre-checkout"],
                returncode=stash.returncode,
                stdout=stash.stdout or "",
                stderr=stash.stderr or "",
            )
        retry = _run_git(workspace, ["checkout", branch], timeout=120)
        _log_git_result(f"checkout {branch} (after stash)", retry)
        if retry.returncode == 0:
            return
        stderr = retry.stderr or ""
        stdout = retry.stdout or ""

    if _checkout_needs_track_branch(stderr):
        track = _run_git(
            workspace,
            ["checkout", "-B", branch, f"origin/{branch}"],
            timeout=120,
        )
        _log_git_result(f"checkout -B {branch} origin/{branch}", track)
        if track.returncode == 0:
            return
        raise GitLifecycleError(
            step=f"checkout -B {branch} origin/{branch}",
            command=["git", "checkout", "-B", branch, f"origin/{branch}"],
            returncode=track.returncode,
            stdout=track.stdout or "",
            stderr=track.stderr or "",
        )

    raise GitLifecycleError(
        step=f"checkout {branch}",
        command=["git", "checkout", branch],
        returncode=checkout.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def sync_workspace_to_branch(
    workspace: Path,
    target_branch: str | None = None,
    *,
    auto_detect: bool = True,
) -> tuple[str, str]:
    """
    Pre-agent: fetch, checkout target branch, pull from origin.
    Returns (log text, effective branch name used on remote).
    Caller must ensure prepare_workspace ran first (.git exists).
    When auto_detect is True (default), branch is inferred from project type.
    """
    workspace = workspace.resolve()
    if not (workspace / ".git").exists():
        raise GitLifecycleError(
            step="sync",
            command=["git"],
            returncode=-1,
            stdout="",
            stderr="workspace is not a git repository",
        )

    notes: list[str] = []
    remote = _repo_remote_name(workspace)

    _run_git_checked(workspace, ["fetch", remote], step="fetch", timeout=180)
    notes.append(f"git fetch {remote} OK")

    project_kind = infer_project_kind(workspace)
    desired_branch = infer_publish_branch(workspace, project_kind=project_kind)
    stack = "Node.js" if project_kind == "node" else "static HTML"
    notes.append(f"Auto-detected: {stack} → branch {desired_branch}")

    effective_branch = resolve_remote_branch(
        workspace,
        desired_branch,
        remote=remote,
        project_kind=project_kind,
    )
    if effective_branch != desired_branch:
        notes.append(
            f"Note: branch {desired_branch} not on remote; using {effective_branch}"
        )

    _checkout_branch(workspace, effective_branch)
    notes.append(f"git checkout {effective_branch} OK")
    assert_checked_out_branch(workspace, effective_branch)

    _run_git_checked(
        workspace,
        ["pull", remote, effective_branch],
        step=f"pull {effective_branch}",
        timeout=180,
    )
    notes.append(f"git pull {remote} {effective_branch} OK")

    bridge_logger.info(
        "Git branch sync OK | workspace=%s desired=%s effective=%s",
        workspace,
        desired_branch,
        effective_branch,
    )
    return "\n".join(notes), effective_branch


def _repo_remote_name(workspace: Path) -> str:
    result = _run_git(workspace, ["remote"], timeout=15)
    if result.returncode != 0:
        return "origin"
    remotes = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return remotes[0] if remotes else "origin"


def _workspace_has_content(workspace: Path) -> bool:
    if not workspace.exists():
        return False
    return any(workspace.iterdir())


def _backup_non_git_workspace(workspace: Path) -> str | None:
    """Move a dirty non-git folder aside so git clone can succeed."""
    if not _workspace_has_content(workspace):
        return None
    backup = workspace.parent / f"{workspace.name}_orphan_{int(time.time())}"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    shutil.move(str(workspace), str(backup))
    workspace.mkdir(parents=True, exist_ok=True)
    bridge_logger.warning(
        "Workspace was not a git repo; moved contents to %s",
        backup,
    )
    return f"Non-git workspace backed up to {backup.name}"


def prepare_workspace(workspace: Path, github_url: str) -> str:
    """
    Ensure workspace is a git checkout of github_url before the agent runs.
    Handles non-empty folders without .git (common when agent wrote files outside git).
    """
    workspace = workspace.resolve()
    git_dir = workspace / ".git"
    notes: list[str] = []

    if git_dir.exists():
        _ensure_repo_git_identity(workspace)
        _configure_git_https_auth(workspace)
        notes.append("Repository ready (branch sync will fetch/checkout/pull)")
        return "\n".join(notes)

    backed_up = _backup_non_git_workspace(workspace)
    if backed_up:
        notes.append(backed_up)

    workspace.mkdir(parents=True, exist_ok=True)
    token = os.getenv("GITHUB_TOKEN", "").strip()
    clone_url = _authenticated_github_url(github_url)
    if token and clone_url == github_url and "github.com" in github_url:
        bridge_logger.warning(
            "GITHUB_TOKEN is set but could not build auth URL for %s",
            github_url,
        )
    elif not token and "github.com" in github_url:
        bridge_logger.warning(
            "GITHUB_TOKEN is not set — git clone may fail for private repos (non-interactive)"
        )
    clone = _run_git(workspace, ["clone", clone_url, "."], timeout=600)
    if clone.returncode != 0:
        detail = (clone.stderr or clone.stdout).strip()
        hint = ""
        if "authentication" in detail.lower() or "could not read username" in detail.lower():
            hint = (
                " Set GITHUB_TOKEN in .env (PAT with repo scope) and restart the bridge."
            )
        raise RuntimeError(f"git clone failed: {detail}{hint}")
    notes.append(f"Repository cloned from {github_url}")
    _ensure_repo_git_identity(workspace)
    _configure_git_https_auth(workspace)
    return "\n".join(notes)


def publish_task_changes(
    workspace: Path,
    *,
    task_id: str | int,
    github_url: str | None,
    target_branch: str | None = None,
) -> tuple[str, bool]:
    """
    Commit and push any local changes. Branch is auto-detected (HTML→staging, Node→main).
    Returns (log_message, push_ok). push_ok is True if no changes needed OR push succeeded.
    """
    if not github_url:
        return ("No github_url — skip publish", True)

    workspace = workspace.resolve()
    notes: list[str] = []
    remote = _repo_remote_name(workspace)
    fetch = _run_git(workspace, ["fetch", remote], timeout=180)
    _log_git_result("fetch (publish)", fetch)
    project_kind = infer_project_kind(workspace)
    desired_branch = infer_publish_branch(workspace, project_kind=project_kind)
    notes.append(
        f"Auto-detected: {'Node.js' if project_kind == 'node' else 'static HTML'} "
        f"→ branch {desired_branch}"
    )
    effective_branch = resolve_remote_branch(
        workspace,
        desired_branch,
        remote=remote,
        project_kind=project_kind,
    )
    assert_checked_out_branch(workspace, effective_branch)
    if effective_branch != desired_branch:
        notes.append(
            f"Note: branch {desired_branch} not on remote; using {effective_branch}"
        )

    status = _run_git(workspace, ["status", "--porcelain"], timeout=30)
    _log_git_result("status", status)
    if status.returncode != 0:
        return (
            f"git status failed: {(status.stderr or status.stdout).strip()}",
            False,
        )

    if not status.stdout.strip():
        notes.append("No local changes to publish")
        return ("\n".join(notes), True)

    _ensure_repo_git_identity(workspace)

    try:
        _run_git_checked(workspace, ["add", "."], step="add", timeout=60)
    except GitLifecycleError as exc:
        return (exc.log_message(), False)

    notes.append("git add . OK")

    message = f"Automated update by AI agent - Task {task_id}"
    commit_proc = _run_git(workspace, ["commit", "-m", message], timeout=60)
    _log_git_result("commit", commit_proc)

    if commit_proc.returncode != 0:
        combined = (commit_proc.stderr or commit_proc.stdout or "").lower()
        if "nothing to commit" in combined:
            notes.append("No changes to commit after git add")
            return ("\n".join(notes), True)
        return (
            f"git commit failed: {(commit_proc.stderr or commit_proc.stdout).strip()}",
            False,
        )

    notes.append(f"Committed: {message}")

    auth_url = _authenticated_github_url(github_url)
    if auth_url != github_url:
        push_args = [
            "push",
            auth_url,
            f"refs/heads/{effective_branch}:refs/heads/{effective_branch}",
        ]
    else:
        push_args = ["push", remote, effective_branch]

    try:
        _run_git_checked(
            workspace, push_args, step=f"push {effective_branch}", timeout=300
        )
    except GitLifecycleError as exc:
        host = urlparse(github_url).netloc or "remote"
        return (
            "\n".join(notes)
            + f"\ngit push failed ({host}): {exc.log_message()}",
            False,
        )

    notes.append(f"Pushed to {remote}/{effective_branch} successfully")
    bridge_logger.info(
        "GitHub publish OK | task_id=%s workspace=%s branch=%s remote=%s",
        task_id,
        workspace,
        effective_branch,
        remote,
    )
    return ("\n".join(notes), True)


def append_publish_note_to_stderr(stderr: str, publish_log: str) -> str:
    block = f"--- bridge git publish ---\n{publish_log.strip()}"
    if stderr.strip():
        return f"{stderr.rstrip()}\n\n{block}"
    return block

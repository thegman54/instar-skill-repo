"""
Repo Tools Base - Path validation, workspace resolution, and security utilities.

SECURITY: All file operations MUST go through validate_path().
This prevents directory traversal and access to unauthorized projects.

Repository access is controlled by the skill_repo_config table,
managed through the admin panel. Tools call check_repo_access()
to validate access and get the permitted operation level.
"""

import os
import re
from pathlib import Path
import structlog

log = structlog.get_logger()

# Workspace root inside the tool-executor container
WORKSPACE_ROOT = "/projects/workspace"

# Blocked paths - NEVER allow access regardless of grant
BLOCKED_PATHS = [
    "/projects/project-instar",  # Self-modification forbidden
]

# Valid repo format: owner/repo-name
REPO_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$')

# Pattern to strip tokens from git URLs in error messages
_TOKEN_URL_PATTERN = re.compile(r'https://[^@]+@github\.com/')

# Access levels and what operations they allow
ACCESS_LEVELS = {
    "full":      {"clone", "read", "list", "grep", "status", "diff", "log", "branch", "write", "commit", "push", "pr", "actions"},
    "no-push":   {"clone", "read", "list", "grep", "status", "diff", "log", "branch", "write", "commit"},
    "read-only": {"clone", "read", "list", "grep", "status", "diff", "log", "branch"},
}


def sanitize_stderr(stderr: str) -> str:
    """Strip any embedded tokens from git stderr output before returning to bot."""
    return _TOKEN_URL_PATTERN.sub('https://github.com/', stderr)


async def check_repo_access(repo: str, operation: str = "read") -> tuple[bool, str, str, str]:
    """
    Check if a repo is allowed and the operation is permitted.

    Queries the skill_repo_config table for access control.

    Args:
        repo: Repository in "owner/repo" format
        operation: The operation being attempted (clone, read, write, push, pr, etc.)

    Returns:
        (valid, workspace_path, access_level, error_message)
    """
    if not repo:
        return False, "", "", "No repo specified"

    if not REPO_PATTERN.match(repo):
        return False, "", "", f"Invalid repo format '{repo}' — use 'owner/repo'"

    # Check DB for access config
    from ...db import get_pool
    pool = get_pool()
    if not pool:
        log.warning("repo_access_check_no_db")
        return False, "", "", "Database not available for access check"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT repo, access, enabled FROM skill_repo_config WHERE repo = $1",
            repo,
        )

    if not row:
        return False, "", "", f"Repository '{repo}' is not configured — add it in the Repos admin panel"

    if not row["enabled"]:
        return False, "", "", f"Repository '{repo}' is disabled"

    access = row["access"]
    allowed_ops = ACCESS_LEVELS.get(access, set())

    if operation not in allowed_ops:
        return False, "", access, f"Operation '{operation}' not allowed — repo has '{access}' access"

    # Resolve workspace path
    workspace_path = os.path.join(WORKSPACE_ROOT, repo)

    # Check blocked paths
    resolved = str(Path(workspace_path).resolve())
    for blocked in BLOCKED_PATHS:
        if resolved.startswith(blocked) or blocked.startswith(resolved):
            log.warning("blocked_repo_access", repo=repo, resolved=resolved)
            return False, "", "", "Access to this repository is forbidden"

    return True, workspace_path, access, ""


def validate_path(project_root: str, relative_path: str) -> tuple[bool, str, str]:
    """
    Validate and resolve a path within a project.

    SECURITY: This is the critical boundary. All file tools MUST use this.

    Args:
        project_root: The project root directory
        relative_path: The path requested by the bot

    Returns:
        (valid, absolute_path, error_message)
    """
    if not project_root or not relative_path:
        return False, "", "Missing project_root or path"

    root = Path(project_root).resolve()

    # Check if project root itself is blocked
    for blocked in BLOCKED_PATHS:
        if str(root).startswith(blocked) or blocked.startswith(str(root)):
            log.warning("blocked_project_access", root=str(root), blocked=blocked)
            return False, "", "Access to this project is forbidden"

    # Resolve the full path
    if os.path.isabs(relative_path):
        full_path = Path(relative_path).resolve()
    else:
        full_path = (root / relative_path).resolve()

    # Critical check: is the resolved path within the project root?
    try:
        full_path.relative_to(root)
    except ValueError:
        log.warning("path_traversal_attempt",
                   requested=relative_path,
                   resolved=str(full_path),
                   root=str(root))
        return False, "", "Path is outside project boundary"

    return True, str(full_path), ""


def validate_project_root(project_root: str) -> tuple[bool, str]:
    """
    Validate a project root directory exists and is accessible.

    Args:
        project_root: Path to validate

    Returns:
        (valid, error_message)
    """
    if not project_root:
        return False, "No project root specified"

    root = Path(project_root)

    resolved = str(root.resolve())
    for blocked in BLOCKED_PATHS:
        if resolved.startswith(blocked) or blocked.startswith(resolved):
            return False, "Access to this project is forbidden"

    if not root.exists():
        return False, f"Project directory does not exist: {project_root}"

    if not root.is_dir():
        return False, f"Project path is not a directory: {project_root}"

    return True, ""

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

# Workspace roots inside the tool-executor container
WORKSPACE_ROOT = "/projects/workspace"           # Legacy flat layout (no workspace)
WORKSPACES_ROOT = "/projects/workspaces"          # Workspace-scoped layout

# Blocked paths - NEVER allow access regardless of grant
BLOCKED_PATHS = [
    "/projects/project-instar",  # Self-modification forbidden
]

# Let git operate on checkouts this process does not own.
#
# THE MISMATCH IS DELIBERATE ON BOTH SIDES. This container runs as root; every clone is
# handed to uid 1000 by _chown_workspace so the expert-runner, which runs unprivileged, can
# use the same checkout. Root can read and write those files perfectly well — it is only
# git's ownership CHECK that objects, and it objects to every git invocation in this skill:
# status, log, diff, branch, commit, push, and the fetch inside repo_clone.
#
# So safe.directory is the remedy git documents for exactly this, not a workaround. The check
# exists to stop you running a repo owned by an untrusted OTHER user; here the other user is
# one we created, on a path this skill already gates through check_repo_access.
#
# Set as GIT_CONFIG_* env rather than written into a config file so it applies to every
# subprocess without a file on disk to drift, and so it cannot leak into the checkouts
# themselves — a `git config` write would land in a repository the bots push from.
GIT_SAFE_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": "*",
}


def git_env(extra: dict = None) -> dict:
    """Environment for any git subprocess in this skill.

    Always use this instead of os.environ. GIT_TERMINAL_PROMPT=0 matters as much as the
    ownership setting: without it a git that wants credentials blocks on a prompt nobody
    can answer, and the call hangs until its timeout instead of failing.
    """
    env = {**os.environ, **GIT_SAFE_ENV, "GIT_TERMINAL_PROMPT": "0"}
    if extra:
        env.update(extra)
    return env

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


# Operations a cross-workspace grant may cover. Reading a neighbouring repository answers a
# question; writing to one changes somebody else's work. `repo_read_all` therefore grants only
# these, and never write/commit/push/pr — so read-many can never quietly become
# read-many/write-many, which is the combination the whole confinement model exists to prevent.
READ_OPS = {"clone", "read", "list", "grep", "status", "diff", "log", "branch", "actions"}


async def check_repo_access(repo: str, operation: str = "read",
                            profile_slug: str = None) -> tuple[bool, str, str, str, str]:
    """
    Check if a repo is allowed and the operation is permitted, FOR THIS PROFILE.

    Access is scoped to the calling profile's workspace. It used to be scoped to nothing at
    all — the lookup matched on repository name alone, so any profile holding repo_read could
    read every repository in skill_repo_config. Nothing leaked, because the table happens to
    hold only Cenora repositories; but the protection was the contents of a table rather than
    a rule, and adding one unrelated repository would have exposed it to every profile with no
    warning.

    A profile may hold `repo_read_all` to read outside its workspace — the coordinator does,
    because cross-repo questions are its entire job. That grant covers READ_OPS only.

    Args:
        repo: Repository in "owner/repo" format
        operation: The operation being attempted (clone, read, write, push, pr, etc.)
        profile_slug: The calling profile. Without it, only same-workspace access is possible.

    Returns:
        (valid, workspace_path, access_level, error_message, branch)
    """
    if not repo:
        return False, "", "", "No repo specified", None

    if not REPO_PATTERN.match(repo):
        return False, "", "", f"Invalid repo format '{repo}' — use 'owner/repo'", None

    # Check DB for access config
    from ...db import get_pool
    pool = get_pool()
    if not pool:
        log.warning("repo_access_check_no_db")
        return False, "", "", "Database not available for access check", None

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT r.repo, r.access, r.enabled, r.workspace_id, r.branch,
                   w.slug AS workspace_slug
            FROM skill_repo_config r
            LEFT JOIN workspaces w ON w.id = r.workspace_id
            WHERE r.repo = $1
        """, repo)

    if not row:
        return False, "", "", f"Repository '{repo}' is not configured — add it in the Repos admin panel", None

    if not row["enabled"]:
        return False, "", "", f"Repository '{repo}' is disabled", None

    access = row["access"]
    allowed_ops = ACCESS_LEVELS.get(access, set())

    if operation not in allowed_ops:
        return False, "", access, f"Operation '{operation}' not allowed — repo has '{access}' access", None

    # --- scope: is this repo in the caller's workspace, or is the caller granted breadth? ---
    #
    # Repos with no workspace are the legacy flat layout. There is nothing to compare against,
    # so scoping cannot apply and they behave as before rather than becoming unreachable.
    if row["workspace_id"]:
        # No caller identity means no way to prove the repo is in scope, so it is not. This
        # branch was originally `and profile_slug`, which skipped the whole check when the
        # profile was unknown — an unidentified caller got unrestricted read, which is the
        # exact hole being closed. Fail closed and say why.
        if not profile_slug:
            return False, "", access, (
                f"Cannot scope access to '{repo}': the calling profile is unknown. "
                "Repository access is per-profile; an unidentified caller gets none."), None

        async with pool.acquire() as conn:
            prof = await conn.fetchrow(
                "SELECT workspace_id, repo_read_all FROM bot_profiles WHERE slug = $1",
                profile_slug)
        same_workspace = bool(prof and prof["workspace_id"]
                              and str(prof["workspace_id"]) == str(row["workspace_id"]))
        read_all = bool(prof and prof["repo_read_all"])

        if not same_workspace:
            if operation not in READ_OPS:
                return False, "", access, (
                    f"'{repo}' is outside this profile's workspace, and '{operation}' writes. "
                    "Cross-workspace access is read-only, always."), None
            if not read_all:
                return False, "", access, (
                    f"'{repo}' is not in this profile's workspace. Reading outside it requires "
                    "the repo_read_all grant, which this profile does not have."), None

    # Resolve workspace path — workspace-scoped if assigned, legacy flat otherwise
    if row["workspace_slug"]:
        workspace_path = os.path.join(WORKSPACES_ROOT, row["workspace_slug"], "repos", repo)
    else:
        workspace_path = os.path.join(WORKSPACE_ROOT, repo)

    configured_branch = row["branch"]  # None if not set

    # Check blocked paths
    resolved = str(Path(workspace_path).resolve())
    for blocked in BLOCKED_PATHS:
        if resolved.startswith(blocked) or blocked.startswith(resolved):
            log.warning("blocked_repo_access", repo=repo, resolved=resolved)
            return False, "", "", "Access to this repository is forbidden", None

    return True, workspace_path, access, "", configured_branch


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

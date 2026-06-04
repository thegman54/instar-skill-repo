"""
Repo Tools Base - Path validation and security utilities.

SECURITY: All file operations MUST go through validate_path().
This prevents directory traversal and access to unauthorized projects.
"""

import os
from pathlib import Path
import structlog

log = structlog.get_logger()

# Blocked paths - NEVER allow access regardless of grant
# Note: In container, /home/g/projects is mounted as /projects
BLOCKED_PATHS = [
    "/projects/project-instar",  # Self-modification forbidden
]


def validate_path(project_root: str, relative_path: str) -> tuple[bool, str, str]:
    """
    Validate and resolve a path within a project.

    SECURITY: This is the critical boundary. All file tools MUST use this.

    Args:
        project_root: The allowed project root (from grant)
        relative_path: The path requested by the bot

    Returns:
        (valid, absolute_path, error_message)
        - valid: True if path is allowed
        - absolute_path: Resolved absolute path (only if valid)
        - error_message: Why validation failed (only if invalid)
    """
    if not project_root or not relative_path:
        return False, "", "Missing project_root or path"

    # Normalize the project root
    root = Path(project_root).resolve()

    # Check if project root itself is blocked
    for blocked in BLOCKED_PATHS:
        if str(root).startswith(blocked) or blocked.startswith(str(root)):
            log.warning("blocked_project_access", root=str(root), blocked=blocked)
            return False, "", "Access to this project is forbidden"

    # Resolve the full path
    # Handle both absolute and relative paths
    if os.path.isabs(relative_path):
        # Absolute path - must still be within project
        full_path = Path(relative_path).resolve()
    else:
        # Relative path - resolve from project root
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
    Validate a project root directory.

    Args:
        project_root: Path to validate

    Returns:
        (valid, error_message)
    """
    if not project_root:
        return False, "No project_root in grant"

    root = Path(project_root)

    # Check blocked list
    for blocked in BLOCKED_PATHS:
        if str(root.resolve()).startswith(blocked):
            return False, "Access to this project is forbidden"

    # Check it exists
    if not root.exists():
        return False, f"Project directory does not exist: {project_root}"

    if not root.is_dir():
        return False, f"Project path is not a directory: {project_root}"

    return True, ""

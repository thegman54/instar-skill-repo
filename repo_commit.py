"""
Repo Commit Tool - Stage and commit changes.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, validate_path, validate_project_root


@register_tool
class RepoCommitTool(BaseTool):
    """Stage and commit changes to the repository."""

    @property
    def name(self) -> str:
        return "repo_commit"

    @property
    def description(self) -> str:
        return "Stage specified files and create a git commit. Always specify which files to stage."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "List of file paths to stage and commit"},
                "message": {"type": "string", "description": "Commit message"},
            },
            "required": ["repo", "files", "message"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, repo: str, files: list[str], message: str, **kwargs) -> ToolResult:
        valid, project_root, access, error, _ = await check_repo_access(repo, "commit")
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        if not files:
            return ToolResult.fail("No files specified to commit")
        if not message or not message.strip():
            return ToolResult.fail("Commit message is required")

        validated_files = []
        for f in files:
            valid, abs_path, error = validate_path(project_root, f)
            if not valid:
                return ToolResult.fail(f"Invalid path '{f}': {error}")
            validated_files.append(f)

        try:
            stage_result = subprocess.run(
                ["git", "add", "--"] + validated_files,
                cwd=project_root, capture_output=True, text=True, timeout=30,
            )
            if stage_result.returncode != 0:
                return ToolResult.fail(f"Stage error: {stage_result.stderr}")

            full_message = f"{message}\n\nCo-Authored-By: Instar Bot <bot@instar.local>"
            commit_result = subprocess.run(
                ["git", "commit", "-m", full_message],
                cwd=project_root, capture_output=True, text=True, timeout=30,
            )
            if commit_result.returncode != 0:
                if "nothing to commit" in commit_result.stdout.lower():
                    return ToolResult.fail("Nothing to commit — files may not have changes")
                return ToolResult.fail(f"Commit error: {commit_result.stderr}")

            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=project_root, capture_output=True, text=True, timeout=10,
            )
            commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else "unknown"

            return ToolResult.ok({"commit": commit_hash, "message": message, "files": validated_files})
        except subprocess.TimeoutExpired:
            return ToolResult.fail("Git command timed out")
        except Exception as e:
            return ToolResult.fail(f"Commit error: {str(e)}")

"""
Repo Status Tool - Show git working tree status.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, validate_project_root


@register_tool
class RepoStatusTool(BaseTool):
    """Show git working tree status."""

    @property
    def name(self) -> str:
        return "repo_status"

    @property
    def description(self) -> str:
        return "Show the git status of the repository — staged, modified, and untracked files."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, repo: str, **kwargs) -> ToolResult:
        valid, project_root, access, error, _ = await check_repo_access(repo, "status")
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_root, capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                return ToolResult.fail(f"Git error: {result.stderr}")

            staged, modified, untracked = [], [], []
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                index_status, work_status = line[0], line[1]
                filepath = line[3:]
                if index_status in ('A', 'M', 'D', 'R', 'C'):
                    staged.append(filepath)
                if work_status == 'M':
                    modified.append(filepath)
                if index_status == '?' and work_status == '?':
                    untracked.append(filepath)

            branch_result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=project_root, capture_output=True, text=True, timeout=5,
            )
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

            return ToolResult.ok({
                "branch": branch, "staged": staged,
                "modified": modified, "untracked": untracked,
                "clean": not staged and not modified and not untracked,
            })
        except subprocess.TimeoutExpired:
            return ToolResult.fail("Git command timed out")
        except Exception as e:
            return ToolResult.fail(f"Status error: {str(e)}")

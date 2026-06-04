"""
Repo Status Tool - Show git status.
"""

import subprocess
from pathlib import Path

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import validate_project_root


@register_tool
class RepoStatusTool(BaseTool):
    """Show git status of the repository."""

    @property
    def name(self) -> str:
        return "repo_status"

    @property
    def description(self) -> str:
        return "Show the git status of the current repository (modified, staged, untracked files)."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def requires_grant_metadata(self) -> list[str]:
        return ["project_root"]

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, **kwargs) -> ToolResult:
        project_root = self.get_grant_metadata("project_root")

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        try:
            # Get status with porcelain format for parsing
            result = subprocess.run(
                ["git", "status", "--porcelain", "-u"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                return ToolResult.fail(f"Git error: {result.stderr}")

            # Parse status
            staged = []
            modified = []
            untracked = []

            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                status = line[:2]
                filepath = line[3:]

                if status[0] in 'MADRC':  # Staged
                    staged.append(filepath)
                if status[1] in 'MD':  # Modified in working tree
                    modified.append(filepath)
                if status == '??':  # Untracked
                    untracked.append(filepath)

            # Get current branch
            branch_result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

            return ToolResult.ok({
                "branch": branch,
                "staged": staged,
                "modified": modified,
                "untracked": untracked[:50],  # Limit untracked
                "clean": len(staged) == 0 and len(modified) == 0 and len(untracked) == 0,
            })

        except subprocess.TimeoutExpired:
            return ToolResult.fail("Git command timed out")
        except Exception as e:
            return ToolResult.fail(f"Status error: {str(e)}")

"""
Repo Diff Tool - Show git diff.
"""

import subprocess
from pathlib import Path

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import validate_path, validate_project_root


@register_tool
class RepoDiffTool(BaseTool):
    """Show git diff of changes."""

    @property
    def name(self) -> str:
        return "repo_diff"

    @property
    def description(self) -> str:
        return (
            "Show git diff of changes in the repository. "
            "Can show all changes or diff for a specific file."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Specific file to diff (optional, defaults to all)",
                },
                "staged": {
                    "type": "boolean",
                    "description": "Show staged changes only (default: false)",
                },
            },
        }

    @property
    def requires_grant_metadata(self) -> list[str]:
        return ["project_root"]

    def credential_keys(self) -> list[str]:
        return []

    async def execute(
        self,
        path: str = None,
        staged: bool = False,
        **kwargs
    ) -> ToolResult:
        project_root = self.get_grant_metadata("project_root")

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        # If specific path, validate it
        if path:
            valid, abs_path, error = validate_path(project_root, path)
            if not valid:
                return ToolResult.fail(error)

        try:
            cmd = ["git", "diff"]
            if staged:
                cmd.append("--staged")
            if path:
                cmd.append("--")
                cmd.append(path)

            result = subprocess.run(
                cmd,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                return ToolResult.fail(f"Git error: {result.stderr}")

            diff = result.stdout

            # Truncate very large diffs
            max_chars = 50000
            truncated = len(diff) > max_chars
            if truncated:
                diff = diff[:max_chars] + "\n... [diff truncated]"

            return ToolResult.ok({
                "path": path or "(all files)",
                "staged": staged,
                "diff": diff,
                "truncated": truncated,
                "has_changes": len(diff.strip()) > 0,
            })

        except subprocess.TimeoutExpired:
            return ToolResult.fail("Git command timed out")
        except Exception as e:
            return ToolResult.fail(f"Diff error: {str(e)}")

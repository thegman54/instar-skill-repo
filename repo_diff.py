"""
Repo Diff Tool - Show git diff.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, validate_project_root


@register_tool
class RepoDiffTool(BaseTool):
    """Show git diff for the repository."""

    @property
    def name(self) -> str:
        return "repo_diff"

    @property
    def description(self) -> str:
        return "Show git diff — unstaged changes by default, or staged changes with the staged flag."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "staged": {"type": "boolean", "description": "Show staged changes instead of unstaged (default: false)"},
                "path": {"type": "string", "description": "Limit diff to specific file or directory"},
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, repo: str, staged: bool = False, path: str = None, **kwargs) -> ToolResult:
        valid, project_root, access, error = await check_repo_access(repo, "diff")
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        try:
            cmd = ["git", "diff", "--no-color"]
            if staged:
                cmd.append("--cached")
            if path:
                cmd.extend(["--", path])

            result = subprocess.run(cmd, cwd=project_root, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return ToolResult.fail(f"Diff error: {result.stderr}")

            output = result.stdout
            max_chars = 50000
            truncated = len(output) > max_chars
            if truncated:
                output = output[:max_chars]

            return ToolResult.ok({
                "staged": staged, "diff": output,
                "truncated": truncated, "empty": not output.strip(),
            })
        except subprocess.TimeoutExpired:
            return ToolResult.fail("Diff timed out")
        except Exception as e:
            return ToolResult.fail(f"Diff error: {str(e)}")

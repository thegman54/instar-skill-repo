"""
Repo Log Tool - View git commit history.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, validate_project_root


@register_tool
class RepoLogTool(BaseTool):
    """View git commit history."""

    @property
    def name(self) -> str:
        return "repo_log"

    @property
    def description(self) -> str:
        return "View git commit history. Shows recent commits with hash, author, date, and message."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "limit": {"type": "integer", "description": "Number of commits to show (default: 20)"},
                "branch": {"type": "string", "description": "Branch to show history for (default: current branch)"},
                "oneline": {"type": "boolean", "description": "Compact one-line format (default: false)"},
                "path": {"type": "string", "description": "Show commits affecting this file/directory only"},
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, repo: str, limit: int = 20, branch: str = None, oneline: bool = False, path: str = None, **kwargs) -> ToolResult:
        valid, project_root, access, error, _ = await check_repo_access(repo, "log")
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        try:
            cmd = ["git", "log", f"-{limit}"]
            if oneline:
                cmd.append("--oneline")
            else:
                cmd.extend(["--format=%h | %an | %ar | %s"])
            if branch:
                cmd.append(branch)
            if path:
                cmd.extend(["--", path])

            result = subprocess.run(cmd, cwd=project_root, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return ToolResult.fail(f"Git error: {result.stderr}")

            log_output = result.stdout.strip()
            if not log_output:
                return ToolResult.ok({"commits": [], "message": "No commits found"})

            return ToolResult.ok({
                "branch": branch or "(current)",
                "count": len(log_output.split('\n')),
                "log": log_output,
            })
        except subprocess.TimeoutExpired:
            return ToolResult.fail("Git command timed out")
        except Exception as e:
            return ToolResult.fail(f"Log error: {str(e)}")

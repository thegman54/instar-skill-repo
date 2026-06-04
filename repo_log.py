"""
Repo Log Tool - View git commit history.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import validate_project_root


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
                "limit": {
                    "type": "integer",
                    "description": "Number of commits to show (default: 20)",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch to show history for (default: current branch)",
                },
                "oneline": {
                    "type": "boolean",
                    "description": "Compact one-line format (default: false)",
                },
                "path": {
                    "type": "string",
                    "description": "Show commits affecting this file/directory only",
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
        limit: int = 20,
        branch: str = None,
        oneline: bool = False,
        path: str = None,
        **kwargs
    ) -> ToolResult:
        project_root = self.get_grant_metadata("project_root")

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        try:
            cmd = ["git", "log", f"-{limit}"]

            if oneline:
                cmd.append("--oneline")
            else:
                # Readable format: hash, author, date, message
                cmd.extend(["--format=%h | %an | %ar | %s"])

            if branch:
                cmd.append(branch)

            if path:
                cmd.extend(["--", path])

            result = subprocess.run(
                cmd,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                return ToolResult.fail(f"Git error: {result.stderr}")

            log_output = result.stdout.strip()

            if not log_output:
                return ToolResult.ok({
                    "commits": [],
                    "message": "No commits found",
                })

            commits = log_output.split('\n')

            return ToolResult.ok({
                "branch": branch or "(current)",
                "count": len(commits),
                "log": log_output,
            })

        except subprocess.TimeoutExpired:
            return ToolResult.fail("Git command timed out")
        except Exception as e:
            return ToolResult.fail(f"Log error: {str(e)}")

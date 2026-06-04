"""
Repo Push Tool - Push commits to remote.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import validate_project_root


@register_tool
class RepoPushTool(BaseTool):
    """Push commits to the remote repository."""

    @property
    def name(self) -> str:
        return "repo_push"

    @property
    def description(self) -> str:
        return "Push local commits to the remote repository."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "Branch to push (default: current branch)",
                },
                "set_upstream": {
                    "type": "boolean",
                    "description": "Set upstream tracking (default: true for new branches)",
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
        branch: str = None,
        set_upstream: bool = True,
        **kwargs
    ) -> ToolResult:
        project_root = self.get_grant_metadata("project_root")

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        try:
            # Get current branch if not specified
            if not branch:
                branch_result = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if branch_result.returncode != 0:
                    return ToolResult.fail("Could not determine current branch")
                branch = branch_result.stdout.strip()

            # Build push command
            cmd = ["git", "push"]
            if set_upstream:
                cmd.extend(["-u", "origin", branch])
            else:
                cmd.extend(["origin", branch])

            result = subprocess.run(
                cmd,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                return ToolResult.fail(f"Push error: {result.stderr}")

            return ToolResult.ok({
                "branch": branch,
                "pushed": True,
                "output": result.stderr.strip(),  # git push outputs to stderr
            })

        except subprocess.TimeoutExpired:
            return ToolResult.fail("Push timed out")
        except Exception as e:
            return ToolResult.fail(f"Push error: {str(e)}")

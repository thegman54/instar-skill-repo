"""
Repo PR Tool - Create a pull request.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import validate_project_root


@register_tool
class RepoPRTool(BaseTool):
    """Create a pull request on GitHub."""

    @property
    def name(self) -> str:
        return "repo_pr"

    @property
    def description(self) -> str:
        return (
            "Create a pull request on GitHub using the gh CLI. "
            "Requires commits to be pushed first."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "PR title",
                },
                "body": {
                    "type": "string",
                    "description": "PR description/body",
                },
                "base": {
                    "type": "string",
                    "description": "Base branch to merge into (default: main)",
                },
                "draft": {
                    "type": "boolean",
                    "description": "Create as draft PR (default: false)",
                },
            },
            "required": ["title", "body"],
        }

    @property
    def requires_grant_metadata(self) -> list[str]:
        return ["project_root"]

    def credential_keys(self) -> list[str]:
        return []

    async def execute(
        self,
        title: str,
        body: str,
        base: str = "main",
        draft: bool = False,
        **kwargs
    ) -> ToolResult:
        project_root = self.get_grant_metadata("project_root")

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        try:
            # Build PR command
            cmd = [
                "gh", "pr", "create",
                "--title", title,
                "--body", body,
                "--base", base,
            ]

            if draft:
                cmd.append("--draft")

            result = subprocess.run(
                cmd,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                # Check for common errors
                stderr = result.stderr.lower()
                if "already exists" in stderr:
                    return ToolResult.fail("A PR already exists for this branch")
                if "not authenticated" in stderr or "gh auth" in stderr:
                    return ToolResult.fail("GitHub CLI not authenticated")
                return ToolResult.fail(f"PR error: {result.stderr}")

            # Parse the PR URL from output
            pr_url = result.stdout.strip()

            return ToolResult.ok({
                "url": pr_url,
                "title": title,
                "base": base,
                "draft": draft,
            })

        except FileNotFoundError:
            return ToolResult.fail("GitHub CLI (gh) not installed")
        except subprocess.TimeoutExpired:
            return ToolResult.fail("PR creation timed out")
        except Exception as e:
            return ToolResult.fail(f"PR error: {str(e)}")

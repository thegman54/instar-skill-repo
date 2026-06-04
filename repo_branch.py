"""
Repo Branch Tool - List, create, switch branches.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import validate_project_root


@register_tool
class RepoBranchTool(BaseTool):
    """List, create, or switch git branches."""

    @property
    def name(self) -> str:
        return "repo_branch"

    @property
    def description(self) -> str:
        return (
            "Manage git branches. Can list all branches, create a new branch, "
            "or switch to an existing branch."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "switch"],
                    "description": "Action to perform (default: list)",
                },
                "name": {
                    "type": "string",
                    "description": "Branch name (required for create/switch)",
                },
                "from_branch": {
                    "type": "string",
                    "description": "Base branch to create from (default: current branch)",
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
        action: str = "list",
        name: str = None,
        from_branch: str = None,
        **kwargs
    ) -> ToolResult:
        project_root = self.get_grant_metadata("project_root")

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        try:
            if action == "list":
                return await self._list_branches(project_root)
            elif action == "create":
                if not name:
                    return ToolResult.fail("Branch name required for create")
                return await self._create_branch(project_root, name, from_branch)
            elif action == "switch":
                if not name:
                    return ToolResult.fail("Branch name required for switch")
                return await self._switch_branch(project_root, name)
            else:
                return ToolResult.fail(f"Unknown action: {action}")

        except subprocess.TimeoutExpired:
            return ToolResult.fail("Git command timed out")
        except Exception as e:
            return ToolResult.fail(f"Branch error: {str(e)}")

    async def _list_branches(self, project_root: str) -> ToolResult:
        """List all branches with current branch marked."""
        result = subprocess.run(
            ["git", "branch", "-a", "-v"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return ToolResult.fail(f"Git error: {result.stderr}")

        # Get current branch
        current = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        current_branch = current.stdout.strip() if current.returncode == 0 else "unknown"

        branches = result.stdout.strip()

        return ToolResult.ok({
            "current": current_branch,
            "branches": branches,
        })

    async def _create_branch(self, project_root: str, name: str, from_branch: str = None) -> ToolResult:
        """Create a new branch."""
        cmd = ["git", "checkout", "-b", name]
        if from_branch:
            cmd.append(from_branch)

        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "already exists" in stderr:
                return ToolResult.fail(f"Branch '{name}' already exists")
            return ToolResult.fail(f"Git error: {result.stderr}")

        return ToolResult.ok({
            "action": "created",
            "branch": name,
            "from": from_branch or "(current branch)",
        })

    async def _switch_branch(self, project_root: str, name: str) -> ToolResult:
        """Switch to an existing branch."""
        result = subprocess.run(
            ["git", "checkout", name],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "did not match" in stderr or "not found" in stderr:
                return ToolResult.fail(f"Branch '{name}' not found")
            if "uncommitted changes" in stderr or "would be overwritten" in stderr:
                return ToolResult.fail("Cannot switch: uncommitted changes would be lost")
            return ToolResult.fail(f"Git error: {result.stderr}")

        return ToolResult.ok({
            "action": "switched",
            "branch": name,
        })

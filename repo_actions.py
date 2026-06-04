"""
Repo Actions Tool - Check GitHub Actions CI status.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import validate_project_root


@register_tool
class RepoActionsTool(BaseTool):
    """Check GitHub Actions workflow status."""

    @property
    def name(self) -> str:
        return "repo_actions"

    @property
    def description(self) -> str:
        return (
            "Check GitHub Actions CI/CD status. "
            "Lists recent workflow runs and their status (success, failure, in_progress)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of runs to show (default: 10)",
                },
                "branch": {
                    "type": "string",
                    "description": "Filter by branch name",
                },
                "workflow": {
                    "type": "string",
                    "description": "Filter by workflow name or filename",
                },
                "run_id": {
                    "type": "string",
                    "description": "Get details for a specific run ID",
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
        limit: int = 10,
        branch: str = None,
        workflow: str = None,
        run_id: str = None,
        **kwargs
    ) -> ToolResult:
        project_root = self.get_grant_metadata("project_root")

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        try:
            if run_id:
                return await self._get_run_details(project_root, run_id)
            else:
                return await self._list_runs(project_root, limit, branch, workflow)

        except FileNotFoundError:
            return ToolResult.fail("GitHub CLI (gh) not installed")
        except subprocess.TimeoutExpired:
            return ToolResult.fail("Command timed out")
        except Exception as e:
            return ToolResult.fail(f"Actions error: {str(e)}")

    async def _list_runs(
        self,
        project_root: str,
        limit: int,
        branch: str = None,
        workflow: str = None
    ) -> ToolResult:
        """List recent workflow runs."""
        cmd = ["gh", "run", "list", f"--limit={limit}"]

        if branch:
            cmd.extend(["--branch", branch])
        if workflow:
            cmd.extend(["--workflow", workflow])

        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "not a git repository" in stderr:
                return ToolResult.fail("Not a git repository")
            if "could not find" in stderr or "no runs found" in stderr:
                return ToolResult.ok({
                    "runs": [],
                    "message": "No workflow runs found",
                })
            return ToolResult.fail(f"gh error: {result.stderr}")

        output = result.stdout.strip()

        if not output:
            return ToolResult.ok({
                "runs": [],
                "message": "No workflow runs found",
            })

        return ToolResult.ok({
            "count": len(output.split('\n')),
            "runs": output,
        })

    async def _get_run_details(self, project_root: str, run_id: str) -> ToolResult:
        """Get details for a specific run."""
        result = subprocess.run(
            ["gh", "run", "view", run_id],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "not found" in stderr:
                return ToolResult.fail(f"Run {run_id} not found")
            return ToolResult.fail(f"gh error: {result.stderr}")

        return ToolResult.ok({
            "run_id": run_id,
            "details": result.stdout.strip(),
        })

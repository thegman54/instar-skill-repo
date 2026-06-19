"""
Repo Actions Tool - Check GitHub Actions CI status via REST API.
"""

import httpx

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import resolve_repo_path


@register_tool
class RepoActionsTool(BaseTool):
    """Check GitHub Actions workflow status via REST API."""

    @property
    def name(self) -> str:
        return "repo_actions"

    @property
    def description(self) -> str:
        return (
            "Check GitHub Actions CI/CD status. "
            "Lists recent workflow runs and their status."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository in owner/repo format",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of runs to show (default: 10)",
                },
                "branch": {
                    "type": "string",
                    "description": "Filter by branch name",
                },
                "status": {
                    "type": "string",
                    "enum": ["completed", "in_progress", "queued"],
                    "description": "Filter by run status",
                },
                "run_id": {
                    "type": "integer",
                    "description": "Get details for a specific run ID",
                },
            },
            "required": ["repo"],
        }

    @property
    def requires_grant_metadata(self) -> list[str]:
        return ["allowed_repos"]

    def credential_keys(self) -> list[str]:
        return ["GITHUB_TOKEN"]

    async def execute(
        self,
        repo: str,
        limit: int = 10,
        branch: str = None,
        status: str = None,
        run_id: int = None,
        **kwargs
    ) -> ToolResult:
        allowed_repos = self.get_grant_metadata("allowed_repos")
        valid, _, error = resolve_repo_path(repo, allowed_repos)
        if not valid:
            return ToolResult.fail(error)

        token = self.get_credential("GITHUB_TOKEN")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if run_id:
                    return await self._get_run_details(client, repo, run_id, headers)
                else:
                    return await self._list_runs(client, repo, limit, branch, status, headers)

        except httpx.TimeoutException:
            return ToolResult.fail("GitHub API request timed out")
        except Exception as e:
            return ToolResult.fail(f"Actions error: {str(e)}")

    async def _list_runs(
        self, client, repo, limit, branch, status, headers
    ) -> ToolResult:
        params = {"per_page": min(limit, 100)}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status

        resp = await client.get(
            f"https://api.github.com/repos/{repo}/actions/runs",
            headers=headers,
            params=params,
        )

        if resp.status_code in (401, 403):
            return ToolResult.fail("GitHub authentication failed — check GITHUB_TOKEN")
        if resp.status_code == 404:
            return ToolResult.fail(f"Repository '{repo}' not found")
        if resp.status_code != 200:
            return ToolResult.fail(f"GitHub API error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        runs = data.get("workflow_runs", [])

        if not runs:
            return ToolResult.ok({
                "runs": [],
                "message": "No workflow runs found",
            })

        formatted = []
        for run in runs:
            formatted.append({
                "id": run["id"],
                "name": run.get("name", ""),
                "status": run["status"],
                "conclusion": run.get("conclusion"),
                "branch": run["head_branch"],
                "event": run["event"],
                "created_at": run["created_at"],
                "url": run["html_url"],
            })

        return ToolResult.ok({
            "count": len(formatted),
            "runs": formatted,
        })

    async def _get_run_details(self, client, repo, run_id, headers) -> ToolResult:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}",
            headers=headers,
        )

        if resp.status_code == 404:
            return ToolResult.fail(f"Run {run_id} not found")
        if resp.status_code != 200:
            return ToolResult.fail(f"GitHub API error {resp.status_code}")

        run = resp.json()

        # Also get jobs
        jobs_resp = await client.get(
            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs",
            headers=headers,
        )

        jobs = []
        if jobs_resp.status_code == 200:
            for job in jobs_resp.json().get("jobs", []):
                jobs.append({
                    "name": job["name"],
                    "status": job["status"],
                    "conclusion": job.get("conclusion"),
                    "started_at": job.get("started_at"),
                    "completed_at": job.get("completed_at"),
                })

        return ToolResult.ok({
            "run_id": run_id,
            "name": run.get("name", ""),
            "status": run["status"],
            "conclusion": run.get("conclusion"),
            "branch": run["head_branch"],
            "event": run["event"],
            "created_at": run["created_at"],
            "url": run["html_url"],
            "jobs": jobs,
        })

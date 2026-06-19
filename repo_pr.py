"""
Repo PR Tool - Create a pull request via GitHub REST API.
"""

import subprocess

import httpx

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, validate_project_root


@register_tool
class RepoPRTool(BaseTool):
    """Create a pull request on GitHub via REST API."""

    @property
    def name(self) -> str:
        return "repo_pr"

    @property
    def description(self) -> str:
        return "Create a pull request on GitHub. Commits must be pushed first."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR description/body"},
                "head": {"type": "string", "description": "Branch containing changes (default: current branch)"},
                "base": {"type": "string", "description": "Base branch to merge into (default: main)"},
                "draft": {"type": "boolean", "description": "Create as draft PR (default: false)"},
            },
            "required": ["repo", "title", "body"],
        }

    def credential_keys(self) -> list[str]:
        return ["GITHUB_TOKEN"]

    async def execute(
        self, repo: str, title: str, body: str,
        head: str = None, base: str = "main", draft: bool = False, **kwargs
    ) -> ToolResult:
        valid, project_root, access, error = await check_repo_access(repo, "pr")
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        token = self.get_credential("GITHUB_TOKEN")

        if not head:
            branch_result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=project_root, capture_output=True, text=True, timeout=10,
            )
            if branch_result.returncode != 0:
                return ToolResult.fail("Could not determine current branch for PR head")
            head = branch_result.stdout.strip()

        if head == base:
            return ToolResult.fail(f"Head branch '{head}' is the same as base branch '{base}'")

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"https://api.github.com/repos/{repo}/pulls",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json={"title": title, "body": body, "head": head, "base": base, "draft": draft},
                )

                if resp.status_code == 201:
                    data = resp.json()
                    return ToolResult.ok({
                        "url": data["html_url"], "number": data["number"],
                        "title": title, "head": head, "base": base, "draft": draft,
                    })
                if resp.status_code == 422:
                    data = resp.json()
                    errors = data.get("errors", [])
                    messages = [e.get("message", "") for e in errors]
                    if any("pull request already exists" in m for m in messages):
                        return ToolResult.fail("A PR already exists for this branch")
                    return ToolResult.fail(f"GitHub validation error: {data.get('message', '')} — {messages}")
                if resp.status_code in (401, 403):
                    return ToolResult.fail("GitHub authentication failed — check GITHUB_TOKEN permissions")
                if resp.status_code == 404:
                    return ToolResult.fail(f"Repository '{repo}' not found or token lacks access")
                return ToolResult.fail(f"GitHub API error {resp.status_code}: {resp.text[:500]}")

        except httpx.TimeoutException:
            return ToolResult.fail("GitHub API request timed out")
        except Exception as e:
            return ToolResult.fail(f"PR creation error: {str(e)}")

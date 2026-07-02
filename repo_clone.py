"""
Repo Clone Tool - Clone a GitHub repository into the workspace.
"""

import os
import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, sanitize_stderr


@register_tool
class RepoCloneTool(BaseTool):
    """Clone a GitHub repository into the shared workspace."""

    @property
    def name(self) -> str:
        return "repo_clone"

    @property
    def description(self) -> str:
        return (
            "Clone a GitHub repository into the workspace. "
            "If already cloned, pulls latest changes instead. "
            "Provide repo as 'owner/repo' format."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub repo in owner/repo format (e.g. 'aglickman/my-project')",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch to checkout after clone (default: default branch)",
                },
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        return ["GITHUB_TOKEN"]

    async def execute(self, repo: str, branch: str = None, **kwargs) -> ToolResult:
        valid, workspace_path, access, error, configured_branch = await check_repo_access(repo, "clone")
        if not valid:
            return ToolResult.fail(error)

        # Use configured branch from repo config if no explicit branch passed
        if not branch and configured_branch:
            branch = configured_branch

        token = self.get_credential("GITHUB_TOKEN")
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"

        try:
            if os.path.exists(workspace_path) and os.path.isdir(os.path.join(workspace_path, ".git")):
                return await self._update_existing(workspace_path, repo, branch, token)
            else:
                return await self._clone_fresh(clone_url, workspace_path, repo, branch)

        except subprocess.TimeoutExpired:
            return ToolResult.fail("Clone timed out (120s limit)")
        except Exception as e:
            return ToolResult.fail(f"Clone error: {sanitize_stderr(str(e))}")

    async def _clone_fresh(
        self, clone_url: str, workspace_path: str, repo: str, branch: str = None
    ) -> ToolResult:
        os.makedirs(os.path.dirname(workspace_path), exist_ok=True)

        cmd = ["git", "clone", "--depth", "50", clone_url, workspace_path]
        if branch:
            cmd.extend(["--branch", branch])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "authentication" in stderr or "403" in stderr:
                return ToolResult.fail("GitHub authentication failed — check GITHUB_TOKEN")
            if "not found" in stderr or "404" in stderr:
                return ToolResult.fail(f"Repository '{repo}' not found")
            return ToolResult.fail(f"Clone error: {sanitize_stderr(result.stderr)}")

        self._configure_git(workspace_path)
        self._sanitize_remote(workspace_path, repo)

        info = self._get_repo_info(workspace_path)

        return ToolResult.ok({
            "repo": repo,
            "action": "cloned",
            "path": workspace_path,
            **info,
        })

    async def _update_existing(
        self, workspace_path: str, repo: str, branch: str = None, token: str = None
    ) -> ToolResult:
        # Verify remote points to the expected repo
        remote_result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if remote_result.returncode == 0:
            current_remote = remote_result.stdout.strip()
            if f"github.com/{repo}" not in current_remote:
                return ToolResult.fail(
                    f"Remote URL mismatch — expected github.com/{repo}. "
                    "Possible tampering. Delete workspace and re-clone."
                )

        # Temporarily set token URL for fetch
        if token:
            auth_url = f"https://x-access-token:{token}@github.com/{repo}.git"
            subprocess.run(
                ["git", "remote", "set-url", "origin", auth_url],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                timeout=10,
            )

        if branch:
            subprocess.run(
                ["git", "checkout", branch],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                timeout=30,
            )

        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

        pull_result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=60,
        )

        self._configure_git(workspace_path)
        self._sanitize_remote(workspace_path, repo)

        info = self._get_repo_info(workspace_path)

        return ToolResult.ok({
            "repo": repo,
            "action": "updated",
            "path": workspace_path,
            "pull_output": sanitize_stderr(
                pull_result.stdout.strip() or pull_result.stderr.strip()
            ),
            **info,
        })

    def _configure_git(self, workspace_path: str):
        subprocess.run(
            ["git", "config", "user.email", "bot@instar.local"],
            cwd=workspace_path, capture_output=True, timeout=5,
        )
        subprocess.run(
            ["git", "config", "user.name", "Instar Bot"],
            cwd=workspace_path, capture_output=True, timeout=5,
        )

    def _sanitize_remote(self, workspace_path: str, repo: str):
        clean_url = f"https://github.com/{repo}.git"
        subprocess.run(
            ["git", "remote", "set-url", "origin", clean_url],
            cwd=workspace_path, capture_output=True, text=True, timeout=5,
        )

    def _get_repo_info(self, workspace_path: str) -> dict:
        info = {}
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace_path, capture_output=True, text=True, timeout=5,
        )
        if branch_result.returncode == 0:
            info["branch"] = branch_result.stdout.strip()

        head_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=workspace_path, capture_output=True, text=True, timeout=5,
        )
        if head_result.returncode == 0:
            info["head"] = head_result.stdout.strip()

        return info

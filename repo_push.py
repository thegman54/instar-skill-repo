"""
Repo Push Tool - Push commits to remote using GITHUB_TOKEN.
"""

import os
import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, sanitize_stderr, validate_project_root


@register_tool
class RepoPushTool(BaseTool):
    """Push commits to the remote repository."""

    @property
    def name(self) -> str:
        return "repo_push"

    @property
    def description(self) -> str:
        return "Push local commits to the remote repository using authenticated credentials."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "branch": {"type": "string", "description": "Branch to push (default: current branch)"},
                "set_upstream": {"type": "boolean", "description": "Set upstream tracking (default: true)"},
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        return ["GITHUB_TOKEN"]

    async def execute(self, repo: str, branch: str = None, set_upstream: bool = True, **kwargs) -> ToolResult:
        valid, project_root, access, error, _ = await check_repo_access(repo, "push")
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        token = self.get_credential("GITHUB_TOKEN")

        try:
            # Verify remote points to the expected repo
            remote_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=project_root, capture_output=True, text=True, timeout=10,
            )
            if remote_result.returncode == 0:
                current_remote = remote_result.stdout.strip()
                if f"github.com/{repo}" not in current_remote:
                    return ToolResult.fail(
                        f"Remote URL mismatch — expected github.com/{repo}. Possible tampering."
                    )

            # Temporarily set token URL for push
            auth_url = f"https://x-access-token:{token}@github.com/{repo}.git"
            subprocess.run(
                ["git", "remote", "set-url", "origin", auth_url],
                cwd=project_root, capture_output=True, text=True, timeout=10,
            )

            if not branch:
                branch_result = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=project_root, capture_output=True, text=True, timeout=10,
                )
                if branch_result.returncode != 0:
                    return ToolResult.fail("Could not determine current branch")
                branch = branch_result.stdout.strip()

            cmd = ["git", "push"]
            if set_upstream:
                cmd.extend(["-u", "origin", branch])
            else:
                cmd.extend(["origin", branch])

            result = subprocess.run(
                cmd, cwd=project_root, capture_output=True, text=True, timeout=60,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )

            # Always strip token from remote after push
            clean_url = f"https://github.com/{repo}.git"
            subprocess.run(
                ["git", "remote", "set-url", "origin", clean_url],
                cwd=project_root, capture_output=True, text=True, timeout=5,
            )

            if result.returncode != 0:
                stderr = result.stderr.lower()
                if "authentication" in stderr or "403" in stderr:
                    return ToolResult.fail("Push authentication failed — check GITHUB_TOKEN")
                return ToolResult.fail(f"Push error: {sanitize_stderr(result.stderr)}")

            return ToolResult.ok({
                "repo": repo, "branch": branch, "pushed": True,
                "output": sanitize_stderr(result.stderr.strip()),
            })

        except subprocess.TimeoutExpired:
            try:
                subprocess.run(
                    ["git", "remote", "set-url", "origin", f"https://github.com/{repo}.git"],
                    cwd=project_root, capture_output=True, timeout=5,
                )
            except Exception:
                pass
            return ToolResult.fail("Push timed out")
        except Exception as e:
            return ToolResult.fail(f"Push error: {sanitize_stderr(str(e))}")

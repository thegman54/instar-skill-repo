"""
Repo Diff Tool - Show git diff.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import git_env, check_repo_access, validate_project_root, validate_ref


@register_tool
class RepoDiffTool(BaseTool):
    """Show git diff for the repository."""

    @property
    def name(self) -> str:
        return "repo_diff"

    @property
    def description(self) -> str:
        return ("Show git diff — unstaged changes by default, or staged changes with the staged "
                "flag. Pass `base` (and optionally `head`) to diff branches or commits instead, "
                "e.g. base=origin/production head=bot/CLC-4755: what that branch changes.")

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "staged": {"type": "boolean", "description": "Show staged changes instead of unstaged (default: false)"},
                "path": {"type": "string", "description": "Limit diff to specific file or directory"},
                "base": {"type": "string",
                         "description": ("Branch or commit to diff FROM (optional), e.g. "
                                         "origin/production. Alone: the checkout's files vs base")},
                "head": {"type": "string",
                         "description": ("Branch or commit to diff TO (needs base), e.g. "
                                         "bot/CLC-4755. Shows what head changed since it left "
                                         "base (git diff base...head)")},
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, repo: str, staged: bool = False, path: str = None,
                      base: str = None, head: str = None, **kwargs) -> ToolResult:
        valid, project_root, access, error, _ = await check_repo_access(
            repo, "diff", self._profile_slug, self._session_id, self._gatekeeper_url)
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        if head and not base:
            return ToolResult.fail("head needs base: the diff is what head changed since base")
        for ref in (base, head):
            if ref:
                ok, error = validate_ref(project_root, ref)
                if not ok:
                    return ToolResult.fail(error)

        try:
            cmd = ["git", "diff", "--no-color"]
            if head:
                # Three dots: the branch's own changes, not everything base gained since the cut.
                cmd.append(f"{base}...{head}")
            else:
                if staged:
                    cmd.append("--cached")
                if base:
                    cmd.append(base)
            if path:
                cmd.extend(["--", path])

            result = subprocess.run(cmd, cwd=project_root, capture_output=True, text=True,
                                    timeout=30, env=git_env())
            if result.returncode != 0:
                return ToolResult.fail(f"Diff error: {result.stderr}")

            output = result.stdout
            max_chars = 50000
            truncated = len(output) > max_chars
            if truncated:
                output = output[:max_chars]

            return ToolResult.ok({
                "staged": staged, "base": base or "", "head": head or "", "diff": output,
                "truncated": truncated, "empty": not output.strip(),
            })
        except subprocess.TimeoutExpired:
            return ToolResult.fail("Diff timed out")
        except Exception as e:
            return ToolResult.fail(f"Diff error: {str(e)}")

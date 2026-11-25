"""
Repo Read Tool - Read a file from the repository.
"""

from pathlib import Path

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import (check_repo_access, validate_path, validate_project_root, checkout_staleness,
                   validate_ref, git_run)


@register_tool
class RepoReadTool(BaseTool):
    """Read a file from the repository."""

    @property
    def name(self) -> str:
        return "repo_read"

    @property
    def description(self) -> str:
        return ("Read the contents of a file in the repository. Pass `ref` to read the file as "
                "it is on a branch or commit (origin/production, bot/CLC-4755, a sha) instead of "
                "the files on disk, which are whatever branch the checkout is on.")

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository in owner/repo format",
                },
                "path": {
                    "type": "string",
                    "description": "File path relative to repository root",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Start reading from this line (1-indexed, optional)",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Stop reading at this line (inclusive, optional)",
                },
                "ref": {
                    "type": "string",
                    "description": ("Branch, tag or commit to read the file at (optional). "
                                    "e.g. origin/production, origin/staging, bot/CLC-4755, "
                                    "a sha. Omit to read the checkout's files on disk"),
                },
            },
            "required": ["repo", "path"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(
        self, repo: str, path: str,
        start_line: int = None, end_line: int = None, ref: str = None, **kwargs
    ) -> ToolResult:
        valid, project_root, access, error, _ = await check_repo_access(
            repo, "read", self._profile_slug, self._session_id, self._gatekeeper_url)
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        valid, abs_path, error = validate_path(project_root, path)
        if not valid:
            return ToolResult.fail(error)

        file_path = Path(abs_path)
        if ref:
            ok, error = validate_ref(project_root, ref)
            if not ok:
                return ToolResult.fail(error)
            rel = file_path.relative_to(Path(project_root).resolve()).as_posix()
            shown = git_run(["git", "show", f"{ref}:{rel}"], cwd=project_root, timeout=30,
                            text=False)
            if shown.returncode != 0:
                return ToolResult.fail(f"File not found at {ref}: {path}")
        elif not file_path.exists():
            stale = checkout_staleness(project_root)
            return ToolResult.fail(f"File not found: {path}" + (f" ({stale})" if stale else ""))
        elif not file_path.is_file():
            return ToolResult.fail(f"Not a file: {path}")

        try:
            content = (shown.stdout.decode('utf-8', errors='replace') if ref
                       else file_path.read_text(encoding='utf-8', errors='replace'))
            lines = content.splitlines(keepends=True)
            total_lines = len(lines)

            if start_line is not None or end_line is not None:
                start = (start_line or 1) - 1
                end = end_line or total_lines
                lines = lines[start:end]
                content = ''.join(lines)

            max_chars = 100000
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars]

            return ToolResult.ok({
                "path": path, "ref": ref or "working tree", "content": content,
                "total_lines": total_lines, "lines_returned": len(lines),
                "truncated": truncated,
            })
        except Exception as e:
            return ToolResult.fail(f"Read error: {str(e)}")

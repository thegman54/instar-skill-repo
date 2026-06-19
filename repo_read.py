"""
Repo Read Tool - Read a file from the repository.
"""

from pathlib import Path

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, validate_path, validate_project_root


@register_tool
class RepoReadTool(BaseTool):
    """Read a file from the repository."""

    @property
    def name(self) -> str:
        return "repo_read"

    @property
    def description(self) -> str:
        return "Read the contents of a file in the repository."

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
            },
            "required": ["repo", "path"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(
        self, repo: str, path: str,
        start_line: int = None, end_line: int = None, **kwargs
    ) -> ToolResult:
        valid, project_root, access, error = await check_repo_access(repo, "read")
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        valid, abs_path, error = validate_path(project_root, path)
        if not valid:
            return ToolResult.fail(error)

        file_path = Path(abs_path)
        if not file_path.exists():
            return ToolResult.fail(f"File not found: {path}")
        if not file_path.is_file():
            return ToolResult.fail(f"Not a file: {path}")

        try:
            content = file_path.read_text(encoding='utf-8', errors='replace')
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
                "path": path, "content": content,
                "total_lines": total_lines, "lines_returned": len(lines),
                "truncated": truncated,
            })
        except Exception as e:
            return ToolResult.fail(f"Read error: {str(e)}")

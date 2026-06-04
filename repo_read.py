"""
Repo Read Tool - Read a file from the repository.
"""

from pathlib import Path

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import validate_path


@register_tool
class RepoReadTool(BaseTool):
    """Read a file from the repository."""

    @property
    def name(self) -> str:
        return "repo_read"

    @property
    def description(self) -> str:
        return "Read the contents of a file in the current repository."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
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
            "required": ["path"],
        }

    @property
    def requires_grant_metadata(self) -> list[str]:
        return ["project_root"]

    def credential_keys(self) -> list[str]:
        return []

    async def execute(
        self,
        path: str,
        start_line: int = None,
        end_line: int = None,
        **kwargs
    ) -> ToolResult:
        project_root = self.get_grant_metadata("project_root")

        # Validate path is within project
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

            # Apply line range if specified
            if start_line is not None or end_line is not None:
                start = (start_line or 1) - 1  # Convert to 0-indexed
                end = end_line or total_lines
                lines = lines[start:end]
                content = ''.join(lines)

            # Truncate very large files
            max_chars = 100000
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars]

            return ToolResult.ok({
                "path": path,
                "content": content,
                "total_lines": total_lines,
                "lines_returned": len(lines),
                "truncated": truncated,
            })

        except Exception as e:
            return ToolResult.fail(f"Read error: {str(e)}")

"""
Repo Write Tool - Write or create a file in the repository.
"""

from pathlib import Path

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, validate_path, validate_project_root


@register_tool
class RepoWriteTool(BaseTool):
    """Write or create a file in the repository."""

    @property
    def name(self) -> str:
        return "repo_write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file in the repository. "
            "Creates the file if it doesn't exist. "
            "Creates parent directories if needed."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "path": {"type": "string", "description": "File path relative to repository root"},
                "content": {"type": "string", "description": "Content to write to the file"},
                "create_parents": {"type": "boolean", "description": "Create parent directories if they don't exist (default: true)"},
            },
            "required": ["repo", "path", "content"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, repo: str, path: str, content: str, create_parents: bool = True, **kwargs) -> ToolResult:
        valid, project_root, access, error = await check_repo_access(repo, "write")
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        valid, abs_path, error = validate_path(project_root, path)
        if not valid:
            return ToolResult.fail(error)

        file_path = Path(abs_path)
        try:
            if create_parents:
                file_path.parent.mkdir(parents=True, exist_ok=True)
            elif not file_path.parent.exists():
                return ToolResult.fail(f"Parent directory does not exist: {file_path.parent}")

            existed = file_path.exists()
            file_path.write_text(content, encoding='utf-8')

            return ToolResult.ok({
                "path": path,
                "action": "updated" if existed else "created",
                "bytes": len(content.encode('utf-8')),
            })
        except Exception as e:
            return ToolResult.fail(f"Write error: {str(e)}")

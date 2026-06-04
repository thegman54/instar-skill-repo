"""
Repo Write Tool - Write or edit a file in the repository.
"""

from pathlib import Path

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import validate_path


@register_tool
class RepoWriteTool(BaseTool):
    """Write or edit a file in the repository."""

    @property
    def name(self) -> str:
        return "repo_write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file in the current repository. "
            "Can create new files or overwrite existing ones. "
            "For edits, use repo_read first to get current content."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to repository root",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
                "create_dirs": {
                    "type": "boolean",
                    "description": "Create parent directories if they don't exist (default: true)",
                },
            },
            "required": ["path", "content"],
        }

    @property
    def requires_grant_metadata(self) -> list[str]:
        return ["project_root"]

    def credential_keys(self) -> list[str]:
        return []

    async def execute(
        self,
        path: str,
        content: str,
        create_dirs: bool = True,
        **kwargs
    ) -> ToolResult:
        project_root = self.get_grant_metadata("project_root")

        # Validate path is within project
        valid, abs_path, error = validate_path(project_root, path)
        if not valid:
            return ToolResult.fail(error)

        file_path = Path(abs_path)

        try:
            # Create parent directories if needed
            if create_dirs:
                file_path.parent.mkdir(parents=True, exist_ok=True)
            elif not file_path.parent.exists():
                return ToolResult.fail(f"Parent directory does not exist: {file_path.parent}")

            # Check if we're creating or updating
            existed = file_path.exists()
            old_size = file_path.stat().st_size if existed else 0

            # Write the file
            file_path.write_text(content, encoding='utf-8')

            new_size = file_path.stat().st_size

            return ToolResult.ok({
                "path": path,
                "action": "updated" if existed else "created",
                "old_size": old_size,
                "new_size": new_size,
                "lines": content.count('\n') + (1 if content and not content.endswith('\n') else 0),
            })

        except PermissionError:
            return ToolResult.fail(f"Permission denied: {path}")
        except Exception as e:
            return ToolResult.fail(f"Write error: {str(e)}")

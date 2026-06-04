"""
Repo List Tool - List files in the repository.
"""

import glob
from pathlib import Path

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import validate_path, validate_project_root


@register_tool
class RepoListTool(BaseTool):
    """List files in the repository."""

    @property
    def name(self) -> str:
        return "repo_list"

    @property
    def description(self) -> str:
        return (
            "List files in the current repository. "
            "Supports glob patterns like '**/*.py' or 'src/**/*'."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (default: '*' for top-level files)",
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "Include hidden files/dirs (default: false)",
                },
            },
        }

    @property
    def requires_grant_metadata(self) -> list[str]:
        return ["project_root"]

    def credential_keys(self) -> list[str]:
        return []  # No credentials needed

    async def execute(
        self,
        pattern: str = "*",
        include_hidden: bool = False,
        **kwargs
    ) -> ToolResult:
        # Get project root from grant metadata
        project_root = self.get_grant_metadata("project_root")

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        root = Path(project_root)

        # Run glob from project root
        try:
            if "**" in pattern:
                matches = list(root.glob(pattern))
            else:
                matches = list(root.glob(pattern))

            # Filter hidden files if not requested
            if not include_hidden:
                matches = [
                    m for m in matches
                    if not any(part.startswith('.') for part in m.parts[len(root.parts):])
                ]

            # Convert to relative paths
            files = []
            dirs = []
            for match in sorted(matches):
                rel_path = str(match.relative_to(root))
                if match.is_dir():
                    dirs.append(rel_path + "/")
                else:
                    files.append(rel_path)

            return ToolResult.ok({
                "pattern": pattern,
                "directories": dirs[:100],  # Limit output
                "files": files[:500],
                "total_dirs": len(dirs),
                "total_files": len(files),
                "truncated": len(files) > 500 or len(dirs) > 100,
            })

        except Exception as e:
            return ToolResult.fail(f"List error: {str(e)}")

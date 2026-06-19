"""
Repo List Tool - List files and directories in the repository.
"""

import fnmatch
from pathlib import Path

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, validate_path, validate_project_root


@register_tool
class RepoListTool(BaseTool):
    """List files and directories in the repository."""

    @property
    def name(self) -> str:
        return "repo_list"

    @property
    def description(self) -> str:
        return "List files and directories in the repository. Supports glob patterns and recursive listing."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "path": {"type": "string", "description": "Directory path relative to repo root (default: root)"},
                "pattern": {"type": "string", "description": "Glob pattern to filter files (e.g. '*.py', '**/*.ts')"},
                "recursive": {"type": "boolean", "description": "List recursively (default: false)"},
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, repo: str, path: str = ".", pattern: str = None, recursive: bool = False, **kwargs) -> ToolResult:
        valid, project_root, access, error = await check_repo_access(repo, "list")
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        valid, abs_path, error = validate_path(project_root, path)
        if not valid:
            return ToolResult.fail(error)

        target = Path(abs_path)
        if not target.exists():
            return ToolResult.fail(f"Directory not found: {path}")
        if not target.is_dir():
            return ToolResult.fail(f"Not a directory: {path}")

        try:
            files, dirs = [], []
            max_files, max_dirs = 500, 100

            if pattern and recursive:
                for item in sorted(target.rglob(pattern)):
                    if item.name.startswith('.'):
                        continue
                    rel = str(item.relative_to(Path(project_root)))
                    if item.is_file() and len(files) < max_files:
                        files.append(rel)
                    elif item.is_dir() and len(dirs) < max_dirs:
                        dirs.append(rel + "/")
            else:
                iterator = target.rglob("*") if recursive else target.iterdir()
                for item in sorted(iterator):
                    if item.name.startswith('.'):
                        continue
                    rel = str(item.relative_to(Path(project_root)))
                    if pattern and not fnmatch.fnmatch(item.name, pattern):
                        continue
                    if item.is_file() and len(files) < max_files:
                        files.append(rel)
                    elif item.is_dir() and len(dirs) < max_dirs:
                        dirs.append(rel + "/")

            return ToolResult.ok({
                "path": path, "files": files, "directories": dirs,
                "file_count": len(files), "dir_count": len(dirs),
                "truncated": len(files) >= max_files or len(dirs) >= max_dirs,
            })
        except Exception as e:
            return ToolResult.fail(f"List error: {str(e)}")

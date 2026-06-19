"""
Repo Grep Tool - Search file contents with regex.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import resolve_repo_path, validate_project_root


@register_tool
class RepoGrepTool(BaseTool):
    """Search file contents in the repository using regex patterns."""

    @property
    def name(self) -> str:
        return "repo_grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents using regex. Returns matching lines with "
            "file paths and line numbers. Supports glob filtering."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository in owner/repo format",
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "glob": {
                    "type": "string",
                    "description": "File glob filter (e.g. '*.py', 'src/**/*.ts')",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default: false)",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context around matches (default: 0)",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max matching lines to return (default: 100)",
                },
            },
            "required": ["repo", "pattern"],
        }

    @property
    def requires_grant_metadata(self) -> list[str]:
        return ["allowed_repos"]

    def credential_keys(self) -> list[str]:
        return []

    async def execute(
        self,
        repo: str,
        pattern: str,
        glob: str = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 100,
        **kwargs,
    ) -> ToolResult:
        allowed_repos = self.get_grant_metadata("allowed_repos")
        valid, project_root, error = resolve_repo_path(repo, allowed_repos)
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        if not pattern:
            return ToolResult.fail("Search pattern is required")

        try:
            # Prefer ripgrep if available, fall back to git grep
            return await self._search_git_grep(
                project_root, pattern, glob, case_insensitive,
                context_lines, max_results,
            )
        except Exception as e:
            return ToolResult.fail(f"Search error: {str(e)}")

    async def _search_git_grep(
        self,
        project_root: str,
        pattern: str,
        glob: str,
        case_insensitive: bool,
        context_lines: int,
        max_results: int,
    ) -> ToolResult:
        """Search using git grep (always available in a git repo)."""
        cmd = ["git", "grep", "-n", "--no-color", "-E"]

        if case_insensitive:
            cmd.append("-i")

        if context_lines > 0:
            cmd.extend(["-C", str(min(context_lines, 10))])

        cmd.append(pattern)

        if glob:
            cmd.extend(["--", glob])

        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 1:
            # No matches (git grep returns 1 for no matches)
            return ToolResult.ok({
                "pattern": pattern,
                "matches": 0,
                "results": "",
            })

        if result.returncode != 0:
            return ToolResult.fail(f"Search error: {result.stderr}")

        output = result.stdout
        lines = output.split('\n')
        total_matches = len([l for l in lines if l.strip()])

        # Truncate if too many results
        if total_matches > max_results:
            lines = lines[:max_results]
            output = '\n'.join(lines)
            truncated = True
        else:
            truncated = False

        # Also truncate by character count
        max_chars = 50000
        if len(output) > max_chars:
            output = output[:max_chars]
            truncated = True

        return ToolResult.ok({
            "pattern": pattern,
            "matches": total_matches,
            "truncated": truncated,
            "results": output,
        })

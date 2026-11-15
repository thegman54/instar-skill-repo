"""
Repo Write Tool - Write or create a file in the repository, or edit it in place.

EDITS, 2026-11-15. The tool could only replace a whole file. On CLC-4615 round 2 the dev had
two five-line fixes to make in a 2681-line C# loader, and its only path was to retype the whole
file, which risks silently corrupting unrelated code. It escalated rather than do that, which was
right. `edits` is the line-level path: exact old/new pairs, each of which must match exactly once,
applied all or none. The file's line endings are kept, so a CRLF file does not come back as LF
and show every line as changed.
"""

from pathlib import Path

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, validate_path, validate_project_root


def apply_edits(text: str, edits: list) -> tuple[str, str]:
    """Apply exact (old_str -> new_str) pairs in order. Returns (new_text, error)."""
    crlf = "\r\n" in text
    for i, e in enumerate(edits or [], 1):
        old = (e or {}).get("old_str", "")
        new = (e or {}).get("new_str", "")
        if not old:
            return text, f"edit {i}: old_str is empty"
        # The model writes \n; a CRLF file holds \r\n. Match the file, not the model.
        if crlf and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        n = text.count(old)
        if n != 1:
            return text, (f"edit {i}: old_str matches {n} times; it must match exactly once. "
                          + ("Copy it from repo_read exactly, including indentation." if n == 0
                             else "Include more surrounding lines to make it unique."))
        text = text.replace(old, new, 1)
    return text, ""


@register_tool
class RepoWriteTool(BaseTool):
    """Write or create a file in the repository, or edit it in place."""

    @property
    def name(self) -> str:
        return "repo_write"

    @property
    def description(self) -> str:
        return (
            "Write a file in the repository. Two modes. `content`: replace the whole file (or "
            "create it, with parent directories). `edits`: change an existing file in place with "
            "exact {old_str, new_str} pairs. Each old_str must match exactly once, copied from "
            "repo_read with its indentation. They apply in order, all or none, and the file's "
            "line endings are kept. Use `edits` for any change to an existing file: retyping a "
            "large file to change a few lines risks corrupting the rest of it.")

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "path": {"type": "string", "description": "File path relative to repository root"},
                "content": {"type": "string", "description": "Whole-file content. Omit when using edits."},
                "edits": {
                    "type": "array",
                    "description": "In-place edits to an existing file; use instead of content.",
                    "items": {"type": "object", "properties": {
                        "old_str": {"type": "string", "description": "Exact text to replace; must occur once"},
                        "new_str": {"type": "string", "description": "Replacement text (empty deletes)"},
                    }, "required": ["old_str", "new_str"]},
                },
                "create_parents": {"type": "boolean", "description": "Create parent directories if they don't exist (default: true)"},
            },
            "required": ["repo", "path"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, repo: str, path: str, content: str = None, edits: list = None,
                      create_parents: bool = True, **kwargs) -> ToolResult:
        if (content is None) == (not edits):
            return ToolResult.fail("Pass exactly one of `content` (whole file) or `edits` (in place).")

        valid, project_root, access, error, _ = await check_repo_access(repo, "write", self._profile_slug)
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
            if edits:
                if not file_path.exists():
                    return ToolResult.fail(f"{path} does not exist; edits change an existing file.")
                # newline="" so \r\n survives the round trip.
                with open(file_path, encoding="utf-8", newline="") as f:
                    before = f.read()
                after, error = apply_edits(before, edits)
                if error:
                    return ToolResult.fail(f"{error}. Nothing was written.")
                with open(file_path, "w", encoding="utf-8", newline="") as f:
                    f.write(after)
                return ToolResult.ok({
                    "path": path, "action": "edited", "edits": len(edits),
                    "lines_before": before.count("\n"), "lines_after": after.count("\n"),
                })

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

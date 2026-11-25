"""
repo_recipe — the conflict recipes that `repo_git op=land` uses (One Track, fleet triage
2026-11-25, rule R6). A bot never improvises a conflict resolution: `land` records the exact
conflict, a bot proposes the resolved file with its reason here, a PERSON approves it (Inbox),
and only then does `land` replay it, and only for that identical conflict.

This tool proposes and reads. It never approves: that is a person's press.
"""

from ..base import BaseTool, ToolResult
from ..registry import register_tool

MARKERS = ("<<<<<<< ", "\n=======\n", ">>>>>>> ")


@register_tool
class RepoRecipeTool(BaseTool):
    """Read conflict recipes, or propose the resolution for one."""

    @property
    def name(self) -> str:
        return "repo_recipe"

    @property
    def short_description(self) -> str:
        return "Read or propose a conflict recipe for a landing"

    @property
    def description(self) -> str:
        return (
            "Conflict recipes for `repo_git op=land` (One Track). When a landing conflicts, "
            "`land` records the exact conflict and stops; it names the other ticket(s) that "
            "changed the file.\n"
            "- `op=list` (repo, optional ticket): the open conflicts and proposals.\n"
            "- `op=show` (id): the conflict as git produced it, who changed the other side, "
            "and any proposal.\n"
            "- `op=propose` (id, resolved, reason): the WHOLE resolved file and why it is right "
            "(what each side intended and how the result keeps both). A person approves it in "
            "the Inbox; then land again and it is applied. It replays only for this exact "
            "conflict.\n"
            "Never propose for a hunk changed by someone outside the fleet (land reports those "
            "as foreign_hunks): that is a person's decision, not a recipe.")

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["list", "show", "propose"]},
                "repo": {"type": "string", "description": "owner/repo (list)"},
                "ticket": {"type": "string", "description": "CLC-n (list, optional)"},
                "id": {"type": "string", "description": "recipe id (show, propose)"},
                "resolved": {"type": "string", "description": "the whole resolved file (propose)"},
                "reason": {"type": "string", "description": "why this resolution is right (propose)"},
            },
            "required": ["op"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, op: str, repo: str = "", ticket: str = "", id: str = "",
                      resolved: str = "", reason: str = "", **kwargs) -> ToolResult:
        from ...db import get_pool                                      # noqa: PLC0415
        pool = get_pool()
        if not pool:
            return ToolResult.fail("database unavailable")
        async with pool.acquire() as conn:
            if op == "list":
                if not repo:
                    return ToolResult.fail("list needs repo")
                rows = await conn.fetch(
                    """SELECT id, path, ticket, env, state, other_tickets, other_authors,
                              proposed_by, created_at
                         FROM repo_conflict_recipes
                        WHERE repo = $1 AND ($2 = '' OR upper(ticket) = upper($2))
                          AND state IN ('conflict', 'proposed')
                        ORDER BY created_at""", repo, ticket or "")
                return ToolResult.ok({"open": [
                    {**{k: r[k] for k in ("path", "ticket", "env", "state", "proposed_by")},
                     "id": str(r["id"]), "other_tickets": list(r["other_tickets"]),
                     "other_authors": list(r["other_authors"]),
                     "created_at": r["created_at"].isoformat()} for r in rows]})
            if not id:
                return ToolResult.fail(f"{op} needs id")
            row = await conn.fetchrow("SELECT * FROM repo_conflict_recipes WHERE id::text = $1", id)
            if not row:
                return ToolResult.fail(f"no recipe {id}")
            if op == "show":
                return ToolResult.ok({k: (str(v) if k == "id" else v) for k, v in dict(row).items()
                                      if k not in ("base_blob", "ours_blob", "theirs_blob")}
                                     | {"created_at": row["created_at"].isoformat()})
            if op == "propose":
                if row["state"] == "approved":
                    return ToolResult.fail("already approved; land again to apply it")
                if not resolved.strip() or len((reason or "").strip()) < 20:
                    return ToolResult.fail("propose needs the whole `resolved` file and a "
                                           "`reason` (a sentence or more: what each side "
                                           "intended and how the result keeps both)")
                if any(m in resolved for m in MARKERS):
                    return ToolResult.fail("the resolved file still has conflict markers")
                who = getattr(self, "_profile_slug", "") or ""
                await conn.execute(
                    """UPDATE repo_conflict_recipes
                          SET state = 'proposed', resolved = $2, reason = $3, proposed_by = $4,
                              proposed_at = now(), decided_by = '', decided_at = NULL,
                              decision_note = ''
                        WHERE id = $1""", row["id"], resolved, reason.strip()[:4000], who)
                return ToolResult.ok({"id": id, "state": "proposed",
                                      "next": "A person approves it in the Inbox (Recipe row). "
                                              "Then land again: it applies to this exact "
                                              "conflict only."})
        return ToolResult.fail(f"unknown op {op!r}: list, show or propose")

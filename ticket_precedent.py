"""
ticket_precedent — which repository has already been changed for a ticket like this one.

WHY. Routing a ticket by KEYWORD is a guess dressed as a score. CLC-4550 scored
cenora-core-data-services at medium on the words "signature" and "sailings"; the repo that
actually carries the work is EntityFeatureDataLoaders, and the evidence was sitting in git the
whole time — CLC-4510, the ticket CLC-4550 follows up on, was fixed on
`fix/CLC-4510-cruisetour-signature-flag`, three commits, one file. A keyword match cannot know
that. A commit message can only be wrong about it if a human wrote the wrong key.

So this is the precedent lookup: given the ticket and the tickets it references, which
repositories have commits or branches naming them, and which files did those commits touch.
That answer is MEASURED — someone already did the work and git recorded where — which is why
it should outrank every keyword score in a routing decision.

WHAT ABSENCE MEANS, and why that is stated rather than implied. A checkout only knows the
history it has fetched. No hits is not proof the work was not done: the clone may be behind,
the branch may live only on the remote, or the human may simply never have put the key in a
commit message. So every repository reports how far its history reaches, and a clean sweep is
reported as "no precedent found", never as "the work is not there".
"""

import asyncio
import re
import subprocess

import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import git_env, check_repo_access, validate_project_root, ACCESS_LEVELS

log = structlog.get_logger()

_KEY = re.compile(r"\b([A-Z][A-Z0-9]*-\d+)\b")

# A sweep is one cheap git call per repo per key. The caps exist so a caller that passes a
# whole ticket body full of keys cannot turn a routing decision into a minutes-long scan.
MAX_REPOS = 40
MAX_KEYS = 6
GIT_TIMEOUT = 20


def _git(args: list[str], cwd: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                           timeout=GIT_TIMEOUT, env=git_env())
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


@register_tool
class TicketPrecedentTool(BaseTool):
    """Find the repository that has already been changed for a ticket, from git history."""

    @property
    def name(self) -> str:
        return "ticket_precedent"

    @property
    def description(self) -> str:
        return (
            "Find which repository a ticket belongs to from GIT HISTORY rather than from "
            "keywords. Call it BEFORE fleet_route whenever a ticket references another "
            "ticket, is a follow-up, a re-fail, or a 'same as' — which is most of them.\n\n"
            "Searches every repository you may read for commits whose message names the key, "
            "and for branches named after it, and reports the files those commits touched. A "
            "repo that has already been changed for CLC-4510 is where CLC-4510's follow-up "
            "belongs, and that is evidence, not a score.\n\n"
            "Pass the keys the ticket REFERENCES as well as its own — a brand new ticket has "
            "no history of its own, and its parent's history is the whole point.\n\n"
            "No hits does not mean the work was not done: a checkout only knows what it has "
            "fetched, and a human may never have put the key in a commit message. Each repo "
            "reports how far its history reaches so you can say which it was."
        )

    @property
    def short_description(self) -> str:
        return "Which repo already has commits for this ticket"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {
            "ticket": {"type": "string",
                       "description": "The ticket key — CLC-4550"},
            "also": {"type": "array", "items": {"type": "string"},
                     "description": "Other keys to search for — the tickets this one "
                                    "references, which is where the history actually is. "
                                    "Read them out of the ticket body"},
            "repos": {"type": "array", "items": {"type": "string"},
                      "description": "Restrict the sweep to these repos (owner/repo). "
                                     "Omit to sweep every repo you may read"},
            "limit": {"type": "integer",
                      "description": "Commits to report per repo per key. Default 5"},
        }, "required": ["ticket"]}

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, ticket: str, also: list = None, repos: list = None,
                      limit: int = 5, **kw) -> ToolResult:
        keys = []
        for raw in [ticket, *(also or [])]:
            for m in _KEY.finditer((raw or "").upper()):
                if m.group(1) not in keys:
                    keys.append(m.group(1))
        if not keys:
            return ToolResult.fail(
                f"No ticket key in {ticket!r}. Give a key like CLC-4550 — this searches commit "
                "messages for it, so it has to be the string a human would have typed.")
        keys = keys[:MAX_KEYS]

        candidates = repos or await self._configured_repos()
        if not candidates:
            return ToolResult.fail(
                "No repository is configured for log access, so there is no history to search. "
                "Repos are added in the Repos admin panel.")

        hits, unreachable = [], []
        for repo in candidates[:MAX_REPOS]:
            ok, root, access, err, _ = await check_repo_access(repo, "log", self._profile_slug)
            if not ok:
                unreachable.append({"repo": repo, "why": err})
                continue
            ok, err = validate_project_root(root)
            if not ok:
                unreachable.append({"repo": repo, "why": err})
                continue
            found = await asyncio.to_thread(self._sweep, repo, root, keys, limit)
            if found["commits"] or found["branches"]:
                hits.append(found)

        hits.sort(key=lambda h: (len(h["commits"]) + len(h["branches"])), reverse=True)

        # The verdict is stated, because a caller handed a list will pick the first row and a
        # single-repo answer and a split answer demand different next moves.
        if not hits:
            verdict = ("no precedent found. None of the searched checkouts has a commit or "
                       "branch naming these keys — which may mean the work was never done, or "
                       "that it was done somewhere this sweep cannot see. Routing falls back "
                       "to keywords, and should say so.")
        elif len(hits) == 1:
            verdict = (f"precedent: {hits[0]['repo']} — it already carries commits or a branch "
                       f"for {', '.join(hits[0]['keys'])}. This outranks a keyword score.")
        else:
            verdict = ("precedent is split across " +
                       ", ".join(h["repo"] for h in hits) +
                       ". A ticket whose history touches several repos probably needs a unit "
                       "in each; do not silently pick the first.")

        log.info("ticket_precedent", keys=keys, searched=len(candidates),
                 hit_repos=[h["repo"] for h in hits])
        return ToolResult.ok({
            "keys": keys,
            "searched": len(candidates[:MAX_REPOS]),
            "hits": hits,
            "unreachable": unreachable,
            "verdict": verdict,
        })

    async def _configured_repos(self) -> list[str]:
        """Every enabled repo whose access level permits reading history."""
        from ...db import get_pool
        pool = get_pool()
        if not pool:
            return []
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT repo, access FROM skill_repo_config WHERE enabled ORDER BY repo")
        return [r["repo"] for r in rows
                if "log" in ACCESS_LEVELS.get(r["access"], set())]

    def _sweep(self, repo: str, root: str, keys: list[str], limit: int) -> dict:
        commits, branches, matched = [], [], []
        for key in keys:
            # --all, because the branch that carries the fix is frequently not the one checked
            # out — and on a repo that promotes by cherry-pick the same subject appears three
            # times, which is itself the signal that it shipped.
            out = _git(["log", "--all", f"-{limit}", f"--grep={key}", "--date=short",
                        "--format=%h\x1f%ad\x1f%an\x1f%s"], root)
            for line in [l for l in out.splitlines() if l.strip()]:
                sha, date, author, subject = (line.split("\x1f") + ["", "", ""])[:4]
                commits.append({"key": key, "sha": sha, "date": date, "author": author,
                                "subject": subject,
                                # The files are the scope hint: CLC-4510 touched exactly one
                                # file, which tells the next ticket where to start.
                                "files": [f for f in _git(
                                    ["show", "--stat=200", "--format=", "--name-only", sha],
                                    root).splitlines() if f.strip()][:20]})
            key_branches = []
            for line in _git(["branch", "-a", "--list", f"*{key}*"], root).splitlines():
                name = line.strip().lstrip("*+ ").strip()
                if name and name not in branches:
                    branches.append(name)
                    key_branches.append(name)
            # Per KEY, not per repo: asking "or branches" against the running list credited
            # every later key with the first key's branch, so a repo with history for CLC-4510
            # claimed history for CLC-4550 as well — a false precedent, which is worse than
            # none because it reads as confirmation.
            if any(c["key"] == key for c in commits) or key_branches:
                if key not in matched:
                    matched.append(key)
        return {
            "repo": repo,
            "keys": matched,
            "commits": commits,
            "branches": branches,
            # How far this checkout's knowledge goes. Absence of a hit means nothing without it.
            "history_reaches": _git(["log", "--all", "-1", "--format=%cI"], root),
        }

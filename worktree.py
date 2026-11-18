"""
worktree — each fleet work unit gets its own working tree of the repo (docs/UNIT_WORKTREES_PLAN.md
in instar-skill-fleet). OFF unless FLEET_UNIT_WORKTREES=on.

WHY (deficiency #56/#60, 2026-11-17). The dev worked every unit in ONE checkout per repo. Three
units on entity-feature-data-loaders interleaved in it: a CLC-4673 commit was attested to CLC-4672,
CLC-4671 was attested at production's tip, bot/CLC-4673 reached GitHub empty, and a review read
the wrong branch. The dispatch lane (fleet lane.py) stops that by running one unit per repo at a
time; a tree per unit removes the need to wait.

LAYOUT. The main checkout stays where it is and stays the object store:
    /projects/workspaces/<ws>/repos/<owner>/<repo>                       main checkout
    /projects/workspaces/<ws>/.units/<unit-uuid>/repos/<owner>/<repo>    the unit's tree
The executor and the dev runner mount the host's workspaces at the same absolute path, so the
absolute paths git records for a worktree resolve in both.

GIT CREATES THE DIRECTORIES. git_run drops privileges; a directory made here as root would be
unwritable to the git that has to fill it (the root-owned mkdtemp that broke `land`, 11-16).
"""

import os
import re

import structlog

from .base import WORKSPACES_ROOT, git_run

log = structlog.get_logger()

UNIT_CONVERSATION = re.compile(r"^fleet:unit:([0-9a-f-]{36})$")


def enabled(repo: str = "") -> bool:
    """FLEET_UNIT_WORKTREES: 'on' = every repo; a comma list of repo names (owner/name or name)
    = only those (the testbed first, per the plan); unset/'off' = none. With no repo given,
    True when it is on for any repo."""
    raw = os.environ.get("FLEET_UNIT_WORKTREES", "off").strip().lower()
    if raw in ("", "off", "0", "false"):
        return False
    if raw == "on":
        return True
    allowed = {x.strip() for x in raw.split(",") if x.strip()}
    if not repo:
        return bool(allowed)
    repo = repo.strip().lower()
    return repo in allowed or repo.split("/")[-1] in allowed


def tree_path(workspace_slug: str, unit_id: str, repo: str) -> str:
    return os.path.join(WORKSPACES_ROOT, workspace_slug, ".units", unit_id, "repos", repo)


def _git(cwd: str, *args, timeout=120):
    return git_run(["git", "-C", cwd, *args], timeout=timeout)


def _branch_checked_out_at(main: str, branch: str) -> str:
    """The worktree path that has `branch` checked out, or ''."""
    out = _git(main, "worktree", "list", "--porcelain").stdout
    path = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.strip() == f"branch refs/heads/{branch}":
            return path
    return ""


def ensure(main: str, tree: str, branch: str, base_ref: str) -> tuple[bool, str]:
    """Make `tree` a worktree of `main` on `branch`. Idempotent. (ok, path-or-reason)

    An existing local branch is checked out as it is (its commits are the unit's work). A new
    one is cut from `base_ref` (e.g. origin/production); the dev re-cuts with repo_branch when
    the base has moved. A branch already checked out in another tree is refused: git forbids it,
    and it means two places would claim the same unit's work.
    """
    if os.path.exists(os.path.join(tree, ".git")):
        return True, tree
    if not os.path.isdir(os.path.join(main, ".git")):
        return False, f"no checkout at {main}"
    _git(main, "worktree", "prune")
    elsewhere = _branch_checked_out_at(main, branch)
    # THE SHARED CHECKOUT HOLDING THE BRANCH is the normal case when worktrees are switched on
    # for a repo that was worked the old way (2026-11-18: entity-feature-data-loaders sat on
    # bot/CLC-4555). A CLEAN shared checkout is detached at the same commit to free the branch:
    # nothing is lost, and the branch's commits move into the unit's tree. A dirty one is left
    # alone and refused below, because its uncommitted work belongs to whoever made it.
    if elsewhere and os.path.realpath(elsewhere) == os.path.realpath(main):
        dirty = _git(main, "status", "--porcelain", "--untracked-files=no").stdout.strip()
        if not dirty and _git(main, "checkout", "--detach").returncode == 0:
            log.info("unit_worktree_freed_branch", main=main, branch=branch)
            elsewhere = ""
    if elsewhere and os.path.realpath(elsewhere) != os.path.realpath(tree):
        return False, (f"{branch} is checked out at {elsewhere}; switch that checkout off it "
                       "before the unit can have its own tree")
    has_branch = _git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
    args = (["worktree", "add", tree, branch] if has_branch
            else ["worktree", "add", "-b", branch, tree, base_ref])
    r = _git(main, *args, timeout=300)
    if r.returncode != 0:
        return False, f"git worktree add failed: {(r.stderr or r.stdout).strip()[:300]}"
    log.info("unit_worktree_created", tree=tree, branch=branch,
             cut_from=None if has_branch else base_ref)
    return True, tree


def remove(main: str, tree: str) -> tuple[bool, str]:
    """Remove a unit's tree if it holds no uncommitted change. (ok, why)"""
    if not os.path.exists(tree):
        return True, "already gone"
    dirty = _git(tree, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        return False, f"kept: {tree} has uncommitted changes"
    r = _git(main, "worktree", "remove", tree)
    if r.returncode != 0:
        return False, f"git worktree remove failed: {(r.stderr or r.stdout).strip()[:300]}"
    _git(main, "worktree", "prune")
    # git removes the tree, not the folders above it. Drop the now-empty ones up to and including
    # .units/<unit>: the runner treats an existing unit folder as "start here" (testbed proof,
    # 2026-11-17, left .units/<unit>/repos/<owner>/ behind). Stops at the first non-empty folder.
    path = os.path.dirname(tree)
    while "/.units/" in path + "/" and not path.endswith("/.units"):
        try:
            os.rmdir(path)
        except OSError:
            break
        path = os.path.dirname(path)
    log.info("unit_worktree_removed", tree=tree)
    return True, "removed"


async def _unit_paths(conn, unit_id: str) -> dict | None:
    """Where a unit's main checkout and tree live, and its branch and base. None if unknown."""
    u = await conn.fetchrow(
        """SELECT u.repo_slug, u.parent_key, u.branch, u.base_branch,
                  r.repo, r.branch AS repo_branch, w.slug AS ws
             FROM fleet_work_units u
             JOIN skill_repo_config r ON r.repo LIKE '%/' || u.repo_slug
             LEFT JOIN workspaces w ON w.id = r.workspace_id
            WHERE u.id = $1::uuid""", unit_id)
    if not (u and u["ws"] and u["parent_key"]):
        return None
    branch = u["branch"] or f"bot/{u['parent_key']}"
    base = u["base_branch"] or u["repo_branch"] or "production"
    return {"main": os.path.join(WORKSPACES_ROOT, u["ws"], "repos", u["repo"]),
            "tree": tree_path(u["ws"], unit_id, u["repo"]),
            "branch": branch, "base_ref": f"origin/{base}", "repo": u["repo"]}


async def ensure_for_unit(conn, unit_id: str) -> tuple[bool, str]:
    """Give a unit its tree at handover. (ok, tree-path-or-reason). Off unless enabled()."""
    if not enabled():
        return False, "unit worktrees are off (FLEET_UNIT_WORKTREES)"
    p = await _unit_paths(conn, unit_id)
    if not p:
        return False, "the unit's repo or workspace is not configured"
    if not enabled(p["repo"]):
        return False, f"unit worktrees are off for {p['repo']} (FLEET_UNIT_WORKTREES)"
    import asyncio                                                      # noqa: PLC0415
    return await asyncio.to_thread(ensure, p["main"], p["tree"], p["branch"], p["base_ref"])


async def remove_for_unit(conn, unit_id: str) -> tuple[bool, str]:
    """Drop a finished unit's tree if it is clean. (ok, why)"""
    if not enabled():
        return False, "unit worktrees are off"
    p = await _unit_paths(conn, unit_id)
    if not p:
        return False, "the unit's repo or workspace is not configured"
    if not enabled(p["repo"]):
        return False, f"unit worktrees are off for {p['repo']}"
    import asyncio                                                      # noqa: PLC0415
    return await asyncio.to_thread(remove, p["main"], p["tree"])


async def calling_unit(session_id: str, gatekeeper_url: str) -> str:
    """The work unit behind a tool call (its conversation is fleet:unit:<id>), or ''."""
    if not (session_id and gatekeeper_url):
        return ""
    try:
        import httpx                                                    # noqa: PLC0415
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{gatekeeper_url.rstrip('/')}/session-info",
                                 params={"session_id": session_id})
        if r.status_code != 200:
            return ""
        m = UNIT_CONVERSATION.match((r.json() or {}).get("conversation_id") or "")
        return m.group(1) if m else ""
    except Exception as exc:                                            # noqa: BLE001
        log.debug("calling_unit_lookup_failed", error=str(exc)[:200])
        return ""

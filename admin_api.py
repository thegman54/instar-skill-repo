"""
Repo Skill — Admin API handlers.

Manages the repository access configuration: which repos the bot can
clone/read/write/push, and what level of access each one gets.
"""

import structlog

log = structlog.get_logger()

TABLE = "skill_repo_config"


def _row(r):
    """Convert asyncpg Record to JSON-safe dict."""
    d = dict(r)
    for k, v in d.items():
        if hasattr(v, 'hex'):  # UUID
            d[k] = str(v)
        elif hasattr(v, 'isoformat'):  # datetime
            d[k] = v.isoformat()
    return d


# ---------------------------------------------------------------------------
# List repos
# ---------------------------------------------------------------------------

async def list_repos(pool, body=None, **kw):
    """List all configured repositories."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, repo, label, access, enabled, created_at, updated_at "
            f"FROM {TABLE} ORDER BY repo"
        )
        return {
            "repos": [_row(r) for r in rows],
            "count": len(rows),
        }


# ---------------------------------------------------------------------------
# Add repo
# ---------------------------------------------------------------------------

async def add_repo(pool, body=None, **kw):
    """Add a new repository to the allowed list."""
    if not body:
        return {"__status": 400, "detail": "Request body required"}

    repo = (body.get("repo") or "").strip()
    if not repo or "/" not in repo:
        return {"__status": 400, "detail": "repo must be in 'owner/repo' format"}

    label = (body.get("label") or "").strip() or None
    access = body.get("access", "full")
    if access not in ("full", "read-only", "no-push"):
        return {"__status": 400, "detail": "access must be 'full', 'read-only', or 'no-push'"}

    enabled = body.get("enabled", True)

    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            f"SELECT id FROM {TABLE} WHERE repo = $1", repo
        )
        if existing:
            return {"__status": 409, "detail": f"Repository '{repo}' already configured"}

        row = await conn.fetchrow(
            f"INSERT INTO {TABLE} (repo, label, access, enabled) "
            f"VALUES ($1, $2, $3, $4) RETURNING id, repo, label, access, enabled",
            repo, label, access, enabled,
        )
        log.info("repo_added", repo=repo, access=access)
        return {"status": "added", "repo": _row(row)}


# ---------------------------------------------------------------------------
# Update repo
# ---------------------------------------------------------------------------

async def update_repo(pool, body=None, repo_id=None, **kw):
    """Update an existing repository configuration."""
    if not body:
        return {"__status": 400, "detail": "Request body required"}

    if not repo_id:
        return {"__status": 400, "detail": "repo_id required"}

    sets = []
    params = []
    idx = 1

    if "label" in body:
        sets.append(f"label = ${idx}")
        params.append((body["label"] or "").strip() or None)
        idx += 1

    if "access" in body:
        access = body["access"]
        if access not in ("full", "read-only", "no-push"):
            return {"__status": 400, "detail": "access must be 'full', 'read-only', or 'no-push'"}
        sets.append(f"access = ${idx}")
        params.append(access)
        idx += 1

    if "enabled" in body:
        sets.append(f"enabled = ${idx}")
        params.append(bool(body["enabled"]))
        idx += 1

    if not sets:
        return {"__status": 400, "detail": "Nothing to update"}

    sets.append(f"updated_at = now()")

    params.append(repo_id)
    query = f"UPDATE {TABLE} SET {', '.join(sets)} WHERE id = ${idx} RETURNING id, repo, label, access, enabled"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *params)
        if not row:
            return {"__status": 404, "detail": "Repository not found"}

        log.info("repo_updated", repo=row["repo"], changes=list(body.keys()))
        return {"status": "updated", "repo": _row(row)}


# ---------------------------------------------------------------------------
# Delete repo
# ---------------------------------------------------------------------------

async def delete_repo(pool, body=None, repo_id=None, **kw):
    """Remove a repository from the allowed list."""
    if not repo_id:
        return {"__status": 400, "detail": "repo_id required"}

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"DELETE FROM {TABLE} WHERE id = $1 RETURNING repo", repo_id
        )
        if not row:
            return {"__status": 404, "detail": "Repository not found"}

        log.info("repo_deleted", repo=row["repo"])
        return {"status": "deleted", "repo": row["repo"]}


# ---------------------------------------------------------------------------
# Check access (called by tools at execution time)
# ---------------------------------------------------------------------------

async def check_access(pool, body=None, repo_name=None, **kw):
    """
    Check if a repo is allowed and what access level it has.
    Used by tools internally before executing operations.
    """
    if not repo_name:
        return {"__status": 400, "detail": "repo_name required"}

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT repo, access, enabled FROM {TABLE} WHERE repo = $1",
            repo_name,
        )
        if not row:
            return {"allowed": False, "reason": "not configured"}
        if not row["enabled"]:
            return {"allowed": False, "reason": "disabled"}
        return {
            "allowed": True,
            "repo": row["repo"],
            "access": row["access"],
        }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

routes = [
    ("GET",    r"/repos$",                              list_repos),
    ("POST",   r"/repos$",                              add_repo),
    ("PUT",    r"/repos/(?P<repo_id>[\w-]+)$",          update_repo),
    ("DELETE", r"/repos/(?P<repo_id>[\w-]+)$",          delete_repo),
    ("GET",    r"/repos/check/(?P<repo_name>[^/]+/[^/]+)$", check_access),
]

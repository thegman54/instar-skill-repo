"""
Repo Tools Base - Path validation, workspace resolution, and security utilities.

SECURITY: All file operations MUST go through validate_path().
This prevents directory traversal and access to unauthorized projects.

Repository access is controlled by the skill_repo_config table,
managed through the admin panel. Tools call check_repo_access()
to validate access and get the permitted operation level.
"""

import os
import re
import subprocess
from pathlib import Path
import structlog

log = structlog.get_logger()

# Workspace roots inside the tool-executor container
WORKSPACE_ROOT = "/projects/workspace"           # Legacy flat layout (no workspace)
WORKSPACES_ROOT = "/projects/workspaces"          # Workspace-scoped layout

# Blocked paths - NEVER allow access regardless of grant
BLOCKED_PATHS = [
    "/projects/project-instar",  # Self-modification forbidden
]

# Let git operate on checkouts this process does not own.
#
# THE MISMATCH IS DELIBERATE ON BOTH SIDES. This container runs as root; every clone is
# handed to uid 1000 by _chown_workspace so the expert-runner, which runs unprivileged, can
# use the same checkout. Root can read and write those files perfectly well — it is only
# git's ownership CHECK that objects, and it objects to every git invocation in this skill:
# status, log, diff, branch, commit, push, and the fetch inside repo_clone.
#
# So safe.directory is the remedy git documents for exactly this, not a workaround. The check
# exists to stop you running a repo owned by an untrusted OTHER user; here the other user is
# one we created, on a path this skill already gates through check_repo_access.
#
# Set as GIT_CONFIG_* env rather than written into a config file so it applies to every
# subprocess without a file on disk to drift, and so it cannot leak into the checkouts
# themselves — a `git config` write would land in a repository the bots push from.
GIT_SAFE_ENV = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": "*",
}


def git_identity(profile_slug: str = "", display_name: str = "") -> dict:
    """Author/committer identity for a commit, as environment rather than config.

    Git refuses to commit without one, and nothing in these containers sets it, so every
    commit through this skill failed with "Author identity unknown" — the last step of the
    write path, reached only after a branch and a file edit had already succeeded.

    The identity names the PROFILE that committed, because a commit whose author is a
    generic service account is a commit nobody can trace back to the bot that wrote it.
    Set via GIT_* env for the same reason as safe.directory: it cannot drift, and it cannot
    be written into a checkout the bots push from.
    """
    slug = (profile_slug or "").strip() or "instar-bot"
    name = (display_name or "").strip() or slug
    email = f"{slug}@bots.instar.local"
    return {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
    }


async def git_author(profile_slug: str = "") -> dict:
    """The same identity, with the NAME a person would recognise in a git log.

    WHY THE NAME AND THE ADDRESS DISAGREE ON PURPOSE. These commits land in other teams'
    repositories, where `fleet_cen_dev` is an internal slug that means nothing to the
    reviewer reading the history. So the name is the profile's `display_name` -- the one
    string a human already maintains for it in the admin UI -- and the address stays
    `<slug>@bots.instar.local`, which is what makes a commit traceable back to exactly one
    profile no matter how often somebody renames it. Rename freely; `git log --author` keeps
    working, because it has the address to go on.

    Falls back to the slug if the lookup fails. A commit must not fail over its byline.
    """
    slug = (profile_slug or "").strip()
    display = ""
    if slug:
        try:
            from ...db import get_pool                                 # noqa: PLC0415
            pool = get_pool()
            if pool:
                async with pool.acquire() as conn:
                    display = await conn.fetchval(
                        "SELECT display_name FROM bot_profiles WHERE slug = $1", slug) or ""
        except Exception as exc:                                       # noqa: BLE001
            log.warning("git_author_lookup_failed", profile=slug, error=str(exc)[:200])
    return git_identity(slug, display)


# WHO THE GIT SUBPROCESS RUNS AS.
#
# tool-executor runs as root; the checkouts belong to uid 1000, because the expert-runner is
# unprivileged and works in the same trees. Root writing into them is not blocked — it is
# recorded, as root-owned files, and that is the damage. On 2026-11-10 a merge inside an
# expert's own repository died with "insufficient permission for adding an object to
# repository database": 156 paths across 22 checkouts were root-owned, 39 in one repo, left
# by repo_commit and repo_clone over seven weeks. The symptom is worse than the cause,
# because git fans objects out by the first two hex digits of their hash — so an expert's
# write succeeded or failed depending on which directory the new object happened to land in.
#
# repo_clone already chowned its clone afterwards, which is a sweep rather than a fix: every
# later write re-created the problem and nothing swept again. Dropping to uid 1000 for the
# subprocess means the file is never created wrong, so there is nothing to repair.
#
# HOME MOVES WITH IT. As uid 1000 the process cannot read root's, and git reading an
# unreadable global config is a failure rather than a default. Pointed at a temp dir, which
# costs nothing: safe.directory already arrives through GIT_CONFIG_* below, so nothing this
# skill relies on lives in a config file.
WORKSPACE_UID = int(os.environ.get("WORKSPACE_UID", "1000"))
WORKSPACE_GID = int(os.environ.get("WORKSPACE_GID", "1000"))


def git_proc_kwargs() -> dict:
    """subprocess kwargs that keep git from creating root-owned files. {} when not root."""
    try:
        if os.geteuid() != 0:
            return {}
    except AttributeError:                                             # pragma: no cover
        return {}
    return {"user": WORKSPACE_UID, "group": WORKSPACE_GID}


def git_run(args: list, **kw):
    """Run git the way this skill always should: shared env, and never as root.

    One chokepoint on purpose. The alternative is remembering, at twenty-odd call sites,
    which ones write — and the one that forgets is never the boring one.
    """
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 120)
    if "env" not in kw:
        kw["env"] = git_env()
    drop = git_proc_kwargs()
    if drop:
        # The env dict is already built; give the unprivileged process a HOME it can use.
        kw["env"] = {**kw["env"], "HOME": os.environ.get("GIT_RUN_HOME", "/tmp")}
        kw.update(drop)
    return subprocess.run(args, **kw)


def git_env(extra: dict = None) -> dict:
    """Environment for any git subprocess in this skill.

    Always use this instead of os.environ. GIT_TERMINAL_PROMPT=0 matters as much as the
    ownership setting: without it a git that wants credentials blocks on a prompt nobody
    can answer, and the call hangs until its timeout instead of failing.
    """
    env = {**os.environ, **GIT_SAFE_ENV, "GIT_TERMINAL_PROMPT": "0"}
    if extra:
        env.update(extra)
    return env

# Valid repo format: owner/repo-name
REPO_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$')

# Pattern to strip tokens from git URLs in error messages
_TOKEN_URL_PATTERN = re.compile(r'https://[^@]+@github\.com/')

# Access levels and what operations they allow
ACCESS_LEVELS = {
    "full":      {"clone", "read", "list", "grep", "status", "diff", "log", "branch", "write", "commit", "push", "pr", "actions"},
    "no-push":   {"clone", "read", "list", "grep", "status", "diff", "log", "branch", "write", "commit"},
    "read-only": {"clone", "read", "list", "grep", "status", "diff", "log", "branch"},
}


def sanitize_stderr(stderr: str) -> str:
    """Strip any embedded tokens from git stderr output before returning to bot."""
    return _TOKEN_URL_PATTERN.sub('https://github.com/', stderr)


# Operations a cross-workspace grant may cover. Reading a neighbouring repository answers a
# question; writing to one changes somebody else's work. `repo_read_all` therefore grants only
# these, and never write/commit/push/pr — so read-many can never quietly become
# read-many/write-many, which is the combination the whole confinement model exists to prevent.
READ_OPS = {"clone", "read", "list", "grep", "status", "diff", "log", "branch", "actions"}


async def check_repo_access(repo: str, operation: str = "read",
                            profile_slug: str = None, session_id: str = None,
                            gatekeeper_url: str = None) -> tuple[bool, str, str, str, str]:
    """
    Check if a repo is allowed and the operation is permitted, FOR THIS PROFILE.

    Access is scoped to the calling profile's workspace. It used to be scoped to nothing at
    all — the lookup matched on repository name alone, so any profile holding repo_read could
    read every repository in skill_repo_config. Nothing leaked, because the table happens to
    hold only Cenora repositories; but the protection was the contents of a table rather than
    a rule, and adding one unrelated repository would have exposed it to every profile with no
    warning.

    A profile may hold `repo_read_all` to read outside its workspace — the coordinator does,
    because cross-repo questions are its entire job. That grant covers READ_OPS only.

    Args:
        repo: Repository in "owner/repo" format
        operation: The operation being attempted (clone, read, write, push, pr, etc.)
        profile_slug: The calling profile. Without it, only same-workspace access is possible.

    Returns:
        (valid, workspace_path, access_level, error_message, branch)
    """
    if not repo:
        return False, "", "", "No repo specified", None

    if not REPO_PATTERN.match(repo):
        return False, "", "", f"Invalid repo format '{repo}' — use 'owner/repo'", None

    # Check DB for access config
    from ...db import get_pool
    pool = get_pool()
    if not pool:
        log.warning("repo_access_check_no_db")
        return False, "", "", "Database not available for access check", None

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT r.repo, r.access, r.enabled, r.workspace_id, r.branch,
                   w.slug AS workspace_slug
            FROM skill_repo_config r
            LEFT JOIN workspaces w ON w.id = r.workspace_id
            WHERE r.repo = $1
        """, repo)

    if not row:
        return False, "", "", f"Repository '{repo}' is not configured — add it in the Repos admin panel", None

    if not row["enabled"]:
        return False, "", "", f"Repository '{repo}' is disabled", None

    access = row["access"]
    allowed_ops = ACCESS_LEVELS.get(access, set())

    if operation not in allowed_ops:
        return False, "", access, f"Operation '{operation}' not allowed — repo has '{access}' access", None

    # --- scope: is this repo in the caller's workspace, or is the caller granted breadth? ---
    #
    # Repos with no workspace are the legacy flat layout. There is nothing to compare against,
    # so scoping cannot apply and they behave as before rather than becoming unreachable.
    if row["workspace_id"]:
        # No caller identity means no way to prove the repo is in scope, so it is not. This
        # branch was originally `and profile_slug`, which skipped the whole check when the
        # profile was unknown — an unidentified caller got unrestricted read, which is the
        # exact hole being closed. Fail closed and say why.
        if not profile_slug:
            return False, "", access, (
                f"Cannot scope access to '{repo}': the calling profile is unknown. "
                "Repository access is per-profile; an unidentified caller gets none."), None

        async with pool.acquire() as conn:
            prof = await conn.fetchrow(
                "SELECT workspace_id, repo_read_all, repo_write_all "
                "  FROM bot_profiles WHERE slug = $1",
                profile_slug)
        same_workspace = bool(prof and prof["workspace_id"]
                              and str(prof["workspace_id"]) == str(row["workspace_id"]))
        read_all = bool(prof and prof["repo_read_all"])
        write_all = bool(prof and prof["repo_write_all"])

        if not same_workspace:
            # WHY THIS EXISTS, given the paragraph above says read-many must never become
            # read-many/write-many. That rule protects tenants from each other, and there is one
            # operator here. The fleet moved to a SINGLE dev covering every repository, which
            # cannot work under per-workspace confinement at all: repos are roughly one per
            # workspace, and the dev must write in all of them.
            #
            # The confinement was not what kept the bots honest anyway. git-gate is — a push
            # needs an open work unit for (repo, ticket), the protected branch names are refused
            # outright, and it fails closed when it cannot check. Those are untouched by this,
            # and they are the guarantees that actually held during every incident so far.
            #
            # A SEPARATE flag rather than widening repo_read_all, so a profile that only needs to
            # read its neighbours still cannot write to them, and so granting this stays a
            # visible decision on whichever profile holds it.
            if operation not in READ_OPS and not write_all:
                return False, "", access, (
                    f"'{repo}' is outside this profile's workspace, and '{operation}' writes. "
                    "That needs the repo_write_all grant, which this profile does not have."), None
            if operation in READ_OPS and not (read_all or write_all):
                return False, "", access, (
                    f"'{repo}' is not in this profile's workspace. Reading outside it requires "
                    "the repo_read_all grant, which this profile does not have."), None

    # Resolve workspace path — workspace-scoped if assigned, legacy flat otherwise
    if row["workspace_slug"]:
        workspace_path = os.path.join(WORKSPACES_ROOT, row["workspace_slug"], "repos", repo)
    else:
        workspace_path = os.path.join(WORKSPACE_ROOT, repo)

    configured_branch = row["branch"]  # None if not set

    # A FLEET UNIT WORKS IN ITS OWN TREE (worktree.py, FLEET_UNIT_WORKTREES=on). When the call
    # comes from a work unit's conversation and that unit has a tree of this repo, every repo
    # tool (read, write, branch, commit, verify) sees the tree instead of the shared checkout.
    # Without a tree, or with the flag off, nothing changes. Callers pass their session and
    # gatekeeper URL; a caller that does not stays on the shared checkout.
    if row["workspace_slug"] and session_id and gatekeeper_url:
        from .worktree import calling_unit, enabled, tree_path       # noqa: PLC0415
        if enabled(repo):
            unit = await calling_unit(session_id, gatekeeper_url)
            if unit:
                tree = tree_path(row["workspace_slug"], unit, repo)
                if os.path.exists(os.path.join(tree, ".git")):
                    workspace_path = tree

    # Check blocked paths
    resolved = str(Path(workspace_path).resolve())
    for blocked in BLOCKED_PATHS:
        if resolved.startswith(blocked) or blocked.startswith(resolved):
            log.warning("blocked_repo_access", repo=repo, resolved=resolved)
            return False, "", "", "Access to this repository is forbidden", None

    return True, workspace_path, access, "", configured_branch


def validate_path(project_root: str, relative_path: str) -> tuple[bool, str, str]:
    """
    Validate and resolve a path within a project.

    SECURITY: This is the critical boundary. All file tools MUST use this.

    Args:
        project_root: The project root directory
        relative_path: The path requested by the bot

    Returns:
        (valid, absolute_path, error_message)
    """
    if not project_root or not relative_path:
        return False, "", "Missing project_root or path"

    root = Path(project_root).resolve()

    # Check if project root itself is blocked
    for blocked in BLOCKED_PATHS:
        if str(root).startswith(blocked) or blocked.startswith(str(root)):
            log.warning("blocked_project_access", root=str(root), blocked=blocked)
            return False, "", "Access to this project is forbidden"

    # Resolve the full path
    if os.path.isabs(relative_path):
        full_path = Path(relative_path).resolve()
    else:
        full_path = (root / relative_path).resolve()

    # Critical check: is the resolved path within the project root?
    try:
        full_path.relative_to(root)
    except ValueError:
        log.warning("path_traversal_attempt",
                   requested=relative_path,
                   resolved=str(full_path),
                   root=str(root))
        return False, "", "Path is outside project boundary"

    return True, str(full_path), ""


def validate_project_root(project_root: str) -> tuple[bool, str]:
    """
    Validate a project root directory exists and is accessible.

    Args:
        project_root: Path to validate

    Returns:
        (valid, error_message)
    """
    if not project_root:
        return False, "No project root specified"

    root = Path(project_root)

    resolved = str(root.resolve())
    for blocked in BLOCKED_PATHS:
        if resolved.startswith(blocked) or blocked.startswith(resolved):
            return False, "Access to this project is forbidden"

    if not root.exists():
        return False, f"Project directory does not exist: {project_root}"

    if not root.is_dir():
        return False, f"Project path is not a directory: {project_root}"

    return True, ""


# A shared checkout older than this is flagged on every read. 30 minutes: long enough that a
# busy session is not nagged, short enough that a day-old checkout never passes as current.
STALE_CHECKOUT_SECONDS = int(os.environ.get("REPO_STALE_CHECKOUT_SECONDS", "1800"))


def checkout_staleness(project_root: str) -> str:
    """A warning when the checkout a read just searched may not be current, else "".

    WHY (2026-11-19, CLC-4683). The coordinator grepped the shared EFDL checkout for
    ExternalAssetUpsert, found nothing, and told Alex "no class named ExternalAssetUpsert
    exists anywhere in the repo". It had been on production for 15 hours; the checkout had
    not been synced since before the merge. Three minutes later her own repo_clone pulled it
    and the same read succeeded. A read cannot tell "absent" from "not pulled yet" unless it
    says how old the checkout is. Local facts only (no network): last fetch time, and commits
    the branch is behind its remote as of that fetch."""
    try:
        git_dir = Path(project_root) / ".git"
        if git_dir.is_file():                       # a worktree: .git points at the real dir
            return ""
        mark = next((p for p in (git_dir / "FETCH_HEAD", git_dir / "HEAD") if p.exists()), None)
        if mark is None:
            return ""
        import time                                                     # noqa: PLC0415
        age = int(time.time() - mark.stat().st_mtime)
        behind = git_run(["git", "rev-list", "--count", "HEAD..@{upstream}"],
                         cwd=project_root, timeout=10)
        n = int(behind.stdout.strip()) if behind.returncode == 0 and behind.stdout.strip().isdigit() else 0
        if n == 0 and age < STALE_CHECKOUT_SECONDS:
            return ""
        parts = [f"this checkout was last synced {age // 60} minute(s) ago"]
        if n:
            parts.append(f"it is {n} commit(s) behind its remote branch")
        return ("; ".join(parts) + ". A miss here may be code merged since: call repo_clone "
                "to refresh before saying something does not exist.")
    except Exception:                                                   # noqa: BLE001
        return ""


# A ref names a commit the caller wants read: a branch (origin/production, bot/CLC-4755), a tag,
# a sha, with ^/~ suffixes. Never an option (leading "-") and never `ref:path` syntax.
_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/@{}^~-]{0,199}$")


def validate_ref(project_root: str, ref: str) -> tuple[bool, str]:
    """(ok, error) for one ref, checked against this checkout's object store.

    WHY (2026-11-24). repo_read and repo_diff could only see the files on disk, and the shared
    checkout sits on whatever branch the dev bot last worked. A CLC-4755 review read pre-fix
    code because the checkout was on bot/CLC-4644-testing; switching branches is refused while
    a unit's worktree holds the branch. Branches are shared by every worktree of a repo, so
    reading at a ref needs no switch at all.
    """
    if not ref or not _REF_RE.match(ref) or ".." in ref:
        return False, (f"Invalid ref {ref!r}: a branch, tag or commit, e.g. origin/production, "
                       "bot/CLC-4755, 1438db54")
    r = git_run(["git", "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                cwd=project_root, timeout=15)
    if r.returncode != 0:
        return False, (f"Ref {ref!r} is not in this checkout. Remote branches are origin/<name>; "
                       "call repo_clone to fetch if it was pushed recently.")
    return True, ""


# ---------------------------------------------------------------- branching policy (One Track)
# The per-repo switch (fleet triage 2026-11-25, D13-D15; design section 9). It lives in the
# gate policy, mounted outside every working tree, so a bot cannot change its own rules with a
# commit. The admin UI owns it.
POLICY_DIR = os.environ.get("REPO_POLICY_DIR", "/app/tools/repo_expert_runtime/expert-runner/policy")

# UNKNOWN MEANS SAFEST. A repo without a `branching` block, an unreadable file, or a value this
# code does not recognise lands only its own commits, only reports drift, and treats every hunk
# outside the fleet as someone else's. A missing setting can never cause a merge or a back-merge.
BRANCHING_SAFEST = {"landing": "own_commits", "drift_action": "report", "shared": True,
                    "fleet_paths": [], "owners": []}
_LANDING = {"merge", "own_commits", "auto"}
_DRIFT = {"pr", "report"}


def branching_policy(repo: str) -> tuple:
    """(policy, problems). Always a usable policy; `problems` says what fell back to safest."""
    import json                                                         # noqa: PLC0415
    path = os.path.join(POLICY_DIR, repo.split("/")[-1] + ".json")
    out, problems = dict(BRANCHING_SAFEST), []
    try:
        with open(path, encoding="utf-8") as fh:
            blk = (json.load(fh) or {}).get("branching")
    except Exception as exc:                                            # noqa: BLE001
        return out, [f"policy {path} unreadable ({str(exc)[:120]}): safest settings"]
    if not isinstance(blk, dict):
        return out, [f"policy {path} has no branching block: safest settings"]
    if blk.get("landing") in _LANDING:
        out["landing"] = blk["landing"]
    elif "landing" in blk:
        problems.append(f"landing {blk.get('landing')!r} unknown: own_commits")
    if blk.get("drift_action") in _DRIFT:
        out["drift_action"] = blk["drift_action"]
    elif "drift_action" in blk:
        problems.append(f"drift_action {blk.get('drift_action')!r} unknown: report")
    if isinstance(blk.get("shared"), bool):
        out["shared"] = blk["shared"]
    for key in ("fleet_paths", "owners"):
        val = blk.get(key)
        if isinstance(val, list) and all(isinstance(v, str) for v in val):
            out[key] = val
        elif key in blk:
            problems.append(f"{key} is not a list of strings: ignored")
    return out, problems


def path_in_fence(path: str, fleet_paths: list) -> bool:
    """True when `path` is inside one of the policy's fleet_paths globs (empty list = whole repo).
    `**` matches any depth; matching is on the repository-relative path."""
    import fnmatch                                                      # noqa: PLC0415
    if not fleet_paths:
        return True
    return any(fnmatch.fnmatchcase(path, g) or (g.endswith("/**") and path.startswith(g[:-2]))
               for g in fleet_paths)

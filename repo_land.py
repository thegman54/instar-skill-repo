"""
repo_land — land a ticket on testing or staging.

ONE TRACK (fleet triage 2026-11-25, D15): `land` merges the ticket's OWN branch (land_merge) whenever
the repo's policy allows it and drift is 0. The cherry-pick path below (land_own_commits) is the
fallback for drift above 0 or a repo on safest settings.

The original module note, for the fallback:
repo_land — cut an environment's LANDING branch and cherry-pick a ticket's commits onto it.

WHY. CLC-4615, 2026-11-16: the staging delivery needed bot/CLC-4615-staging, cut from staging
with only the ticket's four commits. The dev in that repo has file tools only, so it could not
cherry-pick, and rightly refused to retype a 2,700-line loader. The coordinator opened the PR
from the testing landing branch instead (all of testing came with it: 529 files), then from the
production-based bot branch (279 files). A person cut the branch by hand. This is that work, as
an operation.

WHAT IT DOES, in a scratch worktree so the caller's checkout is never touched:
  1. fetch every branch, cut a detached HEAD at origin/<env>;
  2. cherry-pick -x each commit in the order given. One already on <env> (empty pick) is
     skipped and said so;
  3. on a conflict, a file is resolved ONLY when it can be proven exact: <env>'s copy is
     byte-identical to <verified>'s copy just before the ticket's first commit to that file
     there. Then <verified>'s current copy is precisely <env> + this ticket's changes, already
     verified. Anything else stops, aborts, and returns both sides for a person or the dev;
  4. compare every changed file with <verified> (SAME / DIFFERS), point bot/<TICKET>-<env> at
     the result and push it with lease.
It builds nothing: the result is reported as unbuilt, and the build is owed before a merge.
"""

import os
import re
import shutil
import tempfile
import uuid

from .base import git_env, git_identity, git_run

SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
ENVS = ("testing", "staging")
MAX_COMMITS = 30


def _git(wt: str, *args, env=None, timeout=180):
    return git_run(["git", "-C", wt, *args], env=env or git_env(), timeout=timeout)


def _blob(wt: str, rev_path: str) -> str:
    r = _git(wt, "rev-parse", "--verify", "--quiet", rev_path)
    return r.stdout.strip() if r.returncode == 0 else ""


def _provably_exact(wt: str, path: str, verified: str, ticket: str) -> tuple:
    """(True, why) when taking origin/<verified>:path reproduces <env> + the ticket's change."""
    log = _git(wt, "log", "--no-merges", "--format=%H", f"--grep={ticket}",
               f"origin/{verified}", "--", path)
    shas = log.stdout.split()
    if not shas:
        return False, f"no {ticket} commit touches {path} on {verified}"
    first = shas[-1]
    before = _blob(wt, f"{first}^:{path}")
    ours = _blob(wt, f"HEAD:{path}")
    if ours and ours == before:
        return True, (f"{path}: the target's copy equals {verified}'s copy before {first[:8]}, "
                      f"so {verified}'s current copy is exactly the target plus this ticket")
    return False, (f"{path}: the target's copy differs from {verified}'s copy before "
                   f"{first[:8]}, so {verified}'s version would carry other work")


def _ticket_gaps(wt: str, ticket: str, env: str, verified: str, commits: list) -> dict:
    """What of the ticket's VERIFIED work the landing would leave out. {} when nothing.

    Two silent omissions this catches (fleet deficiencies #85 and #89):
    - CLC-4665, 2026-11-19: the coordinator passed only the rework commit c04c75f3, not the
      first commit 9578dca6 it sits on, so the pick conflicted for a reason nobody could see.
    - CLC-4729, 2026-11-19: testing's copy of the ticket included AkexpeditionscruisesMapRule.cs,
      added by a hand resolution in a commit outside the unit's list. land resolved Program.cs
      from testing (which drops the rules line) and pushed a branch with no rule file, so
      relink-maps on staging would have skipped A&K.
    A commit counts as already on <env> when <env> has a commit with the same subject.
    """
    ours = {c[:7] for c in commits}
    on_verified = _git(wt, "log", "--no-merges", "--format=%H%x09%s", f"--grep={ticket}",
                       f"origin/{verified}").stdout.splitlines()
    on_env = set(_git(wt, "log", "--no-merges", "--format=%s", f"--grep={ticket}",
                      f"origin/{env}").stdout.splitlines())
    picked = set(_git(wt, "log", "--no-merges", "--format=%s", f"--grep={ticket}",
                      "HEAD").stdout.splitlines())
    missing_commits, ticket_files = [], set()
    for line in on_verified:
        sha, _, subj = line.partition("\t")
        ticket_files.update(_git(wt, "show", "--name-only", "--format=", sha).stdout.split())
        if sha[:7] in ours or subj in on_env or subj in picked:
            continue
        missing_commits.append({"sha": sha[:12], "subject": subj[:120]})
    missing_files = []
    for f in sorted(ticket_files):
        want = _blob(wt, f"origin/{verified}:{f}")
        have = _blob(wt, f"HEAD:{f}")
        if want and not have:
            missing_files.append({"path": f, "state": f"on {verified}, absent from the landing"})
    out = {}
    if missing_commits:
        out["missing_commits"] = missing_commits
    if missing_files:
        out["missing_files"] = missing_files
    return out


def _push_landing(path: str, auth: str, landing: str):
    """Push a landing branch with an EXPLICIT lease: the remote must still be at what we fetched
    (or must not exist yet). A bare --force-with-lease compares against a remote-tracking ref,
    and a push to a URL (how this skill pushes, with its token) has none, so once the branch
    existed every re-land failed with "(stale info)": CLC-4620, 2026-11-24, three attempts."""
    seen = _blob(path, f"refs/remotes/origin/{landing}")
    return git_run(["git", "-C", path, "push", f"--force-with-lease={landing}:{seen}", auth,
                    f"{landing}:{landing}"], timeout=300)


def _conflict_report(wt: str, paths: list) -> list:
    out = []
    for p in paths:
        d = _git(wt, "diff", "--", p).stdout
        out.append({"path": p, "conflict": d[:3000] + ("\n…(cut)" if len(d) > 3000 else "")})
    return out


async def land_own_commits(path: str, auth: str, scrub, env: str, ticket: str, commits: list,
                           verified: str, profile_slug: str) -> tuple:
    """(ok, payload). The OWN-COMMITS fallback (One Track): used only when the repo's policy
    says own_commits, or `auto` measured drift above 0 (or could not measure it). The caller
    has already checked access and that bot/<ticket>-<env> may be pushed."""
    if env not in ENVS:
        return False, f"land targets testing or staging, not {env!r}"
    commits = [c.strip().lower() for c in (commits or []) if c and c.strip()]
    if not commits or len(commits) > MAX_COMMITS or not all(SHA_RE.match(c) for c in commits):
        return False, (f"land needs 1-{MAX_COMMITS} commit shas, in the order to apply them "
                       "(7-40 hex characters each)")
    verified = (verified or ("testing" if env == "staging" else "")).strip()
    if verified and verified not in ENVS + ("production",):
        return False, f"verified must be an environment branch, not {verified!r}"
    landing = f"bot/{ticket}-{env}"

    r = git_run(["git", "-C", path, "fetch", auth, "+refs/heads/*:refs/remotes/origin/*"],
                timeout=300)
    if r.returncode != 0:
        return False, f"fetch failed: {scrub(r.stderr)[-400:]}"
    for c in commits:
        if not _blob(path, f"{c}^{{commit}}"):
            return False, f"commit {c} is not in this repository (fetched every branch)"

    # A path git creates itself, NOT tempfile.mkdtemp: git_run drops root, and mkdtemp's 0700
    # root-owned directory made every land fail with "'/tmp/land_…' already exists"
    # (CLC-4644, 2026-11-16, the first land ever run by the coordinator).
    wt = os.path.join(tempfile.gettempdir(), f"land_{uuid.uuid4().hex[:12]}")
    ident = git_env(git_identity(profile_slug))
    picks, resolved = [], []
    try:
        r = git_run(["git", "-C", path, "worktree", "add", "--detach", wt, f"origin/{env}"])
        if r.returncode != 0:
            return False, f"could not cut from origin/{env}: {scrub(r.stderr)[-300:]}"
        base = _git(wt, "rev-parse", "HEAD").stdout.strip()
        for c in commits:
            r = _git(wt, "cherry-pick", "-x", c, env=ident)
            if r.returncode == 0:
                picks.append({"sha": c, "result": "clean"})
                continue
            if "empty" in (r.stdout + r.stderr) or "allow-empty" in (r.stdout + r.stderr):
                _git(wt, "cherry-pick", "--skip", env=ident)
                picks.append({"sha": c, "result": f"already on {env} (empty), skipped"})
                continue
            paths = _git(wt, "diff", "--name-only", "--diff-filter=U").stdout.split()
            proofs = [(p, *_provably_exact(wt, p, verified, ticket)) for p in paths] \
                if verified else [(p, False, "no verified environment named") for p in paths]
            if paths and all(ok for _, ok, _ in proofs):
                for p, _, why in proofs:
                    with open(os.path.join(wt, p), "w", encoding="utf-8", newline="") as fh:
                        fh.write(_git(wt, "show", f"origin/{verified}:{p}").stdout)
                    _git(wt, "add", "--", p)
                    resolved.append(why)
                r2 = _git(wt, "-c", "core.editor=true", "cherry-pick", "--continue", env=ident)
                if r2.returncode != 0:
                    _git(wt, "cherry-pick", "--abort")
                    return False, f"{c}: continue after resolving failed: {scrub(r2.stderr)[-300:]}"
                picks.append({"sha": c, "result": f"conflict resolved from {verified}"})
                continue
            report = _conflict_report(wt, paths)
            _git(wt, "cherry-pick", "--abort")
            return False, {
                "error": f"{c} conflicts with {env} and the conflict is not provably safe to "
                         "resolve automatically. Nothing was pushed.",
                "applied_before_it": picks, "why_not_automatic": [w for _, _, w in proofs],
                "conflicts": report,
                "next": ("A person or the dev resolves this hunk. The commits before it "
                         "applied clean; re-run land with all the commits after resolving."),
            }

        head = _git(wt, "rev-parse", "HEAD").stdout.strip()
        gaps = _ticket_gaps(wt, ticket, env, verified, commits) if verified else {}
        if gaps:
            return False, {
                "error": (f"the landing would leave out {ticket} work that is on {verified}. "
                          "Nothing was pushed."),
                **gaps,
                "next": ("Add the missing commits to `commits` (in order) and re-run land. A file "
                         "listed under missing_files with no missing commit was added on "
                         f"{verified} by a hand resolution: land cannot carry it, so ask the "
                         "person who resolved it (often a <Line>MapRule.cs next to a loader)."),
            }
        files = _git(wt, "diff", "--name-only", base, head).stdout.split()
        compare = []
        for f in files:
            same = verified and _blob(wt, f"HEAD:{f}") == _blob(wt, f"origin/{verified}:{f}")
            compare.append({"path": f, f"vs_{verified or 'verified'}": "SAME" if same else "DIFFERS"})
        r = _git(wt, "branch", "-f", landing, head)
        if r.returncode != 0:
            return False, f"could not point {landing} at the result: {scrub(r.stderr)[-300:]}"
    finally:
        git_run(["git", "-C", path, "worktree", "remove", "--force", wt])
        shutil.rmtree(wt, ignore_errors=True)

    r = _push_landing(path, auth, landing)
    if r.returncode != 0:
        return False, f"push of {landing} failed: {scrub((r.stderr or '') + (r.stdout or ''))[-400:]}"
    return True, {
        "op": "land", "branch": landing, "head": head[:12], "cut_from": f"origin/{env} @ {base[:12]}",
        "picks": picks, "resolved": resolved, "files": compare,
        "built": False,
        "next": (f"Pushed {landing}. Nothing was built here: the dev (or repo_verify) builds it "
                 f"before the PR merges. Open the PR with op=pr ref={landing} base={env}."),
    }


# ================================================================ One Track: land by MERGE
# (fleet triage 2026-11-25; design https://claude.ai/artifact/UxkPf9KreaSy8odKJtaRoD, R2/R6, D15)
#
# The ticket's OWN commits go to every environment: a clean merge means the PR is opened from
# bot/<TICKET> itself. A conflict is resolved only by an approved recipe for that EXACT conflict
# (repo_conflict_recipes), applied in a merge on bot/<TICKET>-<env>, a branch that only ever
# holds that merge. In a shared repo, a hunk last changed by anyone outside the fleet is never
# resolved here: it stops and names them.

FLEET_TRAILER = "Fleet-Unit:"
TICKET_KEY_RE = re.compile(r"\b[A-Z]{2,10}-\d+\b")


def _policy_value(repo: str, key: str, default: str = "") -> str:
    import json                                                         # noqa: PLC0415
    from .base import POLICY_DIR                                        # noqa: PLC0415
    try:
        with open(os.path.join(POLICY_DIR, repo.split("/")[-1] + ".json"), encoding="utf-8") as fh:
            return str((json.load(fh) or {}).get(key) or default)
    except Exception:                                                   # noqa: BLE001
        return default


def drift(path: str, env: str, prod: str) -> int:
    """Non-merge commits on origin/<prod> that origin/<env> lacks; -1 when it cannot be measured.
    Merge commits carry no code of their own, so GitHub's merge bubbles do not count."""
    r = _git(path, "rev-list", "--count", "--no-merges", f"origin/{env}..origin/{prod}")
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else -1


def _foreign_commits(path: str, head: str, prod: str, ticket: str) -> list:
    """Commits on the ticket branch that are not the ticket's (R1). A commit is the ticket's when
    its subject names the ticket or it carries the gate's Fleet-Unit trailer. A merge is allowed
    only as a base update: merging production into the branch (its other parent is on prod)."""
    out = []
    for line in _git(path, "log", "--format=%H%x09%P%x09%s%x09%b%x1e",
                     f"origin/{prod}..{head}").stdout.split("\x1e"):
        line = line.strip("\n")
        if not line.strip():
            continue
        sha, parents, subject, body = (line.split("\t", 3) + ["", "", ""])[:4]
        ps = parents.split()
        if len(ps) > 1:
            if not all(_git(path, "merge-base", "--is-ancestor", p, f"origin/{prod}").returncode == 0
                       or _git(path, "merge-base", "--is-ancestor", p, head).returncode == 0
                       for p in ps[1:]):
                out.append({"sha": sha[:12], "subject": subject[:100],
                            "why": "a merge that brings in something other than production"})
            continue
        if ticket in subject.upper() or FLEET_TRAILER in body:
            continue
        out.append({"sha": sha[:12], "subject": subject[:100],
                    "why": f"neither names {ticket} nor carries the {FLEET_TRAILER} trailer"})
    return out


def _stage_blobs(wt: str, p: str) -> dict:
    """{base, ours, theirs} blob ids of an unmerged path ('' where a side is absent)."""
    blobs = {"1": "", "2": "", "3": ""}
    for line in _git(wt, "ls-files", "-u", "--", p).stdout.splitlines():
        meta, _, _ = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3:
            blobs[parts[2]] = parts[1]
    return {"base": blobs["1"], "ours": blobs["2"], "theirs": blobs["3"]}


def _who_changed(wt: str, env: str, prod: str, p: str) -> tuple:
    """(tickets, outside_authors, all_authors) for unshipped env commits touching `p`."""
    tickets, outside, authors = set(), set(), set()
    for rec in _git(wt, "log", "--no-merges", "--format=%an%x09%s%x09%b%x1e",
                    f"origin/{prod}..origin/{env}", "--", p).stdout.split("\x1e"):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        author, subject, body = (rec.split("\t", 2) + ["", ""])[:3]
        authors.add(author)
        tickets.update(TICKET_KEY_RE.findall(subject.upper()))
        if FLEET_TRAILER not in body:
            outside.add(author)
    return sorted(tickets), outside, sorted(authors)


async def _recipe(conn, repo, ticket, env, p, blobs, conflict_text, tickets, authors) -> dict:
    """The recipe row for this exact conflict, recorded if new. Never resolves anything itself."""
    row = await conn.fetchrow(
        """SELECT id, state, resolved, decided_by FROM repo_conflict_recipes
            WHERE repo = $1 AND path = $2 AND base_blob = $3 AND ours_blob = $4 AND theirs_blob = $5""",
        repo, p, blobs["base"], blobs["ours"], blobs["theirs"])
    if row:
        return dict(row)
    row = await conn.fetchrow(
        """INSERT INTO repo_conflict_recipes
               (repo, path, ticket, env, base_blob, ours_blob, theirs_blob, conflict,
                other_tickets, other_authors)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
           ON CONFLICT (repo, path, base_blob, ours_blob, theirs_blob) DO UPDATE SET env = EXCLUDED.env
           RETURNING id, state, resolved, decided_by""",
        repo, p, ticket, env, blobs["base"], blobs["ours"], blobs["theirs"], conflict_text[:20000],
        tickets, authors)
    return dict(row)


async def land_merge(path: str, auth: str, scrub, env: str, ticket: str, repo: str, prod: str,
                     policy: dict, profile_slug: str) -> tuple:
    """(ok, payload). Lands bot/<ticket> on <env> by merge; see the block comment above."""
    from .base import path_in_fence                                     # noqa: PLC0415
    head = f"origin/bot/{ticket}"
    if not _blob(path, f"{head}^{{commit}}"):
        return False, f"bot/{ticket} is not on the remote: the dev pushes the branch first"

    foreign = _foreign_commits(path, head, prod, ticket)
    if foreign:
        return False, {"error": f"bot/{ticket} carries commits that are not {ticket}'s; nothing "
                                "was landed (One Track R1: a ticket branch holds only its ticket).",
                       "foreign_commits": foreign,
                       "next": "The dev removes them from the branch, or a person decides."}
    ticket_files = _git(path, "diff", "--name-only", f"origin/{prod}...{head}").stdout.split()
    fenced = [f for f in ticket_files if not path_in_fence(f, policy.get("fleet_paths") or [])]
    if fenced:
        return False, {"error": "files outside this repo's fleet_paths; nothing was landed.",
                       "outside_fence": fenced, "fleet_paths": policy.get("fleet_paths")}

    wt = os.path.join(tempfile.gettempdir(), f"land_{uuid.uuid4().hex[:12]}")
    ident = git_env(git_identity(profile_slug))
    applied, open_conflicts, foreign_hunks = [], [], []
    try:
        r = git_run(["git", "-C", path, "worktree", "add", "--detach", wt, f"origin/{env}"])
        if r.returncode != 0:
            return False, f"could not cut from origin/{env}: {scrub(r.stderr)[-300:]}"
        base = _git(wt, "rev-parse", "HEAD").stdout.strip()
        tip = _git(wt, "rev-parse", head).stdout.strip()
        m = _git(wt, "merge", "--no-ff", "--no-commit", head, env=ident)
        files = _git(wt, "diff", "--name-only", f"origin/{env}...{head}").stdout.split()
        if m.returncode == 0:
            _git(wt, "merge", "--abort")
            return True, {"op": "land", "mode": "merge", "pr_head": f"bot/{ticket}", "base": env,
                          "head_sha": tip[:12], "env_sha": base[:12], "files": files,
                          "recipes_applied": [], "built": "attested on the branch; the merge "
                          "is clean, so the deployed tree is env + exactly this branch"}

        paths = _git(wt, "diff", "--name-only", "--diff-filter=U").stdout.split()
        from ...db import get_pool                                      # noqa: PLC0415
        pool = get_pool()
        if not pool:
            _git(wt, "merge", "--abort")
            return False, "conflicts found and the recipe store is unavailable; nothing landed"
        async with pool.acquire() as conn:
            for p in paths:
                blobs = _stage_blobs(wt, p)
                tickets, outside, authors = _who_changed(wt, env, prod, p)
                owners = set(policy.get("owners") or [])
                if policy.get("shared") and (outside - owners):
                    foreign_hunks.append({"path": p, "changed_by": sorted(outside - owners),
                                          "tickets": tickets})
                    continue
                text = open(os.path.join(wt, p), encoding="utf-8", errors="replace").read() \
                    if os.path.exists(os.path.join(wt, p)) else ""
                rec = await _recipe(conn, repo, ticket, env, p, blobs, text, tickets, authors)
                if rec["state"] == "approved":
                    with open(os.path.join(wt, p), "w", encoding="utf-8", newline="") as fh:
                        fh.write(rec["resolved"])
                    _git(wt, "add", "--", p)
                    applied.append({"path": p, "recipe": str(rec["id"]),
                                    "approved_by": rec["decided_by"]})
                else:
                    open_conflicts.append({"path": p, "recipe": str(rec["id"]),
                                           "state": rec["state"], "other_tickets": tickets,
                                           "changed_by": authors})
            if foreign_hunks or open_conflicts:
                _git(wt, "merge", "--abort")
                return False, {
                    "error": f"bot/{ticket} conflicts with {env}; nothing was landed.",
                    "foreign_hunks": foreign_hunks, "needs_recipe": open_conflicts,
                    "recipes_applied_before_stopping": applied,
                    "next": ("foreign_hunks: another developer's change. A person decides; no "
                             "recipe may resolve it. needs_recipe: propose the resolved file "
                             "with repo_recipe op=propose recipe_id=<id>; a person approves it, "
                             "then land again.")}
            msg = (f"Land {ticket} on {env} (merge of bot/{ticket})\n\n"
                   + "\n".join(f"Recipe {a['recipe']} for {a['path']} (approved by "
                               f"{a['approved_by']})" for a in applied))
            c = _git(wt, "commit", "--no-edit", "-m", msg, env=ident)
            if c.returncode != 0:
                _git(wt, "merge", "--abort")
                return False, f"merge commit failed: {scrub(c.stderr)[-300:]}"
            for a in applied:
                await conn.execute("UPDATE repo_conflict_recipes SET used_count = used_count + 1, "
                                   "last_used_at = now() WHERE id = $1::uuid", a["recipe"])
        landing = f"bot/{ticket}-{env}"
        merged = _git(wt, "rev-parse", "HEAD").stdout.strip()
        r = _git(wt, "branch", "-f", landing, merged)
        if r.returncode != 0:
            return False, f"could not point {landing} at the merge: {scrub(r.stderr)[-300:]}"
    finally:
        git_run(["git", "-C", path, "worktree", "remove", "--force", wt])
        shutil.rmtree(wt, ignore_errors=True)
    # A landing branch holds only env + this merge and is rebuilt on every land, so it is
    # pushed with lease (the gate's one force-pushable bot ref).
    r = _push_landing(path, auth, landing)
    if r.returncode != 0:
        return False, f"push of {landing} failed: {scrub((r.stderr or '') + (r.stdout or ''))[-400:]}"
    return True, {"op": "land", "mode": "merge+recipes", "pr_head": landing, "base": env,
                  "head_sha": tip[:12], "env_sha": base[:12], "merge_sha": merged[:12],
                  "files": files, "recipes_applied": applied}


async def land(path: str, auth: str, scrub, env: str, ticket: str, commits: list,
               verified: str, profile_slug: str, repo: str = "") -> tuple:
    """(ok, payload). One Track dispatcher: merge when the policy allows and drift is 0,
    otherwise the own-commits fallback. The payload always says which and why (auditable)."""
    from .base import branching_policy                                   # noqa: PLC0415
    if env not in ENVS:
        return False, f"land targets testing or staging, not {env!r}"
    policy, problems = branching_policy(repo) if repo else (None, ["no repo named"])
    prod = _policy_value(repo, "prod_branch", "production") if repo else "production"
    r = git_run(["git", "-C", path, "fetch", auth, "+refs/heads/*:refs/remotes/origin/*"],
                timeout=300)
    if r.returncode != 0:
        return False, f"fetch failed: {scrub(r.stderr)[-400:]}"
    mode = (policy or {}).get("landing", "own_commits")
    d = drift(path, env, prod)
    if mode == "auto":
        mode = "merge" if d == 0 else "own_commits"
    why = {"policy": {k: v for k, v in (policy or {}).items() if k != "owners"},
           "policy_problems": problems, "drift": d, "prod_branch": prod, "chosen": mode}
    if mode == "merge":
        ok, payload = await land_merge(path, auth, scrub, env, ticket, repo, prod,
                                       policy or {}, profile_slug)
    else:
        if not commits:
            commits = _git(path, "rev-list", "--reverse", "--no-merges",
                           f"origin/{prod}..origin/bot/{ticket}").stdout.split()
        ok, payload = await land_own_commits(path, auth, scrub, env, ticket, commits, verified,
                                             profile_slug)
    if isinstance(payload, dict):
        payload["landing_decision"] = why
    else:
        payload = {"error": payload, "landing_decision": why}
    return ok, payload

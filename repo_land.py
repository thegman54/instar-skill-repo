"""
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


def _conflict_report(wt: str, paths: list) -> list:
    out = []
    for p in paths:
        d = _git(wt, "diff", "--", p).stdout
        out.append({"path": p, "conflict": d[:3000] + ("\n…(cut)" if len(d) > 3000 else "")})
    return out


async def land(path: str, auth: str, scrub, env: str, ticket: str, commits: list,
               verified: str, profile_slug: str) -> tuple:
    """(ok, payload). The caller has already checked access and that bot/<ticket>-<env> may
    be pushed."""
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

    r = git_run(["git", "-C", path, "push", "--force-with-lease", auth, f"{landing}:{landing}"],
                timeout=300)
    if r.returncode != 0:
        return False, f"push of {landing} failed: {scrub((r.stderr or '') + (r.stdout or ''))[-400:]}"
    return True, {
        "op": "land", "branch": landing, "head": head[:12], "cut_from": f"origin/{env} @ {base[:12]}",
        "picks": picks, "resolved": resolved, "files": compare,
        "built": False,
        "next": (f"Pushed {landing}. Nothing was built here: the dev (or repo_verify) builds it "
                 f"before the PR merges. Open the PR with op=pr ref={landing} base={env}."),
    }

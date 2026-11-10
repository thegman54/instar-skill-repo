"""
repo_git — every remote git operation, performed where the credential lives.

WHY THIS EXISTS, stated plainly because the previous design read as safe and was not.

git-gate is a script inside the expert's own container, and the expert had `GH_TOKEN` in its
environment plus a global credential helper feeding it to any git command. So `git-gate push`
enforced a careful set of rules that `git push` ignored entirely. Proven by dry-run: an expert
could push an arbitrary ref, and could push `production` directly. git-gate's own header claims
"the expert's shell denies git push" — it did not. The rules were advice.

A rule enforced by the caller is not a rule. So the credential moves here, to a container the
expert cannot reach into, and the ref check runs in the same process that holds the token.
There is no ordering in which the check can be skipped and the push still happen, because they
are the same call.

THE MODEL IS THE COORDINATOR'S, which has always been right: no token, no git binary, no repo
mount — it acts only through tools that execute somewhere else. Applied to experts, that means
local git loses its remote and every network operation becomes a call. The expert keeps a shell
for builds (npm, npx, go — no policy's verify command uses git) and a read-only rootfs it
cannot install a credential helper back into.

WHAT IS ENFORCED, and nothing else is possible:

    fetch            always allowed — reading cannot damage anything
    push bot/<T>     only with an OPEN work unit for that repo and ticket
    push release/<K> only for a repo that is a member of that release group
    push <env>       never here. Promotion goes through the fleet's approval, failing closed
    delete           never, at all
    force            only release/*, only with-lease, because it is rebuilt from base by design

The ticket in a bot branch must match a real work unit. That is the check that makes the
namespace mean something: without it `bot/` is a naming convention an expert can type anything
into, which is exactly as enforcing as the old arrangement.

ONE IMPLEMENTATION, TWO CALLERS. The MCP tool and the HTTP route git-gate calls both go through
`remote_op`. A security rule that exists twice is one that will eventually exist in two
versions, and the copy nobody is reading is the one that stays permissive.
"""

import asyncio
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import git_run, check_repo_access, git_env, sanitize_stderr

log = structlog.get_logger()

BOT_PREFIX = "bot/"
RELEASE_PREFIX = "release/"

# Never pushable here under any argument. Listed by name rather than read from a policy file:
# policy is data, and data the caller can edit is not a guard.
NEVER = {"main", "master", "production", "production-v2", "testing", "staging",
         "develop", "HEAD"}

TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

FLEET_API = os.environ.get("FLEET_API", "http://gatekeeper:8090")


async def _units(repo_slug: str, ticket: str) -> tuple:
    """Open work units for this repo and ticket, and the branch the coordinator named.

    Returns (count, named_branch). count is -1 for "could not ask", which the caller must
    fail closed on; named_branch is "" for a unit raised before branches were named.

    NOT over the fleet's HTTP API, which is how this was written first and why it returned
    502. The call arrives here via the gatekeeper's proxy, so asking the gatekeeper a question
    mid-request makes the chain gatekeeper -> tool-executor -> gatekeeper: re-entrant, and it
    deadlocks until something times out. The tables are in the same Postgres either way, so
    the hop bought nothing but a cycle.
    """
    from ...db import get_pool
    pool = get_pool()
    if not pool:
        return -1, ""      # unknown, not zero — the caller must fail closed on this
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT COALESCE(branch, '') AS branch FROM fleet_work_units
                WHERE upper(parent_key) = upper($1) AND repo_slug = $2
                  AND state <> ALL($3::text[])
                ORDER BY created_at""",
            ticket, repo_slug, ["done", "cancelled", "declined"])
    # The named branch comes back with the count because the two answers are one question:
    # "is this ref the work that was assigned". A count alone let any bot/<TICKET> spelling
    # through, which is how a manual that said feat/ went unnoticed for so long.
    named = next((r["branch"] for r in rows if r["branch"]), "")
    return len(rows), named


async def _release_member(repo_slug: str, group_key: str) -> int:
    """1 if this repo is a member of an open group, 0 if not, -1 if unknowable."""
    from ...db import get_pool
    pool = get_pool()
    if not pool:
        return -1
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """SELECT count(*) FROM fleet_release_groups g
                 JOIN fleet_release_members m ON m.group_id = g.id
                WHERE g.group_key = $1 AND m.repo_slug = $2
                  AND g.state NOT IN ('promoted', 'abandoned')""",
            group_key, repo_slug)


async def _eligible(repo_slug: str) -> str:
    """Whether the fleet may write to this repo at all: yes | off | unregistered | unknown.

    FOUR ANSWERS, NOT TWO, because the refusals are not the same refusal and a person reads
    them to decide what to do. "off" is a decision someone made and can revisit on the
    registry row; "unregistered" means the fleet has never been told this repository exists,
    which is usually a wrong remote rather than a policy to change; "unknown" means the guard
    could not ask. Collapsing them sent somebody looking for a flag to flip on a row that
    was not there.

    WHY THIS IS HERE AND WAS NOT. `fleet_eligible` is the flag that answers "may the fleet
    touch this repository", and `fleet_unit.py` has always described it as bounding what an
    expert can PUSH. It did not: nothing on any push path read it. The only reader was the
    PROMOTION check, so an ineligible repo refused the pull request and accepted the branch —
    and a `bot/<TICKET>` push was authorised on one condition, that an open work unit exists
    for the ticket and repo. Units are created by routing, which is a separate decision from
    eligibility, so two units on an ineligible repo were enough to make it pushable.

    Found 2026-11-08, with two open units sitting on `cenora-core-data-services` and
    `cenora-agency-links-api` — both marked ineligible precisely so the fleet would stay off
    them. The branch push would have gone through.

    A MISSING ROW IS A REFUSAL, not a default. A repo with no registry row is a repo the
    fleet was never told about, and the one thing a guard must not do is treat "I have never
    heard of this" as permission.
    """
    from ...db import get_pool
    pool = get_pool()
    if not pool:
        return "unknown"
    async with pool.acquire() as conn:
        # to_regclass first: the repo skill can be installed without the fleet skill, and a
        # failed statement would abort the surrounding transaction and surface as something
        # unrelated. Absent table is unknowable, not permitted.
        if not await conn.fetchval("SELECT to_regclass('fleet_registry') IS NOT NULL"):
            return "unknown"
        row = await conn.fetchrow(
            "SELECT fleet_eligible FROM fleet_registry WHERE repo_slug = $1", repo_slug)
    if row is None:
        return "unregistered"
    return "yes" if row["fleet_eligible"] else "off"


async def _registered_expert(repo_slug: str) -> str:
    """Which profile is the registered expert for this repo, or "" if it cannot be read.

    Read from the registry, like every other rule here, so it is not something the caller
    can assert about itself.
    """
    from ...db import get_pool
    pool = get_pool()
    if not pool:
        return ""
    try:
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT to_regclass('fleet_registry') IS NOT NULL"):
                return ""
            return await conn.fetchval(
                "SELECT profile_slug FROM fleet_registry WHERE repo_slug = $1",
                repo_slug) or ""
    except Exception as exc:                                            # noqa: BLE001
        log.warning("registered_expert_lookup_failed", repo=repo_slug, error=str(exc)[:160])
        return ""


async def may_push(repo: str, ref: str) -> tuple:
    """Whether this ref may be published, and why either way.

    Checked against the FLEET, never against anything on disk: a local file is something the
    caller can write, and a guard the guarded party can edit is not a guard.
    """
    repo_slug = repo.split("/")[-1]

    # BEFORE the namespace rules, because eligibility is not about the ref. A repo the fleet
    # is not allowed to write to is not allowed a bot branch OR a release branch, and asking
    # "is this ref well formed" first would answer a narrower question than the one the flag
    # settles. Fails closed, the same rule every other check here follows.
    ok = await _eligible(repo_slug)
    if ok == "unknown":
        return False, ("cannot read the fleet registry to confirm "
                       f"{repo_slug} may be written to. Refusing rather than assuming.")
    if ok == "unregistered":
        return False, (f"{repo_slug} has no fleet registry row, so the fleet has never been "
                       "told this repository exists and nothing is pushed to it. Check the "
                       "remote first — a typo or the wrong owner looks exactly like this. "
                       "If the repository is real and the fleet should work it, a person "
                       "adds the registry row.")
    if ok == "off":
        return False, (f"{repo_slug} is not fleet-eligible, so nothing is pushed to it — not "
                       "a bot branch, not a release branch. An open work unit does not "
                       "override this: routing decides what work exists, eligibility decides "
                       "which repositories the fleet may write to at all, and they are "
                       "separate decisions by design. A person flips fleet_eligible on the "
                       "registry row; do not look for another way round it.")

    if ref.startswith(BOT_PREFIX):
        ticket = ref[len(BOT_PREFIX):]
        if not TICKET_RE.match(ticket):
            return False, (f"{ref!r} is in the bot namespace but {ticket!r} is not a ticket "
                           "key. Branches are named bot/<TICKET>, e.g. bot/CLC-4156.")
        n, named = await _units(repo_slug, ticket)
        if n < 0:
            # Fails CLOSED, the same rule the promotion check follows: a gate that cannot
            # confirm it is allowed is not allowed.
            return False, ("cannot reach the database to confirm a work unit exists for "
                           f"{ticket}. Refusing rather than assuming.")
        if n == 0:
            return False, (f"no open work unit for {ticket} in {repo_slug}, so {ref} is not "
                           "work the coordinator asked for. Branches carry assigned work; one "
                           "matching no unit is untraceable by design.")
        # The unit NAMES its branch, so the ref is checked against that name rather than
        # merely against the ticket. Without this, every spelling under bot/<TICKET> passed
        # the same check and the expert was trusted to derive a name it had no reliable
        # source for — which is exactly how a repo manual saying `feat/` stayed invisible.
        # A unit raised before branches were named carries no name; those still pass on the
        # ticket alone rather than becoming unpushable.
        if named and ref != named:
            return False, (f"this unit's branch is {named!r}, not {ref!r}. The coordinator "
                           "names the branch when it raises the work; use the name it gave "
                           "rather than composing one, and if that name looks wrong say so "
                           "instead of pushing a different ref.")
        return True, (f"open work unit for {ticket} in {repo_slug}"
                      + (f", branch named {named}" if named else ""))

    if ref.startswith(RELEASE_PREFIX):
        group_key = ref[len(RELEASE_PREFIX):]
        n = await _release_member(repo_slug, group_key)
        if n < 0:
            return False, ("cannot reach the database to confirm release group "
                           f"{group_key}. Refusing rather than assuming.")
        if n == 0:
            return False, (f"{repo_slug} is not a member of an open release group "
                           f"{group_key!r}, so {ref} composes nothing for it.")
        return True, f"{repo_slug} is a member of release group {group_key}"

    return False, (f"{ref} is outside every namespace this can publish. Expert work is "
                   f"{BOT_PREFIX}<TICKET>; a composed release is {RELEASE_PREFIX}<KEY>. "
                   "Nothing else is pushable, including branches that merely look similar.")



async def _record(repo: str, op: str, ref: str, decision: str, reason: str,
                  profile_slug: str = "") -> None:
    """Write the decision down. Never raises.

    Best-effort on purpose, and in the opposite direction to the checks: a check that cannot
    confirm it is allowed refuses, but a RECORD that cannot be written must not turn a
    legitimate push into a failure. Losing a row costs visibility; refusing here would cost
    work that was properly authorised.
    """
    try:
        from ...db import get_pool
        pool = get_pool()
        if not pool:
            return
        ticket = ""
        if ref.startswith(BOT_PREFIX) and TICKET_RE.match(ref[len(BOT_PREFIX):]):
            ticket = ref[len(BOT_PREFIX):]
        elif ref.startswith(RELEASE_PREFIX):
            ticket = ref[len(RELEASE_PREFIX):]
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO repo_git_decisions
                       (repo, op, ref, decision, reason, profile_slug, ticket)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                repo, op, ref, decision, (reason or "")[:2000], profile_slug, ticket)
    except Exception as exc:
        log.warning("repo_git_record_failed", repo=repo, op=op, error=str(exc)[:200])


async def _report_pr(repo: str, ref: str, base: str, url: str, profile_slug: str) -> str:
    """Tell the fleet which PR this unit now holds. Returns "" or why it did not.

    WHY THIS HAS TO HAPPEN HERE. The landing poll — the only thing that moves a delivery
    rung — reads units where testing_pr/staging_pr/prod_pr is set. It does NOT discover
    pull requests; it refreshes the ones a unit already names. So a PR that is opened and
    not reported is invisible for ever: the ladder stays dark, nobody can tell whether the
    work landed, and the unit looks untouched. Measured on CLC-4555, where the branches were
    pushed and nothing could say so.

    `git-gate pr` has always reported, and its own endpoint's docstring says why it belongs
    in the same operation that created the request: "a coordinator doing it as a second tool
    call can forget the second call, and the panel would then offer a promote button beside
    a branch with no diff to read." This path was added later and never did it.

    Best effort, like report_gate: a PR that exists must not be rolled back because the
    fleet was unreachable. The failure is returned so the caller can say it out loud rather
    than let it pass as success.
    """
    ticket = ref[len(BOT_PREFIX):] if ref.startswith(BOT_PREFIX) else ""
    if not (ticket and url):
        return "no ticket in the ref, or no url" if not ticket else "the PR has no url"
    # The fleet names targets by environment, not by branch. A base that is not an
    # environment branch is a legitimate PR and simply has no rung to move.
    target = {"testing": "testing", "staging": "staging",
              "production": "prod", "prod": "prod", "master": "", "main": ""}.get(base, "")
    if not target:
        return ""
    import json as _j
    import urllib.request as _u
    body = _j.dumps({"repo_slug": repo.split("/")[-1], "parent_key": ticket,
                     "target": target, "url": url}).encode()
    req = _u.Request(FLEET_API + "/api/fleet/units/pr", data=body,
                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: _u.urlopen(req, timeout=10).read())
    except Exception as exc:                                            # noqa: BLE001
        log.warning("repo_git_pr_report_failed", repo=repo, ticket=ticket,
                    target=target, error=str(exc)[:200])
        return f"{type(exc).__name__}: {str(exc)[:160]}"
    log.info("repo_git_pr_reported", repo=repo, ticket=ticket, target=target)
    return ""


async def remote_op(repo: str, op: str, ref: str = "", profile_slug: str = "",
                    token: str = None, ticket_hint: str = "", base: str = "",
                    title: str = "", body: str = "", draft: bool = False) -> tuple:
    """Validate, perform, and RECORD one remote operation. Returns (ok, payload_or_reason).

    Recording happens here, around the whole decision, rather than at each return. A dozen
    exits each responsible for remembering to log is a design where one of them eventually
    does not, and the one that forgets is never the boring one.
    """
    ok, payload = await _decide(repo, op, ref, profile_slug, token, ticket_hint,
                                base, title, body, draft)
    if ok:
        why = payload.get("why_allowed") or f"{op} performed"
        await _record(repo, op, ref, "allowed", why, profile_slug)
    else:
        # A rule saying no and git falling over are different events with different
        # follow-ups, so they are not collapsed into one status.
        failed = any(m in str(payload) for m in ("failed:", "could not", "timed out"))
        await _record(repo, op, ref, "failed" if failed else "refused",
                      str(payload), profile_slug)
    return ok, payload


async def _decide(repo: str, op: str, ref: str = "", profile_slug: str = "",
                  token: str = None, ticket_hint: str = "", base: str = "",
                  title: str = "", body: str = "", draft: bool = False) -> tuple:
    """The decision itself, with no knowledge of how it gets recorded."""
    if op not in ("fetch", "push", "promote", "pr"):
        return False, f"unknown op {op!r} — expected fetch, push, promote or pr"

    valid, path, _access, error, _branch = await check_repo_access(
        repo, "read" if op == "fetch" else "push", profile_slug)
    if not valid:
        return False, error

    ref = (ref or "").strip()
    if ref and not REF_RE.match(ref):
        # Refused on SHAPE before the value is interpolated anywhere. A ref reaches a command
        # line; the place to stop a strange one is before it is used, not after.
        return False, f"{ref!r} is not a usable branch name"

    if not token:
        from ...credentials import CredentialFetcher
        creds = await CredentialFetcher().get_credentials("repo_git", ["GITHUB_TOKEN"])
        token = creds.get("GITHUB_TOKEN")
    if not token:
        return False, "no GITHUB_TOKEN available — refusing rather than attempting unauth'd"

    auth = f"https://x-access-token:{token}@github.com/{repo}.git"

    def scrub(text):
        return sanitize_stderr(text or "").replace(token, "***")

    if op == "fetch":
        # `git fetch <url> --all` is rejected by git: --all means "every configured remote",
        # which cannot be combined with naming one.
        #
        # AN EXPLICIT REFSPEC IS LOAD-BEARING, and its absence was a silent defect. Fetching a
        # URL rather than a named remote carries no configured refspec, so git writes
        # FETCH_HEAD and updates NO remote-tracking ref. `origin/<branch>` therefore never
        # moved, no matter how many times anything fetched: the testbed's `origin/production`
        # sat at the clone's commit while the remote was a commit ahead, and `git-gate sync`
        # read that stale ref and reported the checkout current. Everything downstream
        # inherited it — "is this commit published" compared against a ref frozen at clone
        # time, so pushed work read as unpushed.
        #
        # Forced (`+`) because these refs mirror the remote and are never edited locally;
        # a non-fast-forward there means the remote branch moved, which is the case this
        # exists to notice.
        remote_name = (git_run(["git", "-C", path, "remote"], capture_output=True,
                                      text=True, timeout=30).stdout.split() or ["origin"])[0]
        refspec = (f"+refs/heads/{ref}:refs/remotes/{remote_name}/{ref}" if ref
                   else f"+refs/heads/*:refs/remotes/{remote_name}/*")
        args = ["fetch", auth, refspec]
        r = git_run(["git", "-C", path, *args], capture_output=True, text=True,
                           timeout=180)
        if r.returncode != 0:
            return False, f"fetch failed: {scrub(r.stderr)[-500:]}"
        return True, {"repo": repo, "op": "fetch", "ref": ref or "(all)",
                      "output": scrub(r.stderr or r.stdout)[-600:]}


    # ---------------------------------------------------------------- pr
    #
    # WHY THIS IS HERE AT ALL, 2026-11-07. Creating a pull request was the one remote
    # operation that never moved. On 2026-09-23 the GitHub credential was taken out of the
    # expert-runner container and put here, in the same process as the ref check, because a
    # credential helper in that container meant these rules were advice `git push` ignored.
    # fetch, push and promote came with it; `gh pr create` stayed behind, shelling out in a
    # container that from then on had no credential and no `gh` login. It therefore failed
    # every time -- not only for `git-gate pr staging`, but for the DRAFT PR that every push
    # tries to open, which is the artifact a person is supposed to read before approving a
    # promotion. The promote button had no diff beside it, and the only trace was a warning
    # on stderr that nothing collected.
    #
    # OPENING A PR NEEDS NO FLEET APPROVAL, unlike promote. A pull request is a REQUEST for
    # review; it changes no environment and merging it is a human act performed in GitHub.
    # Requiring an approval to ask for one would invert the order the whole flow depends on:
    # the PR is the thing the approval is granted ON.
    #
    # The REST API rather than `gh`: this process holds the token and must not hand it to a
    # subprocess environment, and nothing here should depend on a CLI being installed and
    # logged in. One POST, one documented response shape.
    if op == "pr":
        if not ref:
            return False, "pr needs a head ref — name the branch to open the request from"
        base = (base or "").strip()
        if not base:
            return False, "pr needs a base — the branch the request is opened against"
        if not REF_RE.match(base):
            return False, f"{base!r} is not a usable branch name"

        # The head is held to the SAME rule as push: a pull request from a branch that is not
        # traceable work is not something this path opens, and a branch that was never
        # pushable cannot have reached the remote to be PR'd anyway.
        if ref in NEVER or ref.split("/")[-1] in NEVER:
            return False, (f"{ref} is an environment branch; a pull request is opened FROM "
                           "work and INTO an environment, not the other way round.")
        allowed, why = await may_push(repo, ref)
        if not allowed:
            return False, why

        # AN EXPERT PUSHES BRANCHES. IT DOES NOT OPEN PULL REQUESTS.
        #
        # The division is the point of having two profiles: the expert works one repository
        # and can attest its own build; the coordinator sees the ticket, the sibling units,
        # the preflight against testing and what has to be run after a deploy, and is
        # therefore the only party that can write a request a human can act on. An expert
        # opening its own PR produces exactly what it produced on 2026-11-10 — title
        # "bot/CLC-4555: bot/CLC-4555", body "Opened by the repo expert gate" — because it
        # is being asked to summarise a decision it did not make.
        #
        # Checked here rather than left to the tool belt because repo_git is ONE tool and
        # both profiles hold it: the expert needs fetch and push from the same surface.
        # Exposing the pr op on 2026-11-10 therefore handed the expert a capability the
        # belts had deliberately withheld — the coordinator carries repo_pr and repo_push,
        # the expert carries neither — and it opened two requests within minutes.
        #
        # Unknown does not pass. If the registry cannot be read, nobody is named as the
        # expert and this cannot tell whether the caller is it.
        expert = await _registered_expert(repo.split("/")[-1])
        if not expert:
            return False, ("cannot read which profile is the registered expert for "
                           f"{repo.split('/')[-1]}, so whether you are allowed to open this "
                           "request cannot be decided. Refusing rather than assuming.")
        if profile_slug and profile_slug == expert:
            return False, (
                f"{profile_slug} is the registered expert for {repo.split('/')[-1]}, and an "
                "expert pushes branches rather than opening pull requests. Push the branch "
                "and say it is ready; the coordinator opens the request, because the body "
                "has to say how to test it and what to run afterwards — which needs the "
                "ticket, the sibling units and the preflight, none of which you can see.")

        # And the base must BE an environment. This path exists to reach staging and
        # production by review; a PR between two ordinary branches is not its business and
        # would be a way to route work somewhere nobody is watching.
        if base not in NEVER:
            return False, (f"{base!r} is not an environment branch. A pull request here is "
                           "how work reaches an environment for a person to merge.")

        owner = repo.split("/")[0]
        api = f"https://api.github.com/repos/{repo}/pulls"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "instar-repo-git",
            "Content-Type": "application/json",
        }

        def _api(url, data=None, method="GET"):
            req = urllib.request.Request(
                url, data=(json.dumps(data).encode() if data is not None else None),
                headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode() or "null")

        payload = {
            "title": (title or f"{ticket_hint or ref}: {ref}")[:250],
            "head": ref,
            "base": base,
            "body": (body or "Opened by the repo expert gate. Merging is a human "
                             "decision.")[:60000],
            "draft": bool(draft),
        }
        try:
            _status, created = _api(api, payload, "POST")
            log.info("repo_git_pr", repo=repo, ref=ref, base=base,
                     number=created.get("number"), draft=bool(draft))
            reported = await _report_pr(repo, ref, base, created.get("html_url", ""),
                                        profile_slug)
            return True, {"repo": repo, "op": "pr", "ref": ref, "base": base,
                          "url": created.get("html_url", ""),
                          "number": created.get("number"), "existing": False,
                          "reported_to_fleet": not reported,
                          "report_error": reported,
                          "why_allowed": why}
        except urllib.error.HTTPError as err:
            detail = ""
            try:
                detail = err.read().decode()[:600]
            except Exception:                                           # noqa: BLE001
                pass
            # ALREADY OPEN IS NOT A FAILURE. Re-running the flow -- which the gate does on
            # every push -- must not break because the request it would create is already
            # there. GitHub answers 422 for that, so the existing one is looked up and
            # returned, making this idempotent rather than merely tolerant.
            if err.code == 422 and "pull request already exists" in detail.lower():
                try:
                    _s, found = _api(
                        f"{api}?state=open&base={urllib.parse.quote(base)}"
                        f"&head={urllib.parse.quote(owner + ':' + ref)}")
                    if found:
                        # Reported on the idempotent path too: the first attempt may have
                        # created the PR and failed to report it, and this is the retry.
                        reported = await _report_pr(
                            repo, ref, base, found[0].get("html_url", ""), profile_slug)
                        return True, {"repo": repo, "op": "pr", "ref": ref, "base": base,
                                      "url": found[0].get("html_url", ""),
                                      "number": found[0].get("number"), "existing": True,
                                      "reported_to_fleet": not reported,
                                      "report_error": reported,
                                      "why_allowed": "a pull request was already open"}
                except Exception as exc:                                # noqa: BLE001
                    return False, (f"a pull request from {ref} into {base} already exists, "
                                   f"and it could not be looked up ({scrub(str(exc))[:200]})")
                return True, {"repo": repo, "op": "pr", "ref": ref, "base": base,
                              "url": "", "number": None, "existing": True,
                              "why_allowed": "a pull request was already open"}
            return False, (f"opening a pull request from {ref} into {base} failed "
                           f"({err.code}): {scrub(detail)[:400]}")
        except Exception as exc:                                        # noqa: BLE001
            return False, (f"opening a pull request from {ref} into {base} failed: "
                           f"{scrub(str(exc))[:400]}")

    # ---------------------------------------------------------------- promote
    #
    # The ONE path that may push an environment branch, and it is not a hole in the NEVER list
    # — it is a different question with a different answer. `push` asks "is this a ref an
    # expert may publish"; `promote` asks "has a person approved this reaching an environment",
    # which only the fleet can answer and which fails closed when it cannot.
    #
    # The merge itself already happened locally, where it costs nothing and can be abandoned.
    # What is privileged is publishing the result.
    if op == "promote":
        env = ref or "testing"
        if env not in NEVER:
            return False, (f"{env!r} is not an environment branch; promote publishes an "
                           "environment, and ordinary refs go through push.")
        ticket = (ticket_hint or "").strip()
        if not TICKET_RE.match(ticket):
            return False, ("promote needs the ticket whose promotion was approved — without "
                           "it there is nothing to check an approval against.")
        # Asked over HTTP deliberately, unlike the checks above: "is this promotion approved"
        # is a decision the fleet composes from several tables plus the release group's order,
        # and duplicating that reasoning here is how two answers to one question appear.
        # The re-entrancy that broke the work-unit check does not apply — promote is called by
        # git-gate directly, not through the gatekeeper's proxy of this route.
        try:
            answer = json.loads(urllib.request.urlopen(
                FLEET_API + f"/api/fleet/units/{repo.split('/')[-1]}/{ticket}/promotion",
                timeout=20).read())
        except Exception as exc:
            return False, (f"could not reach the fleet to check whether promoting {ticket} is "
                           f"approved ({exc}). Refusing: a gate that cannot confirm it is "
                           "allowed is not allowed.")
        if not answer.get("allowed"):
            return False, f"promotion not approved — {answer.get('reason', 'no reason given')}"

        r = git_run(["git", "-C", path, "push", auth, f"{env}:{env}"],
                           capture_output=True, text=True, timeout=300)
        out = scrub((r.stderr or "") + (r.stdout or ""))
        if r.returncode != 0:
            return False, f"promotion push of {env} failed: {out[-600:]}"
        log.info("repo_git_promote", repo=repo, env=env, ticket=ticket)
        return True, {"repo": repo, "op": "promote", "ref": env, "ticket": ticket,
                      "why_allowed": answer.get("reason", ""), "output": out[-600:]}

    # ---------------------------------------------------------------- push
    if not ref:
        return False, "push needs a ref — name the branch to publish"

    # Both the whole ref and its last segment, so refs/heads/production and
    # bot/../../production are caught by the same rule rather than by a special case each.
    if ref in NEVER or ref.split("/")[-1] in NEVER:
        return False, (f"{ref} is an environment branch and is never pushed here. Reaching an "
                       "environment goes through the fleet's promotion approval, which a "
                       "person grants.")

    allowed, why = await may_push(repo, ref)
    if not allowed:
        return False, why

    # --force-with-lease only for release branches, which are rebuilt from base by design and
    # so are legitimately non-fast-forward. Everything else fast-forwards or fails; an expert
    # that needs history rewritten has a problem a force push would hide.
    cmd = ["push", auth, f"{ref}:{ref}"]
    if ref.startswith(RELEASE_PREFIX):
        cmd.insert(1, "--force-with-lease")

    r = git_run(["git", "-C", path, *cmd], capture_output=True, text=True,
                       timeout=300)
    out = scrub((r.stderr or "") + (r.stdout or ""))
    if r.returncode != 0:
        return False, f"push of {ref} failed: {out[-600:]}"

    log.info("repo_git_push", repo=repo, ref=ref, profile=profile_slug)
    return True, {"repo": repo, "op": "push", "ref": ref, "why_allowed": why,
                  "output": out[-600:]}


@register_tool
class RepoGitTool(BaseTool):
    """Remote git operations, with the ref check in the same process as the credential."""

    @property
    def name(self) -> str:
        return "repo_git"

    @property
    def description(self) -> str:
        return (
            "Perform a remote git operation on a repository you own: `fetch`, `push` or "
            "`pr`.\n\n"
            "This is the ONLY path to a remote. Your container has no credential, so a plain "
            "`git push` cannot authenticate — that is deliberate, not a misconfiguration.\n\n"
            "What is allowed:\n"
            "  fetch                — always\n"
            "  push bot/<TICKET>    — only if an OPEN work unit exists for this repo and ticket\n"
            "  push release/<KEY>   — only if this repo is a member of that release group\n\n"
            "Always refused: environment branches (production, testing, staging, main), "
            "deleting any ref, and force-pushing anything but a release branch.\n\n"
            "  pr                   — open a pull request from a branch you pushed into "
            "another branch. Opening is allowed; MERGING stays a human decision, and the "
            "fleet records the request so a delivery rung can observe it landing.\n\n"
            "Promotion to an environment is NOT done here. It goes through the fleet's "
            "approval, which fails closed.\n\n"
            "A refusal is an answer, not an error to route around. It names the rule that "
            "refused and what would satisfy it."
        )

    @property
    def short_description(self) -> str:
        return "Fetch, push or open a pull request, with server-side ref rules"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string",
                         "description": "owner/repo, e.g. cenora-llc/cenora-core-data-services"},
                "op": {"type": "string", "enum": ["fetch", "push", "pr"]},
                "ref": {"type": "string",
                        "description": "Branch to push, e.g. bot/CLC-4156. For fetch, the "
                                       "branch to fetch (optional; defaults to all). For "
                                       "pr, the HEAD branch the request comes from."},
                "base": {"type": "string",
                         "description": "pr only: the branch to merge INTO, e.g. testing. "
                                        "Opening the request is allowed; merging it stays a "
                                        "human decision."},
                "title": {"type": "string", "description": "pr only: the request's title."},
                "body": {"type": "string", "description": "pr only: the request's body."},
                "draft": {"type": "boolean",
                          "description": "pr only: open it as a draft. Default false."},
            },
            "required": ["repo", "op"],
        }

    def credential_keys(self) -> list[str]:
        return ["GITHUB_TOKEN"]

    async def execute(self, repo: str, op: str, ref: str = "", base: str = "",
                      title: str = "", body: str = "", draft: bool = False,
                      **kwargs) -> ToolResult:
        # `pr` WAS IMPLEMENTED HERE AND NOT EXPOSED, which cost CLC-4555 an afternoon. The
        # expert was told to open a pull request, had no way to, said so correctly, and
        # blocked. remote_op has handled pr since 2026-11-07 — the header above explains why
        # PR creation moved into the same process as the ref check — but this schema still
        # offered only fetch and push, so the capability existed and was unreachable. The
        # legacy `repo_pr` tool has its own copy with no ref rules and no PR reporting, and
        # is deliberately NOT the answer: one implementation, one place the rules live.
        ok, payload = await remote_op(repo, op, ref, self._profile_slug,
                                      token=self.get_credential("GITHUB_TOKEN"),
                                      base=base, title=title, body=body, draft=bool(draft))
        return ToolResult.ok(payload) if ok else ToolResult.fail(payload)

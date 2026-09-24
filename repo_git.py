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

import json
import os
import re
import subprocess
import urllib.request

import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, git_env, sanitize_stderr

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


async def _units(repo_slug: str, ticket: str) -> int:
    """Open work units for this repo and ticket, straight from the database.

    NOT over the fleet's HTTP API, which is how this was written first and why it returned
    502. The call arrives here via the gatekeeper's proxy, so asking the gatekeeper a question
    mid-request makes the chain gatekeeper -> tool-executor -> gatekeeper: re-entrant, and it
    deadlocks until something times out. The tables are in the same Postgres either way, so
    the hop bought nothing but a cycle.
    """
    from ...db import get_pool
    pool = get_pool()
    if not pool:
        return -1          # unknown, not zero — the caller must fail closed on this
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """SELECT count(*) FROM fleet_work_units
                WHERE upper(parent_key) = upper($1) AND repo_slug = $2
                  AND state <> ALL($3::text[])""",
            ticket, repo_slug, ["done", "cancelled", "declined"])


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


async def may_push(repo: str, ref: str) -> tuple:
    """Whether this ref may be published, and why either way.

    Checked against the FLEET, never against anything on disk: a local file is something the
    caller can write, and a guard the guarded party can edit is not a guard.
    """
    repo_slug = repo.split("/")[-1]

    if ref.startswith(BOT_PREFIX):
        ticket = ref[len(BOT_PREFIX):]
        if not TICKET_RE.match(ticket):
            return False, (f"{ref!r} is in the bot namespace but {ticket!r} is not a ticket "
                           "key. Branches are named bot/<TICKET>, e.g. bot/CLC-4156.")
        n = await _units(repo_slug, ticket)
        if n < 0:
            # Fails CLOSED, the same rule the promotion check follows: a gate that cannot
            # confirm it is allowed is not allowed.
            return False, ("cannot reach the database to confirm a work unit exists for "
                           f"{ticket}. Refusing rather than assuming.")
        if n == 0:
            return False, (f"no open work unit for {ticket} in {repo_slug}, so {ref} is not "
                           "work the coordinator asked for. Branches carry assigned work; one "
                           "matching no unit is untraceable by design.")
        return True, f"open work unit for {ticket} in {repo_slug}"

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


async def remote_op(repo: str, op: str, ref: str = "", profile_slug: str = "",
                    token: str = None, ticket_hint: str = "") -> tuple:
    """Validate, perform, and RECORD one remote operation. Returns (ok, payload_or_reason).

    Recording happens here, around the whole decision, rather than at each return. A dozen
    exits each responsible for remembering to log is a design where one of them eventually
    does not, and the one that forgets is never the boring one.
    """
    ok, payload = await _decide(repo, op, ref, profile_slug, token, ticket_hint)
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
                  token: str = None, ticket_hint: str = "") -> tuple:
    """The decision itself, with no knowledge of how it gets recorded."""
    if op not in ("fetch", "push", "promote"):
        return False, f"unknown op {op!r} — expected fetch, push or promote"

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
        args = ["fetch", auth] + ([ref] if ref else ["--all"])
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                           timeout=180, env=git_env())
        if r.returncode != 0:
            return False, f"fetch failed: {scrub(r.stderr)[-500:]}"
        return True, {"repo": repo, "op": "fetch", "ref": ref or "(all)",
                      "output": scrub(r.stderr or r.stdout)[-600:]}


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

        r = subprocess.run(["git", "-C", path, "push", auth, f"{env}:{env}"],
                           capture_output=True, text=True, timeout=300, env=git_env())
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

    r = subprocess.run(["git", "-C", path, *cmd], capture_output=True, text=True,
                       timeout=300, env=git_env())
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
            "Perform a remote git operation on a repository you own: `fetch` or `push`.\n\n"
            "This is the ONLY path to a remote. Your container has no credential, so a plain "
            "`git push` cannot authenticate — that is deliberate, not a misconfiguration.\n\n"
            "What is allowed:\n"
            "  fetch                — always\n"
            "  push bot/<TICKET>    — only if an OPEN work unit exists for this repo and ticket\n"
            "  push release/<KEY>   — only if this repo is a member of that release group\n\n"
            "Always refused: environment branches (production, testing, staging, main), "
            "deleting any ref, and force-pushing anything but a release branch.\n\n"
            "Promotion to an environment is NOT done here. It goes through the fleet's "
            "approval, which fails closed.\n\n"
            "A refusal is an answer, not an error to route around. It names the rule that "
            "refused and what would satisfy it."
        )

    @property
    def short_description(self) -> str:
        return "Fetch or push, with server-side ref rules"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string",
                         "description": "owner/repo, e.g. cenora-llc/cenora-core-data-services"},
                "op": {"type": "string", "enum": ["fetch", "push"]},
                "ref": {"type": "string",
                        "description": "Branch to push, e.g. bot/CLC-4156. For fetch, the "
                                       "branch to fetch (optional; defaults to all)."},
            },
            "required": ["repo", "op"],
        }

    def credential_keys(self) -> list[str]:
        return ["GITHUB_TOKEN"]

    async def execute(self, repo: str, op: str, ref: str = "", **kwargs) -> ToolResult:
        ok, payload = await remote_op(repo, op, ref, self._profile_slug,
                                      token=self.get_credential("GITHUB_TOKEN"))
        return ToolResult.ok(payload) if ok else ToolResult.fail(payload)

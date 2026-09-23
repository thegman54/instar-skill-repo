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
local git disappears and every operation becomes a call. The expert keeps a shell for builds
(npm, npx, go — no policy's verify command uses git) and a read-only rootfs it cannot install
git back into.

WHAT IS ENFORCED, and nothing else is possible:

    fetch            always allowed — reading cannot damage anything
    push bot/<T>     only with an OPEN work unit for that repo and ticket
    push release/<K> only for a repo that is a member of that release group
    push <env>       only with an approved promotion, checked by the fleet, failing closed
    delete           never, at all
    force            only release/*, only with-lease, because it is rebuilt from base by design

The ticket in a bot branch must match a real work unit. That is the check that makes the
namespace mean something: without it `bot/` is a naming convention an expert can type anything
into, which is exactly as enforcing as the old arrangement.
"""

import os
import re
import subprocess

import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, git_env, sanitize_stderr

log = structlog.get_logger()

BOT_PREFIX = "bot/"
RELEASE_PREFIX = "release/"

# Never pushable by this tool under any argument, in any mode. Listed by name rather than
# derived from policy: a policy file is data, and data that can be edited is not a guard.
NEVER = {"main", "master", "production", "production-v2", "testing", "staging",
         "develop", "HEAD"}

TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

FLEET_API = os.environ.get("FLEET_API", "http://gatekeeper:8090")


def _git(cwd, *args, timeout=180):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                          timeout=timeout, env=git_env())


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
            "What is refused, always: environment branches (production, testing, staging, "
            "main), deleting any ref, and force-pushing anything but a release branch.\n\n"
            "Promotion to an environment is NOT done here. It goes through the fleet's "
            "approval, which fails closed.\n\n"
            "A refusal is an answer, not an error to route around. It names which rule "
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
                "repo": {"type": "string", "description": "owner/repo, e.g. cenora-llc/cenora-core-data-services"},
                "op": {"type": "string", "enum": ["fetch", "push"]},
                "ref": {
                    "type": "string",
                    "description": "Branch to push, e.g. bot/CLC-4156. For fetch, the branch "
                                   "to fetch (optional; defaults to all).",
                },
            },
            "required": ["repo", "op"],
        }

    def credential_keys(self) -> list[str]:
        return ["GITHUB_TOKEN"]

    async def execute(self, repo: str, op: str, ref: str = "", **kwargs) -> ToolResult:
        valid, path, access, error, _branch = await check_repo_access(
            repo, "push" if op == "push" else "read", self._profile_slug)
        if not valid:
            return ToolResult.fail(error)

        ref = (ref or "").strip()
        if ref and not REF_RE.match(ref):
            # Refused on shape before anything is interpolated anywhere. A ref is a string that
            # reaches a command line; the place to stop a strange one is before it is used.
            return ToolResult.fail(f"{ref!r} is not a usable branch name")

        token = self.get_credential("GITHUB_TOKEN")
        auth = f"https://x-access-token:{token}@github.com/{repo}.git"

        if op == "fetch":
            args = ["fetch", auth] + ([ref] if ref else ["--all"])
            r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                               timeout=180, env=git_env())
            if r.returncode != 0:
                return ToolResult.fail(
                    f"fetch failed: {sanitize_stderr(r.stderr).replace(token, '***')}")
            return ToolResult.ok({"repo": repo, "op": "fetch", "ref": ref or "(all)",
                                  "output": sanitize_stderr(r.stderr or r.stdout)[-600:]})

        # ---------------------------------------------------------------- push
        if not ref:
            return ToolResult.fail("push needs a ref — name the branch to publish")

        bare = ref.split("/")[-1]
        if ref in NEVER or bare in NEVER:
            return ToolResult.fail(
                f"{ref} is an environment branch and is never pushed by this tool. "
                "Reaching an environment goes through the fleet's promotion approval, which "
                "a person grants.")

        allowed, why = await self._may_push(repo, ref)
        if not allowed:
            return ToolResult.fail(why)

        # --force-with-lease only for release branches, which are rebuilt from base by design
        # and therefore legitimately non-fast-forward. Everything else pushes fast-forward or
        # fails; an expert that needs history rewritten has a problem a force push hides.
        cmd = ["push", auth, f"{ref}:{ref}"]
        if ref.startswith(RELEASE_PREFIX):
            cmd.insert(1, "--force-with-lease")

        r = subprocess.run(["git", "-C", path, *cmd], capture_output=True, text=True,
                           timeout=300, env=git_env())
        out = sanitize_stderr((r.stderr or "") + (r.stdout or "")).replace(token, "***")
        if r.returncode != 0:
            return ToolResult.fail(f"push of {ref} failed: {out[-600:]}")

        log.info("repo_git_push", repo=repo, ref=ref, profile=self._profile_slug)
        return ToolResult.ok({"repo": repo, "op": "push", "ref": ref,
                              "why_allowed": why, "output": out[-600:]})

    async def _may_push(self, repo: str, ref: str) -> tuple:
        """Decide whether this ref may be published, and say why either way.

        The namespace alone proves nothing — `bot/` is a string an expert can type. What makes
        it mean something is that the ticket must correspond to work the coordinator actually
        created. Checked against the fleet rather than against anything local, because a local
        file is something the caller can write.
        """
        import json
        import urllib.request

        repo_slug = repo.split("/")[-1]

        if ref.startswith(BOT_PREFIX):
            ticket = ref[len(BOT_PREFIX):]
            if not TICKET_RE.match(ticket):
                return False, (
                    f"{ref!r} is in the bot namespace but {ticket!r} is not a ticket key. "
                    "Branches are named bot/<TICKET>, e.g. bot/CLC-4156.")
            try:
                url = f"{FLEET_API}/api/fleet/units?open_only=true"
                with urllib.request.urlopen(url, timeout=15) as resp:
                    units = json.loads(resp.read()).get("units", [])
            except Exception as exc:
                # Fails CLOSED. A gate that cannot confirm it is allowed is not allowed — the
                # same rule the promotion check follows, for the same reason.
                return False, (f"could not reach the fleet to confirm a work unit exists for "
                               f"{ticket} ({exc}). Refusing rather than assuming.")
            match = [u for u in units
                     if (u.get("parent_key") or "").upper() == ticket.upper()
                     and u.get("repo_slug") == repo_slug]
            if not match:
                return False, (
                    f"no open work unit for {ticket} in {repo_slug}, so {ref} is not work the "
                    "coordinator asked for. Branches exist to carry assigned work; one that "
                    "matches no unit is untraceable by design.")
            return True, f"open work unit for {ticket} in {repo_slug}"

        if ref.startswith(RELEASE_PREFIX):
            group_key = ref[len(RELEASE_PREFIX):]
            try:
                with urllib.request.urlopen(f"{FLEET_API}/api/fleet/releases", timeout=15) as r:
                    releases = json.loads(r.read()).get("releases", [])
            except Exception as exc:
                return False, (f"could not reach the fleet to confirm release group "
                               f"{group_key} ({exc}). Refusing rather than assuming.")
            for g in releases:
                if g.get("group_key") == group_key and g.get("state") not in (
                        "promoted", "abandoned"):
                    if any(m.get("repo_slug") == repo_slug for m in g.get("members", [])):
                        return True, f"{repo_slug} is a member of release group {group_key}"
                    return False, (
                        f"{repo_slug} is not a member of release group {group_key}. A release "
                        "branch belongs to the repos in the group and to no others.")
            return False, (f"no open release group {group_key!r}, so {ref} composes nothing.")

        return False, (
            f"{ref} is outside every namespace this tool can publish. Expert work is "
            f"{BOT_PREFIX}<TICKET>; a composed release is {RELEASE_PREFIX}<KEY>. Nothing else "
            "is pushable, including branches that merely look similar.")

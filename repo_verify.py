"""
repo_verify — run this repository's own build and tests, and record the result.

WHY THIS EXISTS, and why its absence cost a day.

The fleet refuses `awaiting_qa`, `on_testing` and `done` until something has attested the
build, because each of those states tells another party the work is ready. The only thing
that could produce that attestation was `git-gate verify`, a shell script inside the expert's
own container. git-gate is a historical artefact: it dates from when the expert held GH_TOKEN
and did git locally. When that proved unenforceable the credential moved into this executor
and every NETWORK operation became a tool (see repo_git). `verify` stayed behind — it needs no
credential, so nobody moved it.

But the belt is the only interface a caged bot has. On 2026-11-10 two experts, on two repos of
one ticket, independently searched their belts for a way to attest, found none — correctly,
there was none — checked `repo_actions` for a CI run, found none, and escalated that nothing in
the system could verify their work. Both had a shell and both had git-gate on PATH. Neither had
any reason to look there, because a capability that is not on the belt does not exist to them.
Writing the instruction into a manual is not a fix; it only works for a bot that already
believes the capability exists.

WHAT IT DOES NOT DO. It does not reimplement verify. There is exactly one implementation of
what "verified" means — git-gate reading the repo's policy and POSTing the gate report — and a
second copy would drift from it, with the permissive copy being the one nobody reads. This runs
THAT implementation, in the container that holds it, in the unit's own checkout, and reports
what it said. The executor already has the Docker socket (stack_manager runs throwaway
containers the same way) and already mounts /projects, so the checkout path means the same
thing on both sides of the exec.

It promotes nothing and merges nothing, which is why it carries no approval gate: the whole
point of verify is to be cheap enough to run before you are sure.
"""

import asyncio
import os
import subprocess

import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, sanitize_stderr, validate_project_root

log = structlog.get_logger()

# npm install on a cold cache is minutes, not seconds. Long by tool standards and still a
# bound: a verify that hangs forever reads to the caller exactly like one that is working.
VERIFY_TIMEOUT = int(os.environ.get("REPO_VERIFY_TIMEOUT_SECONDS", "900"))

# Which container holds git-gate for this profile. bot_profiles.runner_host is the same column
# the gatekeeper launches a runner on, so there is one answer rather than two.
DEFAULT_RUNNER = os.environ.get("FLEET_RUNNER_CONTAINER", "instar-expert-runner")

# The user inside the runner that owns the checkouts and /gate/state. Running as root would
# leave root-owned node_modules behind and the next expert write would fail — that exact class
# of breakage is documented in project-instar/CLAUDE.md.
RUNNER_USER = os.environ.get("FLEET_RUNNER_USER", "expert")

# How much of the build log comes back. Enough to see which command failed and why; not the
# whole npm install, which is tens of kilobytes of progress noise and would crowd out the
# answer in a context window.
TAIL_CHARS = 4000


async def _runner_for(profile_slug: str) -> str:
    """The container this profile's work lives in, from the same column that launches it."""
    if not profile_slug:
        return DEFAULT_RUNNER
    try:
        from ...db import get_pool
        pool = get_pool()
        if not pool:
            return DEFAULT_RUNNER
        async with pool.acquire() as conn:
            host = await conn.fetchval(
                "SELECT runner_host FROM bot_profiles WHERE slug = $1", profile_slug)
    except Exception as exc:                                           # noqa: BLE001
        log.warning("repo_verify_runner_lookup_failed", error=str(exc)[:160])
        return DEFAULT_RUNNER
    # 'default' means the shared bot container, which has no checkouts and no git-gate.
    if not host or host == "default":
        return DEFAULT_RUNNER
    return host


async def _attestation(repo_slug: str, profile_slug: str) -> dict:
    """What the gate actually recorded, read back from the fleet rather than inferred.

    An exit code of zero says the commands passed; it does not say the report reached the
    unit. Those are different failures and only one of them is fixed by running it again.
    """
    out = {"recorded": None}
    try:
        from ...db import get_pool
        pool = get_pool()
        if not pool:
            return out
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT to_regclass('fleet_work_units') IS NOT NULL"):
                return out
            row = await conn.fetchrow(
                """SELECT parent_key, state, gated_sha, build_ok, tests_ok, gate_summary,
                          gated_at
                     FROM fleet_work_units
                    WHERE repo_slug = $1 AND profile_slug = $2
                      AND state NOT IN ('done', 'cancelled', 'superseded')
                    ORDER BY updated_at DESC LIMIT 1""",
                repo_slug.split("/")[-1], profile_slug)
        if row:
            out = {
                "recorded": bool(row["gated_sha"]),
                "unit": row["parent_key"], "state": row["state"],
                "gated_sha": (row["gated_sha"] or "")[:12],
                "build_ok": row["build_ok"], "tests_ok": row["tests_ok"],
                "summary": row["gate_summary"] or "",
                "gated_at": row["gated_at"].isoformat() if row["gated_at"] else None,
            }
    except Exception as exc:                                           # noqa: BLE001
        log.warning("repo_verify_readback_failed", error=str(exc)[:160])
    return out


@register_tool
class RepoVerifyTool(BaseTool):
    """Run the repo's own build and tests through git-gate and record the attestation."""

    @property
    def name(self) -> str:
        return "repo_verify"

    @property
    def description(self) -> str:
        return ("Run this repository's own build and test commands and record the result "
                "against your work unit. This is what the fleet means by an attestation: "
                "without it, awaiting_qa, on_testing and done are refused. It promotes "
                "nothing and merges nothing, needs no approval, and is safe to run as often "
                "as you like. It is not CI — it runs in your checkout, so an empty "
                "repo_actions list says nothing about whether your unit can be attested.")

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string",
                         "description": "Repository in owner/repo format"},
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, repo: str, **kwargs) -> ToolResult:
        # Same scoping as every other repo tool: a profile verifies only in its own
        # workspace. Nothing new to grant, and no second authorization rule to drift.
        valid, project_root, _access, error, _branch = await check_repo_access(
            repo, "status", self._profile_slug)
        if not valid:
            return ToolResult.fail(error)

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        runner = await _runner_for(self._profile_slug or "")
        cmd = ["docker", "exec", "-u", RUNNER_USER, "-w", project_root,
               runner, "git-gate", "verify"]

        try:
            proc = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, text=True,
                                       timeout=VERIFY_TIMEOUT))
        except subprocess.TimeoutExpired:
            return ToolResult.fail(
                f"verify exceeded {VERIFY_TIMEOUT}s and was killed. Nothing was recorded, so "
                "the unit is unchanged. A build that takes this long is usually a command in "
                "the policy waiting on input or starting a server rather than exiting — read "
                "the policy's verify commands before retrying.")
        except FileNotFoundError:
            return ToolResult.fail(
                "the docker CLI is not available to the tool executor, so verify cannot be "
                "run in the expert runner. This is an infrastructure fault, not something to "
                "work around: escalate it.")

        out = (proc.stdout or "")[-TAIL_CHARS:]
        err = sanitize_stderr((proc.stderr or ""))[-TAIL_CHARS:]
        attested = await _attestation(repo, self._profile_slug or "")

        if proc.returncode != 0:
            # A NON-ZERO EXIT IS NOT AUTOMATICALLY A FAILED BUILD, and saying so was this
            # tool's first bug. git-gate raises Refused for a failed verify AND for every
            # precondition it declines on — no policy, no open unit, an unreachable fleet —
            # and they all leave the same exit code. The first real use hit a gate-to-fleet
            # timeout and was told "the build or the tests failed", which is a different
            # problem with a different owner, and the expert had to argue with its own tool
            # to get back to the truth. So read what it SAID, not just how it exited: a
            # verify that actually ran prints a `verify:` line per command.
            ran = "[git-gate] verify:" in out
            failed_build = "verify failed" in out or "verify failed" in err
            refused = "REFUSED" in out or "REFUSED" in err
            if failed_build:
                meaning = (
                    "The build or the tests failed, which IS an answer about the work — not a "
                    "tool problem and not something to route around. Fix what failed and run "
                    "this again, or move the unit to needs_input naming the command that "
                    "failed. Do not ask for awaiting_qa; it will be refused, correctly.")
            elif refused:
                meaning = (
                    "NOTHING WAS BUILT OR TESTED. The gate declined before running anything, "
                    "so this says nothing about your code — read its refusal above, it names "
                    "the reason. If that reason is about reaching the fleet, a policy, or an "
                    "open unit, it is a platform or setup fault: escalate it as that, and do "
                    "not report it as a test result. Your unit is unchanged.")
            else:
                meaning = (
                    "The gate exited %d without a recognisable verdict and without printing a "
                    "verify step, so it is unknown whether anything ran. Treat this as "
                    "unobserved rather than as a pass or a failure, and escalate the output "
                    "above." % proc.returncode)
            return ToolResult.ok({
                "passed": False,
                "commands_ran": ran,
                "verdict": ("build_or_tests_failed" if failed_build else
                            "refused_before_running" if refused else "unrecognised"),
                "exit_code": proc.returncode,
                "output": out,
                "error": err,
                "attestation": attested,
                "what_this_means": meaning,
            })

        return ToolResult.ok({
            "passed": True,
            "exit_code": 0,
            "output": out,
            "attestation": attested,
            "what_this_means": (
                "Build and tests passed and the gate recorded it against your unit, so the "
                "state you were refused before is now available."
                if attested.get("recorded") else
                "The commands passed, but no attestation is recorded against a unit for this "
                "repository. The commands running and the report reaching the fleet are two "
                "different things: check you have an open unit for this repo, and say so "
                "rather than treating the exit code as the attestation."),
        })

"""
repo_push — the old name for publishing a branch, routed through the one gate.

WHY THIS FILE NO LONGER PUSHES ANYTHING ITSELF. It used to fetch GITHUB_TOKEN, rewrite
`origin` to embed it, and run `git push` behind nothing but `check_repo_access`. That meant
any ref, on any configured repo the profile could write, with no work unit and no branch
rules — including `production`. `repo_git` exists precisely to stop that, and its header
says why: a rule enforced by the caller is not a rule.

Having both on a belt made the gate optional, and on 2026-11-03 both were on all three
fleet belts while `fleet_cen_dev` held repo_write_all over 26 repos configured `full`. The
expert's own instructions told it writes go through git-gate, and it believed them — it
told QA "git push is unavailable to me" while holding this tool. The documentation was
right about the design and wrong about the system.

So this is now a thin caller of `remote_op`, the same entry point the MCP tool and git-gate
use. Deleting the tool outright was the alternative; keeping the name and removing the
capability is better, because a belt that still lists `repo_push` now gets the gate instead
of silently getting around it. There is no ordering in which the checks are skipped and the
push still happens, because the check and the push are the same call.

What that means in practice: `bot/<TICKET>` needs an open work unit for this repo and that
ticket, `release/<KEY>` needs release-group membership, environment branches are refused and
go through the fleet's promotion approval, and every outcome is recorded whether allowed or
refused.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, git_env, validate_project_root
from .repo_git import remote_op


@register_tool
class RepoPushTool(BaseTool):
    """Publish a branch, through the same gate every other remote operation uses."""

    @property
    def name(self) -> str:
        return "repo_push"

    @property
    def description(self) -> str:
        return (
            "Publish a branch to the remote, through the branch gate. A `bot/<TICKET>` branch "
            "is pushed only when an open work unit exists for this repo and that ticket; "
            "`release/<KEY>` only for a member of that release group. Environment branches "
            "(production, staging, testing) are never pushed here — reaching an environment "
            "goes through the fleet's promotion approval, which a person grants.\n\n"
            "Identical rules to repo_git's push, because it is the same call."
        )

    @property
    def short_description(self) -> str:
        return "Publish a branch through the branch gate"

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "branch": {"type": "string",
                           "description": "Branch to publish. Defaults to the checked-out "
                                          "branch, which is resolved and then gated like any "
                                          "other ref — the default is not an exemption"},
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        # Deliberately none. The token is fetched inside remote_op, in the process that makes
        # the decision. A tool that holds a credential it does not use is a credential one
        # edit away from being used.
        return []

    async def execute(self, repo: str, branch: str = None, **kwargs) -> ToolResult:
        # Resolved with a read-only access check so that naming an unconfigured or
        # out-of-scope repo fails here with a clear reason, rather than inside the gate.
        valid, project_root, _access, error, _branch = await check_repo_access(
            repo, "read", self._profile_slug)
        if not valid:
            return ToolResult.fail(error)

        ref = (branch or "").strip()
        if not ref:
            valid, error = validate_project_root(project_root)
            if not valid:
                return ToolResult.fail(error)
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=project_root, capture_output=True, text=True, timeout=10, env=git_env())
            if result.returncode != 0 or not result.stdout.strip():
                return ToolResult.fail(
                    "Could not determine the current branch, so there is nothing to name in "
                    "the push. Pass `branch` explicitly.")
            ref = result.stdout.strip()

        ok, payload = await remote_op(
            repo, "push", ref=ref, profile_slug=self._profile_slug,
            ticket_hint=(kwargs.get("ticket") or "").strip())
        if not ok:
            return ToolResult.fail(str(payload))
        return ToolResult.ok(payload)

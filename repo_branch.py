"""
Repo Branch Tool - List, create, switch branches.
"""

import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import git_run, git_env, check_repo_access, sanitize_stderr, validate_project_root


@register_tool
class RepoBranchTool(BaseTool):
    """List, create, or switch git branches."""

    @property
    def name(self) -> str:
        return "repo_branch"

    @property
    def description(self) -> str:
        return (
            "List branches, create one, or switch to one.\n\n"
            "For work on a ticket, pass `ticket` and leave `name` empty: the branch is the "
            "one the coordinator named when it raised the work unit, and that is the only "
            "ref the push gate will accept. Composing a name yourself is how you end up with "
            "a branch that cannot be published."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/repo format"},
                "action": {"type": "string", "enum": ["list", "create", "switch"], "description": "Action to perform (default: list)"},
                "name": {"type": "string",
                         "description": "Branch name. Omit it and pass `ticket` instead for "
                                        "work on a ticket — the coordinator already named it"},
                "ticket": {"type": "string",
                           "description": "Ticket key, e.g. CLC-4555. The branch name is read "
                                          "from that ticket's open work unit for this repo"},
                "from_branch": {"type": "string", "description": "Base branch to create from (default: current branch)"},
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        return []

    async def execute(self, repo: str, action: str = "list", name: str = None,
                      from_branch: str = None, ticket: str = "", **kwargs) -> ToolResult:
        valid, project_root, access, error, _ = await check_repo_access(repo, "branch", self._profile_slug)
        if not valid:
            return ToolResult.fail(error)

        # THE NAME IS TOLD, NOT COMPOSED. A ticket resolves to the branch its work unit
        # names, so the expert never derives a ref -- it cannot pick the wrong namespace,
        # and the name it builds on is the same one the push gate checks against. Every
        # repo manual said `feat/<TICKET>` while the gate publishes `bot/<TICKET>` only;
        # reading the name removes that disagreement instead of restating it.
        ticket = (ticket or "").strip()
        if ticket:
            from .repo_git import _units
            n, named = await _units(repo.split("/")[-1], ticket)
            if n < 0:
                return ToolResult.fail(
                    "Cannot reach the database to look up the branch for "
                    f"{ticket}. Refusing rather than guessing a name.")
            if n == 0:
                return ToolResult.fail(
                    f"No open work unit for {ticket} in this repo, so no branch has been "
                    "named for it. Ask the coordinator for the unit rather than inventing "
                    "a branch -- the push gate refuses a ref that matches no unit.")
            if not named:
                return ToolResult.fail(
                    f"The open unit for {ticket} does not name a branch (it predates the "
                    "coordinator naming them). Ask the coordinator to set it, so the name "
                    "you build on is the name the gate checks.")
            if name and name != named:
                return ToolResult.fail(
                    f"{ticket}'s branch is {named!r}, not {name!r}. Omit `name` and let the "
                    "unit answer, or raise the disagreement -- do not create a second branch "
                    "for one unit.")
            name = named

        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        try:
            if action == "list":
                result = git_run(["git", "branch", "-a", "-v"], cwd=project_root, capture_output=True, text=True, timeout=30,
            )
                if result.returncode != 0:
                    return ToolResult.fail(f"Git error: {result.stderr}")
                current = git_run(["git", "branch", "--show-current"], cwd=project_root, capture_output=True, text=True, timeout=10,
            )
                return ToolResult.ok({"current": current.stdout.strip() if current.returncode == 0 else "unknown", "branches": result.stdout.strip()})

            elif action == "create":
                if not name:
                    return ToolResult.fail("Branch name required for create")
                # NEVER CUT A TICKET BRANCH FROM ANOTHER TICKET'S BRANCH. The checkout is
                # shared, so "the current branch" is whatever the last unit left there:
                # CLC-4664 (2026-11-16) was cut from bot/CLC-4644 and carried its commit.
                # Without from_branch, a checkout sitting on a bot/ branch is refused.
                current = git_run(["git", "branch", "--show-current"], cwd=project_root,
                                  timeout=10).stdout.strip()
                if not from_branch and current.startswith("bot/") and current != name:
                    return ToolResult.fail(
                        f"The checkout is on {current}, another ticket's branch. Pass "
                        f"from_branch with the base this work starts from (e.g. "
                        f"origin/production), so {name} does not inherit {current}'s commits.")
                # RE-CUT, SAFELY. A branch that already exists may be re-cut from from_branch
                # only when none of its commits exist solely on it (every one is on some
                # remote ref): nothing is lost, and a wrong base can be corrected by the dev
                # itself instead of by a person.
                exists = git_run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
                                 cwd=project_root, timeout=10).returncode == 0
                if exists and from_branch:
                    own = git_run(["git", "rev-list", name, "--not", "--remotes"],
                                  cwd=project_root, timeout=30).stdout.split()
                    if own:
                        return ToolResult.fail(
                            f"Branch '{name}' already exists with {len(own)} commit(s) that are "
                            "on no remote; re-cutting it would lose them. Push or escalate.")
                    if git_run(["git", "status", "--porcelain"], cwd=project_root,
                               timeout=30).stdout.strip():
                        return ToolResult.fail("Uncommitted changes in the checkout; re-cut refused.")
                    cmd = ["git", "checkout", "-B", name, from_branch]
                else:
                    cmd = ["git", "checkout", "-b", name]
                    if from_branch:
                        cmd.append(from_branch)
                # env=git_env() like every other git call in this skill. This one was the
                # single exception, so it inherited an environment without GIT_SAFE_ENV and
                # died on "dubious ownership" against any workspace clone chowned to uid
                # 1000 for the expert-runner — which is all of them. Creating a branch is
                # the first thing an expert does, so the whole write path ended here.
                result = git_run(cmd, cwd=project_root, timeout=30)
                if result.returncode != 0:
                    if "already exists" in result.stderr.lower():
                        return ToolResult.fail(f"Branch '{name}' already exists")
                    return ToolResult.fail(f"Git error: {sanitize_stderr(result.stderr)}")
                return ToolResult.ok({"action": "created", "branch": name, "from": from_branch or "(current branch)"})

            elif action == "switch":
                if not name:
                    return ToolResult.fail("Branch name required for switch")
                result = git_run(["git", "checkout", name], cwd=project_root, capture_output=True, text=True, timeout=30,
            )
                if result.returncode != 0:
                    stderr = result.stderr.lower()
                    if "did not match" in stderr or "not found" in stderr:
                        return ToolResult.fail(f"Branch '{name}' not found")
                    if "uncommitted changes" in stderr or "would be overwritten" in stderr:
                        return ToolResult.fail("Cannot switch: uncommitted changes would be lost")
                    return ToolResult.fail(f"Git error: {result.stderr}")
                return ToolResult.ok({"action": "switched", "branch": name})

            else:
                return ToolResult.fail(f"Unknown action: {action}")

        except subprocess.TimeoutExpired:
            return ToolResult.fail("Git command timed out")
        except Exception as e:
            return ToolResult.fail(f"Branch error: {str(e)}")

"""
Repo Clone Tool - Clone a GitHub repository into the workspace.
"""

import os
import subprocess

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, git_env, sanitize_stderr


# Documents a bot has no business reading, hidden from the WORKING TREE of every checkout the
# fleet works in. Each pattern is a sparse-checkout exclusion, so the files stay tracked, stay
# on the remote, stay in everyone else's clone and stay in git history — they are simply not
# written to disk here.
#
# WHY. A repository accumulates write-ups of one past problem: CLAUDE_TROUBLESHOOTING_MYSORT.md,
# CLAUDE_ASSET_TAGGING_INVESTIGATION.md, CLAUDE1.md. entity-feature-data-loaders has 36 of them
# against 9 live notes files. They were accurate on the day they were written, nobody maintains
# them, and a bot that greps the repo meets them as peers of the current document — which is how
# a procedure from March gets followed in November. Their own manual spends its opening section
# telling the reader which ones to ignore; that section exists because the files are reachable.
#
# WHY NOT MOVE OR DELETE THEM. Because they are not ours, they are useful to the person who
# wrote them, and a repository's contents are not something the fleet gets to decide. This hides
# them locally and ships nothing: the patterns live in .git/info/sparse-checkout, which is never
# committed, so no other clone and no other person is affected.
#
# NOT EXCLUDED, deliberately: CLAUDE.md (the live working notes, which a bot opened in that repo
# should read) and CLAUDEBOT*.md (written for a bot in the first place). Neither matches the
# patterns below, which all require an underscore, a digit or a dash after CLAUDE.
#
# ANCHORS MUST NOT POINT AT AN EXCLUDED PATH. A knowledge anchor resolves with `git grep` in
# this checkout, so a fact anchored to a hidden file is unresolvable here. Nothing anchors to
# these today and nothing should: a claim about a stale document is not a claim about code.
SPARSE_HIDE = (
    "!CLAUDE_*.md", "!**/CLAUDE_*.md",          # CLAUDE_TROUBLESHOOTING_*, CLAUDE_*_INVESTIGATION
    "!CLAUDE[0-9]*.md", "!**/CLAUDE[0-9]*.md",  # CLAUDE1.md and friends
    "!CLAUDE-*.md", "!**/CLAUDE-*.md",          # CLAUDE-20250909-AssetManager-Progress.md
)


def apply_sparse_hide(workspace_path: str) -> dict:
    """Hide the documents in SPARSE_HIDE from this checkout's working tree.

    Idempotent, and safe on a dirty tree for the files it touches: sparse-checkout refuses to
    drop a file with local modifications rather than discarding them. Reported rather than
    raised — a checkout that exists with the documents still present is a working checkout, and
    failing a clone over tidiness would be the wrong trade.
    """
    out = {}
    init = subprocess.run(["git", "sparse-checkout", "init", "--no-cone"],
                          cwd=workspace_path, capture_output=True, text=True, timeout=30,
                          env=git_env())
    if init.returncode != 0:
        return {"sparse": "unavailable", "detail": sanitize_stderr(init.stderr)[:160]}
    # "/*" first: include everything, then subtract. Order matters — a later pattern wins.
    setr = subprocess.run(["git", "sparse-checkout", "set", "/*", *SPARSE_HIDE],
                          cwd=workspace_path, capture_output=True, text=True, timeout=60,
                          env=git_env())
    if setr.returncode != 0:
        return {"sparse": "failed", "detail": sanitize_stderr(setr.stderr)[:160]}
    out["sparse"] = "applied"
    return out


@register_tool
class RepoCloneTool(BaseTool):
    """Clone a GitHub repository into the shared workspace."""

    @property
    def name(self) -> str:
        return "repo_clone"

    @property
    def description(self) -> str:
        return (
            "Clone a GitHub repository into the workspace. "
            "If already cloned, pulls latest changes instead. "
            "Provide repo as 'owner/repo' format."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "GitHub repo in owner/repo format (e.g. 'aglickman/my-project')",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch to checkout after clone (default: default branch)",
                },
            },
            "required": ["repo"],
        }

    def credential_keys(self) -> list[str]:
        return ["GITHUB_TOKEN"]

    async def execute(self, repo: str, branch: str = None, **kwargs) -> ToolResult:
        valid, workspace_path, access, error, configured_branch = await check_repo_access(repo, "clone", self._profile_slug)
        if not valid:
            return ToolResult.fail(error)

        # Use configured branch from repo config if no explicit branch passed
        if not branch and configured_branch:
            branch = configured_branch

        token = self.get_credential("GITHUB_TOKEN")
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"

        try:
            if os.path.exists(workspace_path) and os.path.isdir(os.path.join(workspace_path, ".git")):
                return await self._update_existing(workspace_path, repo, branch, token)
            else:
                return await self._clone_fresh(clone_url, workspace_path, repo, branch)

        except subprocess.TimeoutExpired:
            return ToolResult.fail("Clone timed out (120s limit)")
        except Exception as e:
            return ToolResult.fail(f"Clone error: {sanitize_stderr(str(e))}")

    async def _clone_fresh(
        self, clone_url: str, workspace_path: str, repo: str, branch: str = None
    ) -> ToolResult:
        os.makedirs(os.path.dirname(workspace_path), exist_ok=True)

        cmd = ["git", "clone", "--depth", "50", clone_url, workspace_path]
        if branch:
            cmd.extend(["--branch", branch])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "authentication" in stderr or "403" in stderr:
                return ToolResult.fail("GitHub authentication failed — check GITHUB_TOKEN")
            if "not found" in stderr or "404" in stderr:
                return ToolResult.fail(f"Repository '{repo}' not found")
            return ToolResult.fail(f"Clone error: {sanitize_stderr(result.stderr)}")

        self._configure_git(workspace_path)
        self._sanitize_remote(workspace_path, repo)
        sparse = apply_sparse_hide(workspace_path)
        self._chown_workspace(workspace_path)

        info = self._get_repo_info(workspace_path)

        return ToolResult.ok({
            "repo": repo,
            "action": "cloned",
            **sparse,
            "path": workspace_path,
            **info,
        })

    @staticmethod
    def _chown_workspace(workspace_path: str) -> None:
        """Hand the clone to the unprivileged uid the rest of the stack runs as.

        tool-executor runs as root, so a fresh clone lands root-owned. Anything that later
        works IN that checkout as a normal user — a repo expert's runner at uid 1000, or the
        operator on the host — then cannot write to it, and git additionally refuses to touch
        it at all with "detected dubious ownership".

        Non-fatal: a clone that exists but is owned by root is still a successful clone, and
        failing the whole operation over a chown would be worse than reporting it.
        """
        uid = int(os.environ.get("WORKSPACE_UID", "1000"))
        gid = int(os.environ.get("WORKSPACE_GID", "1000"))
        try:
            for root, dirs, files in os.walk(workspace_path):
                os.chown(root, uid, gid)
                for name in dirs + files:
                    path = os.path.join(root, name)
                    if not os.path.islink(path):
                        os.chown(path, uid, gid)
            os.chown(workspace_path, uid, gid)
        except (OSError, PermissionError):
            pass

    async def _update_existing(
        self, workspace_path: str, repo: str, branch: str = None, token: str = None
    ) -> ToolResult:
        # Verify remote points to the expected repo
        remote_result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=10,
                env=git_env(),
            )
        if remote_result.returncode == 0:
            current_remote = remote_result.stdout.strip()
            if f"github.com/{repo}" not in current_remote:
                return ToolResult.fail(
                    f"Remote URL mismatch — expected github.com/{repo}. "
                    "Possible tampering. Delete workspace and re-clone."
                )

        # Temporarily set token URL for fetch
        if token:
            auth_url = f"https://x-access-token:{token}@github.com/{repo}.git"
            subprocess.run(
                ["git", "remote", "set-url", "origin", auth_url],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                timeout=10,
                env=git_env(),
            )

        if branch:
            checkout = subprocess.run(
                ["git", "checkout", branch],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                timeout=30,
                env=git_env(),
            )
            if checkout.returncode != 0:
                self._sanitize_remote(workspace_path, repo)
                return ToolResult.fail(
                    f"Could not check out '{branch}': "
                    f"{sanitize_stderr(checkout.stderr)}"
                )

        # Every result below is CHECKED, and the reason is a bug this exact function had:
        # none of these returncodes were read, so a fetch that failed for any reason at all
        # still returned ok() with the untouched checkout's details. Eleven repositories
        # reported "updated" — one of them 99 commits behind — while nothing moved. A stale
        # checkout that says it is current is worse than one that says it failed, because an
        # expert then reasons confidently about code that is months old.
        fetch = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=60,
            env=git_env(),
        )
        if fetch.returncode != 0:
            self._sanitize_remote(workspace_path, repo)
            return ToolResult.fail(
                f"Fetch failed for {repo}: {sanitize_stderr(fetch.stderr)}"
            )

        pull_result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=60,
            env=git_env(),
        )

        self._configure_git(workspace_path)
        self._sanitize_remote(workspace_path, repo)
        # Also on pull, so a checkout made before this existed converges on the next sync
        # instead of needing a re-clone.
        apply_sparse_hide(workspace_path)
        self._chown_workspace(workspace_path)

        if pull_result.returncode != 0:
            # Not a fast-forward, or a dirty tree. Reported rather than forced: a workspace
            # that has diverged is a question, and resetting it here would silently discard
            # whatever caused the divergence.
            return ToolResult.fail(
                f"Pull is not a fast-forward for {repo} — the checkout has diverged or is "
                f"dirty, so it was left exactly as it was. "
                f"{sanitize_stderr(pull_result.stderr or pull_result.stdout)}"
            )

        info = self._get_repo_info(workspace_path)

        return ToolResult.ok({
            "repo": repo,
            "action": "updated",
            "path": workspace_path,
            "pull_output": sanitize_stderr(
                pull_result.stdout.strip() or pull_result.stderr.strip()
            ),
            **info,
        })

    def _configure_git(self, workspace_path: str):
        subprocess.run(
            ["git", "config", "user.email", "bot@instar.local"],
            cwd=workspace_path, capture_output=True, timeout=5,
                env=git_env(),
            )
        subprocess.run(
            ["git", "config", "user.name", "Instar Bot"],
            cwd=workspace_path, capture_output=True, timeout=5,
                env=git_env(),
            )

    def _sanitize_remote(self, workspace_path: str, repo: str):
        clean_url = f"https://github.com/{repo}.git"
        subprocess.run(
            ["git", "remote", "set-url", "origin", clean_url],
            cwd=workspace_path, capture_output=True, text=True, timeout=5,
                env=git_env(),
            )

    def _get_repo_info(self, workspace_path: str) -> dict:
        info = {}
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace_path, capture_output=True, text=True, timeout=5,
                env=git_env(),
            )
        if branch_result.returncode == 0:
            info["branch"] = branch_result.stdout.strip()

        head_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=workspace_path, capture_output=True, text=True, timeout=5,
                env=git_env(),
            )
        if head_result.returncode == 0:
            info["head"] = head_result.stdout.strip()

        return info

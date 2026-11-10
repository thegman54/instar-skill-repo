"""
repo_preflight — would this branch land on testing cleanly, and does the RESULT build?

THE QUESTION NOBODY WAS ASKING. `git-gate promote-testing` already refuses to resolve a
conflict on an environment branch: a clean merge proceeds, a conflicting one aborts whole and
leaves testing exactly as it was. That is safe and it is late. The human approving a promotion
is asked before any of it runs, so they approve without knowing whether the thing merges, and
the conflict surfaces afterwards to a bot.

AND THE ATTESTATION IS ABOUT THE WRONG TREE. `repo_verify` builds the bot branch alone. What
deploys is the MERGE of that branch into testing, which is a tree nobody has ever compiled —
two clean branches can merge without a textual conflict and produce something that does not
build, which is the ordinary case of one side renaming what the other side calls. Release
groups already know this: `git-gate integrate` verifies the composition. A single unit had no
equivalent.

So this fetches testing, merges the unit's branch into it IN A THROWAWAY WORKTREE, and runs
the repository's own verify commands against the merged result. Measured on both CLC-4555
repos at about six seconds, which is cheap enough that there is no reason to ask the weaker
question instead.

WHAT IT IS NOT. It attests nothing and records nothing against the unit — `repo_verify` and
the gate own attestation, and a second writer of that fact would eventually disagree with the
first. This is advice for whoever is about to decide, and it is TRUE ONLY FOR THE TESTING TIP
IT RAN AGAINST, which is why that sha is in every answer. Somebody else merging invalidates
it; re-run it.

NOTHING IS TOUCHED. The primary checkout is never modified: the worktree is detached at the
fetched testing commit, the merge happens there, and the worktree is removed afterwards even
on failure. No local branch moves, nothing is pushed, and the expert's own tree keeps whatever
state it was in.
"""

import asyncio
import os
import subprocess

import structlog

from ..base import BaseTool, ToolResult
from ..registry import register_tool
from .base import check_repo_access, sanitize_stderr, validate_project_root

log = structlog.get_logger()

PREFLIGHT_TIMEOUT = int(os.environ.get("REPO_PREFLIGHT_TIMEOUT_SECONDS", "900"))
DEFAULT_RUNNER = os.environ.get("FLEET_RUNNER_CONTAINER", "instar-expert-runner")
RUNNER_USER = os.environ.get("FLEET_RUNNER_USER", "expert")
TAIL_CHARS = 4000

# Run inside the runner, where git, node and the checkout all are. One call rather than a
# dozen docker execs: the steps are ordered and a half-finished sequence would leave a
# worktree behind, so the cleanup belongs in the same process as the setup.
SCRIPT = r'''
import json, os, shutil, subprocess, sys, tempfile, time

root = sys.argv[1]
os.chdir(root)
out = {"steps": []}

def run(cmd, cwd=None, timeout=600):
    t = time.time()
    p = subprocess.run(cmd, cwd=cwd or root, capture_output=True, text=True, timeout=timeout)
    return {"cmd": " ".join(cmd), "exit": p.returncode, "seconds": round(time.time() - t, 1),
            "out": (p.stdout or "")[-4000:], "err": (p.stderr or "")[-4000:]}

pol_path = "/gate/policies/" + os.path.basename(root) + ".json"
if not os.path.exists(pol_path):
    print(json.dumps({"error": "no branch policy at " + pol_path}))
    sys.exit(0)
pol = json.load(open(pol_path))
remote = pol.get("remote") or "origin"
testing = pol.get("testing_branch")
verify = pol.get("verify") or []
if not testing:
    print(json.dumps({"error": "policy names no testing_branch"}))
    sys.exit(0)

branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True).stdout.strip()
head = subprocess.run(["git", "rev-parse", "HEAD"],
                      capture_output=True, text=True).stdout.strip()
dirty = subprocess.run(["git", "status", "--porcelain"],
                       capture_output=True, text=True).stdout.strip()
out.update({"branch": branch, "branch_sha": head[:12], "testing_branch": testing,
            "uncommitted_changes": bool(dirty)})

# NO FETCH HERE. The GitHub credential was deliberately removed from this container (see
# repo_git: every network git operation became a tool call in the executor, which holds the
# token), so a fetch from in here dies with "could not read Username for github.com". The
# executor has already fetched through remote_op, which writes refs/remotes/<remote>/<ref>
# into this same .git — so the ref below is current and this process touches no network.
remote_name = (subprocess.run(["git", "remote"], capture_output=True, text=True)
               .stdout.split() or [remote])[0]
tip = subprocess.run(["git", "rev-parse", remote_name + "/" + testing],
                     capture_output=True, text=True).stdout.strip()
if not tip:
    out["error"] = ("no local ref for " + remote_name + "/" + testing + ". The executor's "
                    "fetch should have written it; without it there is nothing to merge "
                    "against and guessing a base would be worse than refusing.")
    print(json.dumps(out)); sys.exit(0)
out["testing_sha"] = tip[:12]

# Already contained? Then there is nothing to land and nothing to decide.
contained = subprocess.run(["git", "merge-base", "--is-ancestor", head, tip]).returncode == 0
out["already_in_testing"] = contained

wt = tempfile.mkdtemp(prefix="/projects/workspaces/.preflight-")
shutil.rmtree(wt, ignore_errors=True)
try:
    a = run(["git", "worktree", "add", "--detach", wt, tip])
    out["steps"].append(a)
    if a["exit"] != 0:
        out["error"] = "could not create a worktree at the testing tip"
        print(json.dumps(out)); sys.exit(0)

    m = run(["git", "merge", "--no-commit", "--no-ff", head], cwd=wt)
    out["steps"].append(m)
    if m["exit"] != 0:
        conflicts = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"],
                                   cwd=wt, capture_output=True, text=True).stdout.split()
        subprocess.run(["git", "merge", "--abort"], cwd=wt, capture_output=True)
        out["merges_clean"] = False
        out["conflicts"] = conflicts
        print(json.dumps(out)); sys.exit(0)
    out["merges_clean"] = True
    out["conflicts"] = []

    # Dependencies come from the primary checkout by SYMLINK, and install commands are
    # skipped. Running an install inside the worktree would either cost a cold install every
    # time or write through the symlink into the expert's own node_modules, and a preflight
    # that mutates the thing it is inspecting is not a preflight.
    nm = os.path.join(root, "node_modules")
    if os.path.isdir(nm):
        link = os.path.join(wt, "node_modules")
        if not os.path.exists(link):
            os.symlink(nm, link)
        out["node_modules"] = "symlinked from the primary checkout"
    else:
        out["node_modules"] = "absent in the primary checkout; a build needing it will fail"

    built = []
    for cmd in verify:
        if cmd and cmd[0] in ("npm", "yarn", "pnpm") and any(
                x in ("install", "i", "ci") for x in cmd[1:]):
            built.append({"cmd": " ".join(cmd), "skipped":
                          "install skipped; node_modules is symlinked"})
            continue
        r = run(cmd, cwd=wt)
        built.append(r)
        if r["exit"] != 0:
            break
    out["merged_build"] = built
    out["merged_builds_clean"] = all(b.get("exit", 0) == 0 for b in built) and bool(built)
finally:
    subprocess.run(["git", "worktree", "remove", "--force", wt], capture_output=True)
    shutil.rmtree(wt, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], capture_output=True)

print(json.dumps(out))
'''


# The branch policies are mounted into this container too, at the deploy target's path.
# Read here rather than asked of the runner because the fetch needs the branch name BEFORE
# anything is executed over there.
POLICY_DIR = os.environ.get(
    "REPO_POLICY_DIR", "/app/tools/repo_expert_runtime/expert-runner/policy")


def _testing_branch(repo: str) -> tuple:
    """(branch, error). The policy is the only thing that may say which branch deploys."""
    import json as _j
    path = os.path.join(POLICY_DIR, repo.split("/")[-1] + ".json")
    if not os.path.isfile(path):
        return "", (f"no branch policy for {repo} at {path}, so there is no way to know "
                    "which branch is testing. Guessing is how work lands on the wrong "
                    "environment; ask an operator to add the policy.")
    try:
        pol = _j.load(open(path))
    except Exception as exc:                                           # noqa: BLE001
        return "", f"the branch policy for {repo} could not be read: {exc}"
    branch = (pol.get("testing_branch") or "").strip()
    if not branch:
        return "", f"the branch policy for {repo} names no testing_branch"
    return branch, ""


async def _runner_for(profile_slug: str) -> str:
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
        log.warning("repo_preflight_runner_lookup_failed", error=str(exc)[:160])
        return DEFAULT_RUNNER
    if not host or host == "default":
        return DEFAULT_RUNNER
    return host


@register_tool
class RepoPreflightTool(BaseTool):
    """Trial-merge a unit's branch into testing and build the merged result."""

    @property
    def name(self) -> str:
        return "repo_preflight"

    @property
    def description(self) -> str:
        return ("Fetch the testing branch, merge this repository's current bot branch into "
                "it in a throwaway worktree, and run the repo's own build against the MERGED "
                "result. Answers two things an attestation of the bot branch alone cannot: "
                "whether it lands without conflict, and whether what lands actually builds. "
                "It merges nothing for real, pushes nothing, records no attestation, and "
                "leaves the checkout untouched. The answer is only true for the testing "
                "commit it ran against, which is in the result — re-run it if testing moves.")

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
        valid, project_root, _access, error, _branch = await check_repo_access(
            repo, "status", self._profile_slug)
        if not valid:
            return ToolResult.fail(error)
        valid, error = validate_project_root(project_root)
        if not valid:
            return ToolResult.fail(error)

        # THE FETCH HAPPENS HERE, not in the runner. The credential lives in this process
        # and nowhere else, and remote_op is the single implementation of a remote git
        # operation plus its ref rules — a second one here would be the copy that drifts.
        # It writes refs/remotes/<remote>/<branch> into the shared .git, which is what the
        # script then reads.
        testing_branch, policy_err = _testing_branch(repo)
        if policy_err:
            return ToolResult.fail(policy_err)
        from .repo_git import remote_op
        ok, payload = await remote_op(repo, "fetch", ref=testing_branch,
                                      profile_slug=self._profile_slug or "")
        if not ok:
            return ToolResult.fail(
                f"could not fetch {testing_branch}: {payload}. Nothing was merged and "
                "nothing was recorded; without the current tip there is nothing honest to "
                "compare against.")

        runner = await _runner_for(self._profile_slug or "")
        cmd = ["docker", "exec", "-i", "-u", RUNNER_USER, runner,
               "python3", "-", project_root]
        try:
            proc = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(cmd, input=SCRIPT, capture_output=True, text=True,
                                       timeout=PREFLIGHT_TIMEOUT))
        except subprocess.TimeoutExpired:
            return ToolResult.fail(
                f"preflight exceeded {PREFLIGHT_TIMEOUT}s and was killed. Nothing was "
                "recorded and nothing was merged; a worktree may have been left behind, "
                "which `git worktree prune` in the checkout clears.")
        except FileNotFoundError:
            return ToolResult.fail(
                "the docker CLI is not available to the tool executor, so the preflight "
                "cannot run in the expert runner. Infrastructure fault: escalate it.")

        import json as _json
        raw = (proc.stdout or "").strip()
        try:
            data = _json.loads(raw.splitlines()[-1]) if raw else {}
        except Exception:                                              # noqa: BLE001
            return ToolResult.fail(
                "the preflight produced no readable result, which says nothing about the "
                "merge either way. stderr: "
                + sanitize_stderr(proc.stderr or "")[-TAIL_CHARS:]
                + " stdout: " + raw[-TAIL_CHARS:])

        if data.get("error"):
            return ToolResult.fail(data["error"] + " — nothing was merged or recorded.")

        clean = data.get("merges_clean")
        builds = data.get("merged_builds_clean")
        if clean and builds:
            verdict = ("LANDS CLEAN AND THE MERGED RESULT BUILDS, against testing at %s. "
                       "This is advice, not an attestation: it is true for that commit and "
                       "stops being true when testing moves."
                       % data.get("testing_sha", "?"))
        elif clean and builds is False:
            verdict = ("MERGES WITHOUT CONFLICT BUT THE MERGED RESULT DOES NOT BUILD. The "
                       "branch builds alone and the combination does not, which is what a "
                       "bot-branch attestation cannot see — usually one side renamed what "
                       "the other side calls. Do not promote this; fix it on the branch.")
        elif clean is False:
            verdict = ("CONFLICTS WITH TESTING in %d file(s): %s. git-gate would abort the "
                       "promotion whole and leave testing untouched, so nothing is at risk "
                       "— but it cannot be promoted until the branch is updated."
                       % (len(data.get("conflicts") or []),
                          ", ".join((data.get("conflicts") or [])[:12])))
        else:
            verdict = ("The merge was clean and no build ran, so whether the merged result "
                       "compiles is UNOBSERVED. Check the policy's verify commands.")

        if data.get("already_in_testing"):
            verdict = ("This branch is already contained in testing at %s — there is nothing "
                       "to land. " % data.get("testing_sha", "?")) + verdict
        if data.get("uncommitted_changes"):
            verdict += (" NOTE: the checkout has uncommitted changes, which were NOT part of "
                        "this merge. What was tested is the committed branch.")

        return ToolResult.ok({
            "repo": repo,
            "branch": data.get("branch"),
            "branch_sha": data.get("branch_sha"),
            "testing_branch": data.get("testing_branch"),
            "testing_sha": data.get("testing_sha"),
            "merges_clean": clean,
            "conflicts": data.get("conflicts") or [],
            "merged_builds_clean": builds,
            "merged_build": data.get("merged_build") or [],
            "node_modules": data.get("node_modules", ""),
            "already_in_testing": data.get("already_in_testing"),
            "verdict": verdict,
        })

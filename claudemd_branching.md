# Branching: One Track

Every ticket's code moves as ONE set of commits: cut from production, merged unchanged into
testing, then staging, then production. Testing and staging may run ahead of production with
tickets still in flight, and that is normal. Production never has a commit they lack.

## The branches
- **Production branch** per repo: `production` for entity-feature-data-loaders, `production-v2` for
  cenora-core-data-services, and the repo policy's `prod_branch` for any other repo. Never assume.
- **Ticket branch** `bot/CLC-<n>`: cut from the production branch, and it holds ONLY that
  ticket's commits. Every round of work, including QA rework, is a new commit on this same branch.
- **testing / staging** only RECEIVE. Never merge them into a ticket branch, into production, or
  into anything else.

## Never
- Cherry-pick, squash, or copy commits from one branch to another.
- Rebase or force-push a branch that has landed anywhere. To bring it up to date, MERGE
  origin/<production branch> into it (`repo_git op=update_base`).
- Edit another environment's version of a file. You edit the file as it is on production. If your
  change needs code that only testing or staging has, the ticket is waiting on that ticket (below).
- Resolve a conflict by judgement. A conflict either has an approved recipe, or you stop and ask.
- Commit work on a landing branch (`bot/CLC-<n>-testing` / `-staging`). Those carry merges only.

## Waiting on another ticket
Your change needs code that isn't on production yet when:
- your build fails on a symbol that only testing or staging has; or
- the ticket's own text says it builds on unshipped work.

Then do not build on top of it, and do not copy it. Move the unit to `waiting` and name
the ticket(s) it waits for (`fleet_unit_update state=waiting waiting_on=[CLC-...]`).
The fleet resumes you when they reach production. Your first step then is `repo_git op=update_base`.

## Landing on testing or staging
`repo_preflight env=testing` (and `env=staging` before a staging landing) merges your branch into
that environment in a scratch tree, builds the result, and reports:
- **clean**: `repo_git op=land ref=<env>` opens the PR from `bot/CLC-<n>` itself. A person merges it.
- **conflict**: preflight names the file, the hunk, and the ticket(s) whose unshipped commits touch it.
  - If an approved recipe covers that file and ticket, `land` applies it in a landing MERGE on
    `bot/CLC-<n>-<env>`. Your branch stays clean.
  - Otherwise `land` stops and returns a recipe id per file. Propose the resolved file and your
    reasoning with `repo_recipe op=propose`, then `needs_input`. A person approves it in the Inbox
    once; land again and it is applied, for that exact conflict only.
- **Shared repos** (policy `branching.shared: true`): if the conflicting hunk was last changed by
  anyone outside the fleet, there is no recipe and no automatic resolution. Stop and name the
  person and their PR.

Land in order: testing first, then staging after QA passes on testing. A PR that contains any file
your ticket did not change is wrong, and `land` refuses it.

## Production
The fleet never merges to production. A production PR's head is `bot/CLC-<n>`: the same branch
and commits that landed on staging. `repo_git op=pr base=<production branch>` refuses anything else, and anything not already on staging. If
production moved and the PR conflicts, `update_base` (merge), apply the recipe, and land again.

## Path fence (shared repos)
In a repo with `branching.fleet_paths`, you commit only under those paths. In
cenora-core-data-services that is `apps/agency-data-services/**` and the agency/direct libraries
the policy names. A commit outside them is refused: stop and ask.

## When something looks wrong
- A branch has commits that aren't your ticket's: stop, don't push.
- `land` or `preflight` reports drift (production commits missing from an environment): stop and
  report it. Never back-merge yourself. The fleet opens a back-merge PR (in a shared repo it only
  reports), and a person merges it.
- Two copies of the same change show up in history (older work): `ticket_precedent` groups them
  as one change. Trust the grouping, not the commit count.

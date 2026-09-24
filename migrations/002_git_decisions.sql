-- Every remote git decision, allowed or refused, with the reason.
--
-- WHY A TABLE AND NOT A LOG LINE. repo_git is the only path from an expert to a remote, so its
-- refusals are the highest-signal events in the fleet: "this bot tried to push an environment
-- branch", "this branch matches no work unit". Those went to stdout, which means they were
-- invisible to every screen, unqueryable, and gone on the next container restart.
--
-- A refusal nobody can see is indistinguishable from a refusal that never happened. The whole
-- reason the ref check moved to where the credential lives was so it could not be bypassed —
-- and a check that cannot be audited is only half of that.
--
-- ALLOWED DECISIONS ARE RECORDED TOO, deliberately. A table of refusals alone answers "what
-- was blocked" but not "what got through", and the second question is the one asked after
-- something unexpected lands in a repo. The rows are small and the write is off the hot path.

BEGIN;

CREATE TABLE IF NOT EXISTS repo_git_decisions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    repo         TEXT NOT NULL,              -- owner/repo as asked for
    op           TEXT NOT NULL,              -- fetch | push | promote
    ref          TEXT NOT NULL DEFAULT '',   -- the branch, verbatim as requested

    -- allowed  — the operation ran
    -- refused  — a rule said no. `reason` names which one.
    -- failed   — allowed, then git itself failed (network, non-fast-forward, ...)
    --
    -- 'refused' and 'failed' are kept apart because they mean opposite things about the
    -- caller: refused is the system working, failed is something to go and fix.
    decision     TEXT NOT NULL CHECK (decision IN ('allowed', 'refused', 'failed')),

    -- The full sentence the caller was given. Stored rather than a code, because the useful
    -- artifact months later is what the bot was actually told, not a category we can
    -- reconstruct it from.
    reason       TEXT NOT NULL DEFAULT '',

    -- Who asked. Empty when the caller did not identify itself — which is itself worth
    -- seeing, since the HTTP route currently has no caller authentication.
    profile_slug TEXT NOT NULL DEFAULT '',

    -- Parsed out of the ref when it is a bot branch, so a ticket's whole history — including
    -- the pushes that were refused — can be found without a LIKE over every row.
    ticket       TEXT NOT NULL DEFAULT '',

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The two questions actually asked of this table: "what happened to this ticket" and
-- "what has been refused lately".
CREATE INDEX IF NOT EXISTS repo_git_decisions_ticket
    ON repo_git_decisions (ticket, created_at DESC) WHERE ticket <> '';

CREATE INDEX IF NOT EXISTS repo_git_decisions_refused
    ON repo_git_decisions (created_at DESC) WHERE decision <> 'allowed';

CREATE INDEX IF NOT EXISTS repo_git_decisions_repo
    ON repo_git_decisions (repo, created_at DESC);

COMMENT ON TABLE repo_git_decisions IS
    'Every remote git decision made by repo_git. Refusals are the point: they are the highest-'
    'signal events in the fleet and previously existed only as stdout.';

COMMENT ON COLUMN repo_git_decisions.decision IS
    'refused = a rule said no (the system working). failed = allowed, then git failed '
    '(something to fix). Kept apart because they mean opposite things about the caller.';

COMMIT;

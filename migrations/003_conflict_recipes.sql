-- 003: conflict recipes (One Track, fleet triage 2026-11-25, rule R6 / mechanism M8).
--
-- A bot never improvises a conflict resolution. `repo_git op=land` records a conflict it cannot
-- merge cleanly; the dev or the coordinator proposes the resolved file and the reason; a person
-- approves it; and from then on an IDENTICAL conflict (same ancestor, same environment side,
-- same ticket side, byte for byte) is resolved by replaying the approved result. Anything not
-- identical stops again: exactness over convenience, because a recipe applied to a different
-- conflict is exactly the improvisation this exists to remove.
--
-- Auditable by construction (D15): who hit it, who proposed what and why, who approved it, and
-- every time it was used.

CREATE TABLE IF NOT EXISTS repo_conflict_recipes (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    repo           text NOT NULL,                 -- owner/repo
    path           text NOT NULL,                 -- repository-relative file
    ticket         text NOT NULL,                 -- the ticket whose landing hit it
    env            text NOT NULL,                 -- testing | staging | production
    -- The exact inputs, as git blob ids. '' for a side where the file did not exist.
    base_blob      text NOT NULL DEFAULT '',
    ours_blob      text NOT NULL DEFAULT '',      -- the environment's version
    theirs_blob    text NOT NULL DEFAULT '',      -- the ticket branch's version
    conflict       text NOT NULL DEFAULT '',      -- the conflicted text, as git produced it
    other_tickets  text[] NOT NULL DEFAULT '{}',  -- unshipped tickets that changed the file on env
    other_authors  text[] NOT NULL DEFAULT '{}',  -- their authors (shared-repo ownership)
    state          text NOT NULL DEFAULT 'conflict', -- conflict | proposed | approved | rejected
    resolved       text NOT NULL DEFAULT '',      -- the resolved file, when proposed
    reason         text NOT NULL DEFAULT '',      -- why that resolution is right
    proposed_by    text NOT NULL DEFAULT '',
    proposed_at    timestamptz,
    decided_by     text NOT NULL DEFAULT '',      -- the person who approved or rejected it
    decided_at     timestamptz,
    decision_note  text NOT NULL DEFAULT '',
    used_count     integer NOT NULL DEFAULT 0,
    last_used_at   timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- One record per exact conflict: hitting it again finds the same row.
CREATE UNIQUE INDEX IF NOT EXISTS repo_conflict_recipes_exact
    ON repo_conflict_recipes (repo, path, base_blob, ours_blob, theirs_blob);
CREATE INDEX IF NOT EXISTS repo_conflict_recipes_open
    ON repo_conflict_recipes (state) WHERE state IN ('conflict', 'proposed');

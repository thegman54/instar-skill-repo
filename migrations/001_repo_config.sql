-- Repo skill — repository access configuration
-- Controls which repos the bot can clone/access and what operations are allowed

CREATE TABLE IF NOT EXISTS skill_repo_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo TEXT NOT NULL UNIQUE,              -- "owner/repo" format
    label TEXT,                             -- friendly display name
    access TEXT NOT NULL DEFAULT 'full',     -- full, read-only, no-push
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_skill_repo_config_repo ON skill_repo_config(repo);
CREATE INDEX IF NOT EXISTS idx_skill_repo_config_enabled ON skill_repo_config(enabled);

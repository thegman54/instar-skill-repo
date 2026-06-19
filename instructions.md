# Repository Tools

You have full git repository access through these tools. All operations require a `repo` parameter in `owner/repo` format.

## Workflow

1. **Clone first**: `repo_clone(repo="owner/repo")` — clones into workspace. If already cloned, pulls latest.
2. **Work**: Read, write, grep, list files. Stage and commit changes.
3. **Push & PR**: Push your branch, then create a PR.

## Tools

| Tool | Purpose |
|------|---------|
| `repo_clone` | Clone a repo (or pull if already cloned) |
| `repo_list` | List files/dirs with optional glob |
| `repo_read` | Read file contents with line range |
| `repo_write` | Write/create files |
| `repo_grep` | Search file contents with regex |
| `repo_status` | Git status (staged/modified/untracked) |
| `repo_diff` | Git diff (staged or unstaged) |
| `repo_commit` | Stage specific files and commit |
| `repo_branch` | List/create/switch branches |
| `repo_log` | View commit history |
| `repo_push` | Push to remote (authenticated) |
| `repo_pr` | Create a pull request via GitHub API |
| `repo_actions` | Check GitHub Actions CI status |

## Rules

- Always clone before any other operation on a repo
- Always specify individual files when committing — never use blanket staging
- Create a feature branch before making changes
- Push before creating a PR
- The `repo` param must match the `allowed_repos` in your grant

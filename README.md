# Instar Skill: Repository

A [Project Instar](https://github.com/thegman54/project-instar) skill that gives the bot controlled, path-validated access to git repositories. Read files, write code, commit, push, create PRs, and manage branches — all scoped to an allowed project directory.

## Tools

| Tool | Permission Level | Description |
|---|---|---|
| `repo_list` | Read | List files and directories |
| `repo_read` | Read | Read file contents |
| `repo_write` | Write | Create or modify files |
| `repo_status` | Read | Git status (staged, modified, untracked) |
| `repo_diff` | Read | Git diff (working tree or between refs) |
| `repo_commit` | Write | Stage and commit changes |
| `repo_push` | Write | Push commits to remote |
| `repo_pr` | Write | Create pull requests via GitHub CLI |
| `repo_log` | Read | Git log with filters |
| `repo_branch` | Write | Create, switch, list, delete branches |
| `repo_actions` | Read | Check GitHub Actions workflow status |

## Security

All file operations go through `validate_path()` which:
- Resolves symlinks and `..` traversal attempts
- Ensures the resolved path stays within the grant's `project_root`
- Blocks access to the Instar installation itself (self-modification forbidden)

The bot never sees file paths outside its allowed project boundary.

## How It Works

1. Install this skill via the Admin UI
2. Create a grant that includes the repo tools you want to allow
3. Set the `project_root` grant metadata to the filesystem path of the target repo
4. The bot can now operate on that repo within its permission boundary

## Grant Metadata

| Key | Description |
|---|---|
| `project_root` | Filesystem path to the git repository (e.g. `/projects/my-app`) |

No external credentials needed — the tool operates on locally mounted repositories.

## File Structure

```
repo/
├── manifest.yaml       # Skill metadata + grant config
├── __init__.py          # Imports all tool classes
├── base.py              # Path validation + security utilities
├── repo_list.py         # List files/directories
├── repo_read.py         # Read file contents
├── repo_write.py        # Write/create files
├── repo_status.py       # Git status
├── repo_diff.py         # Git diff
├── repo_commit.py       # Git commit
├── repo_push.py         # Git push
├── repo_pr.py           # GitHub PR creation
├── repo_log.py          # Git log
├── repo_branch.py       # Branch management
└── repo_actions.py      # GitHub Actions status
```

## Installation

### Via Admin UI (Recommended)

1. Download this repo as a zip
2. In the Instar Admin UI, go to **Tools**
3. Click **Upload** and select the zip
4. Create grants with `project_root` pointing to the repo you want the bot to access

### Manual

```bash
git clone https://github.com/thegman54/instar-skill-repo.git
cp -r instar-skill-repo/ /path/to/project-instar/tool-executor/src/tools/repo/
```

## License

MIT

# friction-loop

Standalone extraction of the agent friction-note loop (from `../agent-feedback-loop`). Agents file a one-line note when the docs failed them; git hooks record each commit on a hidden `fbl-insights` branch; a weekly report groups recurring gaps.

## Install

Plain script (needs git, python3; `jsonschema` is pip-installed if missing):

```sh
./install.sh --repo /path/to/project --domain my-team [--publish] [--adapter boundary|codex|none] [--home DIR] [--no-agents-md]
```

It copies the tool to `~/.local/share/fbl`, creates `.fbl/bin/fbl-friction`, installs `post-commit` and `pre-push` hooks (refuses to overwrite existing ones), creates the insights branch, and appends the reporting block to `AGENTS.md` (uncommitted, for review). `--publish` pushes the branch to the remote.

## As an APM package

```sh
apm install <owner>/friction-loop --target claude     # once this folder is pushed to a git repo
apm install /path/to/friction-tool --target claude    # or from this folder
```

This deploys one skill, `friction-loop`, to `.claude/skills/` (other `--target` values work too). The skill tells agents how to file notes and bundles `install.sh` and the tool, so the user can then ask the agent to set the loop up, or run `sh .claude/skills/friction-loop/install.sh --repo .`. APM only copies files; it cannot run the hook installation, so that step stays explicit.

## Friction origins

Notes take `--origin` (where the problem started): `repository-discovery`, `task-context`, `execution-environment`, `verification`, `conflicting-knowledge`. The report groups by origin when present, else by the older `--category`, so existing records and `AGENTS.md` blocks keep working. `task-context` is reported as informational because the fix is outside the repository.

## Layout

```
install.sh                         wrapper
apm.yml                            APM manifest
.apm/skills/friction-loop/
  SKILL.md                         agent instructions
  install.sh                       real installer
  tool/                            scripts, hooks, schemas, weekly workflow template, tests
```

Tests: `cd .apm/skills/friction-loop/tool && uv run --with pytest --with jsonschema pytest -q` (117 pass).

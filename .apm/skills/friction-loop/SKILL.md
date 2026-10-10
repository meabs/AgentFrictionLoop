---
name: friction-loop
description: Report friction (blocked or slowed work caused by missing, unclear or out-of-order docs) with a one-line note before finishing a task. Use when something in the repo setup, tests, build or docs cost you time, or when asked to install the agent feedback loop in a repository.
---

# Friction loop

Agents file a one-line note when the repo's docs failed them. Git hooks turn each commit into an analysis record on the hidden `fbl-insights` branch; a weekly report groups recurring gaps so a person can fix the docs.

## Reporting (any repo that has the loop installed)

If `./.fbl/bin/fbl-friction` (`.\.fbl\bin\fbl-friction.cmd` in PowerShell/cmd) exists and something blocked or slowed you down, run this before finishing, even if you worked around it:

```sh
./.fbl/bin/fbl-friction --origin <origin> "<what happened and what fixed it, one line, no secrets>"
```

Pick the origin by asking "where did the problem start?":

- `repository-discovery`: the contract, doc or entry point exists but you could not find it.
- `task-context`: the task lacked an acceptance criterion, constraint or input you needed.
- `execution-environment`: a prerequisite, migration, tool or service was missing or had to run in a certain order.
- `verification`: you could not tell whether your change was correct (no fast test, flaky or missing fixtures).
- `conflicting-knowledge`: two sources disagreed, or a doc contradicted the code. Say which one you trusted.

If `./.fbl/bin/fbl-friction` does not exist (cloud agents and CI have no launcher), add trailer lines to your commit message instead, one per note: `Friction[<origin>]: <one line, no secrets>`.

For a machine fault no doc could prevent (disk full, no network, permissions) use `--category environment` instead. The older `--category` values (`test-order`, `missing-doc`, `unclear-instruction`, `setup`, `environment`, `other`) still work, alone or together with `--origin`.

One note per root cause, at most 3. Write nothing if nothing went wrong. Notes over 240 characters are truncated; anything that looks like a secret is dropped. A commit trailer `Friction[category]: text` also works.

## Installing (only when the user asks)

```sh
python <this-skill-dir>/install.py --repo <project> --domain <team-name> [--publish]
# macOS/Linux also: sh install.sh ...   Windows PowerShell also: .\install.ps1 ...
```

This copies the tool to `~/.local/share/fbl` (`%LOCALAPPDATA%\fbl` on Windows), creates `.fbl/bin/fbl-friction`, installs `post-commit` and `pre-push` hooks (it refuses to overwrite existing hooks), creates the insights branch, and appends the reporting block to `AGENTS.md` for the user to review and commit. Needs git (Git for Windows on Windows, which supplies the `sh` that runs the hooks), Python 3, and `jsonschema` (installed with pip if missing). Do not run `--publish` without the user's go-ahead: it pushes a branch to the remote.

Records contain changed-path names, counts and a pseudonymous actor id; never prompts, file contents or commit messages. See `tool/docs/TRANSPARENCY.md`.

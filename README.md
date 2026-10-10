# Agent Friction Loop

Find out where your repository gets in the way of coding agents, and fix the docs that cause it.

Agents (Claude Code, Codex, Cursor, Copilot) lose time to the same few things: a migration nobody wrote down, a design doc that contradicts the code, a contract they cannot find. This tool makes those moments visible. The agent files a one-line note when something slows it down, a git hook attaches the note to the commit, and a weekly report groups the notes so a person can fix the doc gap that keeps costing time. Nothing edits your repository automatically.

[Interactive explainer](docs/friction-loop.html) (download and open locally; it has an Export as HTML button when viewed as a Claude artifact)

```
 agent                       git hooks                       GitHub Action            you
 ─────                       ─────────                       ─────────────            ───
 fbl-friction --origin …  →  post-commit: note + commit  →   weekly.py groups   →     fix AGENTS.md /
 "one line, no secrets"      become one JSON record on       records, opens an        docs in a normal
                             the hidden fbl-insights         issue only when the      pull request
                             branch; pre-push syncs it       same gap recurs
```

## Contents

- [Quick start](#quick-start)
- [What an agent reports](#what-an-agent-reports)
- [What the installer does](#what-the-installer-does)
- [Sample output](#sample-output)
- [Configuring AGENTS.md](#configuring-agentsmd)
- [The weekly review](#the-weekly-review)
- [Privacy](#privacy)
- [Install as an APM package](#install-as-an-apm-package)
- [Platforms](#platforms)
- [Commands](#commands)
- [Limits](#limits)
- [Repository layout and development](#repository-layout-and-development)

## Quick start

You need git and Python 3. On Windows, install [Git for Windows](https://gitforwindows.org/), which provides the `sh` that runs git hooks. The installer adds the `jsonschema` package with pip if it is missing.

```sh
# macOS / Linux / Git Bash
./install.sh --repo ~/code/my-project --domain my-team
```

```powershell
# Windows PowerShell
.\install.ps1 --repo C:\code\my-project --domain my-team
```

Then review and commit the block the installer appended to `AGENTS.md`, and add `--publish` once per project (not per clone) to push the insights branch so everyone shares one actor salt:

```sh
./install.sh --repo ~/code/my-project --domain my-team --publish
```

Every clone needs its own install, because hooks and the launcher live in `.git` and `.fbl/`, which git does not share.

Try it:

```sh
cd ~/code/my-project
./.fbl/bin/fbl-friction --origin execution-environment "tests fail until make db-migrate has run"
git commit --allow-empty -m "try the loop"
git log fbl-insights --oneline        # a "record my-project@…" commit appears within seconds
```

## What an agent reports

Each note has an **origin**: where the problem started, which decides who can fix it. The agent answers one question, "where did the problem start?"

| Origin | Example | Remedy the report suggests |
| --- | --- | --- |
| `repository-discovery` | The agent cannot find a contract that exists | Improve navigation or indexing; link it from `AGENTS.md` |
| `task-context` | The brief had no acceptance criterion | Improve how tasks are briefed (usually outside the repo) |
| `execution-environment` | A migration had to run before the tests | Fix the tooling, or document the prerequisite and its order |
| `verification` | No fast way to tell whether the change works | Improve tests or test infrastructure |
| `conflicting-knowledge` | An old design doc contradicts the code | Correct provenance or authority; say which source wins |

```sh
./.fbl/bin/fbl-friction --origin <origin> "<what happened and what fixed it, one line, no secrets>"
.\.fbl\bin\fbl-friction.cmd --origin <origin> "<...>"      # PowerShell / cmd.exe
```

- The older `--category` (`test-order`, `missing-doc`, `unclear-instruction`, `setup`, `environment`, `tooling`, `other`) still works, alone or together with `--origin`. With only `--origin`, the category is filled in for older readers.
- A note with an origin is grouped by it in the report; a note without one is grouped by its category. Existing records and `AGENTS.md` blocks keep working.
- Use `--category environment` alone for a machine fault no doc could prevent (disk full, no network).
- `task-context` notes are reported as informational: the repository cannot fix them, so they do not become a docs recommendation.
- Notes are cut to 240 characters, control characters are removed, and anything that looks like a secret is dropped, not stored.
- One note per root cause, at most three per session. A commit trailer `Friction[category]: text` also works.

## What the installer does

`install.py` (wrapped by `install.sh` and `install.ps1`) changes three places and commits or pushes nothing unless you pass `--publish`.

| Step | What happens | Where |
| --- | --- | --- |
| Check | Confirms git and Python 3, finds the repo root, pip-installs `jsonschema` if missing | nowhere |
| Copy the tool | Replaces scripts, hooks, schemas and templates; safe to re-run to upgrade | `~/.local/share/fbl` or `%LOCALAPPDATA%\fbl` |
| Launcher | Writes `fbl-friction` (sh) and `fbl-friction.cmd`; adds `.fbl/` to `.git/info/exclude` | `<project>/.fbl/bin/` |
| Insights branch | Creates the orphan branch `fbl-insights` as a linked worktree, or adopts the remote one | `.git/fbl/insights` |
| Hooks | Writes `post-commit` and `pre-push`; stops with an error rather than overwrite a different existing hook | `.git/hooks/` |
| Publish (optional) | Commits the shared actor salt and schema, pushes the branch | the remote |
| AGENTS.md | Appends the friction block unless a `## Friction reporting` heading exists; left uncommitted | `AGENTS.md` |

Options: `--repo` (default `.`), `--domain` (team label stored with each record), `--adapter boundary|codex|boundary,codex|none` (`codex` also reads local Codex session files for token and cost figures), `--home` (tool location), `--publish`, `--no-agents-md`.

Hooks run in the background and never block a commit or push. Failures are logged to `var/hook.log` inside the tool folder. If the network is down, records stay committed locally and go out with the next push.

Re-running the installer with the same `--domain` is safe. A different `--domain` on a repo that already has the hooks stops with "refusing to overwrite existing hook": delete the two hooks first.

## Sample output

Captured from real runs on a scratch repository, with long paths shortened.

**Installer**

```
$ ./install.sh --repo . --domain my-team
tool installed at /Users/you/.local/share/fbl
installed /code/my-project/.fbl/bin/fbl-friction
insights worktree: /code/my-project/.git/fbl/insights
installed post-commit hook
installed pre-push hook
added friction block to /code/my-project/AGENTS.md (review and commit it)
```

**An agent files a note**

```
$ ./.fbl/bin/fbl-friction --origin execution-environment "pytest failed until I ran make db-migrate"
friction recorded: execution-environment / setup

$ ./.fbl/bin/fbl-friction --origin task-context "password=hunter2"
friction note dropped: looks like a secret
```

Read the raw notes with `python boundary.py events --repo .`:

```json
{"category": "setup", "origin": "execution-environment", "harness": "claude-code",
 "session": {"id": "89a547fb-…", "source": "env"},
 "text": "pytest failed until I ran make db-migrate", "ts": "2026-10-09T22:38:05.965Z", "type": "friction"}
```

The harness and session id come from the agent's environment variables, and are `unknown` when absent.

**A commit becomes a record** on `fbl-insights`, at `records/my-team/my-project/2026-10/4c4e65e5cff5.json`:

```json
{
  "actor": { "id": "03ff3f5e04ef", "kind": "human", "scheme": "hmac-sha256/v1" },
  "agent": { "harness": "claude-code" },
  "changed_files": [ { "path": "a", "status": "A" } ],
  "diff_stats": { "files": 1, "insertions": 1, "deletions": 0 },
  "friction": [ { "category": "setup", "origin": "execution-environment", "source": "event",
                  "text": "pytest failed until I ran make db-migrate" } ],
  "guidance": { "digest": "e3b0c442…", "guidance_changed": false, "agents_md_referenced_paths": [] }
}
```

**Weekly report** (`python weekly.py --insights .git/fbl/insights`):

```
## Repeated friction
- F-001: Agents self-reported 'conflicting-knowledge' friction in 1 sessions. Scope: concentrated;
  distinct actors: 1; actor share: 100%. Evidence: 1 records (my-project@54cacb36048a).
- F-003: Informational (no recommendation without a linked failure): Agents self-reported
  'task-context' friction in 1 sessions. ...
```

On a larger sample (17 records) the same report also lists verification failures (`First tests verification attempt failed in 10 sessions. Dominant failing command: 'uv run pytest'`) and `AGENTS.md` references agents never opened.

## Configuring AGENTS.md

The file is `AGENTS.md` at the repository root. The installer appends this block:

```text
## Friction reporting

If something blocked or slowed you down, report it before finishing, even if you worked around it:

./.fbl/bin/fbl-friction --origin <origin> "<what happened and what fixed it, one line, no secrets>"

On Windows PowerShell or cmd.exe use `.\.fbl\bin\fbl-friction.cmd` instead.

Pick the origin by asking "where did the problem start?":
- repository-discovery: the contract, doc or entry point exists but you could not find it.
- task-context: the task lacked an acceptance criterion, constraint or input you needed.
- execution-environment: a prerequisite, migration, tool or service was missing or had to run in a certain order.
- verification: you could not tell whether your change was correct (no fast test, flaky or missing fixtures).
- conflicting-knowledge: two sources disagreed, or a doc contradicted the code. Say which one you trusted.

For a machine fault no doc could prevent (disk full, no network, permissions) use `--category environment` instead of `--origin`.

One note per root cause, at most 3 notes. Write nothing if nothing went wrong.
```

What you still need to do:

- **Commit the block.** It is left uncommitted; teammates' agents only see it once merged.
- **Add `@AGENTS.md` to `CLAUDE.md`.** Claude Code reads `CLAUDE.md`, not `AGENTS.md`. Codex and Cursor read `AGENTS.md` directly.
- **Keep the launcher path as written.** If a clone has not run the installer, the command is not found; tell new contributors to run it.
- **Reference your docs from `AGENTS.md`.** The report compares the paths it links to against the files agents actually opened.
- **Expect a new "epoch" when guidance changes.** Edits to `AGENTS.md`, `CLAUDE.md`, `service.yaml` or anything under `.claude/skills/` or `.agents/skills/` change the guidance digest, which before/after comparisons use.
- **Tell agents to name both sources** for `conflicting-knowledge`, and which one they trusted. Agents are poor at spotting contradictions unprompted, so expect fewer of these notes than the true number.

## The weekly review

Copy `.apm/skills/friction-loop/tool/templates/fbl-weekly.yml` (also installed to the tool folder) to `.github/workflows/` in your project and set the repository variables `FBL_TOOL_REPOSITORY` (where this tool is hosted, e.g. `meabs/AgentFrictionLoop`) and `FBL_TOOL_REF` (a tag or branch). It runs `weekly.py`, which builds the report with `review.py` and opens an issue with `gh issue create` only when the recommended set has changed.

A finding becomes a recommendation only when enough records and more than one person back it. Smaller ones are labelled "Not established", so one agent's bad day does not become advice.

## Privacy

Records hold changed-path names, line counts, a digest of the guidance files, the friction notes, and a pseudonymous actor id (the first 12 hex characters of an HMAC of the lowercased author email, keyed with a salt stored on the insights branch). They never hold prompts, transcripts, file contents, commit messages, author names or emails. The pseudonym lets the report count distinct people; it is not anonymous, because anyone with the salt can test a guess. Reports show actor counts and shares, never ids. Details: [TRANSPARENCY.md](.apm/skills/friction-loop/tool/docs/TRANSPARENCY.md).

## Install as an APM package

This folder is an [Agent Package Manager](https://microsoft.github.io/apm/) package that ships one skill, `friction-loop`. The skill teaches agents when and how to file a note and bundles the installer.

```sh
apm install meabs/AgentFrictionLoop --target claude     # or: apm install ./friction-tool --target claude
```

APM only copies files, so it cannot install hooks. After `apm install`, ask the agent to set the loop up, or run `python .claude/skills/friction-loop/install.py --repo .`. The skill lands in `.claude/skills/`, which counts as guidance, so installing it starts a new epoch in before/after comparisons. `--target` is required (APM 0.13 no longer defaults).

## Platforms

| Platform | State |
| --- | --- |
| macOS | All tests pass; install, note, commit and record verified end to end |
| Linux | Tests pass in CI (Ubuntu) |
| Windows | CI runs the tests and an install smoke test on `windows-latest`; see the Actions tab for the current result |

Windows differences: file locking uses `msvcrt` instead of `fcntl`; git runs with an empty hooks directory instead of `/dev/null`; hook scripts get forward-slash paths and the Python interpreter recorded at install time; `fbl-friction.cmd` serves PowerShell and cmd.exe.

## Commands

| Command | What it does |
| --- | --- |
| `boundary.py install --repo R` | Creates `.fbl/bin/fbl-friction[.cmd]` and excludes `.fbl/` from git |
| `boundary.py friction [--category C] [--origin O] TEXT` | Records one note (the launcher calls this) |
| `boundary.py events --repo R [--json]` | Prints the notes recorded for a repository |
| `insights_sync.py init [--publish]` | Creates or adopts the insights branch |
| `insights_sync.py install --domain D [--adapter A]` | Installs the `post-commit` and `pre-push` hooks |
| `insights_sync.py push` | Fetch, rebase and push the insights branch |
| `analyse-change.py --repo R --commit SHA --insights I` | Builds one record (the hook calls this) |
| `review.py --insights I report` | Builds the review report |
| `review.py --insights I mine --author-email E` | Prints only your own records |
| `weekly.py --insights I [--push]` | Report plus issue body, as the workflow runs it |

## Limits

- Cloud-hosted agents are not covered: the hooks only run on a developer's machine.
- Notes are self-reported. Treat them as leads unless a command failure or transcript signal agrees.
- Five origins split a small sample thin; with few records most findings stay "Not established".
- The check that keeps note text out of the report is a substring match, so a very short note (for example `x`) can make `review.py` fail with "report contains friction sample text".
- The weekly workflow and `gh issue create` have not been run on GitHub.
- Claude Code and Copilot sessions have had less testing than Codex and Cursor.

## Repository layout and development

```
install.sh, install.ps1            wrappers for install.py
apm.yml                            APM manifest
docs/friction-loop.html            explainer page
.github/workflows/test.yml         tests on Ubuntu, macOS, Windows + install smoke test
.apm/skills/friction-loop/
  SKILL.md                         agent instructions
  install.py                       cross-platform installer
  tool/                            boundary.py, insights_sync.py, analyse-change.py, review.py, weekly.py,
                                   feedback_loop/, hooks/, schemas/, templates/, docs/, tests/
```

```sh
cd .apm/skills/friction-loop/tool
uv run --with pytest --with jsonschema pytest -q      # or: pip install pytest jsonschema && python -m pytest -q
```

Extracted from an experiment on agent-ready repositories; the full build history lives in the original `agent-feedback-loop` project.

# Agent Friction Loop

**Version 0.2.0.** Find out where your repository gets in the way of coding agents, and fix the docs that cause it.

Agents (Claude Code, Codex, Cursor, GitHub Copilot) lose time to the same few things: a migration nobody wrote down, a design doc that contradicts the code, a contract they cannot find. This tool makes those moments visible. The agent files a one-line note when something slows it down, a git hook attaches the note to the commit, and a weekly report groups the notes so a person can fix the doc gap that keeps costing time. Nothing edits your repository automatically.

[Interactive explainer](https://meabs.github.io/AgentFrictionLoop/): how the loop works, sample outputs and the AGENTS.md block.

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
- [Harnesses](#harnesses)
- [Existing hooks and uninstalling](#existing-hooks-and-uninstalling)
- [Platforms](#platforms)
- [Commands](#commands)
- [Limits](#limits)
- [Roadmap](#roadmap)
- [Repository layout and development](#repository-layout-and-development)
- [License](#license)

## Quick start

You need git and Python 3.10 or newer (CI runs 3.10, 3.12 and 3.13). On Windows, install [Git for Windows](https://gitforwindows.org/), which provides the `sh` that runs git hooks. The installer adds the `jsonschema` package with pip if it is missing.

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
| Hooks | Writes `post-commit` and `pre-push`; updates hooks it wrote earlier, stops rather than overwrite one it did not write, refuses if `core.hooksPath` is set | `.git/hooks/` |
| Publish (optional) | Commits the shared actor salt and schema, pushes the branch | the remote |
| Command capture | Writes hook configs that send each shell command's result to `boundary.py command`: `.claude/settings.local.json`, `.cursor/hooks.json`, `.github/hooks/fbl-commands.json`. Existing settings are merged, tracked files are left alone, and all three are excluded from git | project root |
| Instructions | Appends the friction block to `AGENTS.md`, `.github/copilot-instructions.md` and `.cursor/rules/friction-reporting.mdc`, and adds `@AGENTS.md` to `CLAUDE.md`. Skips any file that already has a `## Friction reporting` heading; everything is left uncommitted | project root |

Options: `--no-command-capture` (skip the shell-command hooks), `--uninstall` (see below), `--repo` (default `.`), `--harness` (extra instruction files: `claude`, `copilot`, `cursor`, or `none`; default all three), `--domain` (team label stored with each record), `--adapter boundary|codex|boundary,codex|none` (`codex` also reads local Codex session files for token and cost figures), `--home` (tool location), `--publish`, `--no-agents-md`.

Hooks run in the background and never block a commit or push. Failures are logged to `var/hook.log` inside the tool folder. If the network is down, records stay committed locally and go out with the next push.

Re-running the installer is safe: it updates the tool copy and its own hooks, and leaves hooks it did not write alone.

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

If `./.fbl/bin/fbl-friction` does not exist (cloud agents and CI have no launcher), add trailer lines to your commit message instead, one per note:

Friction[<origin>]: <what happened and what fixed it, one line, no secrets>

For a machine fault no doc could prevent (disk full, no network, permissions) use `--category environment` instead of `--origin`.

One note per root cause, at most 3 notes. Write nothing if nothing went wrong. You may instead add a commit trailer: `Friction[category]: text`.
```

What you still need to do:

- **Commit the block.** It is left uncommitted; teammates' agents only see it once merged.
- **`CLAUDE.md`.** Claude Code reads `CLAUDE.md`, not `AGENTS.md`; the installer adds an `@AGENTS.md` import. Codex and Cursor read `AGENTS.md` directly.
- **Keep the launcher path as written.** If a clone has not run the installer, the command is not found; tell new contributors to run it.
- **Reference your docs from `AGENTS.md`.** The report compares the paths it links to against the files agents actually opened.
- **Expect a new "epoch" when guidance changes.** Edits to `AGENTS.md`, `CLAUDE.md`, `service.yaml` or anything under `.claude/skills/` or `.agents/skills/` change the guidance digest, which before/after comparisons use.
- **Tell agents to name both sources** for `conflicting-knowledge`, and which one they trusted. Agents are poor at spotting contradictions unprompted, so expect fewer of these notes than the true number.

## The weekly review

Copy `fbl-weekly.yml` from `.apm/skills/friction-loop/tool/templates/` (also installed to the tool folder) to `.github/workflows/` in your project and set the repository variables `FBL_TOOL_REPOSITORY` (where this tool is hosted, e.g. `meabs/AgentFrictionLoop`) and `FBL_TOOL_REF` (a tag or branch). It runs `weekly.py`, which builds the report with `review.py` and opens an issue with `gh issue create` only when the set of real recommendations has changed. Findings that sit with one or two people stay in the report and are left out of the issue.

Set the repository variable `FBL_ISSUE_NOTES` to `true` to add the agents' note text to the issue body (the report on the branch never contains note text). A finding becomes a recommendation only when enough records and more than one person back it. Smaller ones are labelled "Not established", so one agent's bad day does not become advice.

## Privacy

Records hold changed-path names, line counts, a digest of the guidance files, the friction notes, and a pseudonymous actor id (the first 12 hex characters of an HMAC of the lowercased author email, keyed with a salt stored on the insights branch). They never hold prompts, transcripts, file contents, commit messages, author names or emails. The pseudonym lets the report count distinct people; it is not anonymous, because anyone with the salt can test a guess. Reports show actor counts and shares, never ids. Details: [TRANSPARENCY.md](.apm/skills/friction-loop/tool/docs/TRANSPARENCY.md).

## Install as an APM package

This folder is an [Agent Package Manager](https://microsoft.github.io/apm/) package that ships one skill, `friction-loop`. The skill teaches agents when and how to file a note and bundles the installer.

```sh
apm install meabs/AgentFrictionLoop --target claude     # or: apm install ./friction-tool --target claude
```

APM only copies files, so it cannot install hooks. After `apm install`, ask the agent to set the loop up, or run `python .claude/skills/friction-loop/install.py --repo .`. The skill lands in `.claude/skills/`, which counts as guidance, so installing it starts a new epoch in before/after comparisons. `--target` is required (APM 0.13 no longer defaults).

## Harnesses

The note path does not depend on a particular agent: it is a command line call plus an instruction file, so any agent that can read repo instructions and run a shell command can use it. The installer writes the instructions where each tool looks (`--harness claude,copilot,cursor`, all by default; `none` writes only `AGENTS.md`).

| Harness | Reads instructions from | Files notes with | Detected as | Extra signal | Tested |
| --- | --- | --- | --- | --- | --- |
| Claude Code | `CLAUDE.md` (installer adds `@AGENTS.md`) | `fbl-friction` launcher | yes (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`) | shell-command results from a `PostToolUse` hook | notes and command capture verified in a live `claude -p` session |
| Codex CLI | `AGENTS.md` | launcher | yes (`CODEX_THREAD_ID`, `CODEX_SESSION_ID`, `CODEX_CI`) | token, cost and command failures from local session files with `--adapter codex` | real sessions in the original experiment |
| Cursor | `AGENTS.md` and `.cursor/rules/friction-reporting.mdc` (always applied) | launcher | yes (`CURSOR_AGENT`, `CURSOR_CONVERSATION_ID`) | shell-command results from `postToolUse` and `postToolUseFailure` hooks | notes: real sessions in the original experiment. Command capture verified in a live `cursor-agent` session |
| GitHub Copilot CLI (VS Code agent mode has no documented hooks) | `.github/copilot-instructions.md` (Copilot also reads `AGENTS.md` and `CLAUDE.md`) | launcher | no; set `FBL_HARNESS=copilot` to label it | shell-command results from a `postToolUse` hook; the exit code is read from the result text | command capture verified in a live Copilot CLI session; instruction file not tested |
| Cloud agents, experimental (Copilot coding agent, Codex cloud, Cursor background agents) | whichever file the product reads, same as above | commit trailers, no launcher | no (recorded as `unknown`) | none | trailer parsing is tested; `fbl-record.yml` ran on GitHub Actions in a private test repo, fed by an agent-style commit I made, not by a real Copilot or Codex cloud run |

Detection only labels the record; a harness that is not detected is recorded as `unknown` and everything else still works.

### Command capture

Notes depend on the agent remembering to write one. Command capture adds an objective signal: each harness's after-shell-command hook calls `boundary.py command`, which stores an event with the command's leading words, its exit code and a kind (`tests`, `lint`, `build`, `migrate`, `install`, `git`, `other`). The record then shows failed commands and whether the first test run passed, even when no note was filed.

What is stored is deliberately small. Output is discarded before anything is written. The signature is the executable plus up to two plain lowercase words, stopping at the first flag, so `uv run pytest -x tests/secret_name.py` becomes `uv run pytest`. For anything that is not a recognised test, lint, build, migrate or install step, only the executable name is kept, so `mycli login hunter2` is stored as `mycli`. Commands in quotes are never split open. The hook is silent and always exits 0, so it cannot disturb the agent.

Each integration was checked against a live session, and each one differed from its documentation or from what a reasonable guess would be:

| Harness | Hooks used | What the live session showed |
| --- | --- | --- |
| Claude Code | `PostToolUse`, `PostToolUseFailure` (matcher `Bash`) | There is no `exit_code` field. A failure arrives as `PostToolUseFailure` with `error: "Exit code N …"`; the parser reads that number and nothing else from the text. |
| Cursor | `postToolUse`, `postToolUseFailure` | `afterShellExecution` fires for successes and failures alike and carries no exit code, so it is not used. Failures give the code in `error_message` when Cursor words it that way, otherwise the command's output, in which case the code is recorded as 1. Duration arrives as a float. |
| Copilot CLI | `postToolUse` | A failed command still arrives as `resultType: "success"`, and `postToolUseFailure` does not fire for it. The exit code is only in the text, `<shellId: 0 completed with exit code 1>`, so the parser reads that trailer at the end of the text and ignores the same words inside command output. |

In all three, a failing `python3 -m pytest` and `false` were recorded with exit 1 and a passing command with 0, with the command's session id. The Copilot CLI runs a repository's hooks only in folders you have trusted: open the folder in `copilot` once and accept the prompt. Without that the hooks silently do nothing. The tool never records an outcome it cannot see (for example Cursor's `afterShellExecution`).

Copilot's notes carry no session id, because Copilot sets no environment variable the tool recognises. They are linked to the commands of the same working session by time instead.

### Cloud agents and CI (experimental)

This path is experimental and not part of what 0.2.0 promises. Agents that run on a remote machine have no hooks and no `.fbl/` launcher, because both live in your local `.git`. The block tells them to add `Friction[<origin>]: text` trailers to their commit messages instead:

```
Fix payment retry

Friction[repository-discovery]: could not find the payments contract
Friction[verification]: no single-test command documented
```

Copy `templates/fbl-record.yml` (installed to the tool folder) to `.github/workflows/` and set the repository variables `FBL_TOOL_REPOSITORY`, `FBL_TOOL_REF` and `FBL_DOMAIN`. On every push to `main` it records each new commit, trailers included, on the `fbl-insights` branch. If a developer's machine already recorded the commit, the existing record is kept. Trailers also work locally and are merged with notes filed through the launcher.

### Verified on GitHub Actions

In a private test repo, a commit with three `Friction[...]` trailers pushed to `main` produced a record on the remote `fbl-insights` branch with all three notes and their origins. The weekly workflow then ran by manual dispatch, wrote `reports/<date>.md` to the branch, and `gh issue create` opened an issue.

## Existing hooks and uninstalling

The installer writes `post-commit` and `pre-push` into the repository's hooks folder and stops rather than overwrite a hook it did not write. Re-running it with different settings updates its own hooks.

If `core.hooksPath` is set (Husky and similar tools do this), the installer refuses, because the hooks folder is tracked and the hooks contain machine-specific paths. Call the tool from your hook manager instead. Each hook is a small script that needs `FBL_HOME` (the tool folder), `FBL_INSIGHTS` (the insights worktree, `<repo>/.git/fbl/insights`), `FBL_DOMAIN`, `FBL_ADAPTER` and `FBL_PYTHON` in its environment; copy the exports from a hook the installer wrote in another clone. Husky-style chaining is on the roadmap.

To remove the loop from a repository:

```sh
python install.py --repo ~/code/my-project --uninstall
```

This removes the hooks it installed, `.fbl/` and the insights worktree. It keeps the `fbl-insights` branch (your history of records) and the friction block in `AGENTS.md` and the other instruction files, which you delete by hand. The shared tool folder stays for other repositories.

## Platforms

| Platform | State |
| --- | --- |
| macOS | All tests pass; install, note, commit and record verified end to end |
| Linux | CI passes on Ubuntu (tests and install smoke test) |
| Windows | CI passes on `windows-latest`: the test suite plus an install smoke test (install, file a note, commit, record appears). Not tried on a developer's own Windows machine or with PowerShell agents |

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

- Cloud-hosted agents have no launcher or hooks. They rely on commit trailers and the `fbl-record.yml` workflow. That workflow has only been exercised with a simulated agent commit, not a real cloud agent.
- Notes are self-reported. Treat them as leads unless a command failure or transcript signal agrees.
- Five origins split a small sample thin; with few records most findings stay "Not established".
- Command capture needs each tool's hook support: not available in Copilot's VS Code agent mode, and the Copilot CLI only runs repository hooks in trusted folders. Copilot's cloud agent runs only committed hooks, which this tool does not write.

## Roadmap

Not in 0.2.0, roughly in the order I would do them.

**Next, decided by the first pilot**
- **Copilot cloud agent hooks.** The cloud agent runs only hooks committed to the repository, so it needs a variant with no machine-specific paths. Also the Copilot instruction file in VS Code agent mode, which is untested.
- **Under-reporting measure.** Count sessions with failed verification and no note, so the report says how much friction goes unreported.
- **Trend and outcome tracking.** The review already has a ledger of recommendations. Wire it up so a fixed doc shows whether its friction notes dropped afterwards.

**Later**
- **Cloud agents validated for real.** Run the commit-trailer path with a real Copilot coding agent and Codex cloud, confirm they follow the instruction, and detect them as harnesses. Today only a simulated agent commit has been run.
- **GitHub Copilot tested** in VS Code agent mode and the CLI, with a harness label detected automatically.
- **Hook manager support** (Husky, lefthook, pre-commit) instead of refusing when `core.hooksPath` is set.
- **Reusable GitHub Action** (`uses: meabs/AgentFrictionLoop@v1`) instead of copying two workflow files and checking out the tool.
- **Installable package** (`pipx install`) so there is no folder to copy and upgrades are one command.
- **`conflicting-knowledge` from the diff.** Flag a commit that changes code a document describes without touching the document, rather than relying on the agent to notice a contradiction.
- **Task-context notes.** Brief quality lives in issue and prompt templates, outside the repository. Read it from the linked issue so these notes can be cross-checked.

**Ideas**
- Aggregate several repositories into one review.
- A privacy mode that stores note categories but no note text.
- Optional narration of the report by a model, kept behind the existing check that no sample text leaks.
- Per-person view of your own records (`review.py mine` exists; make it friendlier).

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

## License

[MIT](LICENSE).

# Agent friction reporting

Paste this block into `AGENTS.md` (the installer does it for you):

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

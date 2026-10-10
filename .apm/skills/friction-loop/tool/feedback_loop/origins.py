"""Where a friction note originated, which decides who can fix it. Optional next to the older category."""

from __future__ import annotations

ORIGINS = (
    "repository-discovery",   # agent could not find a contract, doc or entry point that exists
    "task-context",          # the task itself lacked an acceptance criterion, constraint or input
    "execution-environment", # a prerequisite, migration, tool or service was missing or out of order
    "verification",          # agent could not establish whether its change was correct
    "conflicting-knowledge", # two sources disagreed, or a doc contradicted the implementation
)

# Used when a note gives only --origin, so older consumers that read `category` still get a valid value.
DEFAULT_CATEGORY = {
    "repository-discovery": "missing-doc",
    "task-context": "other",
    "execution-environment": "setup",
    "verification": "test-order",
    "conflicting-knowledge": "unclear-instruction",
}


def valid(value: object) -> str | None:
    return value if isinstance(value, str) and value in ORIGINS else None

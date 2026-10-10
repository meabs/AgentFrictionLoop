"""Turn a harness's after-shell-command hook payload into a command event.

Only the command's leading words, its exit code and a coarse kind are kept. Output, arguments and paths
beyond the first words are discarded here, before anything is written.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import uuid
from pathlib import Path
from typing import Any

from . import SHIM_VERSION
from .events import _harness, _safe, append, session

HARNESS_LABELS = {"claude-code": "claude-code", "cursor": "cursor", "copilot": "copilot"}
SHELL_TOOLS = {"bash", "shell", "powershell", "run_terminal_cmd", "run_terminal_command", "terminal", "execute_bash", "local_shell"}
_WORD = re.compile(r"^[A-Za-z0-9_.:@+-]+$")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# (kind, tokens that identify it). Order matters: first match wins.
_KINDS = (
    ("migrate", ("alembic", "migrate", "makemigrations", "db:migrate", "flyway", "liquibase", "prisma migrate", "db migrate", "knex migrate")),
    ("tests", ("pytest", "py.test", "jest", "vitest", "mocha", "rspec", "phpunit", "go test", "cargo test", "dotnet test", "mvn test", "gradle test", "gradlew test", "npm test", "yarn test", "pnpm test", "bun test", "npm run test", "bun run test", "pnpm run test", "yarn run test", "tox", "nox", "unittest", "ctest", "deno test", "playwright test", "cypress run")),
    ("lint", ("ruff", "eslint", "flake8", "pylint", "rubocop", "golangci-lint", "mypy", "pyright", "prettier", "black", "isort", "clippy", "stylelint", "biome", "npm run lint", "bun run lint", "pnpm run lint", "yarn lint", "tsc --noemit", "shellcheck")),
    ("build", ("npm run build", "bun run build", "pnpm run build", "yarn build", "cargo build", "go build", "mvn package", "mvn compile", "gradle build", "gradlew build", "dotnet build", "make", "cmake", "tsc", "webpack", "vite build", "next build", "docker build", "uv build", "python -m build")),
    ("install", ("pip install", "pip3 install", "uv sync", "uv pip install", "npm install", "npm ci", "yarn install", "pnpm install", "bun install", "poetry install", "bundle install", "cargo fetch", "go mod download", "composer install", "apt install", "apt-get install", "brew install")),
)


_OPERATORS = {"&&", "||", ";", "|", "&", ";;"}


def _groups(command: str) -> list[list[str]]:
    """Split a shell line into commands on operators that are outside quotes, dropping leading VAR=value words."""
    try:
        lexer = shlex.shlex(command.strip(), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()[:1]  # unbalanced quotes: keep the executable only
    groups: list[list[str]] = [[]]
    for token in tokens:
        if token in _OPERATORS:
            groups.append([])
        else:
            groups[-1].append(token)
    result = []
    for group in groups:
        while group and _ENV_ASSIGN.match(group[0]):
            group = group[1:]
        if group:
            result.append(group)
    return result


def _kind_of(tokens: list[str]) -> str:
    if not tokens:
        return "other"
    if Path(tokens[0]).name.lower() in {"git", "gh"}:
        return "git"
    lowered = " ".join([Path(tokens[0]).name.lower(), *[t.lower() for t in tokens[1:]]])
    for kind, needles in _KINDS:
        for needle in needles:
            if re.search(r"(?:^|[\s/])" + re.escape(needle) + r"(?:$|[\s=])", lowered):
                return kind
    return "other"


_PLAIN = re.compile(r"^[a-z][a-z:_-]{0,19}$")


def _signature(tokens: list[str], kind: str = "other") -> str:
    """Executable name plus up to two lowercase plain words, stopping at the first flag. For commands that are not a
    recognised test, lint, build, migrate or install step, only the executable (and a git subcommand) is kept, so a
    positional argument such as a token or password never reaches the log."""
    if not tokens:
        return ""
    words = [Path(tokens[0]).name]
    rest = tokens[1:]
    if rest[:1] == ["-m"] and len(rest) > 1 and _WORD.match(rest[1]):
        return f"{words[0]} {rest[1]}"[:60]  # python -m pytest
    if kind in {"other"}:
        return words[0][:60]
    for token in rest:
        if len(words) >= (2 if kind == "git" else 3) or token.startswith("-") or not _PLAIN.match(token):
            break
        words.append(token)
    return " ".join(words)[:60]


def classify(command: str) -> tuple[str, str]:
    """(kind, signature) for the part of a compound command that matters most: a recognised kind wins over 'other'."""
    segments = _groups(command)
    segments = [t for t in segments if t and t[0] not in {"cd", "export", "source", "echo", "set", "true", "false"}] or segments
    if not segments:
        return "other", ""
    ranked = [(_kind_of(t), t) for t in segments]
    for kind, tokens in ranked:
        if kind not in {"other", "git"}:
            return kind, _signature(tokens, kind)
    kind, tokens = ranked[-1]
    return kind, _signature(tokens, kind)


def _find(payload: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = payload
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value not in (None, ""):
            return value
    return None


def _as_command(value: Any) -> str | None:
    if isinstance(value, str):
        try:  # Copilot may pass toolArgs as a JSON string
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return _as_command(parsed)
        except ValueError:
            pass
        return value
    if isinstance(value, dict):
        for key in ("command", "cmd", "script"):
            if isinstance(value.get(key), str):
                return value[key]
    return None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse(harness: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull command, exit code, session id, cwd and duration out of a hook payload. None means: not a shell command, or no
    reliable outcome, so record nothing rather than guess."""
    event_name = str(_find(payload, "hook_event_name", "hookEventName", "event") or "")
    failed_hook = "failure" in event_name.lower()
    tool = str(_find(payload, "tool_name", "toolName", "tool") or "").lower()
    if tool and tool not in SHELL_TOOLS:
        return None
    command = _as_command(_find(payload, "tool_input", "toolArgs", "tool_args", "args")) or _as_command(payload.get("command"))
    if not command:
        return None
    if payload.get("is_interrupt") or payload.get("interrupted"):
        return None  # cancelled by the user, not an outcome of the command
    exit_code = _int(_find(payload, "exit_code", "exitCode", "tool_response.exit_code", "toolResult.exit_code", "toolResult.exitCode"))
    if exit_code is None:
        # Claude Code (verified in a live session) sends no exit_code field: a failure arrives as
        # PostToolUseFailure with error "Exit code N ...". Only that leading number is read; the rest is output.
        match = re.match(r"\s*Exit code (\d{1,3})\b", str(payload.get("error") or ""))
        if match:
            exit_code = int(match.group(1))
        elif failed_hook or _find(payload, "error"):
            exit_code = 1
        elif harness == "copilot" and str(_find(payload, "toolResult.resultType") or "") == "success":
            exit_code = 0
        elif harness == "claude-code" and event_name == "PostToolUse":
            exit_code = 0
        else:
            return None  # Cursor's shell hook documents no exit code: do not claim success
    duration = _int(_find(payload, "duration", "duration_ms", "durationMs")) or 0
    return {"command": command, "exit_code": exit_code, "session": _find(payload, "session_id", "sessionId", "conversation_id"), "cwd": _find(payload, "cwd") or os.getcwd(), "duration_ms": max(duration, 0)}


def record(harness: str, payload: dict[str, Any], env: dict[str, str] | None = None) -> bool:
    env = dict(os.environ if env is None else env)
    parsed = parse(harness, payload)
    if not parsed:
        return False
    groups = _groups(parsed["command"])
    tokens = groups[0] if groups else []
    if tokens and Path(tokens[0]).name.lower().startswith("fbl-friction"):
        return False  # the note command itself is not work
    kind, signature = classify(parsed["command"])
    if not signature:
        return False
    cwd = str(parsed["cwd"])
    root = Path(cwd)
    try:
        from .events import _root
        top, _ = _root(cwd)
        rel = os.path.relpath(cwd, top).replace(os.sep, "/") if top else ""
    except Exception:
        rel = ""
    rel = "" if rel in {".", ""} or rel.startswith("..") else rel
    label = harness if harness in HARNESS_LABELS.values() else _harness(env)
    sess = _safe(parsed["session"])
    session_info = {"id": sess, "source": "hook"} if sess else session(env, label)
    append({
        "schema_version": "boundary-event/v1", "type": "command", "event_id": uuid.uuid4().hex, "parent_event_id": None,
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "duration_ms": parsed["duration_ms"], "exit_code": parsed["exit_code"], "kind": kind, "signature": signature, "cwd_rel": rel,
        "session": session_info, "harness": label, "shim_version": SHIM_VERSION,
    }, str(root))
    return True

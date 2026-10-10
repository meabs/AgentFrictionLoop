"""Extract bounded, derived execution evidence from local Codex rollouts."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,24}$")
_COMMAND_KINDS = ("pytest", "alembic", "git", "search", "read", "ls", "package", "build_or_lint", "other")
_READ_COMMANDS = {"sed", "cat", "head", "tail", "nl", "less", "bat"}
_SEARCH_COMMANDS = {"rg", "grep", "find", "fd"}
_PACKAGE_COMMANDS = {"uv", "pip", "npm", "bun", "yarn", "pnpm"}


def _timestamp(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _record_time(value: str) -> dt.datetime:
    parsed = _timestamp(value)
    if parsed is None:
        raise ValueError("invalid record timestamp")
    return parsed


def _json_string(text: str, key: str, start: int) -> tuple[str, int] | None:
    match = re.search(rf"\b{re.escape(key)}\s*:\s*", text[start:])
    if not match:
        return None
    position = start + match.end()
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(text[position:])
    except (json.JSONDecodeError, TypeError):
        return None
    return (value, position + end) if isinstance(value, str) else None


def _exec_calls(text: str) -> list[tuple[str, str | None]]:
    calls: list[tuple[str, str | None]] = []
    for match in re.finditer(r"tools\.exec_command\s*\(\s*\{", text):
        command = _json_string(text, "cmd", match.end())
        if command is None:
            continue
        workdir = _json_string(text, "workdir", match.end())
        calls.append((command[0], workdir[0] if workdir else None))
    return calls


def _segments(command: str) -> list[list[str]]:
    try:
        tokens = list(shlex.shlex(command, posix=True, punctuation_chars=";&|"))
    except ValueError:
        return []
    result: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            if current:
                result.append(current)
                current = []
        else:
            current.append(token)
    if current:
        result.append(current)
    return result


def _without_assignments(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[index]):
        index += 1
    return tokens[index:]


def _executable(tokens: list[str]) -> str:
    tokens = _without_assignments(tokens)
    if not tokens:
        return ""
    return Path(tokens[0]).name


def _kind(tokens: list[str]) -> str:
    executable = _executable(tokens)
    lowered = {token.lower() for token in tokens}
    if executable == "pytest" or "pytest" in lowered or (executable == "python" and "-m" in tokens and "pytest" in lowered):
        return "pytest"
    if executable == "alembic":
        return "alembic"
    if executable == "git":
        return "git"
    if executable in _SEARCH_COMMANDS:
        return "search"
    if executable in _READ_COMMANDS or executable == "awk":
        return "read"
    if executable == "ls":
        return "ls"
    if executable in _PACKAGE_COMMANDS:
        return "package"
    if any(token in {"build", "lint", "check", "typecheck", "format"} for token in lowered):
        return "build_or_lint"
    return "other"


def _signature(tokens: list[str]) -> str:
    tokens = _without_assignments(tokens)
    if not tokens:
        return ""
    kept: list[str] = []
    for token in tokens:
        if len(kept) >= 3:
            break
        if token.startswith("-") and len(kept) > 0:
            continue
        if "/" in token or "\\" in token or "=" in token or "@" in token or "://" in token:
            continue
        if _SAFE_TOKEN.match(token):
            kept.append(token)
    return " ".join(kept)[:40]


def _command_pattern(tokens: list[str]) -> str | None:
    tokens = _without_assignments(tokens)
    executable = _executable(tokens)
    if executable not in _SEARCH_COMMANDS:
        return None
    after_double_dash = False
    for token in tokens[1:]:
        if token == "--":
            after_double_dash = True
            continue
        if token.startswith("-") and not after_double_dash:
            continue
        if token and token != "--":
            return re.sub(r"\s+", " ", token).strip().lower()
    return None


def _local_path(value: str) -> str:
    if value.startswith("file://"):
        return unquote(urlparse(value).path)
    return value


def _repo_relative(value: str, workdir: str | None, repo: Path, rollout_cwd: str | None = None) -> str | None:
    if not value or value == "-" or value.startswith("-"):
        return None
    candidate = Path(_local_path(value))
    if workdir:
        workdir_path = Path(_local_path(workdir))
        base = (workdir_path if workdir_path.is_absolute() else repo / workdir_path).resolve()
    else:
        base = repo
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        relative = candidate.resolve().relative_to(repo)
    except ValueError:
        if rollout_cwd:
            original = Path(_local_path(rollout_cwd)).resolve()
            candidate = candidate.resolve()
            try:
                relative_to_rollout = candidate.relative_to(original)
            except ValueError:
                return None
            relative = (repo / relative_to_rollout).resolve().relative_to(repo)
        else:
            return None
    path = relative.as_posix()
    return path if path and path != "." else None


def _read_paths(tokens: list[str], workdir: str | None, repo: Path, rollout_cwd: str | None = None) -> list[str]:
    tokens = _without_assignments(tokens)
    executable = _executable(tokens)
    if executable not in _READ_COMMANDS and executable != "awk":
        return []
    values = tokens[1:]
    paths: list[str] = []
    operands: list[str] = []
    index = 0
    after_options = False
    sed_script_seen = False
    option_arguments = {
        "head": {"-n", "--lines", "-c", "--bytes"},
        "tail": {"-n", "--lines", "-c", "--bytes"},
        "nl": {"-v", "--starting-line-number", "-i", "--line-increment", "-s", "--number-separator", "-w", "--number-width"},
    }
    while index < len(values):
        value = values[index]
        if not after_options and value == "--":
            after_options = True
            index += 1
            continue
        if executable == "sed":
            if not after_options and value in {"-n", "-E", "-r", "-s", "-z", "-u", "--quiet", "--silent", "--regexp-extended", "--separate"}:
                index += 1
                continue
            if not after_options and value in {"-e", "--expression"}:
                sed_script_seen = True
                index += 2
                continue
            if not after_options and value in {"-f", "--file"}:
                sed_script_seen = True
                if index + 1 < len(values):
                    operands.append(values[index + 1])
                index += 2
                continue
            if not after_options and (value.startswith("-e") or value.startswith("--expression=") or value.startswith("-f")):
                if value.startswith("-f") and not value.startswith("--") and len(value) > 2:
                    sed_script_seen = True
                    operands.append(value[2:])
                index += 1
                continue
            if not sed_script_seen and not after_options:
                index += 1  # sed's first positional operand is its script
                sed_script_seen = True
                operands.append("__sed_script__")
                continue
            operands.append(value)
            index += 1
            continue
        if executable == "awk":
            if not operands and not after_options and not value.startswith("-"):
                operands.append("__awk_program__")
            else:
                operands.append(value)
            index += 1
            continue
        if not after_options and value in option_arguments.get(executable, set()):
            index += 2
            continue
        if not after_options and any(value.startswith(option + "=") for option in option_arguments.get(executable, set())):
            index += 1
            continue
        if not after_options and value.startswith("-"):
            index += 1
            continue
        operands.append(value)
        index += 1
    for value in operands:
        if value.startswith("__") and value.endswith("__"):
            continue
        if any(char in value for char in "*?[]<>|&;$(){}=") or value.isdigit():
            continue
        path = _repo_relative(value, workdir, repo, rollout_cwd)
        if path and not (repo / path).is_dir() and ((repo / path).is_file() or "/" in path or re.search(r"\.\w+$", path)):
            paths.append(path)
    return paths


def _verification_kind(kind: str, tokens: list[str]) -> str | None:
    executable = _executable(tokens)
    lowered = {token.lower() for token in tokens}
    if kind == "pytest" or "pytest" in lowered or (executable == "go" and "test" in lowered):
        return "tests"
    if executable in {"bun", "npm", "yarn", "pnpm", "mvn", "gradle"} and "test" in lowered:
        return "tests"
    if executable == "alembic" and ("upgrade" in lowered or "downgrade" in lowered):
        return "migration"
    if executable in {"ruff", "eslint", "biome", "flake8"} or any(token.lower() in {"lint", "check", "typecheck", "format"} for token in tokens):
        return "lint"
    if executable == "tsc" or "build" in lowered or (executable in {"npm", "bun", "yarn", "pnpm"} and "build" in lowered) or (executable in {"mvn", "gradle"} and {"package", "compile"} & lowered):
        return "build"
    return None


def _output_exit(output: Any) -> int | None:
    if isinstance(output, list):
        text = "\n".join(item.get("text", "") for item in output if isinstance(item, dict) and isinstance(item.get("text"), str))
    elif isinstance(output, str):
        text = output
    else:
        return None
    match = re.search(r'"exit_code"\s*:\s*(-?\d+)', text)
    return int(match.group(1)) if match else None


def _output_session(output: Any) -> int | None:
    if isinstance(output, list):
        text = "\n".join(item.get("text", "") for item in output if isinstance(item, dict) and isinstance(item.get("text"), str))
    elif isinstance(output, str):
        text = output
    else:
        return None
    match = re.search(r'"session_id"\s*:\s*(\d+)', text)
    return int(match.group(1)) if match else None


def _tool_number(text: str, key: str) -> int | None:
    match = re.search(rf"\b{re.escape(key)}\s*:\s*(\d+)", text)
    return int(match.group(1)) if match else None


_TRIVIAL_COMMANDS = {"cd", "export", "set", "source", ".", "echo", "printf", "true", "pwd", "env"}
_EXIT_ONE_NONFAILURES = {"grep", "rg", "diff", "cmp", "test", "["}


def _is_trivial(tokens: list[str]) -> bool:
    executable = _executable(tokens)
    return not executable or executable in _TRIVIAL_COMMANDS


def _read_segments_to_count(segments: list[list[str]], exit_code: int | None) -> set[int]:
    """Return read segments that can conservatively be treated as opened."""
    read_indices = {index for index, segment in enumerate(segments) if _kind(segment) == "read"}
    if exit_code is None or exit_code == 0:
        return read_indices
    nontrivial_indices = [index for index, segment in enumerate(segments) if not _is_trivial(segment)]
    if not nontrivial_indices:
        return set()
    last_nontrivial = nontrivial_indices[-1]
    if _kind(segments[last_nontrivial]) == "read":
        return set()
    return {index for index in read_indices if index < last_nontrivial}


def _exit_is_failure(tokens: list[str], exit_code: int) -> bool:
    if exit_code == 0:
        return False
    executable = _executable(tokens)
    if exit_code != 1:
        return True
    if executable in _EXIT_ONE_NONFAILURES:
        return False
    if executable == "git" and len(_without_assignments(tokens)) > 1:
        args = _without_assignments(tokens)[1:]
        return args[0] in {"diff", "status"}
    return True


def _pricing(insights: Path) -> dict[str, float] | None:
    try:
        data = json.loads((insights / "config/pricing.json").read_text(encoding="utf-8"))
        return {key: float(data[key]) for key in ("input_per_million", "cached_input_per_million", "output_per_million")}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _files(root: Path | None) -> list[Path]:
    if root is None:
        root = Path(os.environ.get("CODEX_SESSIONS_DIR", Path.home() / ".codex/sessions"))
    if root.is_file():
        return [root]
    try:
        return sorted(root.glob("*/*/*/rollout-*.jsonl"))
    except OSError:
        return []


def _load(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    meta = next((event.get("payload", {}) for event in events if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict)), {})
    return events, meta


def _matches(path: Path, repo: Path, commit_time: dt.datetime, *, ignore_cwd: bool = False) -> tuple[dt.datetime, list[dict[str, Any]], dict[str, Any]] | None:
    try:
        events, meta = _load(path)
    except OSError:
        return None
    if not ignore_cwd and meta.get("cwd") != str(repo):
        return None
    start = _timestamp(meta.get("timestamp"))
    times = [_timestamp(event.get("timestamp")) for event in events]
    valid_times = [value for value in times if value is not None]
    if start is None or not valid_times:
        return None
    last = max(valid_times)
    if not (start <= commit_time <= last + dt.timedelta(seconds=300)):
        return None
    return start, events, meta


def _select(repo: Path, commit_timestamp: str, session: str | os.PathLike[str] | None) -> tuple[Path, list[dict[str, Any]], dict[str, Any]] | None:
    commit_time = _record_time(commit_timestamp)
    explicit = Path(session).expanduser() if session else None
    candidates: list[tuple[dt.datetime, Path, list[dict[str, Any]], dict[str, Any]]] = []
    for path in _files(explicit):
        match = _matches(path, repo, commit_time, ignore_cwd=explicit is not None)
        if match:
            candidates.append((match[0], path, match[1], match[2]))
    if not candidates:
        return None
    _, path, events, meta = max(candidates, key=lambda item: item[0])
    return path, events, meta


def _real_command_items(events: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    items = []
    for index, event in enumerate(events):
        payload = event.get("payload", {})
        if event.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "item_completed":
            continue
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "CommandExecution" and isinstance(item.get("command"), list):
            items.append((index, item))
    return items


def _analyse_real(
    events: list[dict[str, Any]],
    repo: Path,
    meta: dict[str, Any],
    insights: Path,
    command_items: list[tuple[int, dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    rollout_cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else None
    model = None
    for event in events:
        if event.get("type") == "turn_context" and isinstance(event.get("payload"), dict):
            candidate = event["payload"].get("model")
            if isinstance(candidate, str):
                model = candidate
                break
    token_events = [
        event["payload"]["thread_token_usage"]
        for event in events
        if event.get("type") == "token_usage_record"
        and isinstance(event.get("payload"), dict)
        and isinstance(event["payload"].get("thread_token_usage"), dict)
    ]
    usage = token_events[-1] if token_events else {}
    task_started = next(
        (_timestamp(event.get("timestamp")) for event in events if event.get("type") == "event_msg" and isinstance(event.get("payload"), dict) and event["payload"].get("type") == "task_started"),
        None,
    )
    task_complete = next(
        (_timestamp(event.get("timestamp")) for event in reversed(events) if event.get("type") == "event_msg" and isinstance(event.get("payload"), dict) and event["payload"].get("type") == "task_complete"),
        None,
    )
    command_kinds: Counter[str] = Counter({kind: 0 for kind in _COMMAND_KINDS})
    search_patterns: list[str] = []
    opened: list[str] = []
    read_counts: Counter[str] = Counter()
    verification: list[dict[str, Any]] = []
    commands: list[tuple[list[str], int | None, list[str]]] = []
    first_edit_index = next(
        (index for index, event in enumerate(events) if event.get("type") == "event_msg" and isinstance(event.get("payload"), dict) and event["payload"].get("type") == "item_completed" and isinstance(event["payload"].get("item"), dict) and event["payload"]["item"].get("type") == "FileChange"),
        None,
    )
    edited_files: set[str] = set()
    edit_count = 0
    for event in events:
        payload = event.get("payload", {})
        if event.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "item_completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "FileChange":
            continue
        edit_count += 1
        changes = item.get("changes")
        if isinstance(changes, dict):
            for path in changes:
                relative = _repo_relative(path, rollout_cwd, repo, rollout_cwd)
                if relative:
                    edited_files.add(relative)
    for _, item in command_items:
        raw_command = item["command"][-1] if item["command"] and isinstance(item["command"][-1], str) else ""
        segments = _segments(raw_command)
        cwd = item.get("cwd") if isinstance(item.get("cwd"), str) else rollout_cwd
        exit_code = item.get("exit_code") if isinstance(item.get("exit_code"), int) else None
        nontrivial = [segment for segment in segments if not _is_trivial(segment)]
        dominant = nontrivial[-1] if nontrivial else (segments[-1] if segments else [])
        command_kinds[_kind(dominant)] += 1
        counted_read_segments = _read_segments_to_count(segments, exit_code)
        for segment_index, segment in enumerate(segments):
            pattern = _command_pattern(segment)
            if pattern is not None:
                search_patterns.append(pattern)
            if segment_index in counted_read_segments:
                for path in _read_paths(segment, cwd, repo, rollout_cwd):
                    opened.append(path)
                    read_counts[path] += 1
            verification_kind = _verification_kind(_kind(segment), segment)
            if verification_kind:
                verification.append({"kind": verification_kind, "exit_code": exit_code})
        failure_target = next(
            (segment for segment in reversed(segments) if _verification_kind(_kind(segment), segment)),
            dominant,
        )
        commands.append((dominant, exit_code, failure_target))
    failed: Counter[tuple[str, int]] = Counter()
    for _, exit_code, failure_target in commands:
        if exit_code is not None and _exit_is_failure(failure_target, exit_code):
            failed[(_signature(failure_target), exit_code)] += 1
    first_verification = verification[0]["exit_code"] if verification else None
    repeated_searches = sum(count - 1 for count in Counter(search_patterns).values() if count > 1)
    session_complete = any(event.get("type") == "event_msg" and isinstance(event.get("payload"), dict) and event["payload"].get("type") == "task_complete" for event in events)
    telemetry: dict[str, Any] = {
        "harness": "codex",
        "model": model,
        "cli_version": meta.get("cli_version"),
        "session_id": meta.get("session_id") or meta.get("id"),
        "session_complete": session_complete,
        "api_turns": len(token_events),
        "tool_calls": len(commands),
        "file_edits": edit_count,
        "shell_execs": len(commands),
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost_usd": None,
        "wall_seconds": (task_complete - task_started).total_seconds() if task_started and task_complete and task_complete >= task_started else None,
    }
    pricing = _pricing(insights)
    if pricing and all(isinstance(telemetry.get(key), (int, float)) for key in ("input_tokens", "cached_input_tokens", "output_tokens")):
        telemetry["cost_usd"] = ((telemetry["input_tokens"] - telemetry["cached_input_tokens"]) * pricing["input_per_million"] + telemetry["cached_input_tokens"] * pricing["cached_input_per_million"] + telemetry["output_tokens"] * pricing["output_per_million"]) / 1_000_000
    discovery = sum(
        1
        for event_index, item in command_items
        if first_edit_index is None or event_index < first_edit_index
        for segment in _segments(item["command"][-1] if item.get("command") and isinstance(item["command"][-1], str) else "")
        if _kind(segment) in {"read", "search", "ls"}
    )
    execution = {
        "session_complete": session_complete,
        "commands_total": len(commands),
        "commands_failed": sum(failed.values()),
        "failed_commands": [{"signature": signature, "exit_code": code, "count": count} for (signature, code), count in sorted(failed.items()) if signature],
        "command_kinds": dict(command_kinds),
        "files_opened": sorted(set(opened)),
        "file_read_counts": {path: count for path, count in sorted(read_counts.items()) if count > 1},
        "skills_loaded": sorted({path.split("/")[2] for path in opened if path.startswith((".agents/skills/", ".claude/skills/")) and path.endswith("/SKILL.md")}),
        "guidance_files_opened": sorted(path for path in set(opened) if path == "AGENTS.md" or path == "CLAUDE.md" or path in {"service.yaml", "service.yml"} or path.startswith(("okf/", ".agents/skills/", ".claude/skills/", "docs/adr/", "docs/ard/"))),
        "search_count": command_kinds["search"],
        "repeated_searches": repeated_searches,
        "discovery_steps_before_first_edit": discovery,
        "verification_attempts": verification,
        "first_verification_passed": None if first_verification is None else first_verification == 0,
        "edit_count": edit_count,
        "edited_files": sorted(edited_files),
    }
    return execution, telemetry


def _analyse(events: Iterable[dict[str, Any]], repo: Path, meta: dict[str, Any], insights: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    events = list(events)
    command_items = _real_command_items(events)
    if command_items:
        return _analyse_real(events, repo, meta, insights, command_items)
    outputs: dict[str, Any] = {}
    commands: list[tuple[list[str], str | None, int | None]] = []
    tool_calls = file_edits = 0
    task_started = task_complete = None
    model = None
    token_events: list[dict[str, Any]] = []
    search_patterns: list[str] = []
    opened: list[str] = []
    read_counts: Counter[str] = Counter()
    read_candidates: dict[int, list[str]] = {}
    command_kinds: Counter[str] = Counter({kind: 0 for kind in _COMMAND_KINDS})
    verification: list[dict[str, Any]] = []
    exec_groups: list[tuple[str | None, list[int]]] = []
    stdin_calls: list[tuple[str | None, int | None]] = []
    edits = 0
    for event in events:
        timestamp = _timestamp(event.get("timestamp"))
        payload = event.get("payload", {})
        event_type = event.get("type")
        if event_type == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "task_started" and timestamp and task_started is None:
                task_started = timestamp
            if payload.get("type") == "task_complete" and timestamp:
                task_complete = timestamp
        if event_type == "token_usage_record" and isinstance(payload, dict):
            usage = payload.get("thread_token_usage")
            if isinstance(usage, dict):
                token_events.append(usage)
        if event_type != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") == "turn_context" and model is None:
            context = payload.get("payload")
            if isinstance(context, dict) and isinstance(context.get("model"), str):
                model = context["model"]
            elif isinstance(payload.get("model"), str):
                model = payload["model"]
        if payload.get("type") == "custom_tool_call":
            tool_calls += 1
            text = payload.get("input", "") if isinstance(payload.get("input"), str) else ""
            if "*** Begin Patch" in text:
                file_edits += 1
            call_id = payload.get("call_id")
            for command, workdir in _exec_calls(text):
                indices: list[int] = []
                for segment in _segments(command):
                    kind = _kind(segment)
                    command_kinds[kind] += 1
                    commands.append((segment, workdir, None))
                    indices.append(len(commands) - 1)
                    pattern = _command_pattern(segment)
                    if pattern is not None:
                        search_patterns.append(pattern)
                    read_candidates[len(commands) - 1] = _read_paths(segment, workdir, repo)
                    verification_kind = _verification_kind(kind, segment)
                    if verification_kind:
                        verification.append({"kind": verification_kind, "command_index": len(commands) - 1})
                exec_groups.append((call_id if isinstance(call_id, str) else None, indices))
            if "tools.write_stdin" in text:
                stdin_calls.append((call_id if isinstance(call_id, str) else None, _tool_number(text, "session_id")))
            edits += int("*** Begin Patch" in text)
        elif payload.get("type") == "custom_tool_call_output":
            call_id = payload.get("call_id")
            if call_id:
                outputs[call_id] = payload.get("output")
    # Associate one shell exit with the last non-trivial segment of each exec call.
    pending_sessions: dict[int, list[int]] = {}
    for call_id, indices in exec_groups:
        output = outputs.get(call_id)
        exit_code = _output_exit(output)
        session_id = _output_session(output)
        if exit_code is None and session_id is not None:
            pending_sessions.setdefault(session_id, []).extend(indices)
        elif indices:
            target = next((index for index in reversed(indices) if not _is_trivial(commands[index][0])), indices[-1])
            command = commands[target]
            commands[target] = (command[0], command[1], exit_code)
            for attempt in verification:
                if attempt["command_index"] == target:
                    attempt["exit_code"] = exit_code
    for call_id, session_id in stdin_calls:
        if session_id is None or session_id not in pending_sessions:
            continue
        exit_code = _output_exit(outputs.get(call_id))
        if exit_code is None:
            continue
        indices = pending_sessions.pop(session_id)
        target = next((index for index in reversed(indices) if not _is_trivial(commands[index][0])), indices[-1])
        command = commands[target]
        commands[target] = (command[0], command[1], exit_code)
        for attempt in verification:
            if attempt["command_index"] == target:
                attempt["exit_code"] = exit_code
    for _, indices in exec_groups:
        segments = [commands[index][0] for index in indices]
        exit_code = commands[indices[-1]][2] if indices else None
        for segment_index in _read_segments_to_count(segments, exit_code):
            for path in read_candidates.get(indices[segment_index], []):
                opened.append(path)
                read_counts[path] += 1
    failed: Counter[tuple[str, int]] = Counter()
    for tokens, _, exit_code in commands:
        if exit_code is not None and _exit_is_failure(tokens, exit_code):
            failed[(_signature(tokens), exit_code)] += 1
    repeated_searches = sum(count - 1 for count in Counter(search_patterns).values() if count > 1)
    first_verification = next((attempt.get("exit_code") for attempt in verification if attempt.get("exit_code") is not None), None)
    usage = token_events[-1] if token_events else {}
    telemetry: dict[str, Any] = {
        "harness": "codex",
        "model": model,
        "cli_version": meta.get("cli_version"),
        "session_id": meta.get("session_id") or meta.get("id"),
        "session_complete": any(event.get("type") == "event_msg" and isinstance(event.get("payload"), dict) and event["payload"].get("type") == "task_complete" for event in events),
        "api_turns": len(token_events),
        "tool_calls": tool_calls,
        "file_edits": file_edits,
        "shell_execs": len(commands),
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost_usd": None,
        "wall_seconds": (task_complete - task_started).total_seconds() if task_started and task_complete and task_complete >= task_started else None,
    }
    pricing = _pricing(insights)
    if pricing and all(isinstance(telemetry.get(key), (int, float)) for key in ("input_tokens", "cached_input_tokens", "output_tokens")):
        telemetry["cost_usd"] = ((telemetry["input_tokens"] - telemetry["cached_input_tokens"]) * pricing["input_per_million"] + telemetry["cached_input_tokens"] * pricing["cached_input_per_million"] + telemetry["output_tokens"] * pricing["output_per_million"]) / 1_000_000
    execution: dict[str, Any] = {
        "session_complete": telemetry["session_complete"],
        "commands_total": len(commands),
        "commands_failed": sum(failed.values()),
        "failed_commands": [{"signature": signature, "exit_code": code, "count": count} for (signature, code), count in sorted(failed.items()) if signature],
        "command_kinds": dict(command_kinds),
        "files_opened": sorted(set(opened)),
        "file_read_counts": {path: count for path, count in sorted(read_counts.items()) if count > 1},
        "skills_loaded": sorted({path.split("/")[2] for path in opened if path.startswith((".agents/skills/", ".claude/skills/")) and path.endswith("/SKILL.md")}),
        "guidance_files_opened": sorted(path for path in set(opened) if path == "AGENTS.md" or path == "CLAUDE.md" or path in {"service.yaml", "service.yml"} or path.startswith(("okf/", ".agents/skills/", ".claude/skills/", "docs/adr/", "docs/ard/"))),
        "search_count": command_kinds["search"],
        "repeated_searches": repeated_searches,
        "discovery_steps_before_first_edit": 0,
        "verification_attempts": [{"kind": attempt["kind"], "exit_code": attempt.get("exit_code")} for attempt in verification],
        "first_verification_passed": None if first_verification is None else first_verification == 0,
        "edit_count": edits,
    }
    first_edit = next((index for index, event in enumerate(events) if event.get("type") == "response_item" and isinstance(event.get("payload"), dict) and event["payload"].get("type") == "custom_tool_call" and isinstance(event["payload"].get("input"), str) and "*** Begin Patch" in event["payload"]["input"]), None)
    if first_edit is not None:
        steps = 0
        for event in events[:first_edit]:
            payload = event.get("payload", {})
            if not isinstance(payload, dict) or payload.get("type") != "custom_tool_call":
                continue
            text = payload.get("input", "") if isinstance(payload.get("input"), str) else ""
            steps += sum(1 for command, _ in _exec_calls(text) for segment in _segments(command) if _kind(segment) in {"read", "search", "ls"})
        execution["discovery_steps_before_first_edit"] = steps
    return execution, telemetry


def enrich_record(record: dict[str, Any], repo: str | os.PathLike[str], insights: str | os.PathLike[str], session: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return a record enriched from the best matching rollout, or unchanged."""
    selected = _select(Path(repo).resolve(), record["timestamp"], session)
    if selected is None:
        return record
    _, events, meta = selected
    execution, telemetry = _analyse(events, Path(repo).resolve(), meta, Path(insights).resolve())
    enriched = dict(record)
    enriched["execution"] = execution
    enriched["harness_telemetry"] = telemetry
    enriched["agent"] = {"harness": "codex"}
    return enriched

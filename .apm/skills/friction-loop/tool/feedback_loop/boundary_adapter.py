"""Turn append-only boundary events into a bounded analysis record.

Events without a session id are grouped into sessions using a 30 minute idle
gap.  The gap is inclusive: an event exactly 30 minutes after the previous
event remains in the same session.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import secrets
from .origins import DEFAULT_CATEGORY, valid as valid_origin

IDLE_GAP_SECONDS = 30 * 60
COMMIT_FUZZ_SECONDS = 5
FRICTION_GRACE_SECONDS = 15 * 60
CATEGORIES = {"setup", "test-order", "missing-doc", "unclear-instruction", "environment", "tooling", "other"}
VERIFICATION_KINDS = {"tests", "lint", "build", "migrate", "migration"}
HARNESS_NAMES = {"unknown", "codex", "claude-code", "cursor", "copilot", "gemini", "human"}


def _time(value: Any) -> dt.datetime | None:
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    return result.stdout if result.returncode == 0 else ""


def _events_path(repo: Path) -> Path | None:
    value = _git(repo, "rev-parse", "--git-common-dir").strip()
    if not value:
        return None
    common = Path(value)
    if not common.is_absolute():
        common = (repo / common).resolve()
    return common / "fbl" / "boundary" / "events.jsonl"


def _read_events(repo: Path) -> list[dict[str, Any]]:
    path = _events_path(repo)
    if not path or not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict) and _time(value.get("ts")):
            result.append(value)
    return sorted(result, key=lambda item: _time(item["ts"]) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))


def _groups(events: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    named: dict[str, list[dict[str, Any]]] = defaultdict(list)
    gaps: list[list[dict[str, Any]]] = []
    for event in events:
        session = event.get("session") if isinstance(event.get("session"), dict) else {}
        identifier = session.get("id") if session.get("source") != "none" else None
        if identifier:
            named[str(identifier)].append(event)
        elif not gaps or (_time(event["ts"]) - _time(gaps[-1][-1]["ts"])).total_seconds() > IDLE_GAP_SECONDS:
            gaps.append([event])
        else:
            gaps[-1].append(event)
    result = [(key, value) for key, value in named.items()]
    result.extend((f"gap-{_time(value[0]['ts']).isoformat().replace('+00:00', 'Z')}", value) for value in gaps)
    return result


def _sanitise(text: Any) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(text))
    return re.sub(r"\s+", " ", text).strip()[:240]


def _friction_item(category: Any, text: Any, source: str, origin: Any = None) -> dict[str, str] | None:
    category = category if category in CATEGORIES else "other"
    text = _sanitise(text)
    if not text or secrets.find(text) or re.search(r"(?i)\b(?:password|secret|token|api_key)\s*[:=]\s*\S+", text):
        return None
    item = {"category": category, "text": text, "source": source}
    if valid_origin(origin):
        item["origin"] = origin
    return item


def _trailers(repo: Path, commit: str) -> list[dict[str, str]]:
    result = []
    for line in _git(repo, "show", "-s", "--format=%B", commit).splitlines():
        match = re.match(r"^Friction(?:\[([^]]+)\])?:\s*(.*)$", line.strip(), re.I)
        if match:
            label = match.group(1) or "other"
            if valid_origin(label):  # Friction[conflicting-knowledge]: ... works like --origin
                result.append(_friction_item(DEFAULT_CATEGORY[label], match.group(2), "trailer", label))
            else:
                result.append(_friction_item(label, match.group(2), "trailer"))
    return [item for item in result if item]


def _previous_timestamp(repo: Path, record: dict[str, Any]) -> dt.datetime | None:
    parent = (record.get("parents") or [None])[0]
    if not parent:
        return None
    value = _git(repo, "show", "-s", "--format=%cI", parent).strip()
    return _time(value)


def _eligible(identifier: str, values: list[dict[str, Any]], commit_time: dt.datetime) -> list[dict[str, Any]]:
    # Agents file friction "before finishing", which is often after the commit.
    # Only sessions with a real id may attribute such late notes; id-less
    # sessions stay bounded by the commit so adjacent commits cannot share them.
    named = not identifier.startswith("gap-")
    result = []
    for event in values:
        grace = FRICTION_GRACE_SECONDS if named and event.get("type") == "friction" else COMMIT_FUZZ_SECONDS
        if _time(event["ts"]) <= commit_time + dt.timedelta(seconds=grace):
            result.append(event)
    return result


def _session_for(events: list[dict[str, Any]], commit_time: dt.datetime, session_id: str | None) -> tuple[str, list[dict[str, Any]]] | None:
    candidates = []
    for identifier, values in _groups(events):
        if session_id and identifier != session_id:
            continue
        eligible = _eligible(identifier, values, commit_time)
        if eligible:
            latest = max(_time(event["ts"]) for event in eligible)
            candidates.append((abs(commit_time - latest), identifier, eligible))
    if not candidates:
        return None
    _, identifier, values = min(candidates, key=lambda item: item[0])
    return identifier, values


def _verification_kind(kind: Any) -> str | None:
    kind = str(kind).lower()
    if kind == "migrate":
        return "migration"
    return kind if kind in {"tests", "lint", "build", "migration"} else None


def _with_trailers(record: dict[str, Any], repo_path: Path) -> dict[str, Any]:
    """Friction trailers live in the commit itself, so they need no local event log (cloud agents, CI)."""
    notes = list(record.get("friction", [])) + _trailers(repo_path, record.get("commit", "HEAD"))
    unique, seen = [], set()
    for item in notes:
        key = (item["category"], item.get("origin"), item["text"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    if unique:
        record["friction"] = unique[:20]
    return record


def enrich_record(record: dict[str, Any], repo: str | Path, insights: str | Path, session: str | None = None) -> dict[str, Any]:
    del insights  # adapter parity with the Codex adapter; boundary data lives beside git.
    repo_path = Path(repo).resolve()
    events = _read_events(repo_path)
    commit_time = _time(record.get("timestamp"))
    if not events or not commit_time:
        return _with_trailers(record, repo_path)
    selected = _session_for(events, commit_time, session)
    if not selected:
        return _with_trailers(record, repo_path)
    identifier, values = selected
    previous = _previous_timestamp(repo_path, record)
    bounded = list(values)
    if identifier.startswith("gap-") and previous:
        bounded = [event for event in bounded if (_time(event["ts"]) or commit_time) > previous]
    if not bounded:
        return _with_trailers(record, repo_path)
    top_level = [event for event in bounded if event.get("parent_event_id") is None]
    command_events = [event for event in top_level if event.get("type") == "command"]
    failed = [event for event in command_events if isinstance(event.get("exit_code"), int) and event["exit_code"] != 0]
    failed_groups: dict[tuple[str, int], int] = Counter((event.get("signature", ""), event["exit_code"]) for event in failed)
    attempts = [{"kind": kind, "exit_code": event.get("exit_code")} for event in command_events if (kind := _verification_kind(event.get("kind"))) is not None]
    command_kinds = Counter(str(event.get("kind", "other")) for event in command_events)
    telemetry_harness = Counter(str(event.get("harness", "unknown")) for event in bounded if event.get("harness") != "unknown")
    harness = telemetry_harness.most_common(1)[0][0] if telemetry_harness else "unknown"
    if harness not in HARNESS_NAMES:
        harness = "unknown"
    first = attempts[0]["exit_code"] if attempts else None
    execution = {
        "commands_total": len(command_events),
        "commands_failed": sum(1 for event in failed if event.get("kind") != "git" and event.get("exit_code") not in {126, 127}),
        "failed_commands": [{"signature": signature, "exit_code": code, "count": count} for (signature, code), count in sorted(failed_groups.items())],
        "verification_attempts": attempts,
        "first_verification_passed": None if first is None else first == 0,
        "command_kinds": dict(sorted(command_kinds.items())),
        "session_complete": True,
    }
    had_execution = "execution" in record
    if had_execution:
        existing = dict(record["execution"])
        existing["boundary"] = {key: execution[key] for key in ("commands_total", "commands_failed", "verification_attempts", "first_verification_passed")}
        record["execution"] = existing
    else:
        record["execution"] = execution
    timed = [event for event in bounded if event.get("type") != "friction"] or bounded
    first_ts = min(_time(event["ts"]) for event in timed)
    last = max(timed, key=lambda event: _time(event["ts"]))
    wall = ((_time(last["ts"]) - first_ts).total_seconds() + max(0, int(last.get("duration_ms", 0))) / 1000)
    telemetry = {"harness": harness, "session_id": identifier, "session_complete": True, "wall_seconds": wall, "evidence_level": "boundary"}
    if not had_execution:
        record["harness_telemetry"] = telemetry
        record["agent"] = {**record.get("agent", {}), "harness": harness}
    notes = []
    for event in bounded:
        if event.get("type") == "friction":
            item = _friction_item(event.get("category"), event.get("text"), "event", event.get("origin"))
            if item:
                notes.append(item)
    notes.extend(_trailers(repo_path, record.get("commit", "HEAD")))
    unique = []
    seen = set()
    for item in notes:
        key = (item["category"], item.get("origin"), item["text"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    if unique:
        record["friction"] = unique[:20]
    return record

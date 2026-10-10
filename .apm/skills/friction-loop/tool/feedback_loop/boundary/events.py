"""Append-only boundary event storage."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from . import SHIM_VERSION

_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HARNESS = {"codex", "cursor", "claude-code", "copilot", "unknown"}


def _safe(value: object) -> str | None:
    value = str(value) if value is not None else ""
    return value if _SAFE.fullmatch(value) else None


def _harness(env: dict[str, str]) -> str:
    override = env.get("FBL_HARNESS")
    if override in _HARNESS:
        return override
    if env.get("CURSOR_AGENT") is not None or env.get("CURSOR_CONVERSATION_ID") is not None:
        return "cursor"
    if env.get("CODEX_THREAD_ID") is not None or env.get("CODEX_SESSION_ID") is not None or env.get("CODEX_CI") is not None:
        return "codex"
    if env.get("CLAUDE_CODE_SESSION_ID") is not None or env.get("CLAUDECODE") is not None:
        return "claude-code"
    return "unknown"


def session(env: dict[str, str], harness: str | None = None) -> dict[str, object]:
    harness = harness or _harness(env)
    value = env.get("FBL_SESSION_ID")
    if value is None:
        value = {"cursor": "CURSOR_CONVERSATION_ID", "codex": "CODEX_THREAD_ID", "claude-code": "CLAUDE_CODE_SESSION_ID"}.get(harness) and env.get({"cursor": "CURSOR_CONVERSATION_ID", "codex": "CODEX_THREAD_ID", "claude-code": "CLAUDE_CODE_SESSION_ID"}[harness])
        if harness == "codex" and value is None:
            value = env.get("CODEX_SESSION_ID")
    value = _safe(value)
    return {"id": value, "source": "env" if value else "none"}


def _root(cwd: str) -> tuple[Path | None, Path | None]:
    try:
        env = os.environ.copy()
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=cwd, env=env, text=True, capture_output=True, check=True).stdout.strip()
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, env=env, text=True, capture_output=True, check=True).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (Path(cwd) / common_path).resolve()
        return Path(top).resolve(), common_path.resolve()
    except Exception:
        return None, None


def _event_path(cwd: str) -> tuple[Path | None, Path | None]:
    root, common = _root(cwd)
    if common:
        return root, common / "fbl" / "boundary" / "events.jsonl"
    fallback = os.environ.get("FBL_BOUNDARY_DIR")
    return None, Path(fallback) / "events.jsonl" if fallback else (None, None)[1]


def _ops(message: str) -> None:
    home = os.environ.get("FBL_HOME")
    if not home:
        return
    try:
        path = Path(home) / "var" / "ops.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(message.replace("\n", " ") + "\n")
    except Exception:
        pass


def append(event: dict[str, object], cwd: str) -> None:
    try:
        _, path = _event_path(cwd)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception as exc:
        _ops(f"boundary logging failed: {type(exc).__name__}")


def friction_event(*, cwd: str, category: str, text: str, env: dict[str, str], origin: str | None = None) -> None:
    harness = _harness(env)
    extra = {"origin": origin} if origin else {}
    append({**extra, "schema_version": "boundary-event/v1", "type": "friction", "event_id": uuid.uuid4().hex, "parent_event_id": _safe(env.get("FBL_PARENT_EVENT")), "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "category": category, "text": text, "session": session(env, harness), "harness": harness, "shim_version": SHIM_VERSION}, cwd)

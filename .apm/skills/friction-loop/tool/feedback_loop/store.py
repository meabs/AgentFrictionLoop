"""Validated, locked writes to a local insights Git repository."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import secrets
from pathlib import Path

from jsonschema import Draft202012Validator

from .compat import empty_hooks_dir, lock_exclusive
from .secrets import find


def _log(home: str | Path | None, reason: str) -> None:
    try:
        base = os.environ.get("FBL_HOME") or home or Path(__file__).resolve().parent.parent
        path = Path(base).resolve() / "var" / "ops.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": "analysis_failure", "reason": reason[:200]}, sort_keys=True) + "\n")
    except OSError:
        pass


# Git exports these to hooks; inherited, they would redirect insights-worktree commands to the calling repository.
_HOOK_GIT_VARS = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}


def _git(insights: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _HOOK_GIT_VARS}
    return subprocess.run(["git", "-c", "core.hooksPath=" + empty_hooks_dir(), *args], cwd=insights, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env)


def _component(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("domain and repository must be single path components")
    return value


def _initialise(insights: Path, schema: Path) -> None:
    insights.mkdir(parents=True, exist_ok=True)
    for name in ("records", "reports", "schemas", "config"):
        (insights / name).mkdir(exist_ok=True)
    if not (insights / ".git").exists():
        result = _git(insights, "init")
        if result.returncode:
            raise RuntimeError("unable to initialise insights repository")
    destination = insights / "schemas" / "analysis-v1.schema.json"
    if not destination.exists():
        shutil.copyfile(schema, destination)


def _actor_salt_unlocked(insights: Path) -> str:
    configured = os.environ.get("FBL_ACTOR_SALT")
    if configured:
        bytes.fromhex(configured)
        if len(configured) != 32:
            raise ValueError("FBL_ACTOR_SALT must be 32 hex characters")
        return configured.lower()
    path = insights / "config/actor.json"
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8")).get("salt")
        if isinstance(value, str) and len(value) == 32:
            bytes.fromhex(value)
            return value.lower()
        raise ValueError("invalid config/actor.json salt")
    salt = secrets.token_hex(16)
    path.write_text(json.dumps({"salt": salt}, indent=2) + "\n", encoding="utf-8")
    return salt


def actor_salt(insights: str | Path) -> str:
    target = Path(insights).resolve()
    target.mkdir(parents=True, exist_ok=True)
    lock_path = target / ".lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        lock_exclusive(lock)
        _initialise(target, Path(__file__).resolve().parents[1] / "schemas/analysis-v1.schema.json")
        return _actor_salt_unlocked(target)


def persist(record: dict, insights: str | Path, schema: str | Path, no_commit: bool = False) -> Path:
    target = Path(insights).resolve()
    schema_path = Path(schema).resolve()
    target.mkdir(parents=True, exist_ok=True)
    lock_path = target / ".lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        lock_exclusive(lock)
        _initialise(target, schema_path)
        _actor_salt_unlocked(target)
        validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
        errors = sorted(validator.iter_errors(record), key=lambda error: list(error.path))
        if errors:
            raise ValueError("schema validation failed: " + errors[0].message)
        serialised = json.dumps(record, sort_keys=True, indent=2) + "\n"
        if (reason := find(serialised)):
            _log(None, "secret pattern refused: " + reason)
            raise ValueError("secret pattern refused")
        month = record["timestamp"][:7]
        path = target / "records" / _component(record["domain"]) / _component(record["repository"]) / month / f"{record['commit_short']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        should_commit = False
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                merged = dict(existing)
                for key in ("execution", "harness_telemetry"):
                    if key in record:
                        merged[key] = record[key]
                if record.get("agent", {}).get("harness") != "unknown":
                    merged["agent"] = record["agent"]
                verification = record.get("verification", {})
                if any(value != "unknown" for key, value in verification.items() if key in {"tests", "lint", "build"}):
                    merged["verification"] = verification
                if merged != existing:
                    validator.validate(merged)
                    path.write_text(json.dumps(merged, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                    should_commit = True
            except (OSError, json.JSONDecodeError):
                path.write_text(serialised, encoding="utf-8")
                should_commit = True
        else:
            path.write_text(serialised, encoding="utf-8")
            should_commit = True
        if not no_commit:
            add = _git(target, "add", str(path.relative_to(target)), "schemas/analysis-v1.schema.json", "config/actor.json")
            if add.returncode:
                raise RuntimeError("git add failed")
            status = _git(target, "diff", "--cached", "--quiet", "--", str(path.relative_to(target)), "schemas/analysis-v1.schema.json", "config/actor.json")
            if status.returncode == 1:
                commit = _git(target, "commit", "--only", "-m", f"record {record['record_id']}", "--", str(path.relative_to(target)), "schemas/analysis-v1.schema.json", "config/actor.json")
                if commit.returncode:
                    raise RuntimeError("git commit failed")
            elif status.returncode > 1:
                raise RuntimeError("unable to inspect staged changes")
        return path

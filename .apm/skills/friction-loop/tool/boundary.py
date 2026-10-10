#!/usr/bin/env python3
"""Friction-note launcher: installs .fbl/bin/fbl-friction and records sanitised friction events."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CATEGORIES = {"setup", "test-order", "missing-doc", "unclear-instruction", "environment", "tooling", "other"}


def _git(repo: Path, *args: str) -> str | None:
    import subprocess
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _common(repo: Path) -> Path:
    value = _git(repo, "rev-parse", "--git-common-dir")
    path = Path(value or ".git")
    return path if path.is_absolute() else (repo / path).resolve()


def install(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    bindir = repo / ".fbl" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    launcher = bindir / "fbl-friction"
    python, script = Path(sys.executable).as_posix(), Path(__file__).resolve().as_posix()
    if not launcher.exists():
        # Unix shells and Git Bash on Windows run this one; newline="\n" keeps it LF on Windows.
        with launcher.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(f'#!/bin/sh\nexec "{python}" "{script}" friction "$@"\n')
        launcher.chmod(0o755)
    cmd = bindir / "fbl-friction.cmd"  # PowerShell and cmd.exe: .\\.fbl\\bin\\fbl-friction
    if not cmd.exists():
        with cmd.open("w", encoding="utf-8", newline="\r\n") as stream:
            stream.write(f'@echo off\r\n"{Path(sys.executable)}" "{Path(__file__).resolve()}" friction %*\r\n')
    exclude = _common(repo) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".fbl/" not in current.splitlines():
        exclude.write_text(current + ("" if not current or current.endswith("\n") else "\n") + ".fbl/\n", encoding="utf-8")
    print(f"installed {launcher}")
    return 0


def friction(args: argparse.Namespace) -> int:
    import re
    from feedback_loop import origins, secrets
    from feedback_loop.boundary import events
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(args.text))
    text = re.sub(r"\s+", " ", text).strip()[:240]
    if secrets.find(text) or re.search(r"(?i)\b(?:password|secret|token|api_key)\s*[:=]\s*\S+", text):
        print("friction note dropped: looks like a secret", file=sys.stderr)
        return 0
    if not args.category and not args.origin:
        print("friction: give --category, --origin or both", file=sys.stderr)
        return 2
    origin = origins.valid(args.origin)
    if args.origin and not origin:
        print(f"unknown --origin {args.origin!r}; use one of: {', '.join(origins.ORIGINS)}", file=sys.stderr)
        return 2
    category = args.category if args.category in CATEGORIES else (origins.DEFAULT_CATEGORY[origin] if origin and not args.category else "other")
    events.friction_event(cwd=os.getcwd(), category=category, text=text, env=dict(os.environ), origin=origin)
    print(f"friction recorded: {origin + ' / ' if origin else ''}{category}")
    return 0


def command(args: argparse.Namespace) -> int:
    """Hook entry point: read a harness's after-shell-command payload on stdin. Silent, and never fails the agent."""
    try:
        from feedback_loop.boundary import commands
        raw = sys.stdin.read(1_000_000)
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            commands.record(args.harness, payload)
    except Exception:
        pass
    return 0


def events_command(args: argparse.Namespace) -> int:
    from feedback_loop.boundary.events import _event_path
    path = _event_path(str(Path(args.repo).resolve()))[1]
    values = []
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if args.since and value.get("ts", "") < args.since:
                continue
            if args.session and value.get("session", {}).get("id") != args.session:
                continue
            values.append(value)
    if args.json:
        print(json.dumps(values, indent=2, sort_keys=True))
    else:
        for value in values:
            print(json.dumps(value, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install"); p.add_argument("--repo", required=True); p.set_defaults(func=install)
    p = sub.add_parser("events"); p.add_argument("--repo", required=True); p.add_argument("--since"); p.add_argument("--session"); p.add_argument("--json", action="store_true"); p.set_defaults(func=events_command)
    p = sub.add_parser("command"); p.add_argument("--harness", default="unknown"); p.set_defaults(func=command)
    p = sub.add_parser("friction"); p.add_argument("--category"); p.add_argument("--origin"); p.add_argument("text"); p.set_defaults(func=friction)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

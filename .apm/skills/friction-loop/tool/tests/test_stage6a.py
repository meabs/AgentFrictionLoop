from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from feedback_loop.boundary.events import _harness, session



def launcher(path: Path) -> list[str]:
    """The command an agent would run: the .cmd launcher on Windows, the sh one elsewhere."""
    bindir = path / ".fbl" / "bin"
    return [str(bindir / "fbl-friction.cmd")] if os.name == "nt" else [str(bindir / "fbl-friction")]

ROOT = Path(__file__).resolve().parents[1]


def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def install(path: Path) -> Path:
    subprocess.run([sys.executable, str(ROOT / "boundary.py"), "install", "--repo", str(path)], check=True, capture_output=True, text=True)
    return path / ".fbl" / "bin"


def events(path: Path) -> list[dict]:
    value = path / ".git/fbl/boundary/events.jsonl"
    return [json.loads(line) for line in value.read_text().splitlines()]


def test_harness_priority_and_session_sources():
    assert _harness({"CLAUDECODE": "1", "CODEX_CI": "1", "CURSOR_AGENT": "1"}) == "cursor"
    assert _harness({"CLAUDECODE": "1", "CODEX_CI": "1"}) == "codex"
    assert _harness({"CLAUDECODE": "1"}) == "claude-code"
    assert session({"CODEX_SESSION_ID": "x"}) == {"id": "x", "source": "env"}
    assert session({"FBL_SESSION_ID": "bad value\n"}) == {"id": None, "source": "none"}
    assert session({"FBL_SESSION_ID": "a" * 65})["id"] is None


def test_harness_override_allowlist():
    assert _harness({"FBL_HARNESS": "copilot"}) == "copilot"
    assert _harness({"FBL_HARNESS": "invented", "CODEX_CI": "1"}) == "codex"


def test_friction_sanitizes_category_text_and_secret_drop(tmp_path: Path):
    path = repo(tmp_path); install(path)
    result = subprocess.run([sys.executable, str(ROOT / "boundary.py"), "friction", "--category", "bad", "hello\nworld\x07"], cwd=path / ".", capture_output=True, text=True)
    assert result.returncode == 0 and "friction recorded: other" in result.stdout and events(path)[0]["text"] == "helloworld"
    dropped = subprocess.run([sys.executable, str(ROOT / "boundary.py"), "friction", "--category", "tooling", "password=hunter2"], cwd=path, capture_output=True, text=True)
    assert dropped.returncode == 0 and "dropped" in dropped.stderr and len(events(path)) == 1


def test_friction_launcher_works_from_subdirectory_and_truncates(tmp_path: Path):
    path = repo(tmp_path); install(path); child = path / "sub"; child.mkdir()
    result = subprocess.run([*launcher(path), "--category", "environment", "x" * 300], cwd=child, capture_output=True, text=True)
    assert result.returncode == 0 and len(events(path)[0]["text"]) == 240


def test_install_is_idempotent_and_creates_only_the_launcher(tmp_path: Path):
    path = repo(tmp_path)
    install(path)
    before = (path / ".fbl/bin/fbl-friction").read_text()
    install(path)
    assert (path / ".fbl/bin/fbl-friction").read_text() == before
    assert sorted(p.name for p in (path / ".fbl/bin").iterdir()) == ["fbl-doctor", "fbl-doctor.cmd", "fbl-friction", "fbl-friction.cmd"]
    assert (path / ".git/info/exclude").read_text().splitlines().count(".fbl/") == 1


def test_launcher_records_a_note_for_the_detected_session(tmp_path: Path):
    path = repo(tmp_path)
    install(path)
    result = subprocess.run(["sh", "-c", "./.fbl/bin/fbl-friction --category setup 'needs migration first'"], cwd=path, capture_output=True, text=True, env={**os.environ, "CODEX_THREAD_ID": "thread-1"})
    assert result.returncode == 0
    note = events(path)[0]
    assert note["text"] == "needs migration first" and note["harness"] == "codex" and note["session"]["id"] == "thread-1"

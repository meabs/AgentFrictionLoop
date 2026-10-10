from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from feedback_loop.boundary.commands import classify, parse
from feedback_loop.boundary_adapter import enrich_record

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT.parent / "install.py"
SCHEMA = Draft202012Validator(json.loads((ROOT / "schemas/boundary-event-v1.schema.json").read_text()))


def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def hook(path: Path, harness: str, payload) -> subprocess.CompletedProcess:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(ROOT / "boundary.py"), "command", "--harness", harness], input=data, cwd=path, capture_output=True, text=True)


def events(path: Path) -> list[dict]:
    log = path / ".git/fbl/boundary/events.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_classification_and_signature_keep_only_plain_leading_words():
    assert classify("uv run pytest -x tests/test_a.py::t") == ("tests", "uv run pytest")
    assert classify("cd app && npm test -- --watch=false") == ("tests", "npm test")
    assert classify("python -m pytest -q") == ("tests", "python pytest")
    assert classify("FOO=bar ruff check .") == ("lint", "ruff check")
    assert classify("alembic upgrade head") == ("migrate", "alembic upgrade head")
    assert classify("pip install -r requirements.txt") == ("install", "pip install")
    assert classify("git push origin main") == ("git", "git push")


def test_unrecognised_commands_never_log_arguments():
    for command in ("mycli login hunter2", "curl -H 'Authorization: Bearer abc123' https://x", "deploy --token=abc prod"):
        kind, signature = classify(command)
        assert kind == "other" and " " not in signature
    assert "hunter2" not in classify("git commit -m 'hunter2 stuff'")[1]


def test_claude_code_payloads_as_actually_sent():
    """Captured from a live Claude Code 2.1.296 session: no exit_code field; failures carry it in the error text."""
    failure = {"session_id": "s", "cwd": "/x", "hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {"command": "python3 -m pytest -q"}, "tool_use_id": "t", "error": "Exit code 2\nsome output SECRET", "is_interrupt": False, "duration_ms": 283}
    parsed = parse("claude-code", failure)
    assert parsed["exit_code"] == 2 and parsed["duration_ms"] == 283 and "SECRET" not in json.dumps(parsed)
    success = {"session_id": "s", "cwd": "/x", "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "true"}, "tool_response": {"stdout": "", "stderr": "", "interrupted": False}, "duration_ms": 5}
    assert parse("claude-code", success)["exit_code"] == 0
    assert parse("claude-code", {**failure, "is_interrupt": True}) is None
    assert parse("claude-code", {**failure, "error": "tool crashed"})["exit_code"] == 1


def test_copilot_cli_payloads_as_actually_sent():
    """Captured from a live Copilot CLI 1.0.95 session: resultType is "success" even for a nonzero exit."""
    def payload(text):
        return {"sessionId": "s", "timestamp": 1, "cwd": "/work/repo", "toolName": "bash", "toolArgs": {"command": "python3 -m pytest -q", "description": "d"}, "toolResult": {"resultType": "success", "textResultForLlm": text}}
    assert parse("copilot", payload("\n<shellId: 0 completed with exit code 1>"))["exit_code"] == 1
    assert parse("copilot", payload("1\n<shellId: 1 completed with exit code 0>"))["exit_code"] == 0
    # output that merely mentions an exit code must not be mistaken for the trailer
    assert parse("copilot", payload("job said: exit code 7 seen\n<shellId: 2 completed with exit code 0>"))["exit_code"] == 0
    assert parse("copilot", payload("exit code 9 in the output, no trailer")) is None
    assert parse("copilot", payload("x\n<shellId: 0 completed with exit code 2>"))["cwd"] == "/work/repo"


def test_operators_inside_quotes_do_not_split_the_command():
    assert classify("python3 -c 'import sys; sys.exit(2)'") == ("other", "python3")
    assert classify("python3 -c \"token='abc'; print(1)\"") == ("other", "python3")
    assert classify("git add -A . && git commit -m 'x; y'") == ("git", "git commit")
    assert classify("echo 'unbalanced") == ("other", "echo")


def test_payloads_in_each_harnesss_documented_shape():
    claude_fail = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {"command": "npm test"}, "exit_code": 1, "session_id": "s1", "cwd": "/x"}
    assert parse("claude-code", claude_fail)["exit_code"] == 1
    claude_ok = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"text": "SECRET OUTPUT"}, "exit_code": 0}
    assert parse("claude-code", claude_ok)["exit_code"] == 0 and "SECRET" not in json.dumps(parse("claude-code", claude_ok))
    assert parse("claude-code", {"tool_name": "Read", "tool_input": {"file_path": "x"}}) is None
    assert parse("copilot", {"toolName": "bash", "toolArgs": "{\"command\":\"pytest\"}", "toolResult": {"resultType": "success", "textResultForLlm": "ok\n<shellId: 1 completed with exit code 0>"}, "sessionId": "c1"})["exit_code"] == 0
    assert parse("copilot", {"hook_event_name": "postToolUseFailure", "toolName": "powershell", "toolArgs": {"command": "pytest"}, "error": "boom"})["exit_code"] == 1
    assert parse("copilot", {"toolName": "view", "toolArgs": {"path": "x"}}) is None
    # Cursor's afterShellExecution has no exit code and fires for successes too: claim neither.
    assert parse("cursor", {"hook_event_name": "afterShellExecution", "command": "npm test", "output": "SECRET", "duration": 12}) is None


def test_cursor_payloads_as_actually_sent():
    """Captured from a live cursor-agent 2026.10.01 session."""
    base = {"conversation_id": "c", "session_id": "c", "tool_name": "Shell", "tool_input": {"command": "python3 -m pytest -q", "cwd": "", "timeout": 30000}, "duration": 684.39, "workspace_roots": ["/work/repo"], "cwd": ""}
    failure = {**base, "hook_event_name": "postToolUseFailure", "error_message": "Command failed with exit code 3", "failure_type": "error", "is_interrupt": False}
    parsed = parse("cursor", failure)
    assert parsed["exit_code"] == 3 and parsed["duration_ms"] == 684 and parsed["cwd"] == "/work/repo" and parsed["session"] == "c"
    output_only = {**failure, "error_message": "/usr/bin/python3: No module named pytest SECRET"}
    assert parse("cursor", output_only)["exit_code"] == 1 and "SECRET" not in json.dumps(parse("cursor", output_only))
    success = {**base, "hook_event_name": "postToolUse", "tool_output": "1\n"}
    assert parse("cursor", success)["exit_code"] == 0
    assert parse("cursor", {**failure, "failure_type": "timeout"}) is None
    assert parse("cursor", {**base, "hook_event_name": "postToolUse", "tool_name": "Read"}) is None


def test_hook_writes_a_valid_event_and_drops_output(tmp_path: Path):
    path = repo(tmp_path)
    payload = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {"command": "uv run pytest tests/secret_name.py"}, "error": "Command failed", "exit_code": 2, "session_id": "sess-1", "cwd": str(path)}
    result = hook(path, "claude-code", payload)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    (event,) = events(path)
    SCHEMA.validate(event)
    assert (event["type"], event["kind"], event["exit_code"], event["signature"], event["harness"]) == ("command", "tests", 2, "uv run pytest", "claude-code")
    assert event["session"] == {"id": "sess-1", "source": "hook"} and event["cwd_rel"] == ""
    assert "secret_name" not in json.dumps(event)


def test_hook_never_fails_the_agent(tmp_path: Path):
    path = repo(tmp_path)
    for junk in ("", "not json", "[1,2]", "{\"tool_name\": 3}", "\x00\x01"):
        result = hook(path, "claude-code", junk)
        assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    assert events(path) == []


def test_the_note_command_itself_is_not_recorded(tmp_path: Path):
    path = repo(tmp_path)
    hook(path, "claude-code", {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "./.fbl/bin/fbl-friction --origin verification 'x'"}, "exit_code": 0, "cwd": str(path)})
    assert events(path) == []


def test_recorded_failures_reach_the_analysis_record(tmp_path: Path):
    path = repo(tmp_path)
    (path / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "work"], cwd=path, check=True)
    for name, command, code in (("PostToolUseFailure", "uv run pytest", 1), ("PostToolUse", "uv run pytest", 0)):
        hook(path, "claude-code", {"hook_event_name": name, "tool_name": "Bash", "tool_input": {"command": command}, "exit_code": code, "session_id": "s9", "cwd": str(path)})
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    record = enrich_record({"timestamp": now, "commit": "HEAD"}, path, tmp_path / "ins")
    execution = record["execution"]
    assert execution["commands_total"] == 2 and execution["commands_failed"] == 1
    assert execution["first_verification_passed"] is False and record["agent"]["harness"] == "claude-code"


# ---- installer wiring

def install(path: Path, tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(INSTALL), "--repo", str(path), "--domain", "t", "--home", str(tmp_path / "home"), *extra], capture_output=True, text=True)


def test_install_writes_local_hook_configs_and_keeps_existing_settings(tmp_path: Path):
    path = repo(tmp_path)
    (path / ".claude").mkdir()
    (path / ".claude/settings.local.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "mine.sh"}]}]}}))
    result = install(path, tmp_path)
    assert result.returncode == 0, result.stderr
    claude = json.loads((path / ".claude/settings.local.json").read_text())
    assert claude["permissions"] == {"allow": ["Bash(ls)"]}
    assert [e["matcher"] for e in claude["hooks"]["PostToolUse"]] == ["Edit", "Bash"]
    assert "boundary.py" in claude["hooks"]["PostToolUseFailure"][0]["hooks"][0]["command"]
    cursor = json.loads((path / ".cursor/hooks.json").read_text())
    assert set(cursor["hooks"]) == {"postToolUse", "postToolUseFailure"} and cursor["version"] == 1
    copilot = json.loads((path / ".github/hooks/fbl-commands.json").read_text())
    assert set(copilot["hooks"]) == {"postToolUse", "postToolUseFailure"} and "powershell" in copilot["hooks"]["postToolUse"][0]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True).stdout
    assert "settings.local.json" not in status and "hooks.json" not in status and "fbl-commands" not in status
    before = (path / ".claude/settings.local.json").read_text()
    assert install(path, tmp_path).returncode == 0
    assert (path / ".claude/settings.local.json").read_text() == before


def test_hook_command_installed_for_claude_really_records(tmp_path: Path):
    path = repo(tmp_path)
    assert install(path, tmp_path, "--harness", "claude").returncode == 0
    command = json.loads((path / ".claude/settings.local.json").read_text())["hooks"]["PostToolUseFailure"][0]["hooks"][0]["command"]
    payload = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {"command": "pytest"}, "exit_code": 1, "cwd": str(path)}
    ran = subprocess.run(command, shell=True, input=json.dumps(payload), cwd=path, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert events(path)[0]["exit_code"] == 1


def test_tracked_cursor_config_is_left_alone_and_flag_disables_capture(tmp_path: Path):
    path = repo(tmp_path)
    (path / ".cursor").mkdir()
    (path / ".cursor/hooks.json").write_text('{"version": 1, "hooks": {}}\n')
    subprocess.run(["git", "add", ".cursor/hooks.json"], cwd=path, check=True)
    assert install(path, tmp_path).returncode == 0
    assert (path / ".cursor/hooks.json").read_text() == '{"version": 1, "hooks": {}}\n'
    other = repo(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    assert install(other, tmp_path / "x", "--no-command-capture").returncode == 0
    assert not (other / ".claude/settings.local.json").exists() and not (other / ".github/hooks").exists()


def test_uninstall_removes_only_our_hook_entries(tmp_path: Path):
    path = repo(tmp_path)
    (path / ".claude").mkdir()
    (path / ".claude/settings.local.json").write_text(json.dumps({"permissions": {"allow": ["x"]}}))
    assert install(path, tmp_path).returncode == 0
    out = subprocess.run([sys.executable, str(INSTALL), "--repo", str(path), "--uninstall"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads((path / ".claude/settings.local.json").read_text()) == {"permissions": {"allow": ["x"]}}
    assert not (path / ".cursor/hooks.json").exists() and not (path / ".github/hooks/fbl-commands.json").exists()

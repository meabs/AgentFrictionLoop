from __future__ import annotations

import json
from pathlib import Path

from jsonschema import validate

from feedback_loop.codex_adapter import enrich_record


def _event(timestamp: str, event_type: str, payload: dict) -> dict:
    return {"timestamp": timestamp, "type": event_type, "payload": payload}


def _rollout(path: Path, repo: Path, command_input: str, output: str, *, complete: bool = True) -> None:
    events = [
        _event("2026-09-29T10:00:00Z", "session_meta", {"id": "session-1", "cwd": str(repo), "timestamp": "2026-09-29T10:00:00Z", "cli_version": "1.2.3"}),
        _event("2026-09-29T10:00:01Z", "event_msg", {"type": "task_started"}),
        _event("2026-09-29T10:00:02Z", "response_item", {"type": "turn_context", "model": "test-model"}),
        _event("2026-09-29T10:00:03Z", "response_item", {"type": "custom_tool_call", "call_id": "call-1", "input": command_input}),
        _event("2026-09-29T10:00:04Z", "response_item", {"type": "custom_tool_call_output", "call_id": "call-1", "output": [{"type": "input_text", "text": output}]}),
        _event("2026-09-29T10:00:05Z", "token_usage_record", {"thread_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30, "reasoning_output_tokens": 4, "total_tokens": 130}}),
    ]
    if complete:
        events.append(_event("2026-09-29T10:00:06Z", "event_msg", {"type": "task_complete"}))
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def _record(repo: Path) -> dict:
    return {"timestamp": "2026-09-29T10:00:05Z"}


def test_codex_adapter_extracts_bounded_execution_and_telemetry(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src.py").write_text("x\n", encoding="utf-8")
    session = tmp_path / "rollout.jsonl"
    command = "tools.exec_command({cmd:" + json.dumps("A=b uv run pytest tests/test_app.py && rg needle src.py && cat src.py") + ",workdir:" + json.dumps(str(repo)) + "})\n*** Begin Patch\n"
    _rollout(session, repo, command, '{"exit_code": 1, "output": "src.py:1"}')
    result = enrich_record(_record(repo), repo, tmp_path / "insights", session)

    assert result["agent"] == {"harness": "codex"}
    execution = result["execution"]
    assert execution["commands_total"] == 3
    assert execution["commands_failed"] == 1
    assert execution["failed_commands"] == [{"signature": "cat src.py", "exit_code": 1, "count": 1}]
    assert execution["files_opened"] == []
    assert execution["file_read_counts"] == {}
    assert execution["search_count"] == 1
    assert result["harness_telemetry"]["session_id"] == "session-1"
    assert result["harness_telemetry"]["total_tokens"] == 130
    assert "src.py:1" not in json.dumps(result)


def test_codex_adapter_matches_latest_session_and_requires_time_overlap(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = tmp_path / "sessions"
    (sessions / "a/b/c").mkdir(parents=True)
    first = sessions / "a/b/c/rollout-one.jsonl"
    _rollout(first, repo, 'tools.exec_command({cmd:"cat one.txt"})', '{"exit_code":0}')
    second = sessions / "a/b/c/rollout-two.jsonl"
    _rollout(second, repo, 'tools.exec_command({cmd:"cat two.txt"})', '{"exit_code":0}')
    text = second.read_text(encoding="utf-8").replace('"2026-09-29T10:00:00Z"', '"2026-09-29T10:00:01Z"')
    second.write_text(text, encoding="utf-8")
    result = enrich_record(_record(repo), repo, tmp_path / "insights", sessions)
    assert result["execution"]["files_opened"] == ["two.txt"]

    no_match = enrich_record({"timestamp": "2026-09-29T12:00:00Z"}, repo, tmp_path / "insights", first)
    assert no_match == {"timestamp": "2026-09-29T12:00:00Z"}


def test_codex_adapter_read_options_only_record_paths(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for path in ("backend/tests/conftest.py", "backend/app.py", "backend/numbered.txt", "backend/script.sed"):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    session = tmp_path / "rollout.jsonl"
    command = (
        "tools.exec_command({cmd:"
        + json.dumps(
            "sed -n '1,220p' backend/tests/conftest.py | head -n 20 backend/app.py && "
            "sed -n -e '70,120p' -f backend/script.sed backend/app.py && "
            "nl -ba backend/numbered.txt"
        )
        + ",workdir:"
        + json.dumps(str(repo))
        + "})"
    )
    _rollout(session, repo, command, '{"exit_code": 0}')

    execution = enrich_record(_record(repo), repo, tmp_path / "insights", session)["execution"]

    assert execution["files_opened"] == [
        "backend/app.py",
        "backend/numbered.txt",
        "backend/script.sed",
        "backend/tests/conftest.py",
    ]


def test_codex_adapter_uses_nested_model_and_compound_failure_rules(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    session = tmp_path / "rollout.jsonl"
    events = [
        _event("2026-09-29T10:00:00Z", "session_meta", {"id": "session-2", "cwd": str(repo), "timestamp": "2026-09-29T10:00:00Z", "cli_version": "1.2.3"}),
        _event("2026-09-29T10:00:01Z", "event_msg", {"type": "task_started"}),
        _event("2026-09-29T10:00:02Z", "response_item", {"type": "turn_context", "payload": {"model": "nested-model"}}),
        _event("2026-09-29T10:00:03Z", "response_item", {"type": "custom_tool_call", "call_id": "call-2", "input": "tools.exec_command({cmd:" + json.dumps(
            "cd backend && uv run pytest tests/test_app.py"
        ) + "})"}),
        _event("2026-09-29T10:00:04Z", "response_item", {"type": "custom_tool_call_output", "call_id": "call-2", "output": [{"type": "input_text", "text": '{"exit_code": 2}'}]}),
        _event("2026-09-29T10:00:05Z", "response_item", {"type": "custom_tool_call", "call_id": "call-3", "input": "tools.exec_command({cmd:" + json.dumps(
            "rg absent backend/app.py | head -120 && git diff --exit-code && test -f backend/app.py"
        ) + "})"}),
        _event("2026-09-29T10:00:06Z", "response_item", {"type": "custom_tool_call_output", "call_id": "call-3", "output": [{"type": "input_text", "text": '{"exit_code": 1}'}]}),
        _event("2026-09-29T10:00:07Z", "event_msg", {"type": "task_complete"}),
    ]
    session.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    result = enrich_record(_record(repo), repo, tmp_path / "insights", session)

    assert result["harness_telemetry"]["model"] == "nested-model"
    assert result["execution"]["commands_failed"] == 1
    assert result["execution"]["failed_commands"] == [{"signature": "uv run pytest", "exit_code": 2, "count": 1}]


def test_codex_adapter_follows_long_running_write_stdin(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    session = tmp_path / "rollout.jsonl"
    exec_input = "tools.exec_command({cmd:" + json.dumps("uv run pytest") + ",workdir:" + json.dumps(str(repo)) + "})"
    stdin_input = "tools.write_stdin({session_id:42,chars:\"\",yield_time_ms:1000})"
    events = [
        _event("2026-09-29T10:00:00Z", "session_meta", {"id": "session-3", "cwd": str(repo), "timestamp": "2026-09-29T10:00:00Z"}),
        _event("2026-09-29T10:00:01Z", "response_item", {"type": "custom_tool_call", "call_id": "exec-1", "input": exec_input}),
        _event("2026-09-29T10:00:02Z", "response_item", {"type": "custom_tool_call_output", "call_id": "exec-1", "output": [{"type": "input_text", "text": '{"session_id":42}'}]}),
        _event("2026-09-29T10:00:03Z", "response_item", {"type": "custom_tool_call", "call_id": "stdin-1", "input": stdin_input}),
        _event("2026-09-29T10:00:04Z", "response_item", {"type": "custom_tool_call_output", "call_id": "stdin-1", "output": [{"type": "input_text", "text": '{"session_id":42,"exit_code":0}'}]}),
    ]
    session.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    execution = enrich_record(_record(repo), repo, tmp_path / "insights", session)["execution"]

    assert execution["commands_failed"] == 0
    assert execution["verification_attempts"] == [{"kind": "tests", "exit_code": 0}]
    assert execution["first_verification_passed"] is True


def test_schema_allows_nullable_stage2_fields():
    schema = json.loads(Path("schemas/analysis-v1.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["harness_telemetry"]["properties"]["model"] == {"type": ["string", "null"]}
    assert schema["properties"]["execution"]["properties"]["verification_attempts"]["items"]["properties"]["exit_code"] == {"type": ["integer", "null"]}
    validate(
        {
            "schema_version": "1.0",
            "record_id": "repo@123456789012",
            "domain": "test",
            "repository": "repo",
            "commit": "0" * 40,
            "commit_short": "0" * 12,
            "parents": [],
            "timestamp": "2026-09-29T10:00:00Z",
            "analysed_at": "2026-09-29T10:00:00Z",
            "changed_files": [],
            "diff_stats": {"files": 0, "insertions": 0, "deletions": 0, "by_category": {key: 0 for key in ("source", "tests", "docs", "config", "guidance", "other")}},
            "verification": {"tests": "unknown", "lint": "unknown", "build": "unknown"},
            "documentation": {key: False for key in ("agents_md_present", "claude_md_present", "service_yaml_present", "okf_present", "adr_present", "api_schema_present", "skills_present", "readme_present", "relevant_docs_changed")},
            "guidance": {"digest": "0" * 64, "file_count": 0, "skills_available": [], "agents_md_referenced_paths": [], "guidance_changed": False},
            "observations": [],
            "agent": {"harness": "codex"},
            "execution": {"verification_attempts": [{"kind": "tests", "exit_code": None}]},
            "harness_telemetry": {"model": None},
        },
        schema,
    )

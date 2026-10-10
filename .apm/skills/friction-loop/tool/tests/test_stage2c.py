from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import validate

from feedback_loop.codex_adapter import enrich_record




def event(timestamp: str, event_type: str, payload: dict) -> dict:
    return {"timestamp": timestamp, "ordinal": 1, "type": event_type, "payload": payload}


def command(timestamp: str, command_text: str, cwd: Path, exit_code: int | None, *, status: str | None = None) -> dict:
    return event(timestamp, "event_msg", {"type": "item_completed", "item": {
        "type": "CommandExecution", "command": ["/bin/zsh", "-lc", command_text],
        "cwd": f"file://{cwd}", "status": status or ("failed" if exit_code else "completed"), "exit_code": exit_code,
        "duration": {"secs": 0, "nanos": 1}, "aggregated_output": "",
    }})


def write_rollout(path: Path, repo: Path, commands: list[tuple[str, int | None]], *, model: str = "gpt-real", original_cwd: Path | None = None, fallback: bool = False, first_pass: bool = True) -> None:
    cwd = original_cwd or repo
    lines = [
        event("2026-09-29T10:00:00Z", "session_meta", {"id": "real-session", "cwd": str(cwd), "timestamp": "2026-09-29T10:00:00Z"}),
        event("2026-09-29T10:00:01Z", "turn_context", {"model": model, "cwd": str(cwd), "effort": "medium"}),
        event("2026-09-29T10:00:02Z", "event_msg", {"type": "task_started"}),
    ]
    for index, (text, exit_code) in enumerate(commands, 3):
        if fallback:
            input_text = "tools.exec_command({cmd:" + json.dumps(text) + ",workdir:" + json.dumps(str(repo)) + "})"
            lines.extend([
                event(f"2026-09-29T10:00:{index:02d}Z", "response_item", {"type": "custom_tool_call", "call_id": f"call-{index}", "input": input_text}),
                event(f"2026-09-29T10:00:{index + 1:02d}Z", "response_item", {"type": "custom_tool_call_output", "call_id": f"call-{index}", "output": [{"type": "input_text", "text": json.dumps({"exit_code": exit_code})}]}),
            ])
        else:
            lines.append(command(f"2026-09-29T10:00:{index:02d}Z", text, cwd, exit_code))
    if not fallback:
        lines.append(event("2026-09-29T10:01:00Z", "event_msg", {"type": "item_completed", "item": {"type": "FileChange", "changes": {str(cwd / "changed.py"): {"type": "update"}}}}))
    lines.extend([
        event("2026-09-29T10:02:00Z", "token_usage_record", {"thread_token_usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 3, "reasoning_output_tokens": 1, "total_tokens": 13}}),
        event("2026-09-29T10:03:00Z", "event_msg", {"type": "task_complete"}),
    ])
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def record() -> dict:
    return {"timestamp": "2026-09-29T10:00:00Z"}


def test_real_events_extract_model_count_exit_attribution_and_benign_exit_one(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    session = tmp_path / "rollout.jsonl"
    write_rollout(session, repo, [("git diff --check && cd backend && uv run pytest -q", 1), ("rg missing app.py", 1), ("uv run ruff check app.py", 1)])

    result = enrich_record(record(), repo, tmp_path / "insights", session)
    execution = result["execution"]
    assert result["harness_telemetry"]["model"] == "gpt-real"
    assert execution["commands_total"] == 3
    assert execution["commands_failed"] == 2
    assert execution["failed_commands"] == [
        {"signature": "uv run pytest", "exit_code": 1, "count": 1},
        {"signature": "uv run ruff", "exit_code": 1, "count": 1},
    ]
    assert execution["verification_attempts"] == [{"kind": "tests", "exit_code": 1}, {"kind": "lint", "exit_code": 1}]


@pytest.mark.parametrize("exit_code, expected", [(0, True), (1, False)])
def test_real_verification_first_pass_flag(tmp_path: Path, exit_code: int, expected: bool):
    repo = tmp_path / "repo"
    repo.mkdir()
    session = tmp_path / f"rollout-{exit_code}.jsonl"
    write_rollout(session, repo, [("uv run pytest -q", exit_code)])
    assert enrich_record(record(), repo, tmp_path / "insights", session)["execution"]["first_verification_passed"] is expected


def test_real_reads_rebase_cwd_and_count_file_changes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    original = tmp_path / "old" / "fastapi-app"
    session = tmp_path / "rollout.jsonl"
    write_rollout(session, repo, [("sed -n '1,20p' backend/app.py && cat backend/tests/conftest.py && head -n 5 backend/app.py && nl -ba backend/app.py", 0)], original_cwd=original)
    execution = enrich_record(record(), repo, tmp_path / "insights", session)["execution"]
    assert execution["files_opened"] == ["backend/app.py", "backend/tests/conftest.py"]
    assert not any(re.match(r"^\d+(,\d+)?p$", path) for path in execution["files_opened"])
    assert execution["edit_count"] == 1
    assert execution["edited_files"] == ["changed.py"]


def test_read_openings_require_success_or_unknown_top_level_exit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    session = tmp_path / "rollout.jsonl"
    write_rollout(session, repo, [
        ("sed -n '1,20p' .agents/skills/missing/SKILL.md", 1),
        ("sed -n '1,20p' .agents/skills/missing/SKILL.md", 1),
        ("cat .agents/skills/good/SKILL.md", 0),
        ("cat a.md && uv run pytest -q", 1),
        ("cat unknown.md", None),
    ])

    execution = enrich_record(record(), repo, tmp_path / "insights", session)["execution"]

    assert execution["files_opened"] == [".agents/skills/good/SKILL.md", "a.md", "unknown.md"]
    assert execution["file_read_counts"] == {}
    assert execution["skills_loaded"] == ["good"]
    assert execution["guidance_files_opened"] == [".agents/skills/good/SKILL.md"]
    assert execution["failed_commands"] == [
        {"signature": "sed", "exit_code": 1, "count": 2},
        {"signature": "uv run pytest", "exit_code": 1, "count": 1},
    ]


def test_custom_tool_read_openings_use_resolved_exit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    session = tmp_path / "rollout.jsonl"
    write_rollout(session, repo, [
        ("sed -n '1,20p' .agents/skills/missing/SKILL.md", 1),
        ("cat a.md && uv run pytest -q", 1),
        ("cat unknown.md", None),
    ], fallback=True)

    execution = enrich_record(record(), repo, tmp_path / "insights", session)["execution"]

    assert execution["files_opened"] == ["a.md", "unknown.md"]
    assert execution["failed_commands"] == [
        {"signature": "sed", "exit_code": 1, "count": 1},
        {"signature": "uv run pytest", "exit_code": 1, "count": 1},
    ]


def test_custom_tool_fallback_remains_supported(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    session = tmp_path / "rollout.jsonl"
    write_rollout(session, repo, [("cat fallback.txt", 0)], fallback=True)
    result = enrich_record(record(), repo, tmp_path / "insights", session)
    assert result["execution"]["commands_total"] == 1
    assert result["execution"]["files_opened"] == ["fallback.txt"]


def test_real_result_is_schema_valid(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    session = tmp_path / "rollout.jsonl"
    write_rollout(session, repo, [("uv run pytest -q", 0)])
    result = enrich_record(record(), repo, tmp_path / "insights", session)
    schema = json.loads(Path("schemas/analysis-v1.schema.json").read_text(encoding="utf-8"))
    validate({"schema_version": "1.0", "record_id": "x", "domain": "x", "repository": "x", "commit": "0" * 40, "commit_short": "0" * 12, "parents": [], "timestamp": record()["timestamp"], "analysed_at": record()["timestamp"], "changed_files": [], "diff_stats": {"files": 0, "insertions": 0, "deletions": 0, "by_category": {key: 0 for key in ("source", "tests", "docs", "config", "guidance", "other")}}, "verification": {"tests": "unknown", "lint": "unknown", "build": "unknown"}, "documentation": {key: False for key in ("agents_md_present", "claude_md_present", "service_yaml_present", "okf_present", "adr_present", "api_schema_present", "skills_present", "readme_present", "relevant_docs_changed")}, "guidance": {"digest": "0" * 64, "file_count": 0, "skills_available": [], "agents_md_referenced_paths": [], "guidance_changed": False}, "observations": [], **result}, schema)

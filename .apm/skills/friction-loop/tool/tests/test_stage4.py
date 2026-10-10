from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from feedback_loop.record import actor_from_email, build_record
from feedback_loop.report import facts_json, render_report, validate_report
from feedback_loop.review_core import detect_patterns, effect_analysis
from feedback_loop.store import actor_salt, persist


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/analysis-v1.schema.json"


def _record(number: int, actor: str | None = None, *, digest: str = "a" * 64) -> dict:
    item = {
        "record_id": f"repo@{number:012x}", "repository": "repo", "schema_version": "1.0", "domain": "test",
        "commit": f"{number:040x}", "commit_short": f"{number:012x}", "parents": [],
        "timestamp": f"2026-09-{number:02d}T00:00:00Z", "analysed_at": "2026-09-29T00:00:00Z",
        "changed_files": [], "diff_stats": {"files": 0, "insertions": 0, "deletions": 0, "by_category": {k: 0 for k in ("source", "tests", "docs", "config", "guidance", "other")}},
        "verification": {"tests": "unknown", "lint": "unknown", "build": "unknown"},
        "documentation": {k: True for k in ("agents_md_present", "claude_md_present", "service_yaml_present", "okf_present", "adr_present", "api_schema_present", "skills_present", "readme_present", "relevant_docs_changed")},
        "guidance": {"digest": digest, "file_count": 0, "skills_available": [], "agents_md_referenced_paths": [], "guidance_changed": False},
        "observations": [], "agent": {"harness": "unknown"},
        "execution": {"verification_attempts": [], "failed_commands": [], "files_opened": []},
        "harness_telemetry": {"session_complete": True, "session_id": f"s{number}", "total_tokens": number},
    }
    if actor:
        item["actor"] = {"id": actor, "scheme": "hmac-sha256/v1", "kind": "human"}
    Draft202012Validator(json.loads(SCHEMA.read_text())).validate(item)
    return item


def test_actor_hmac_and_bot_and_missing_email(tmp_path: Path):
    assert actor_from_email(" Alice@Example.com ", "a" * 32) == actor_from_email("alice@example.com", "a" * 32)
    assert actor_from_email("alice@example.com", "a" * 32) != actor_from_email("alice@example.com", "b" * 32)
    assert actor_from_email("dev[bot]@users.noreply.github.com", "a" * 32)["kind"] == "bot"
    assert actor_from_email("", "a" * 32) is None


def test_scope_boundaries_and_concentration_is_not_recommended():
    def findings(actors):
        values = [_record(i + 1, actor) for i, actor in enumerate(actors)]
        for value in values:
            value["execution"]["failed_commands"] = [{"signature": "sed", "exit_code": 1, "count": 1}]
        return detect_patterns(values, min_evidence=1, min_actors=3)
    two = next(f for f in findings(["a" * 12, "b" * 12]) if f["pattern_class"] == "common_failed_command")
    assert two["scope"] == "limited" and two["actors"] == 2
    shared = next(f for f in findings(["a" * 12, "b" * 12, "c" * 12]) if f["pattern_class"] == "common_failed_command")
    assert shared["scope"] == "shared"
    concentrated = next(f for f in findings(["a" * 12] * 7 + ["b" * 12] * 3) if f["pattern_class"] == "common_failed_command")
    assert concentrated["scope"] == "concentrated"
    facts = {"records": concentrated["evidence"], "findings": [concentrated], "effects": [], "ledger": [], "invalid_records": 0, "incomplete_telemetry": 0}
    assert "Not recommended (concentrated in few people)" in render_report(facts)


def test_effect_mix_caps_dominated_verdict_and_report_validator_redacts_actor():
    before = [_record(i + 1, "a" * 12, digest="a" * 64) for i in range(1, 4)] + [_record(4, "b" * 12, digest="a" * 64)]
    after = [_record(i + 10, "a" * 12, digest="b" * 64) for i in range(10, 13)] + [_record(13, "b" * 12, digest="b" * 64)]
    effect = next(e for e in effect_analysis(before, after) if e["metric"] == "total_tokens")
    assert effect["actor_mix"]["before"]["distinct"] == 2
    assert "dominated by one actor" in effect["reasons"]
    facts = {"records": [], "findings": [], "effects": [], "ledger": [], "actor_ids": ["a" * 12], "invalid_records": 0, "incomplete_telemetry": 0}
    with pytest.raises(ValueError, match="actor id"):
        validate_report(render_report(facts) + "\n" + "a" * 12, facts)
    assert "a" * 12 not in facts_json(facts)


def test_store_record_is_schema_valid_and_does_not_store_email(tmp_path: Path):
    repo = tmp_path / "repo"; repo.mkdir(); subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "x.py").write_text("x\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Private Name", "GIT_AUTHOR_EMAIL": "Private@Example.com", "GIT_COMMITTER_NAME": "Private Name", "GIT_COMMITTER_EMAIL": "Private@Example.com"}
    subprocess.run(["git", "add", "."], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "x"], cwd=repo, env=env, check=True, capture_output=True)
    insights = tmp_path / "insights"; salt = actor_salt(insights); record = build_record(repo, actor_salt=salt)
    persist(record, insights, SCHEMA, no_commit=True)
    text = next((insights / "records").rglob("*.json")).read_text()
    assert "Private Name" not in text and "Private@Example.com" not in text and record["actor"]["id"] in text

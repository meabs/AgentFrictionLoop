from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from feedback_loop.boundary_adapter import enrich_record
from feedback_loop.record import build_record
from feedback_loop.report import render_report, validate_report
from feedback_loop.review_core import detect_patterns, effect_analysis

ROOT = Path(__file__).parents[1]
SCHEMA = Draft202012Validator(json.loads((ROOT / "schemas/analysis-v1.schema.json").read_text()))


def repo(tmp_path: Path, when: str = "2026-09-29T12:00:00Z") -> tuple[Path, dict]:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "a@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Agent"], cwd=path, check=True)
    (path / "README.md").write_text("x\n")
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, env={**__import__("os").environ, **env}, check=True)
    record = build_record(path, "HEAD", "test", "repo", None, None)
    return path, record


def write_events(path: Path, values: list[dict]) -> None:
    target = path / ".git/fbl/boundary/events.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(json.dumps(item) for item in values) + "\n")


def event(ts: str, *, session="s", harness="cursor", kind="tests", code=0, signature="pytest", parent=None, typ="command", **extra):
    value = {"type": typ, "ts": ts, "duration_ms": 10, "exit_code": code, "kind": kind, "signature": signature, "parent_event_id": parent, "session": {"id": session, "source": "env"} if session else {"id": None, "source": "none"}, "harness": harness}
    value.update(extra)
    return value


def test_boundary_only_record_validates_without_tokens_or_cost(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:59:00Z")])
    result = enrich_record(record, path, tmp_path / "insights")
    SCHEMA.validate(result)
    assert result["harness_telemetry"]["evidence_level"] == "boundary"
    assert "total_tokens" not in result["harness_telemetry"] and "cost_usd" not in result["harness_telemetry"]


def test_cursor_environment_sets_harness_and_session(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:59:00Z", session="conversation-1", harness="cursor")])
    result = enrich_record(record, path, tmp_path / "i")
    assert result["agent"]["harness"] == "cursor" and result["harness_telemetry"]["session_id"] == "conversation-1"


def test_two_interleaved_named_sessions_choose_closest_latest(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:55:00Z", session="a", code=1), event("2026-09-29T11:59:00Z", session="b", code=0), event("2026-09-29T11:56:00Z", session="a", code=1)])
    assert enrich_record(record, path, tmp_path / "i")["harness_telemetry"]["session_id"] == "b"


def test_gap_sessionisation_uses_thirty_minute_boundary(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:00:00Z", session=None), event("2026-09-29T11:30:00Z", session=None)])
    result = enrich_record(record, path, tmp_path / "i")
    assert result["harness_telemetry"]["session_id"] == "gap-2026-09-29T11:00:00Z"


def test_gap_after_thirty_minutes_starts_new_session(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T10:00:00Z", session=None), event("2026-09-29T11:31:00Z", session=None)])
    result = enrich_record(record, path, tmp_path / "i")
    assert result["harness_telemetry"]["session_id"] == "gap-2026-09-29T11:31:00Z"


def test_nested_events_are_not_counted(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:59:00Z", code=1), event("2026-09-29T11:59:01Z", code=1, parent="outer")])
    assert enrich_record(record, path, tmp_path / "i")["execution"]["commands_total"] == 1


def test_environment_exit_codes_are_excluded_but_preserved(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:59:00Z", code=127), event("2026-09-29T11:59:01Z", code=126)])
    execution = enrich_record(record, path, tmp_path / "i")["execution"]
    assert execution["commands_failed"] == 0 and len(execution["failed_commands"]) == 2


def test_verification_order_and_first_pass(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:58:00Z", kind="lint", code=0), event("2026-09-29T11:59:00Z", kind="tests", code=1)])
    execution = enrich_record(record, path, tmp_path / "i")["execution"]
    assert [x["kind"] for x in execution["verification_attempts"]] == ["lint", "tests"] and execution["first_verification_passed"] is True


def test_migrate_maps_to_migration(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:59:00Z", kind="migrate")])
    assert enrich_record(record, path, tmp_path / "i")["execution"]["verification_attempts"] == [{"kind": "migration", "exit_code": 0}]


def test_codex_execution_is_cross_checked_without_overwrite(tmp_path):
    path, record = repo(tmp_path)
    old = {"commands_total": 9, "custom": "kept"}
    record["execution"] = old.copy(); record["harness_telemetry"] = {"harness": "codex", "session_id": "c", "total_tokens": 4}; record["agent"] = {"harness": "codex"}
    write_events(path, [event("2026-09-29T11:59:00Z")])
    result = enrich_record(record, path, tmp_path / "i")
    assert result["execution"]["commands_total"] == 9 and result["execution"]["custom"] == "kept" and result["execution"]["boundary"]["commands_total"] == 1
    assert result["harness_telemetry"]["total_tokens"] == 4


def test_no_events_returns_same_object_content(tmp_path):
    path, record = repo(tmp_path)
    before = json.loads(json.dumps(record))
    assert enrich_record(record, path, tmp_path / "i") == before


def test_malformed_jsonl_is_skipped(tmp_path):
    path, record = repo(tmp_path)
    target = path / ".git/fbl/boundary/events.jsonl"; target.parent.mkdir(parents=True); target.write_text("{bad\n" + json.dumps(event("2026-09-29T11:59:00Z")))
    assert enrich_record(record, path, tmp_path / "i")["execution"]["commands_total"] == 1


def test_event_friction_is_sanitised_and_deduplicated(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:59:00Z", typ="friction", category="setup", text="need\nsetup"), event("2026-09-29T11:59:01Z", typ="friction", category="setup", text="need setup")])
    assert enrich_record(record, path, tmp_path / "i")["friction"] == [{"category": "setup", "text": "needsetup", "source": "event"}, {"category": "setup", "text": "need setup", "source": "event"}]


def test_secret_friction_is_dropped(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:59:00Z", typ="friction", category="other", text="password=hunter2")])
    assert "friction" not in enrich_record(record, path, tmp_path / "i")


def test_trailer_friction_is_added(tmp_path):
    path, record = repo(tmp_path)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "change\n\nFriction[test-order]: tests need setup"], cwd=path, check=True)
    record = build_record(path, "HEAD", "test", "repo", None, None)
    write_events(path, [event("2026-09-29T11:59:00Z")])
    assert enrich_record(record, path, tmp_path / "i")["friction"][0]["category"] == "test-order"


def test_trailer_category_defaults_to_other(tmp_path):
    path, record = repo(tmp_path)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "change\n\nFriction: note"], cwd=path, check=True)
    record = build_record(path, "HEAD", "test", "repo", None, None); write_events(path, [event("2026-09-29T11:59:00Z")])
    assert enrich_record(record, path, tmp_path / "i")["friction"][0]["category"] == "other"


def test_friction_is_capped_at_twenty(tmp_path):
    path, record = repo(tmp_path)
    values = [event((dt.datetime(2026, 9, 29, 11, 40) + dt.timedelta(seconds=i)).isoformat() + "Z", typ="friction", category="tooling", text=f"n{i}") for i in range(20)]
    values.append(event("2026-09-29T11:59:00Z", typ="friction", category="tooling", text="n20"))
    write_events(path, values)
    assert len(enrich_record(record, path, tmp_path / "i")["friction"]) == 20


def base_review_record(i, *, friction=None, attempts=None, repo_name="repo"):
    return {"record_id": f"{repo_name}@{i:012x}", "repository": repo_name, "timestamp": f"2026-09-{i:02d}T00:00:00Z", "guidance": {"digest": "a" * 64}, "agent": {"harness": "cursor"}, "harness_telemetry": {"session_id": f"s{i}", "session_complete": True}, "execution": {"verification_attempts": attempts or []}, "friction": friction or [], "documentation": {"service_yaml_present": True, "okf_present": True, "api_schema_present": True}, "actor": {"id": f"{i:012x}", "kind": "bot"}}


def test_friction_detector_fires_at_threshold(tmp_path):
    values = [base_review_record(i, friction=[{"category": "setup", "text": f"note {i}", "source": "event"}]) for i in (1, 2, 3)]
    finding = next(f for f in detect_patterns(values, 3) if f["pattern_class"] == "self_reported_friction")
    assert finding["established"] and finding["evidence_count"] == 3


def test_friction_detector_is_below_threshold_at_min_minus_one(tmp_path):
    values = [base_review_record(i, friction=[{"category": "setup", "text": f"note {i}", "source": "event"}]) for i in (1, 2)]
    finding = next(f for f in detect_patterns(values, 3) if f["pattern_class"] == "self_reported_friction")
    assert not finding["established"]


def test_environment_friction_has_no_recommendation(tmp_path):
    values = [base_review_record(i, friction=[{"category": "environment", "text": "env", "source": "event"}]) for i in (1, 2, 3)]
    finding = next(f for f in detect_patterns(values, 3) if f["pattern_class"] == "self_reported_friction")
    assert finding["recommendation"] is False


def test_friction_samples_are_facts_but_not_rendered(tmp_path):
    values = [base_review_record(i, friction=[{"category": "setup", "text": "secret-ish sample", "source": "event"}]) for i in (1, 2, 3)]
    facts = {"records": [r["record_id"] for r in values], "findings": detect_patterns(values, 3), "effects": [], "ledger": [], "actor_ids": []}
    markdown = render_report(facts); validate_report(markdown, facts)
    assert "secret-ish sample" not in markdown
    with pytest.raises(ValueError): validate_report(markdown + "\nsecret-ish sample", facts)


def test_agreement_counts_friction_and_first_failure(tmp_path):
    values = [base_review_record(i, friction=[{"category": "test-order", "text": "x", "source": "event"}], attempts=[{"kind": "tests", "exit_code": 1}]) for i in (1, 2, 3)]
    values += [base_review_record(4, friction=[{"category": "test-order", "text": "x", "source": "event"}], attempts=[{"kind": "tests", "exit_code": 0}])]
    finding = next(f for f in detect_patterns(values, 3) if f["pattern_class"] == "self_reported_friction")
    assert finding["agreement"]["agreeing_sessions"] == 3


def test_boundary_only_effects_skip_missing_token_metrics(tmp_path):
    left = [base_review_record(1)]; right = [base_review_record(2)]
    assert all(item["metric"] not in {"total_tokens", "cost_usd"} for item in effect_analysis(left, right))


def test_comma_adapter_parser_accepts_known_values_and_rejects_unknown():
    import runpy
    module = runpy.run_path(str(ROOT / "analyse-change.py"))
    assert module["_adapters"]("codex,boundary") == ["codex", "boundary"]
    with pytest.raises(Exception): module["_adapters"]("boundary,nope")


def test_report_contains_self_reported_friction_finding(tmp_path):
    values = [base_review_record(i, friction=[{"category": "setup", "text": f"x{i}", "source": "event"}]) for i in (1, 2, 3)]
    facts = {"records": [r["record_id"] for r in values], "findings": detect_patterns(values, 3), "effects": [], "ledger": [], "actor_ids": []}
    markdown = render_report(facts)
    assert "self-reported 'setup' friction" in markdown


def test_end_to_end_cursor_boundary_analysis_and_review(tmp_path):
    path = tmp_path / "e2e"; path.mkdir()
    env = dict(__import__("os").environ)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True); subprocess.run(["git", "config", "user.email", "a@example.test"], cwd=path, check=True); subprocess.run(["git", "config", "user.name", "Agent"], cwd=path, check=True)
    (path / "README.md").write_text("x\n"); subprocess.run(["git", "add", "."], cwd=path, check=True); subprocess.run(["git", "commit", "-qm", "base"], cwd=path, env=env, check=True)
    subprocess.run([sys.executable, str(ROOT / "boundary.py"), "install", "--repo", str(path)], cwd=path, env=env, check=True, capture_output=True)
    insights = tmp_path / "insights"
    for number in range(3):
        session = f"cursor-{number}"; run_env = {**env, "CURSOR_AGENT": "1", "CURSOR_CONVERSATION_ID": session, "FBL_SESSION_ID": session}
        subprocess.run([str(path / ".fbl/bin/fbl-friction"), "--category", "test-order", "tests need setup"], cwd=path, env=run_env, check=True, capture_output=True)
        (path / f"file{number}.txt").write_text(str(number)); subprocess.run(["git", "add", "."], cwd=path, env=env, check=True); subprocess.run(["git", "commit", "-qm", f"change {number}\n\nFriction[test-order]: tests need setup"], cwd=path, env=env, check=True)
        subprocess.run([sys.executable, str(ROOT / "analyse-change.py"), "--repo", str(path), "--commit", "HEAD", "--insights", str(insights), "--adapter", "boundary", "--no-commit"], cwd=ROOT, env=env, check=True)
    out = tmp_path / "report.md"
    subprocess.run([sys.executable, str(ROOT / "review.py"), "--insights", str(insights), "--min-evidence", "3", "--out", str(out)], cwd=ROOT, env=env, check=True)
    text = out.read_text(); assert "self-reported 'test-order' friction" in text
    facts = json.loads((tmp_path / "report.facts.json").read_text()); validate_report(text, facts)


def test_friction_filed_after_the_commit_is_attributed_to_a_named_session(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T12:03:00Z", typ="friction", category="test-order", text="ran tests before migrate")])
    result = enrich_record(record, path, tmp_path / "i")
    assert result["friction"] == [{"category": "test-order", "text": "ran tests before migrate", "source": "event"}]
    assert result["harness_telemetry"]["session_id"] == "s"


def test_late_friction_beyond_grace_or_without_session_is_not_attributed(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T12:20:00Z", typ="friction", category="other", text="too late")])
    assert "friction" not in enrich_record(record, path, tmp_path / "i")
    write_events(path, [event("2026-09-29T12:03:00Z", session=None, typ="friction", category="other", text="no session")])
    assert "friction" not in enrich_record(record, path, tmp_path / "i")


def test_late_command_events_are_still_excluded(tmp_path):
    path, record = repo(tmp_path)
    write_events(path, [event("2026-09-29T11:59:00Z"), event("2026-09-29T12:03:00Z"), event("2026-09-29T12:03:30Z", typ="friction", category="other", text="late note")])
    result = enrich_record(record, path, tmp_path / "i")
    assert result["execution"]["commands_total"] == 1
    assert result["friction"][0]["text"] == "late note"

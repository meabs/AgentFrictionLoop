from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from feedback_loop.record import actor_from_email
from feedback_loop.report import facts_json, render_report, validate_report
from feedback_loop.review_core import build_facts, classify_scope, detect_patterns, effect_analysis
from feedback_loop.store import actor_salt

from test_stage4 import _record


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/analysis-v1.schema.json").read_text())


def records(tmp_path: Path, actors: list[tuple[str, str] | None]) -> list[dict]:
    result = []
    for number, actor in enumerate(actors, 1):
        seed = (number - 1) % 28 + 1
        item = _record(seed, actor[0] if actor else None)
        item["record_id"] = f"repo@{number:012x}"
        item["commit_short"] = f"{number:012x}"
        item["commit"] = f"{number:040x}"
        item["harness_telemetry"]["session_id"] = f"s{number}"
        if actor and "actor" in item:
            item["actor"]["kind"] = actor[1]
        item["execution"]["failed_commands"] = [{"signature": "sed", "exit_code": 1, "count": 1}]
        Draft202012Validator(SCHEMA).validate(item)
        result.append(item)
    return result


def write_records(insights: Path, values: list[dict]) -> None:
    for value in values:
        path = insights / "records" / "test" / "repo" / "2026-09" / f"{value['commit_short']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


def failed_finding(values: list[dict], *, min_actors: int = 3) -> dict:
    return next(f for f in detect_patterns(values, min_evidence=1, min_actors=min_actors) if f["pattern_class"] == "common_failed_command")


def test_scope_is_shared_at_exactly_three_actors():
    values = records(Path("."), [("a" * 12, "human"), ("b" * 12, "human"), ("c" * 12, "human")])
    assert classify_scope(values, 3) == "shared"
    assert failed_finding(values)["scope"] == "shared"


def test_scope_is_limited_for_two_evenly_split_actors():
    values = records(Path("."), [("a" * 12, "human"), ("b" * 12, "human")])
    assert classify_scope(values, 3) == "limited"
    assert failed_finding(values)["scope"] == "limited"


def test_scope_is_concentrated_at_seventy_percent():
    values = records(Path("."), [("a" * 12, "human")] * 7 + [("b" * 12, "human")] * 3)
    assert classify_scope(values, 3) == "concentrated"
    assert failed_finding(values)["scope"] == "concentrated"


def test_scope_is_not_concentrated_at_sixty_nine_percent():
    values = records(Path("."), [("a" * 12, "human")] * 69 + [("b" * 12, "human")] * 31)
    assert classify_scope(values, 3) == "limited"
    assert failed_finding(values)["scope"] == "limited"


def test_unattributed_records_preserve_findings_and_recommendations():
    with_actors = records(Path("."), [("a" * 12, "human")] * 3)
    old = [dict(value) for value in with_actors]
    for value in old:
        value.pop("actor", None)
    old_findings = detect_patterns(old, min_evidence=1)
    new_findings = detect_patterns(with_actors, min_evidence=1)
    assert old_findings and all(f["scope"] == "unattributed" for f in old_findings)
    assert [(f["pattern_class"], f["observation"], f["recommendation"]) for f in old_findings] == [(f["pattern_class"], f["observation"], f["recommendation"]) for f in new_findings]
    facts = {"findings": old_findings, "records": [], "effects": [], "ledger": []}
    assert "Recommended improvements" in render_report(facts)


def test_bots_are_excluded_from_scope_and_share():
    values = records(Path("."), [("a" * 12, "human"), ("b" * 12, "human"), ("c" * 12, "bot")])
    assert classify_scope(values, 3) == "limited"
    assert failed_finding(values)["attributed_records"] == 2
    assert classify_scope(records(Path("."), [("c" * 12, "bot")]), 3) == "unattributed"


def test_concentrated_is_counts_only_and_limited_is_marked_recommended():
    concentrated = failed_finding(records(Path("."), [("a" * 12, "human")] * 7 + [("b" * 12, "human")] * 3))
    limited = failed_finding(records(Path("."), [("a" * 12, "human"), ("b" * 12, "human")]))
    concentrated["id"], limited["id"] = "F-C", "F-L"
    facts = {"records": concentrated["evidence"] + limited["evidence"], "findings": [concentrated, limited], "effects": [], "ledger": []}
    report = render_report(facts)
    recommendation = report.split("## Recommended improvements", 1)[1]
    assert concentrated["id"] not in recommendation.split("Not recommended", 1)[0]
    assert "Not recommended (concentrated in few people)" in recommendation
    assert f"{limited['id']}: Observation:" in recommendation and "(limited spread)" in recommendation


def test_effect_is_capped_only_for_dominated_multi_actor_mix():
    before = records(Path("."), [("a" * 12, "human")] * 3 + [("b" * 12, "human")])
    after = records(Path("."), [("a" * 12, "human")] * 3 + [("b" * 12, "human")])
    for value in after:
        value["harness_telemetry"]["total_tokens"] = 200
    dominated = next(e for e in effect_analysis(before, after) if e["metric"] == "total_tokens")
    assert dominated["verdict"] == "directional" and "dominated by one actor" in dominated["reasons"]
    balanced_before = records(Path("."), [("a" * 12, "human")] * 2 + [("b" * 12, "human")] * 2)
    balanced_after = records(Path("."), [("a" * 12, "human")] * 2 + [("b" * 12, "human")] * 2)
    balanced = next(e for e in effect_analysis(balanced_before, balanced_after) if e["metric"] == "total_tokens")
    assert "dominated by one actor" not in balanced["reasons"]


def test_single_author_report_has_person_effect_caveat():
    values = records(Path("."), [("a" * 12, "human")])
    facts = {"records": [values[0]["record_id"]], "findings": [], "effects": [], "ledger": [], "repositories": ["repo"], "sessions": 1, "guidance_epochs": [], "actor_distinct": 1, "invalid_records": 0, "incomplete_telemetry": 0}
    assert "single author: person effect cannot be separated from guidance effect" in render_report(facts)


def test_mine_filters_to_requested_author_and_never_prints_other_actor(tmp_path: Path):
    insights = tmp_path / "insights"
    salt = actor_salt(insights)
    alice = actor_from_email("Alice@example.com", salt)["id"]
    bob = actor_from_email("bob@example.com", salt)["id"]
    write_records(insights, records(tmp_path, [(alice, "human"), (bob, "human")]))
    result = subprocess.run([sys.executable, str(ROOT / "review.py"), "--insights", str(insights), "mine", "--author-email", " alice@example.com "], capture_output=True, text=True, check=True)
    assert alice in result.stdout and bob not in result.stdout and "bob@example.com" not in result.stdout


def test_report_facts_and_narration_redact_actor_identifiers():
    actor = "a" * 12
    email = "private@example.com"
    facts = {"records": [], "findings": [], "effects": [], "ledger": [], "actor_ids": [actor], "invalid_records": 0, "incomplete_telemetry": 0}
    report = render_report(facts)
    assert actor not in report and email not in report and actor not in facts_json(facts) and email not in facts_json(facts)
    with pytest.raises(ValueError, match="actor id"):
        validate_report(report + actor, facts)


def test_actor_salt_is_stable_reused_and_never_persists_identity(tmp_path: Path):
    insights = tmp_path / "insights"
    first = actor_salt(insights)
    assert first == actor_salt(insights)
    actor = actor_from_email(" Person@Example.com ", first)
    assert actor == actor_from_email("person@example.com", first)
    assert actor["id"] != actor_from_email("person@example.com", "b" * 32)["id"]
    assert "person@example.com" not in (insights / "config/actor.json").read_text()


def test_full_report_with_concentrated_finding_passes_validator_and_hides_recommendation():
    values = records(Path("."), [("a" * 12, "human")] * 7 + [("b" * 12, "human")] * 3)
    facts = build_facts_from(values)
    assert any(f["scope"] == "concentrated" and f["established"] for f in facts["findings"])
    markdown = render_report(facts)
    validate_report(markdown, facts)
    assert "Proposed change:" not in markdown.split("Not recommended", 1)[0].split("## Recommended improvements", 1)[1]


def build_facts_from(values: list[dict]) -> dict:
    findings = detect_patterns(values, min_evidence=3, min_actors=3)
    for number, finding in enumerate(findings, 1):
        finding["id"] = f"F-{number:03d}"
    return {"records": [v["record_id"] for v in values], "findings": findings, "effects": [], "ledger": [], "repositories": ["repo"], "sessions": len(values), "guidance_epochs": [], "actor_distinct": 2, "invalid_records": 0, "incomplete_telemetry": 0}


def test_repeated_guidance_comparisons_with_same_verdict_validate_and_are_labelled():
    def effect(n: int, mean_before: float, change: str) -> dict:
        side = lambda mean, count: {"n": count, "mean": mean, "median": mean, "min": mean, "max": mean}
        return {"repository": "repo", "metric": "cost_usd", "guidance_change": change, "before": side(mean_before, n), "after": side(mean_before / 2, n), "median_change_percent": -50.0, "p_value": 0.01, "verdict": "improved", "reasons": [], "actor_mix": {}}
    facts = {"records": [], "findings": [], "ledger": [], "effects": [effect(8, 0.02, "aaaaaaaa to bbbbbbbb"), effect(6, 0.03, "aaaaaaaa to bbbbbbbb")]}
    markdown = render_report(facts)
    validate_report(markdown, facts)
    assert markdown.count("guidance aaaaaaaa to bbbbbbbb;") == 2

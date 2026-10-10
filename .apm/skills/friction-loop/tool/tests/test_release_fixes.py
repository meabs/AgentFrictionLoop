from __future__ import annotations

import json
from pathlib import Path

import pytest

import weekly
from feedback_loop.report import SECTIONS, validate_report


def markdown(extra: str = "") -> str:
    return "\n\n".join(SECTIONS) + "\n" + extra


def facts(sample: str) -> dict:
    return {"findings": [{"id": "F-001", "friction_samples": [sample]}], "records": [], "actor_ids": []}


def test_very_short_note_does_not_fail_the_report():
    validate_report(markdown("The summary mentions x and ok."), facts("x"))
    validate_report(markdown(), facts("ok"))


def test_a_real_note_in_the_report_is_still_rejected():
    note = "docs/db.md says sqlite but code uses postgres"
    with pytest.raises(ValueError, match="friction sample"):
        validate_report(markdown(f"Leaked: {note}"), facts(note))


def test_agent_notes_come_from_facts_and_missing_file_is_empty(tmp_path: Path):
    path = tmp_path / "r.facts.json"
    path.write_text(json.dumps({"findings": [{"id": "F-002", "friction_samples": ["a", "b"]}, {"id": "F-003"}]}))
    assert weekly.agent_notes(path) == "- F-002: a\n- F-002: b"
    assert weekly.agent_notes(tmp_path / "none.json") == ""


def test_issue_ignores_findings_concentrated_in_few_people():
    only_concentrated = "No established recommendations.\nNot recommended (concentrated in few people):\n- F-001: 1 distinct actors across 2 attributed records; largest share 100%."
    assert not weekly.has_recommendations(only_concentrated)
    real = "- F-001: Observation: First tests verification attempt failed in 10 sessions. Proposed change: x.\nNot recommended (concentrated in few people):\n- F-002: y"
    assert weekly.has_recommendations(real)
    assert "F-002" not in weekly.actionable(real)

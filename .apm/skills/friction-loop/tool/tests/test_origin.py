from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from feedback_loop import origins
from feedback_loop.boundary_adapter import _friction_item
from feedback_loop.review_core import FRICTION_PROPOSALS, _friction, _friction_key
from tests.test_stage6a import events, install, repo

ROOT = Path(__file__).resolve().parents[1]


def fbl(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "boundary.py"), "friction", *args], cwd=path, capture_output=True, text=True)


def test_origin_is_recorded_and_category_defaults_from_it(tmp_path: Path):
    path = repo(tmp_path)
    install(path)
    result = fbl(path, "--origin", "conflicting-knowledge", "old ADR says sqlite, code uses postgres")
    assert result.returncode == 0 and "conflicting-knowledge / unclear-instruction" in result.stdout
    event = events(path)[0]
    assert event["origin"] == "conflicting-knowledge" and event["category"] == "unclear-instruction"


def test_category_only_notes_are_unchanged_and_carry_no_origin(tmp_path: Path):
    path = repo(tmp_path)
    install(path)
    assert fbl(path, "--category", "missing-doc", "x").returncode == 0
    assert "origin" not in events(path)[0]


def test_explicit_category_wins_and_both_are_kept(tmp_path: Path):
    path = repo(tmp_path)
    install(path)
    fbl(path, "--category", "test-order", "--origin", "execution-environment", "run migrate first")
    event = events(path)[0]
    assert (event["category"], event["origin"]) == ("test-order", "execution-environment")


def test_unknown_origin_and_missing_both_are_rejected(tmp_path: Path):
    path = repo(tmp_path)
    install(path)
    assert fbl(path, "--origin", "vibes", "x").returncode == 2
    assert fbl(path, "x").returncode == 2
    assert not (path / ".git/fbl/boundary/events.jsonl").exists()


def test_origin_survives_into_record_item_and_validates():
    item = _friction_item("other", "brief had no acceptance criterion", "event", "task-context")
    assert item["origin"] == "task-context"
    assert "origin" not in _friction_item("other", "x", "event", "bogus")
    schema = json.loads((ROOT / "schemas/analysis-v1.schema.json").read_text())
    friction_schema = schema["properties"]["friction"]["items"]
    Draft202012Validator(friction_schema).validate(item)
    Draft202012Validator(friction_schema).validate({"category": "other", "text": "old note", "source": "event"})


def test_event_schema_accepts_origin(tmp_path: Path):
    path = repo(tmp_path)
    install(path)
    fbl(path, "--origin", "verification", "no way to run one test")
    schema = json.loads((ROOT / "schemas/boundary-event-v1.schema.json").read_text())
    Draft202012Validator(schema).validate(events(path)[0])


def test_review_groups_by_origin_when_present_else_category():
    record = {"friction": [{"category": "other", "text": "a", "source": "event", "origin": "task-context"}, {"category": "setup", "text": "b", "source": "event"}]}
    assert [_friction_key(i) for i in _friction(record)] == ["task-context", "setup"]
    assert len(_friction(record, "task-context")) == 1 and len(_friction(record, "other")) == 0
    assert set(origins.ORIGINS) <= set(FRICTION_PROPOSALS)

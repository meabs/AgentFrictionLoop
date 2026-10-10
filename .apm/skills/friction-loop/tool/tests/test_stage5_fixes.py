from __future__ import annotations

from pathlib import Path

from feedback_loop.codex_adapter import _read_paths
from feedback_loop.review_core import detect_patterns, effect_analysis

from test_stage4 import _record
from test_stage4b import records


def test_read_paths_reject_shell_tokens_globs_and_directories(tmp_path: Path):
    (tmp_path / "backend/tests").mkdir(parents=True)
    (tmp_path / "backend/app.py").write_text("x")
    for tokens in (["sed", "-n", "1,5p", "printf"], ["cat", "2"], ["cat", ">"], ["cat", "*.py"], ["cat", "backend/tests"], ["sed", "-n", "1p", "status"]):
        assert _read_paths(tokens, str(tmp_path), tmp_path) == [], tokens
    assert _read_paths(["sed", "-n", "1,5p", "backend/app.py"], str(tmp_path), tmp_path) == ["backend/app.py"]
    assert _read_paths(["cat", "README.md"], str(tmp_path), tmp_path) == ["README.md"]


def test_entrypoint_files_are_not_documentation_gaps():
    values = records(Path("."), [("a" * 12, "human")] * 3)
    for value in values:
        value["execution"]["files_opened"] = ["AGENTS.md", "docs/extra.md"]
        value["guidance"]["agents_md_referenced_paths"] = []
    observations = [f["observation"] for f in detect_patterns(values, min_evidence=3) if f["pattern_class"] == "documentation_gap"]
    assert any("docs/extra.md" in o for o in observations)
    assert not any("'AGENTS.md'" in o for o in observations)


def test_unloaded_skill_finding_is_emitted_once_across_guidance_epochs():
    values = records(Path("."), [("a" * 12, "human")] * 6)
    for index, value in enumerate(values):
        value["guidance"]["digest"] = ("1" if index % 2 else "2") * 64
        value["guidance"]["skills_available"] = ["never-used"]
        value["execution"]["skills_loaded"] = []
    found = [f for f in detect_patterns(values, min_evidence=3) if f["pattern_class"] == "ineffective_skills"]
    assert len(found) == 1 and found[0]["evidence_count"] == 6


def test_command_not_found_is_environmental_not_a_verification_failure():
    values = records(Path("."), [("a" * 12, "human")] * 4)
    for value in values:
        value["execution"]["verification_attempts"] = [{"kind": "build", "exit_code": 127}]
    assert not [f for f in detect_patterns(values, min_evidence=3) if f["pattern_class"] == "verification_failure"]
    for value in values:
        value["execution"]["verification_attempts"] = [{"kind": "build", "exit_code": 1}]
    assert [f for f in detect_patterns(values, min_evidence=3) if f["pattern_class"] == "verification_failure"]
    env = records(Path("."), [("a" * 12, "human")] * 4)
    for value in env:
        value["execution"]["verification_attempts"] = [{"kind": "build", "exit_code": 127}]
    assert not [e for e in effect_analysis(env, env) if e["metric"] == "first_verification_failed"]


def test_effect_identity_distinguishes_repeated_guidance_flips():
    from feedback_loop.report import SECTIONS, validate_report

    def effect(after):
        return {"verdict": "directional", "repository": "app", "metric": "commands_failed", "guidance_change": "a to b", "before": {"n": 2, "mean": 1.0, "median": 1.0}, "after": {"n": 1, "mean": after, "median": after}, "p_value": 1.0, "median_change_percent": (after - 1.0) * 100}

    facts = {"effects": [effect(2.0), effect(0.0)], "findings": [], "records": []}
    lines = [f"- DIRECTIONAL app commands_failed: guidance a to b; n=2 vs n=1; means=1.0 -> {a}; medians=1.0 -> {a}; p-value=1.0; median change={(a - 1.0) * 100}%;" for a in (2.0, 0.0)]
    validate_report("\n".join(SECTIONS) + "\n" + "\n".join(lines) + "\n", facts)


def test_effect_identity_allows_identical_repeated_effects():
    import pytest
    from feedback_loop.report import SECTIONS, validate_report

    effect = {"verdict": "directional", "repository": "app", "metric": "commands_failed", "guidance_change": "a to b", "before": {"n": 2, "mean": 1.0, "median": 1.0}, "after": {"n": 2, "mean": 0.0, "median": 0.0}, "p_value": 1.0, "median_change_percent": -100.0}
    line = "- DIRECTIONAL app commands_failed: guidance a to b; n=2 vs n=2; means=1.0 -> 0.0; medians=1.0 -> 0.0; p-value=1.0; median change=-100.0%;"
    facts = {"effects": [effect, dict(effect)], "findings": [], "records": []}
    validate_report("\n".join(SECTIONS) + "\n" + line + "\n" + line + "\n", facts)
    with pytest.raises(ValueError):
        validate_report("\n".join(SECTIONS) + "\n" + "\n".join([line] * 3) + "\n", facts)

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import doctor


def project(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "r"
    for rel, body in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "x"], check=True)  # --run clones the committed state
    return repo


APP = {
    "AGENTS.md": "# Notes\nBe nice.\n",
    "backend/alembic.ini": "[alembic]\n",
    "backend/scripts/test.sh": "pytest tests/\n",
    "backend/tests/conftest.py": "",
    ".env.example": "DATABASE_URL=postgresql://x/y\n",
    ".gitignore": "backend/app/frontend\n",
    "backend/app/main.py": "from pathlib import Path\nFRONTEND = Path(__file__).parent / \"frontend\"\nassert FRONTEND.exists()\n",
}


def kinds(repo: Path) -> set[str]:
    return {f["kind"] for f in doctor.check(repo, False, None, 10)}


def test_finds_the_gaps_an_agent_would_trip_on(tmp_path: Path):
    repo = project(tmp_path, APP)
    found = {f["kind"]: f for f in doctor.check(repo, False, None, 10)}
    assert {"undocumented-test-command", "env-file", "migrations", "generated-dir"} <= set(found)
    assert found["generated-dir"]["where"] == "backend/app/main.py:2"
    assert "alembic upgrade head" in found["migrations"]["fix"]


def test_a_documented_project_is_clean(tmp_path: Path):
    files = {"AGENTS.md": "# Tests\n```sh\ncp .env.example .env\nuv run alembic upgrade head\nuv run pytest tests/\n```\nDATABASE_URL is in .env\n",
             "backend/alembic.ini": "", "backend/tests/conftest.py": "", ".env.example": "DATABASE_URL=x\n"}
    assert kinds(project(tmp_path, files)) == set()


def test_run_names_the_cause_of_a_failure_in_a_clean_clone(tmp_path: Path):
    repo = project(tmp_path, {"AGENTS.md": "```sh\npytest\n```\n", "t.sh": "echo \"RuntimeError: Frontend directory '/x/frontend' does not exist\" >&2; exit 4\n"})
    f = [x for x in doctor.check(repo, True, "sh t.sh", 30) if x["kind"] == "run-failed"]
    assert f and "generated directory is missing" in f[0]["message"] and "exit 4" in f[0]["message"]
    ok = doctor.check(repo, True, "true", 30)
    assert any(x["kind"] == "ran" for x in ok)


def test_suggest_wraps_the_documented_command_with_the_missing_steps(tmp_path: Path):
    files = dict(APP, **{"AGENTS.md": "# Tests\n```sh\nFASTAPI_ENV=development uv run pytest tests/\n```\n"})
    script, notes = doctor.build_script(project(tmp_path, files))
    assert script.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in script
    assert "export FASTAPI_ENV=development" in script
    assert script.index("alembic upgrade head") < script.index("uv run pytest")
    assert "cp .env.example .env" in script and any("alembic" in n for n in notes)


def test_triage_ranks_by_sessions_and_names_a_likely_fix(tmp_path: Path, capsys):
    def ev(i, sess, text):
        return json.dumps({"type": "friction", "event_id": str(i), "text": text, "origin": "execution-environment", "harness": "codex", "session": {"id": sess}})

    lines = [ev(1, "a", "pytest could not collect because the frontend directory is missing"),
             ev(2, "b", "tests failed: frontend directory is missing for collection"),
             ev(3, "b", "pytest collection: frontend directory missing"),
             ev(4, "a", "lint wanted a newer node version")]
    f = tmp_path / "events.jsonl"
    f.write_text("\n".join(lines), encoding="utf-8")
    assert doctor.main(["triage", "--events", str(f)]) == 0
    out = capsys.readouterr().out
    assert out.index("2 sessions") < out.index("1 session,") and "generated directory is missing" in out


def test_launcher_is_installed_next_to_the_friction_one(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    import boundary
    boundary.main(["install", "--repo", str(repo)])
    assert (repo / ".fbl" / "bin" / "fbl-doctor").read_text(encoding="utf-8").count("doctor.py") == 1

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import insights_sync
from jsonschema import Draft202012Validator

from feedback_loop.record import _tree, build_record
from feedback_loop.store import persist


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/analysis-v1.schema.json"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check)


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Test User",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test User",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    return env


def commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=repo, env=git_env(), check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, env=git_env(), check=True, capture_output=True)
    return run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "project"
    repo.mkdir(parents=True)
    run("git", "init", cwd=repo)
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    commit(repo, "initial")
    return repo


def test_record_fields_and_ci_results(tmp_path: Path):
    repo = make_repo(tmp_path, {"app.py": "print('x')\n", "tests/test_app.py": "assert True\n", "AGENTS.md": "See `docs/guide.md`.\n", "docs/guide.md": "guide\n"})
    (repo / "app.py").write_text("print('y')\n", encoding="utf-8")
    (repo / "tests/test_app.py").write_text("assert 1\n", encoding="utf-8")
    sha = commit(repo, "change")
    ci = tmp_path / "ci.json"
    ci.write_text(json.dumps({"tests": "passed", "lint": "failed", "build": "unknown", "details": {"tests": {"passed": 1}}}), encoding="utf-8")
    record = build_record(repo, sha, "platform", ci_results=ci)
    assert record["commit"] == sha
    assert record["commit_short"] == sha[:12]
    assert record["parents"]
    assert record["changed_files"] == [{"path": "app.py", "status": "M"}, {"path": "tests/test_app.py", "status": "M"}]
    assert record["diff_stats"]["by_category"]["source"] == 1
    assert record["verification"]["tests"] == "passed"
    assert record["guidance"]["agents_md_referenced_paths"] == ["docs/guide.md"]
    Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8"))).validate(record)


def test_guidance_digest_changes_only_for_guidance_path_content_or_existence(tmp_path: Path):
    repo = make_repo(tmp_path, {"AGENTS.md": "See `docs/adr/guide.md`.\n", "docs/adr/guide.md": "one\n", "app.py": "one\n"})
    first = build_record(repo)["guidance"]["digest"]
    (repo / "app.py").write_text("two\n", encoding="utf-8")
    commit(repo, "non-guidance")
    assert build_record(repo)["guidance"]["digest"] == first
    (repo / "docs/adr/guide.md").write_text("two\n", encoding="utf-8")
    commit(repo, "guidance content")
    second = build_record(repo)["guidance"]["digest"]
    assert second != first
    (repo / "docs/adr/guide.md").rename(repo / "docs/adr/renamed.md")
    commit(repo, "guidance path")
    assert build_record(repo)["guidance"]["digest"] != second
    (repo / "docs/adr/renamed.md").unlink()
    commit(repo, "guidance removal")
    assert build_record(repo)["guidance"]["digest"] != second


def test_tree_reads_only_agents_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo(tmp_path, {"AGENTS.md": "guide\n", "a.txt": "a\n", "docs/x.md": "x\n"})
    seen: list[tuple[str, ...]] = []
    import feedback_loop.record as record_module

    original = record_module._git

    def wrapped(root: Path, *args: str, **kwargs):
        seen.append(args)
        return original(root, *args, **kwargs)

    monkeypatch.setattr(record_module, "_git", wrapped)
    _tree(repo, "HEAD")
    shows = [args for args in seen if args and args[0] == "show"]
    assert shows == [("show", "HEAD:AGENTS.md")]


def test_schema_failure_and_secret_refusal_write_no_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = make_repo(tmp_path, {"app.py": "x\n"})
    insights = tmp_path / "insights"
    record = build_record(repo)
    invalid = dict(record, schema_version="bad")
    with pytest.raises(ValueError, match="schema validation failed"):
        persist(invalid, insights, SCHEMA, no_commit=True)
    assert not list((insights / "records").rglob("*.json"))
    secret_repo = make_repo(tmp_path / "secret", {"token=supersecret.txt": "x\n"})
    secret_record = build_record(secret_repo)
    monkeypatch.setenv("FBL_HOME", str(tmp_path / "tool"))
    with pytest.raises(ValueError, match="secret pattern refused"):
        persist(secret_record, tmp_path / "secret-insights", SCHEMA, no_commit=True)
    assert not list((tmp_path / "secret-insights" / "records").rglob("*.json"))
    assert "secret pattern refused" in (tmp_path / "tool/var/ops.log").read_text(encoding="utf-8")


def test_idempotent_rerun_replaces_new_optional_values(tmp_path: Path):
    repo = make_repo(tmp_path, {"app.py": "x\n"})
    insights = tmp_path / "insights"
    old = build_record(repo)
    old.update(execution={"version": 1}, harness_telemetry={"version": 1}, agent={"harness": "unknown"}, verification={"tests": "unknown", "lint": "unknown", "build": "unknown"})
    path = persist(old, insights, SCHEMA, no_commit=True)
    new = build_record(repo)
    new.update(execution={"version": 2}, harness_telemetry={"version": 2}, agent={"harness": "codex"}, verification={"tests": "passed", "lint": "unknown", "build": "unknown"})
    persist(new, insights, SCHEMA, no_commit=True)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["execution"] == {"version": 2}
    assert saved["harness_telemetry"] == {"version": 2}
    assert saved["agent"] == {"harness": "codex"}
    assert saved["verification"]["tests"] == "passed"
    for key in ("commit", "changed_files", "diff_stats", "documentation", "guidance", "observations"):
        assert saved[key] == old[key]


def _persist_worker(insights: str, schema: str, record: dict):
    persist(record, insights, schema, no_commit=False)


def test_concurrent_runs_leave_consistent_insights_repo(tmp_path: Path):
    repo = make_repo(tmp_path, {"app.py": "x\n"})
    insights = tmp_path / "insights"
    record = build_record(repo)
    ctx = multiprocessing.get_context("fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn")
    processes = [ctx.Process(target=_persist_worker, args=(str(insights), str(SCHEMA), record)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    path = next((insights / "records").rglob("*.json"))
    assert json.loads(path.read_text(encoding="utf-8"))["commit"] == record["commit"]
    assert not run("git", "fsck", "--no-progress", cwd=insights, check=False).returncode


def test_cli_invalid_ci_results_normal_and_fail_open(tmp_path: Path):
    repo = make_repo(tmp_path, {"app.py": "x\n"})
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    normal = subprocess.run([sys.executable, str(ROOT / "analyse-change.py"), "--repo", str(repo), "--insights", str(tmp_path / "i"), "--ci-results", str(bad)], text=True, capture_output=True)
    assert normal.returncode == 1
    home = tmp_path / "tool"
    fail_open = subprocess.run([sys.executable, str(ROOT / "analyse-change.py"), "--repo", str(repo), "--insights", str(tmp_path / "i2"), "--ci-results", str(bad), "--fail-open"], env={**os.environ, "FBL_HOME": str(home)}, text=True, capture_output=True)
    assert fail_open.returncode == 0
    assert "JSONDecodeError" in (home / "var/ops.log").read_text(encoding="utf-8")


def test_installed_hook_is_async_and_never_dirties_committed_repo(tmp_path: Path):
    repo = make_repo(tmp_path, {"app.py": "x\n"})
    install = subprocess.run([sys.executable, str(ROOT / "insights_sync.py"), "install", "--repo", str(repo), "--domain", "platform"], text=True, capture_output=True)
    assert install.returncode == 0, install.stderr
    insights = insights_sync.worktree_path(repo)
    hook = repo / ".git/hooks/post-commit"
    text = hook.read_text(encoding="utf-8")
    assert f"FBL_HOME='{ROOT.as_posix()}'" in text
    assert f"FBL_INSIGHTS='{Path(insights).as_posix()}'" in text
    before = run("git", "status", "--porcelain", cwd=repo).stdout
    (repo / "app.py").write_text("changed\n", encoding="utf-8")
    started = time.monotonic()
    commit(repo, "hook")
    assert time.monotonic() - started < 5
    time.sleep(0.5)
    assert run("git", "status", "--porcelain", cwd=repo).stdout == before
    assert not (repo / "var").exists()
    assert insights.exists()


def test_hook_without_insights_logs_and_exits_zero(tmp_path: Path):
    repo = make_repo(tmp_path, {"app.py": "x\n"})
    env = os.environ.copy()
    env.pop("FBL_INSIGHTS", None)
    env["FBL_HOME"] = str(tmp_path / "tool")
    result = subprocess.run(["sh", str(ROOT / "hooks/post-commit")], cwd=repo, env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert "FBL_INSIGHTS is unset" in (tmp_path / "tool/var/hook.log").read_text(encoding="utf-8")


def test_hook_returns_zero_without_waiting_when_analyser_is_broken(tmp_path: Path):
    repo = make_repo(tmp_path, {"app.py": "x\n"})
    env = os.environ.copy()
    env.update(FBL_HOME=str(tmp_path / "missing-tool"), FBL_INSIGHTS=str(tmp_path / "insights"))
    started = time.monotonic()
    result = subprocess.run(["sh", str(ROOT / "hooks/post-commit")], cwd=repo, env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert time.monotonic() - started < 2
    time.sleep(0.2)
    assert "can't open file" in (tmp_path / "missing-tool/var/hook.log").read_text(encoding="utf-8")

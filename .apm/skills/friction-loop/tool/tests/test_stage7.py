"""In-repo insights branch: init, push, hooks, weekly job. A local bare repository stands in for GitHub."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import insights_sync
import weekly

ROOT = Path(__file__).parents[1]


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def make_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    git(seed, "config", "user.email", "seed@example.test")
    git(seed, "config", "user.name", "Seed")
    (seed / "README.md").write_text("x\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "base")
    git(seed, "push", "-q", "origin", "HEAD:main")
    return origin


def clone(tmp_path: Path, origin: Path, name: str) -> Path:
    path = tmp_path / name
    git(tmp_path, "clone", "-q", str(origin), str(path))
    git(path, "config", "user.email", f"{name}@example.test")
    git(path, "config", "user.name", name)
    return path


def commit(repo: Path, name: str) -> str:
    (repo / f"{name}.txt").write_text(name + "\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def analyse(repo: Path, wt: Path, sha: str) -> None:
    env = {**os.environ, "FBL_HOME": str(ROOT)}
    subprocess.run([sys.executable, str(ROOT / "analyse-change.py"), "--repo", str(repo), "--commit", sha, "--insights", str(wt), "--domain", "demo", "--repository", "app"], check=True, env=env, capture_output=True)


def remote_files(origin: Path) -> list[str]:
    return git(origin, "ls-tree", "-r", "--name-only", "fbl-insights").stdout.split()


def test_init_creates_orphan_branch_with_worktree(tmp_path):
    origin = make_origin(tmp_path)
    dev = clone(tmp_path, origin, "dev1")
    wt = insights_sync.init(dev)
    assert wt == insights_sync.worktree_path(dev)
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "fbl-insights"
    assert git(wt, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    assert git(dev, "merge-base", "main", "fbl-insights", check=False).returncode == 1
    assert (wt / ".gitignore").read_text().strip() == ".lock"
    assert insights_sync.init(dev) == wt


def test_publish_then_second_developer_adopts_remote_branch_and_salt(tmp_path):
    origin = make_origin(tmp_path)
    one, two = clone(tmp_path, origin, "dev1"), clone(tmp_path, origin, "dev2")
    assert insights_sync.main(["init", "--repo", str(one), "--publish"]) == 0
    assert "config/actor.json" in remote_files(origin)
    wt_two = insights_sync.init(two)
    one_salt = json.loads((insights_sync.worktree_path(one) / "config/actor.json").read_text())["salt"]
    assert json.loads((wt_two / "config/actor.json").read_text())["salt"] == one_salt


def test_two_developers_pushing_concurrently_end_with_both_records(tmp_path):
    origin = make_origin(tmp_path)
    one, two = clone(tmp_path, origin, "dev1"), clone(tmp_path, origin, "dev2")
    insights_sync.main(["init", "--repo", str(one), "--publish"])
    wts = [insights_sync.init(one), insights_sync.init(two)]
    for dev, wt in ((one, wts[0]), (two, wts[1])):
        analyse(dev, wt, commit(dev, dev.name))
    results: list[str] = []
    threads = [threading.Thread(target=lambda w=w: results.append(insights_sync.push(w))) for w in wts]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["pushed", "pushed"]
    assert len([f for f in remote_files(origin) if f.startswith("records/")]) == 2
    assert git(wts[0], "status", "--porcelain").stdout == ""


def test_offline_push_defers_and_later_push_delivers_queued_records(tmp_path):
    origin = make_origin(tmp_path)
    dev = clone(tmp_path, origin, "dev1")
    insights_sync.main(["init", "--repo", str(dev), "--publish"])
    wt = insights_sync.init(dev)
    good = git(dev, "remote", "get-url", "origin").stdout.strip()
    git(dev, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    analyse(dev, wt, commit(dev, "first"))
    assert insights_sync.push(wt) == "offline"
    assert git(wt, "log", "--oneline", "origin/fbl-insights..HEAD", check=False).stdout.count("\n") >= 1
    git(dev, "remote", "set-url", "origin", good)
    analyse(dev, wt, commit(dev, "second"))
    assert insights_sync.push(wt) == "pushed"
    assert len([f for f in remote_files(origin) if f.startswith("records/")]) == 2


def test_independently_created_branches_with_different_salts_report_diverged_without_damage(tmp_path):
    origin = make_origin(tmp_path)
    one, two = clone(tmp_path, origin, "dev1"), clone(tmp_path, origin, "dev2")
    wt_two = insights_sync.init(two)
    analyse(two, wt_two, commit(two, "two"))
    insights_sync.main(["init", "--repo", str(one), "--publish"])
    before = git(wt_two, "rev-parse", "HEAD").stdout
    assert insights_sync.push(wt_two) == "diverged"
    assert git(wt_two, "rev-parse", "HEAD").stdout == before
    assert git(wt_two, "status", "--porcelain").stdout == ""


def test_hooks_analyse_a_commit_without_recursing_on_insights_commits(tmp_path):
    origin = make_origin(tmp_path)
    dev = clone(tmp_path, origin, "dev1")
    insights_sync.main(["init", "--repo", str(dev), "--publish"])
    assert insights_sync.main(["install", "--repo", str(dev), "--domain", "demo"]) == 0
    assert insights_sync.main(["install", "--repo", str(dev), "--domain", "demo"]) == 0
    wt = insights_sync.worktree_path(dev)
    commit(dev, "work")
    deadline = time.time() + 30
    while time.time() < deadline and not list((wt / "records").rglob("*.json")):
        time.sleep(0.3)
    time.sleep(1.5)
    assert len(list((wt / "records").rglob("*.json"))) == 1
    count = git(wt, "rev-list", "--count", "HEAD").stdout
    time.sleep(1.5)
    assert git(wt, "rev-list", "--count", "HEAD").stdout == count
    git(dev, "push", "-q", "origin", "HEAD:main")
    deadline = time.time() + 30
    while time.time() < deadline and not any(f.startswith("records/") for f in remote_files(origin)):
        time.sleep(0.3)
    assert any(f.startswith("records/") for f in remote_files(origin))


def test_install_refuses_to_overwrite_a_foreign_hook(tmp_path):
    origin = make_origin(tmp_path)
    dev = clone(tmp_path, origin, "dev1")
    hook = dev / ".git/hooks/pre-push"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    assert insights_sync.main(["install", "--repo", str(dev)]) == 1
    assert hook.read_text() == "#!/bin/sh\nexit 0\n"


def test_weekly_with_no_records_writes_and_commits_report_but_no_issue(tmp_path, monkeypatch):
    origin = make_origin(tmp_path)
    dev = clone(tmp_path, origin, "dev1")
    insights_sync.main(["init", "--repo", str(dev), "--publish"])
    wt = insights_sync.init(dev)
    result = weekly.run(wt, push=True, remote="origin", today="2026-10-05")
    assert result["issue_body"] is None
    assert result["push"] == "pushed"
    assert "reports/2026-10-05.md" in remote_files(origin)


def test_weekly_opens_one_issue_per_distinct_recommendation_set(tmp_path, monkeypatch):
    origin = make_origin(tmp_path)
    dev = clone(tmp_path, origin, "dev1")
    insights_sync.main(["init", "--repo", str(dev), "--publish"])
    wt = insights_sync.init(dev)
    text = {"value": "# Agent Engineering Review\n\n## Recommended improvements\n\n- F-003: Observation: x. Proposed change: y.\n\n## Previous recommendation outcomes\n\nNone.\n"}

    def fake_review(argv):
        out = Path(argv[argv.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text["value"])
        out.with_name(out.stem + ".facts.json").write_text("{}\n")
        return 0

    monkeypatch.setattr(weekly.review, "main", fake_review)
    first = weekly.run(wt, push=False, remote="origin", today="2026-10-05")
    assert first["issue_body"] and "F-003" in Path(first["issue_body"]).read_text()
    assert "not proof" in Path(first["issue_body"]).read_text()
    assert weekly.run(wt, push=False, remote="origin", today="2026-10-12")["issue_body"] is None
    text["value"] = text["value"].replace("F-003", "F-004")
    assert weekly.run(wt, push=False, remote="origin", today="2026-10-19")["issue_body"]


def test_recommendation_parser_ignores_empty_sections():
    assert not weekly.has_recommendations(weekly.recommended_section("## Recommended improvements\n\nNo recommendation.\n\n## Next\n"))
    assert weekly.has_recommendations(weekly.recommended_section("## Recommended improvements\n\n- F-001: x\n"))


def test_installed_hook_attaches_a_friction_note_filed_before_the_commit(tmp_path):
    origin = make_origin(tmp_path)
    dev = clone(tmp_path, origin, "dev1")
    insights_sync.main(["init", "--repo", str(dev), "--publish"])
    assert subprocess.run([sys.executable, str(ROOT / "boundary.py"), "install", "--repo", str(dev)], capture_output=True).returncode == 0
    assert insights_sync.main(["install", "--repo", str(dev), "--domain", "demo"]) == 0
    assert "FBL_ADAPTER='boundary'" in (dev / ".git/hooks/post-commit").read_text()
    env = {**os.environ, "FBL_HARNESS": "codex", "FBL_SESSION_ID": "s-hook-1"}
    subprocess.run([str(dev / ".fbl/bin/fbl-friction"), "--category", "test-order", "Tests failed until migrations were applied."], cwd=dev, env=env, check=True, capture_output=True)
    commit(dev, "work")
    wt = insights_sync.worktree_path(dev)
    deadline = time.time() + 30
    while time.time() < deadline and not list((wt / "records").rglob("*.json")):
        time.sleep(0.3)
    record = json.loads(next((wt / "records").rglob("*.json")).read_text())
    assert [(f["category"], f["source"]) for f in record["friction"]] == [("test-order", "event")]


def test_install_rejects_an_unknown_adapter(tmp_path):
    origin = make_origin(tmp_path)
    dev = clone(tmp_path, origin, "dev1")
    assert insights_sync.main(["install", "--repo", str(dev), "--adapter", "bogus"]) == 1

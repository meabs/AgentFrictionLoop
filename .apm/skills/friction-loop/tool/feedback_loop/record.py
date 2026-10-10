"""Build a Level 1 record from a committed Git tree."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path

from .guidance import Tree, inspect


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if check and result.returncode:
        raise GitError(result.stderr.decode("utf-8", errors="replace").strip() or "git command failed")
    return result.stdout


def _utc(seconds: str | int | float) -> str:
    return dt.datetime.fromtimestamp(float(seconds), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tree(repo: Path, commit: str) -> Tree:
    raw = _git(repo, "ls-tree", "-r", "-z", "--format=%(objectname)%x09%(path)", commit)
    entries = [entry for entry in raw.decode("utf-8", errors="surrogateescape").split("\0") if entry]
    blob_shas: dict[str, str] = {}
    for entry in entries:
        sha, path = entry.split("\t", 1)
        blob_shas[path] = sha
    files = tuple(blob_shas)
    blobs = {}
    if "AGENTS.md" in blob_shas:
        blobs["AGENTS.md"] = _git(repo, "show", f"{commit}:AGENTS.md")
    return Tree(files, blob_shas, blobs)


def _changed(repo: Path, commit: str) -> list[dict[str, str]]:
    raw = _git(repo, "diff-tree", "--root", "--no-commit-id", "-r", "--name-status", "-M", "-z", commit)
    pieces = raw.decode("utf-8", errors="surrogateescape").split("\0")
    result = []
    index = 0
    while index < len(pieces) - 1:
        status = pieces[index]
        index += 1
        if not status:
            continue
        code = status[0]
        if code == "R":
            old = pieces[index]
            new = pieces[index + 1]
            index += 2
            result.append({"path": new, "status": "R"})
        else:
            result.append({"path": pieces[index], "status": code if code in "AMD" else "M"})
            index += 1
    return result


def _category(path: str) -> str:
    lower = path.lower()
    name = Path(path).name.lower()
    if name in {"agents.md", "claude.md", "service.yaml", "service.yml"} or lower.startswith(("okf/", ".agents/skills/", ".claude/skills/", "docs/adr/", "docs/ard/")):
        return "guidance"
    if "/test" in lower or lower.startswith(("test/", "tests/")) or name.startswith("test_") or name.endswith("_test.py"):
        return "tests"
    if lower.endswith((".md", ".rst", ".adoc")) or lower.startswith(("docs/", "doc/")):
        return "docs"
    if name in {"dockerfile", "makefile", "pyproject.toml", "package.json", "tsconfig.json", ".gitignore"} or lower.endswith((".yml", ".yaml", ".toml", ".ini", ".cfg")):
        return "config"
    if lower.endswith((".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".rb", ".c", ".cpp", ".h", ".cs")):
        return "source"
    return "other"


def _stats(repo: Path, commit: str, changed: list[dict[str, str]]) -> dict:
    raw = _git(repo, "diff-tree", "--root", "--no-commit-id", "--numstat", "-M", "-r", commit).decode("utf-8", errors="surrogateescape")
    insertions = deletions = 0
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) >= 3:
            try:
                insertions += int(fields[0])
                deletions += int(fields[1])
            except ValueError:
                pass
    categories = {name: 0 for name in ("source", "tests", "docs", "config", "guidance", "other")}
    for item in changed:
        categories[_category(item["path"])] += 1
    return {"files": len(changed), "insertions": insertions, "deletions": deletions, "by_category": categories}


def _verification(ci_results: Path | None) -> dict:
    result = {key: "unknown" for key in ("tests", "lint", "build")}
    if ci_results is None:
        return result
    loaded = json.loads(ci_results.read_text(encoding="utf-8"))
    for key in result:
        value = loaded.get(key)
        if value in {"passed", "failed", "unknown"}:
            result[key] = value
    details = loaded.get("details")
    if isinstance(details, dict):
        result["details"] = {key: value for key, value in details.items() if key in result and isinstance(value, dict)}
    return result


def actor_id(email: str, salt: str) -> str:
    return hmac.new(bytes.fromhex(salt), email.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def actor_from_email(email: str, salt: str) -> dict[str, str] | None:
    normalized = email.strip().lower()
    if not normalized:
        return None
    kind = "bot" if "noreply" in normalized or "[bot]" in normalized or "users.noreply.github.com" in normalized else "human"
    return {"id": actor_id(normalized, salt), "scheme": "hmac-sha256/v1", "kind": kind}


def build_record(repo: str | os.PathLike[str], commit_ref: str = "HEAD", domain: str = "default", repository: str | None = None, ci_results: str | os.PathLike[str] | None = None, actor_salt: str | None = None) -> dict:
    root = Path(repo).resolve()
    commit = _git(root, "rev-parse", f"{commit_ref}^{{commit}}").decode().strip()
    short = commit[:12]
    parents = _git(root, "rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
    seconds = _git(root, "show", "-s", "--format=%ct", commit).decode().strip()
    changed = _changed(root, commit)
    tree = _tree(root, commit)
    guidance, documentation = inspect(tree, {item["path"] for item in changed})
    stats = _stats(root, commit, changed)
    observations = []
    cats = stats["by_category"]
    if cats["source"] and not cats["tests"]:
        observations.append("source changed without tests")
    if cats["source"] and cats["tests"]:
        observations.append("tests changed with source")
    if cats["guidance"]:
        observations.append("guidance files changed")
    if cats["docs"] and cats["source"]:
        observations.append("docs changed with source")
    if not documentation["agents_md_present"]:
        observations.append("no AGENTS.md")
    if stats["files"] > 20:
        observations.append("large change (>20 files)")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo_name = repository or root.name
    result = {
        "schema_version": "1.0", "record_id": f"{repo_name}@{short}", "domain": domain, "repository": repo_name,
        "commit": commit, "commit_short": short, "parents": parents, "timestamp": _utc(seconds), "analysed_at": now,
        "changed_files": changed, "diff_stats": stats, "verification": _verification(Path(ci_results) if ci_results else None),
        "documentation": documentation, "guidance": guidance, "observations": observations, "agent": {"harness": "unknown"},
    }
    if actor_salt is not None:
        email = _git(root, "show", "-s", "--format=%ae", commit, check=False).decode("utf-8", errors="replace")
        actor = actor_from_email(email, actor_salt)
        if actor is not None:
            result["actor"] = actor
    return result

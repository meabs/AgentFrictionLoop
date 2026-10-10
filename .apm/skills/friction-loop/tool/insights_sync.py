#!/usr/bin/env python3
"""Keep analysis records on an orphan branch of the project repository and sync it with a remote."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feedback_loop.compat import empty_hooks_dir, lock_exclusive, posix, python_executable  # noqa: E402

BRANCH = "fbl-insights"
RETRIES = 3
HOME = Path(os.environ.get("FBL_HOME") or Path(__file__).resolve().parent)


def _ops(event: str, reason: str) -> None:
    try:
        path = HOME / "var" / "ops.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "reason": reason[:200]}, sort_keys=True) + "\n")
    except OSError:
        pass


def _clean_env() -> dict:
    # Git exports GIT_DIR and friends to hooks; inherited, they would redirect worktree commands to the main repository.
    return {k: v for k, v in os.environ.items() if k not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.hooksPath=" + empty_hooks_dir(), *args], cwd=cwd, text=True, capture_output=True, check=False, env=_clean_env())


def worktree_path(repo: Path) -> Path:
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        raise RuntimeError("not a git repository: " + str(repo))
    return Path(common.stdout.strip()) / "fbl" / "insights"


def _has_ref(cwd: Path, ref: str) -> bool:
    return git(cwd, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def init(repo: Path, remote: str = "origin") -> Path:
    """Create (or reuse) the insights worktree. Adopts the remote branch when it exists."""
    wt = worktree_path(repo)
    if (wt / ".git").exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "fetch", "--quiet", remote, f"{BRANCH}:refs/remotes/{remote}/{BRANCH}")
    remote_ref = f"refs/remotes/{remote}/{BRANCH}"
    if _has_ref(repo, remote_ref):
        result = git(repo, "worktree", "add", "--quiet", "-B", BRANCH, str(wt), remote_ref)
    elif _has_ref(repo, f"refs/heads/{BRANCH}"):
        result = git(repo, "worktree", "add", "--quiet", str(wt), BRANCH)
    elif not _has_ref(repo, "HEAD"):
        # A repository with no commits yet cannot host a detached worktree; build the root commit by hand.
        tree = subprocess.run(["git", "mktree"], cwd=repo, input="", text=True, capture_output=True, env=_clean_env()).stdout.strip()
        identity = {**_clean_env(), "GIT_AUTHOR_NAME": "fbl", "GIT_AUTHOR_EMAIL": "fbl@localhost", "GIT_COMMITTER_NAME": "fbl", "GIT_COMMITTER_EMAIL": "fbl@localhost"}
        root = subprocess.run(["git", "commit-tree", tree, "-m", "Initialise fbl insights"], cwd=repo, text=True, capture_output=True, env=identity).stdout.strip()
        if not tree or not root:
            raise RuntimeError("unable to create the insights root commit")
        git(repo, "update-ref", f"refs/heads/{BRANCH}", root)
        result = git(repo, "worktree", "add", "--quiet", str(wt), BRANCH)
        if not result.returncode:
            (wt / ".gitignore").write_text(".lock\n", encoding="utf-8")
            git(wt, "add", ".gitignore")
            subprocess.run(["git", "-c", "core.hooksPath=" + empty_hooks_dir(), "commit", "--quiet", "-m", "Ignore lock file"], cwd=wt, env=identity, capture_output=True, text=True)
    else:
        result = git(repo, "worktree", "add", "--quiet", "--detach", str(wt))
        if not result.returncode:
            for step in (("checkout", "--quiet", "--orphan", BRANCH), ("rm", "-rf", "--quiet", "--ignore-unmatch", ".")):
                if git(wt, *step).returncode:
                    raise RuntimeError("unable to create orphan branch")
            (wt / ".gitignore").write_text(".lock\n", encoding="utf-8")
            git(wt, "add", ".gitignore")
            result = git(wt, "commit", "--quiet", "-m", "Initialise fbl insights")
    if result.returncode:
        raise RuntimeError("unable to create insights worktree: " + result.stderr.strip()[:200])
    return wt


def push(wt: Path, remote: str = "origin") -> str:
    """Fetch, rebase and push. Never raises for network or divergence problems; the local commits are the queue."""
    if not (wt / ".git").exists():
        return "no-worktree"
    branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or BRANCH
    with (wt / ".lock").open("a+", encoding="utf-8") as lock:
        lock_exclusive(lock)
        for _ in range(RETRIES):
            fetch = git(wt, "fetch", "--quiet", remote, branch)
            remote_missing = fetch.returncode != 0 and "couldn't find remote ref" in fetch.stderr
            if fetch.returncode and not remote_missing:
                _ops("insights_push_deferred", "fetch failed: " + fetch.stderr.strip())
                return "offline"
            if not remote_missing:
                rebase = git(wt, "rebase", "--quiet", "FETCH_HEAD")
                if rebase.returncode:
                    git(wt, "rebase", "--abort")
                    _ops("insights_push_diverged", "rebase onto remote failed: " + rebase.stderr.strip())
                    return "diverged"
            pushed = git(wt, "push", "--quiet", remote, f"{branch}:{branch}")
            if pushed.returncode == 0:
                return "pushed"
            if "rejected" not in pushed.stderr and "fetch first" not in pushed.stderr:
                _ops("insights_push_deferred", "push failed: " + pushed.stderr.strip())
                return "offline"
        _ops("insights_push_deferred", "push rejected after retries")
        return "rejected"


def _remote_name(repo: Path, requested: str | None) -> str:
    names = git(repo, "remote").stdout.split()
    return requested if requested in names else ("origin" if "origin" in names or not names else names[0])


def _install_hook(repo: Path, name: str, lines: list[str]) -> str:
    custom = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=repo, text=True, capture_output=True).stdout.strip()
    if custom:
        raise RuntimeError(f"core.hooksPath is set ({custom}), as Husky and similar tools do; installing there would add machine-specific files to a tracked folder. Call {name} from your hook manager yourself, see README \"Existing hooks\"")
    hooks = Path(subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-path", "hooks"], cwd=repo, text=True, capture_output=True, check=True).stdout.strip())
    hooks.mkdir(parents=True, exist_ok=True)
    body = "#!/bin/sh\n" + "\n".join(lines) + "\n"
    target = hooks / name
    if target.exists():
        current = target.read_text(encoding="utf-8")
        if current == body:
            return f"{name} hook already installed"
        if "export FBL_HOME=" not in current:  # not ours
            raise RuntimeError(f"refusing to overwrite existing hook: {target}")
        target.write_text(body, encoding="utf-8")
        return f"updated {name} hook"
    target.write_text(body, encoding="utf-8")
    target.chmod(0o755)
    return f"installed {name} hook"


ADAPTERS = {"boundary", "codex", "none"}


def install(repo: Path, domain: str, remote: str | None, adapter: str = "boundary") -> list[str]:
    if not adapter or not set(adapter.split(",")) <= ADAPTERS:
        raise RuntimeError("unknown adapter: " + adapter)
    wt = init(repo, _remote_name(repo, remote))
    tool = Path(__file__).resolve().parent
    q = lambda value: "'" + str(value).replace("'", "'\\''") + "'"
    messages = [
        _install_hook(repo, "post-commit", [f"export FBL_PYTHON={q(posix(python_executable()))}", f"export FBL_HOME={q(posix(tool))}", f"export FBL_INSIGHTS={q(posix(wt))}", f"export FBL_DOMAIN={q(domain)}", f"export FBL_ADAPTER={q(adapter)}", f"exec {q(posix(tool / 'hooks/post-commit'))} \"$@\""]),
        _install_hook(repo, "pre-push", [f"export FBL_PYTHON={q(posix(python_executable()))}", f"export FBL_HOME={q(posix(tool))}", f"exec {q(posix(tool / 'hooks/pre-push'))} \"$@\""]),
    ]
    return [f"insights worktree: {wt}", *messages]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "push", "install"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--remote")
        if name == "init":
            p.add_argument("--publish", action="store_true", help="also push the new branch so others adopt its actor salt")
        if name == "install":
            p.add_argument("--domain", default="default")
            p.add_argument("--adapter", default="boundary", help="comma list of boundary, codex, none")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    remote = _remote_name(repo, args.remote)
    try:
        if args.command == "init":
            wt = init(repo, remote)
            print(wt)
            if args.publish:
                from feedback_loop.store import actor_salt
                actor_salt(wt)
                git(wt, "add", "config/actor.json", "schemas")
                git(wt, "commit", "--quiet", "-m", "Add actor salt and schema")
                print(push(wt, remote))
        elif args.command == "push":
            print(push(worktree_path(repo), remote))
        else:
            print("\n".join(install(repo, args.domain, args.remote, args.adapter)))
    except Exception as error:
        _ops("insights_sync_failure", f"{type(error).__name__}: {error}")
        print(f"insights_sync: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

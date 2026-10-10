#!/usr/bin/env python3
"""Install the friction-note loop into a git repository (Windows, macOS, Linux).

  python install.py --repo PATH [--domain NAME] [--adapter boundary|codex|boundary,codex|none]
                    [--home DIR] [--publish] [--no-agents-md]

The tool is copied to --home (default: ~/.local/share/fbl, %LOCALAPPDATA%\\fbl on Windows) so the
hooks keep working if this folder moves.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "tool"
ITEMS = ["boundary.py", "insights_sync.py", "analyse-change.py", "review.py", "weekly.py", "feedback_loop", "hooks", "schemas", "templates", "docs"]


def default_home() -> Path:
    if os.environ.get("FBL_HOME"):
        return Path(os.environ["FBL_HOME"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "fbl"
    return Path.home() / ".local/share/fbl"


def run(*cmd: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, **kw)


MARK = " command --harness "  # identifies hook entries this tool wrote


def _ours(entry: object) -> bool:
    text = json.dumps(entry)
    return "boundary.py" in text and MARK in text


def _tracked(repo: Path, rel: str) -> bool:
    return run("git", "-C", str(repo), "ls-files", "--error-unmatch", rel, capture_output=True).returncode == 0


def _exclude(repo: Path, lines: list[str]) -> None:
    common = Path(run("git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir", capture_output=True).stdout.strip())
    path = common / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    missing = [line for line in lines if line not in current.splitlines()]
    if missing:
        path.write_text(current + ("" if not current or current.endswith("\n") else "\n") + "\n".join(missing) + "\n", encoding="utf-8")


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        sys.exit(f"{path} is not valid JSON; fix or remove it, or rerun with --no-command-capture")
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _merge(config: dict, event: str, entry: dict) -> None:
    hooks = config.setdefault("hooks", {})
    kept = [e for e in hooks.get(event, []) if not _ours(e)]
    hooks[event] = [*kept, entry]


def _strip(config: dict) -> dict:
    hooks = config.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not _ours(e)]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        config.pop("hooks", None)
    return config


def command_capture(repo: Path, home: Path, wanted: set[str]) -> list[str]:
    """Write per-clone hook configs that feed shell-command results to `boundary.py command`. Local files, excluded from git."""
    python, script = Path(sys.executable).as_posix(), (home / "boundary.py").as_posix()
    done: list[str] = []
    targets = []

    def cmd(label: str) -> str:
        return f'"{python}" "{script}"{MARK}{label}'

    if "claude" in wanted:
        rel = ".claude/settings.local.json"
        config = _load(repo / rel)
        entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": cmd("claude-code"), "timeout": 10}]}
        _merge(config, "PostToolUse", entry)
        _merge(config, "PostToolUseFailure", entry)
        _write(repo / rel, config); targets.append(rel); done.append(rel)
    if "cursor" in wanted:
        rel = ".cursor/hooks.json"
        if _tracked(repo, rel):
            print(f"{rel} is tracked by git; not modified. Add `{cmd('cursor')}` to afterShellExecution and postToolUseFailure yourself.")
        else:
            config = _load(repo / rel)
            config.setdefault("version", 1)
            entry = {"command": cmd("cursor")}
            _merge(config, "afterShellExecution", entry)
            _merge(config, "postToolUseFailure", entry)
            _write(repo / rel, config); targets.append(rel); done.append(rel)
    if "copilot" in wanted:
        rel = ".github/hooks/fbl-commands.json"
        entry = {"type": "command", "bash": cmd("copilot"), "powershell": "& " + cmd("copilot"), "timeoutSec": 10}
        _write(repo / rel, {"version": 1, "hooks": {"postToolUse": [entry], "postToolUseFailure": [entry]}})
        targets.append(rel); done.append(rel)
    _exclude(repo, targets)
    return done


def remove_command_capture(repo: Path) -> None:
    for rel, own_file in ((".claude/settings.local.json", False), (".cursor/hooks.json", False), (".github/hooks/fbl-commands.json", True)):
        path = repo / rel
        if not path.exists():
            continue
        config = _strip(_load(path))
        if own_file or not config or config == {"version": 1}:
            path.unlink()
        else:
            _write(path, config)
        print(f"removed command capture from {rel}")


def uninstall(repo: Path) -> int:
    """Undo the per-repository parts of the install. Only hooks this tool wrote are removed."""
    hooks = Path(run("git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-path", "hooks", capture_output=True).stdout.strip())
    for name in ("post-commit", "pre-push"):
        hook = hooks / name
        if hook.exists() and "export FBL_HOME=" in hook.read_text(encoding="utf-8"):
            hook.unlink()
            print(f"removed {name} hook")
        elif hook.exists():
            print(f"left {name} hook alone: not installed by this tool")
    worktree = Path(run("git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir", capture_output=True).stdout.strip()) / "fbl" / "insights"
    if worktree.exists():
        run("git", "-C", str(repo), "worktree", "remove", "--force", str(worktree), capture_output=True)
        print("removed the insights worktree (the fbl-insights branch is kept)")
    remove_command_capture(repo)
    if (repo / ".fbl").exists():
        shutil.rmtree(repo / ".fbl")
        print("removed .fbl/")
    print("kept: the fbl-insights branch, the friction block in AGENTS.md and the other instruction files (delete by hand)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--domain", default="default")
    ap.add_argument("--adapter", default="boundary")
    ap.add_argument("--home", type=Path, default=None)
    ap.add_argument("--publish", action="store_true", help="push the insights branch to the remote")
    ap.add_argument("--no-agents-md", action="store_true")
    ap.add_argument("--no-command-capture", action="store_true", help="do not write the shell-command hooks for Claude Code, Cursor and Copilot")
    ap.add_argument("--uninstall", action="store_true", help="remove this repository's hooks, launcher and insights worktree (keeps the fbl-insights branch and the instruction files)")
    ap.add_argument("--harness", default="claude,copilot,cursor", help="extra instruction files to write besides AGENTS.md (Codex reads AGENTS.md): any of claude, copilot, cursor, or none")
    args = ap.parse_args()

    wanted = {h.strip() for h in args.harness.split(",") if h.strip()} - {"none"}
    unknown = wanted - {"claude", "copilot", "cursor"}
    if unknown:
        sys.exit("unknown --harness: " + ", ".join(sorted(unknown)))
    if not shutil.which("git"):
        sys.exit("git is required")
    top = run("git", "-C", args.repo, "rev-parse", "--show-toplevel", capture_output=True)
    if top.returncode:
        sys.exit(f"not a git repository: {args.repo}")
    repo = Path(top.stdout.strip()).resolve()

    if args.uninstall:
        return uninstall(repo)

    try:
        import jsonschema  # noqa: F401
    except ImportError:
        print("installing jsonschema (pip install --user jsonschema)")
        if run(sys.executable, "-m", "pip", "install", "--user", "--quiet", "jsonschema").returncode:
            sys.exit("pip failed; install jsonschema manually")

    home = (args.home or default_home()).resolve()
    home.mkdir(parents=True, exist_ok=True)
    for item in ITEMS:
        src, dst = TOOL / item, home / item
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dst)
    for hook in (home / "hooks").iterdir():  # hooks run under sh: force LF even if git converted them
        hook.write_bytes(hook.read_bytes().replace(b"\r\n", b"\n"))
        hook.chmod(0o755)
    print(f"tool installed at {home}")

    py = sys.executable
    steps = [[py, str(home / "boundary.py"), "install", "--repo", str(repo)],
             [py, str(home / "insights_sync.py"), "install", "--repo", str(repo), "--domain", args.domain, "--adapter", args.adapter]]
    if args.publish:
        steps.append([py, str(home / "insights_sync.py"), "init", "--repo", str(repo), "--publish"])
    for step in steps:
        if run(*step).returncode:
            sys.exit(1)

    snippet = (home / "docs/AGENT_SNIPPET.md").read_text(encoding="utf-8")
    block = re.search(r"```text\n(.*?)```", snippet, re.S).group(1)
    written = []

    def append_block(path: Path, text: str, prefix: str = "") -> None:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if re.search(r"^## Friction reporting", existing, re.M):
            print(f"{path.relative_to(repo)} already has the friction block")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(("\n" if existing.strip() else "") + (prefix if not existing else "") + text)
        written.append(str(path.relative_to(repo)))

    if not args.no_agents_md:
        append_block(repo / "AGENTS.md", block)
    if "claude" in wanted:  # Claude Code reads CLAUDE.md, not AGENTS.md; import it
        claude = repo / "CLAUDE.md"
        existing = claude.read_text(encoding="utf-8") if claude.exists() else ""
        if "@AGENTS.md" not in existing:
            with claude.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(("\n" if existing.strip() and not existing.endswith("\n") else "") + "@AGENTS.md\n")
            written.append("CLAUDE.md")
    if "copilot" in wanted:
        append_block(repo / ".github" / "copilot-instructions.md", block)
    if "cursor" in wanted:
        append_block(repo / ".cursor" / "rules" / "friction-reporting.mdc", block, "---\ndescription: Report friction notes before finishing a task\nalwaysApply: true\n---\n\n")
    if not args.no_command_capture:
        captured = command_capture(repo, home, wanted)
        if captured:
            print("wrote shell-command capture to: " + ", ".join(captured) + " (local only, excluded from git)")
    if written:
        print("wrote the friction instructions to: " + ", ".join(written) + " (review and commit)")
    print(f"done. For cloud agents copy {home / 'templates/fbl-record.yml'} and for the weekly report {home / 'templates/fbl-weekly.yml'} to {repo / '.github/workflows'}, then set FBL_TOOL_REPOSITORY, FBL_TOOL_REF and FBL_DOMAIN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

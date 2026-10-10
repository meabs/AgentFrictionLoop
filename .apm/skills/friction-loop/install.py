#!/usr/bin/env python3
"""Install the friction-note loop into a git repository (Windows, macOS, Linux).

  python install.py --repo PATH [--domain NAME] [--adapter boundary|codex|boundary,codex|none]
                    [--home DIR] [--publish] [--no-agents-md]

The tool is copied to --home (default: ~/.local/share/fbl, %LOCALAPPDATA%\\fbl on Windows) so the
hooks keep working if this folder moves.
"""

from __future__ import annotations

import argparse
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--domain", default="default")
    ap.add_argument("--adapter", default="boundary")
    ap.add_argument("--home", type=Path, default=None)
    ap.add_argument("--publish", action="store_true", help="push the insights branch to the remote")
    ap.add_argument("--no-agents-md", action="store_true")
    args = ap.parse_args()

    if not shutil.which("git"):
        sys.exit("git is required")
    top = run("git", "-C", args.repo, "rev-parse", "--show-toplevel", capture_output=True)
    if top.returncode:
        sys.exit(f"not a git repository: {args.repo}")
    repo = Path(top.stdout.strip()).resolve()

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

    if not args.no_agents_md:
        agents = repo / "AGENTS.md"
        existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
        if re.search(r"^## Friction reporting", existing, re.M):
            print("AGENTS.md already has the friction block")
        else:
            snippet = (home / "docs/AGENT_SNIPPET.md").read_text(encoding="utf-8")
            block = re.search(r"```text\n(.*?)```", snippet, re.S).group(1)
            with agents.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(("\n" if existing.strip() else "") + block)
            print(f"added friction block to {agents} (review and commit it)")
    print(f"done. Weekly review: copy {home / 'templates/fbl-weekly.yml'} to {repo / '.github/workflows'} and set FBL_TOOL_REPOSITORY / FBL_TOOL_REF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""fbl-doctor: find what stops an agent verifying a change, before an agent has to discover it.

  fbl-doctor [check] [--run]     static checks of a fresh checkout; --run also executes the test command in a throwaway clone
  fbl-doctor suggest [--write]   turn the prose steps for running tests into one script
  fbl-doctor triage              group the friction notes agents filed into recurring problems, most-hit first

Nothing here calls a model. Findings are leads: each names the file or line it came from.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md", "CONTRIBUTING.md", "README.md")
TEST_WORDS = re.compile(r"\b(pytest|py\.test|jest|vitest|mocha|rspec|phpunit|go test|cargo test|dotnet test|mvn(?:w)? (?:-\S+ )*test|gradle(?:w)? test|npm (?:run )?test|yarn (?:run )?test|pnpm (?:run )?test|bun (?:run )?test|tox|nox|make test|scripts/test\.sh)\b")
ENV_EXAMPLES = (".env.example", ".env.sample", "env.example", ".env.template", "example.env")
MIGRATIONS = (
    ("alembic.ini", "uv run alembic upgrade head" , r"alembic"),
    ("backend/alembic.ini", "cd backend && alembic upgrade head", r"alembic"),
    ("prisma/schema.prisma", "npx prisma migrate deploy", r"prisma"),
    ("manage.py", "python manage.py migrate", r"manage\.py migrate|\bmigrate\b"),
    ("knexfile.js", "npx knex migrate:latest", r"knex"),
    ("db/migrate", "bin/rails db:migrate", r"db:migrate"),
)
PATH_CONTEXT = re.compile(r"\b(Path|directory|exists|isdir|is_dir|StaticFiles|join|resolve|readdir|existsSync|static)\b")

# (pattern in the failure output, short cause, what usually fixes it)
FAILURES = (
    (r"(?i)(directory|folder|file|path)[^\n]{0,400}(does not exist|not found|no such file|absent|missing)|ENOENT|FileNotFoundError", "a file or generated directory is missing", "say which build step creates it, or let the tests skip the check"),
    (r"relation .* does not exist|had no tables|has no tables|no tables|no such table|Table .* doesn't exist|table or view does not exist|SQLITE_ERROR", "the database has no tables", "run the migrations before the tests, in the same command"),
    (r"connection refused|could not connect|ECONNREFUSED|Connection refused|is the server running|Can't connect to", "a service the tests need is not running", "document how to start it (docker compose up -d db) or point the tests at one"),
    (r"KeyError: '[A-Z_]+'|Missing required|not set|environment variable|ValidationError[^\n]*(?:Field required|missing)", "an environment variable is missing", "list the variables, or commit an .env.example and copy it in the script"),
    (r"ModuleNotFoundError|Cannot find module|No module named", "dependencies are not installed", "document or script the install step (uv sync, npm ci, pip install -e .)"),
    (r"command not found|not recognized as|No such file or directory: '(?:uv|node|npm|pnpm|yarn|poetry|mvn|gradle)'", "a tool is not installed", "name the tool and the version it needs"),
    (r"(?i)permission denied|EACCES|not executable|chmod \+x", "a permission problem", "check file modes and the user the tests run as"),
)


def sh(args, cwd, timeout=60, env=None):
    try:
        r = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return 124, ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout or "") + "\n(timed out)"
    except OSError as exc:
        return 127, str(exc)


def tracked(repo: Path) -> list[str]:
    code, out = sh(["git", "ls-files"], repo)
    if code == 0 and out.strip():
        return out.splitlines()
    return [str(p.relative_to(repo)).replace(os.sep, "/") for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts][:20000]


def read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def docs_text(repo: Path) -> dict[str, str]:
    return {rel: read(repo, rel) for rel in INSTRUCTION_FILES if (repo / rel).is_file()}


def fenced_commands(text: str) -> list[tuple[str, str]]:
    """(heading, command line) for each line in a fenced shell block."""
    out, heading, fence = [], "", False
    for line in text.splitlines():
        if line.startswith("```"):
            fence = not fence
            continue
        if not fence and line.startswith("#"):
            heading = line.lstrip("# ").strip()
        elif fence and line.strip() and not line.strip().startswith("#"):
            out.append((heading, line.strip().lstrip("$ ").strip()))
    return out


def test_candidates(repo: Path, files: list[str], docs: dict[str, str]) -> list[tuple[str, str]]:
    """(command, where it was found), most authoritative first."""
    found: list[tuple[str, str]] = []
    for rel, text in docs.items():
        for heading, cmd in fenced_commands(text):
            if TEST_WORDS.search(cmd) and "install" not in cmd.split()[:2]:
                found.append((cmd, rel))
    for rel in files:
        if rel.endswith("scripts/test.sh") or rel == "test.sh":
            found.append((f"bash {rel}", rel))
    if "Makefile" in files and re.search(r"^test\s*:", read(repo, "Makefile"), re.M):
        found.append(("make test", "Makefile"))
    pkg = read(repo, "package.json")
    if pkg:
        try:
            scripts = json.loads(pkg).get("scripts", {})
            if "test" in scripts:
                found.append(("npm test", "package.json"))
        except ValueError:
            pass
    pyproject = read(repo, "pyproject.toml") + read(repo, "backend/pyproject.toml")
    if "pytest" in pyproject or any(f.endswith("conftest.py") for f in files):
        sub = "backend" if any(f.startswith("backend/") and f.endswith("conftest.py") for f in files) else ""
        found.append((f"cd {sub} && pytest" if sub else "pytest", "pytest config"))
    if "pom.xml" in files:
        found.append(("mvn test", "pom.xml"))
    if "build.gradle" in files or "build.gradle.kts" in files:
        found.append(("./gradlew test", "build.gradle"))
    seen, uniq = set(), []
    for cmd, where in found:
        if cmd not in seen:
            seen.add(cmd)
            uniq.append((cmd, where))
    return uniq


def gitignored_paths(repo: Path) -> list[str]:
    out = []
    for line in read(repo, ".gitignore").splitlines():
        s = line.strip().strip("/")
        if s and not s.startswith(("#", "!")) and not re.search(r"[*?\[]", s) and "." not in s.rsplit("/", 1)[-1][:1]:
            out.append(s)
    return out


def check(repo: Path, run: bool, command: str | None, timeout: int) -> list[dict]:
    files = tracked(repo)
    docs = docs_text(repo)
    all_docs = "\n".join(docs.values())
    findings: list[dict] = []

    def add(kind, message, where="", fix=""):
        findings.append({"kind": kind, "message": message, "where": where, "fix": fix})

    cands = test_candidates(repo, files, docs)
    instr = {k: v for k, v in docs.items() if k in ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")}
    if not cands:
        add("no-test-command", "no test command found in the instructions, a script, a Makefile or the package manifest", fix="document the one command that runs the tests")
    elif not any(TEST_WORDS.search(t) for t in instr.values()):
        add("undocumented-test-command", f"the test command (`{cands[0][0]}`, from {cands[0][1]}) is not in AGENTS.md or CLAUDE.md", ", ".join(instr) or "no agent instruction file", "add it, with its prerequisites")

    env_example = next((e for e in ENV_EXAMPLES if e in files), None)
    if env_example and ".env" not in files and not re.search(r"cp\s+\S*\.env|copy\s+\S*\.env|\.env", "\n".join(instr.values())):
        add("env-file", f"{env_example} exists but no committed .env, and the agent instructions do not say to copy it", env_example, "copy it in the test script, or say so in AGENTS.md")

    db_files = [f for f in files if f.endswith(("docker-compose.yml", "docker-compose.yaml", "compose.yml", ".env", ".env.example")) and re.search(r"(?i)DATABASE_URL|postgres|mysql|mongo", read(repo, f))]
    if db_files and not re.search(r"(?i)docker compose|docker-compose|postgres|database|createdb|DATABASE_URL", "\n".join(instr.values())):
        add("database", "the project uses a database but the agent instructions do not say how to get one", db_files[0], "document how to start it and which URL the tests use")

    for marker, cmd, word in MIGRATIONS:
        present = marker in files or any(f.startswith(marker.rstrip("/") + "/") for f in files)
        if present and not re.search(word, "\n".join(instr.values()), re.I):
            add("migrations", f"{marker} is present but the agent instructions never mention migrating; a new database has no tables", marker, f"run `{cmd}` before the tests, in the same script")
            break

    ignored = gitignored_paths(repo)
    src = [f for f in files if f.endswith((".py", ".ts", ".tsx", ".js", ".mjs"))][:3000]
    flagged: set[str] = set()
    for rel in src:
        text = read(repo, rel)
        for ig in ignored:
            base = ig.rsplit("/", 1)[-1]
            if len(base) < 3 or base in ("node_modules", "venv", "env", "__pycache__", "dist", "coverage", "tmp", "logs", "log") or ig in flagged or (repo / ig).exists():
                continue
            m = re.search(rf"^.*[\"'/]{re.escape(base)}[\"'/].*$", text, re.M)
            if m and PATH_CONTEXT.search(m.group(0)) and ig in "\n".join(read(repo, ".gitignore").splitlines()):
                flagged.add(ig)
                line = text[: m.start()].count("\n") + 1
                add("generated-dir", f"{rel}:{line} refers to `{base}`, which is git-ignored and absent from a fresh checkout", f"{rel}:{line}", "say which build step creates it, or have the code skip the check when it is absent")
    if run:
        cmd = command or (cands[0][0] if cands else None)
        if not cmd:
            add("not-run", "no test command to run")
        else:
            tmp = Path(tempfile.mkdtemp(prefix="fbl-doctor-"))
            try:
                clone = tmp / "clone"
                code, out = sh(["git", "clone", "-q", "--local", str(repo), str(clone)], tmp, 300)
                if code:
                    add("not-run", f"could not clone to a clean directory: {out.strip()[:200]}")
                else:
                    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR", "SHELL", "USER")}
                    code, out = sh(["bash", "-lc", cmd], clone, timeout, env)
                    tail = [ln for ln in out.splitlines() if ln.strip()][-12:]
                    if code == 0:
                        findings.append({"kind": "ran", "message": f"`{cmd}` passed in a clean clone", "where": "", "fix": ""})
                    else:
                        for pat, cause, fix in FAILURES:
                            m = re.search(pat, out)
                            if m:
                                line = next((ln.strip() for ln in out.splitlines() if re.search(pat, ln)), m.group(0))
                                add("run-failed", f"`{cmd}` failed in a clean clone (exit {code}): {cause}", line[:160], fix)
                                break
                        else:
                            add("run-failed", f"`{cmd}` failed in a clean clone (exit {code}), cause not recognised", " | ".join(tail[-3:])[:200], "read the output above and document the missing step")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    return findings


def cmd_check(a) -> int:
    repo = Path(a.repo).resolve()
    findings = check(repo, a.run, a.command, a.timeout)
    if a.json:
        print(json.dumps(findings, indent=2))
    else:
        bad = [f for f in findings if f["kind"] != "ran"]
        for f in findings:
            mark = "ok  " if f["kind"] == "ran" else "warn"
            print(f"[{mark}] {f['message']}")
            if f["where"]:
                print(f"       at   {f['where']}")
            if f["fix"]:
                print(f"       fix  {f['fix']}")
        print(f"{len(bad)} thing{'s' if len(bad) != 1 else ''} an agent could trip on" if bad else "nothing found")
    return 1 if any(f["kind"] != "ran" for f in findings) else 0


def build_script(repo: Path) -> tuple[str, list[str]]:
    files = tracked(repo)
    docs = docs_text(repo)
    notes: list[str] = []
    steps: list[str] = []
    envs: list[str] = []
    main = None
    for rel, text in docs.items():
        for heading, cmd in fenced_commands(text):
            if TEST_WORDS.search(cmd) and main is None:
                main = cmd
                notes.append(f"test command from {rel}")
            if re.match(r"^(uv sync|npm (ci|install)|pip install|pnpm install|yarn install|poetry install|bundle install)", cmd) and cmd not in steps and heading.lower().find("test") >= 0:
                steps.append(cmd)
    if main is None:
        cands = test_candidates(repo, files, docs)
        if cands:
            main = cands[0][0]
            notes.append(f"test command from {cands[0][1]}")
    if main is None:
        return "", ["no test command found; nothing to script"]
    while main and re.match(r"^[A-Z_][A-Z0-9_]*=\S+\s", main):
        kv, main = main.split(None, 1)
        envs.append(f"export {kv}")
    cd = ""
    m = re.match(r"^cd\s+(\S+)\s*&&\s*(.*)$", main)
    if m:
        cd, main = m.group(1), m.group(2)
    mig = next(((marker, cmd) for marker, cmd, word in MIGRATIONS if marker in files and not re.search(word, main)), None)
    if mig and mig[0].endswith("alembic.ini") and "/" in mig[0]:
        cd = cd or mig[0].rsplit("/", 1)[0]
    pre = []
    if mig and not re.search(r"migrat|alembic", "\n".join(steps) + main):
        mc = mig[1]
        mc = re.sub(r"^cd \S+ && ", "", mc)
        if (repo / "uv.lock").exists() or (repo / (cd or ".") / "uv.lock").exists():
            mc = mc if mc.startswith("uv ") else "uv run " + mc
        pre.append(mc)
        notes.append(f"added `{mc}` because {mig[0]} exists and the docs never say to migrate")
    for e in ENV_EXAMPLES:
        if e in files and ".env" not in files:
            pre.insert(0, f'[ -f .env ] || cp {e} .env')
            notes.append(f"copy {e} to .env when there is none")
            break
    if cd and cd != ".":
        main = re.sub(rf"(?<=\s){re.escape(cd)}/", "", main)  # `bash backend/scripts/test.sh` after `cd backend`
    lines = ["#!/usr/bin/env bash", "# Generated by `fbl-doctor suggest`: one command that prepares the environment and runs the tests.", "set -euo pipefail", 'cd "$(git rev-parse --show-toplevel)"']
    if cd and cd not in (".",):
        lines.append(f"cd {cd}")
    lines += envs + steps + pre + [f'{main} "$@"']
    return "\n".join(lines) + "\n", notes


def cmd_suggest(a) -> int:
    repo = Path(a.repo).resolve()
    script, notes = build_script(repo)
    if not script:
        print(notes[0])
        return 1
    for n in notes:
        print(f"# {n}", file=sys.stderr)
    if a.write:
        target = repo / a.write
        if target.exists() and not a.force:
            print(f"{a.write} exists; pass --force to overwrite", file=sys.stderr)
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(script, encoding="utf-8")
        target.chmod(0o755)
        print(f"wrote {a.write}; then tell agents in AGENTS.md: run `bash {a.write}`, nothing else", file=sys.stderr)
    else:
        print(script, end="")
    return 0


STOP = set("the a an and or of to in on for with was were is are it its this that when which then from by as at be not no but into after before while could would should only also used use ran run running tests test".split())


def tokens(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{2,}", text.lower())
    return {w.strip("./-") for w in words if w not in STOP}


def load_notes(repo: Path, extra: list[str]) -> list[dict]:
    paths = [Path(p) for p in extra]
    if not paths:
        code, common = sh(["git", "rev-parse", "--git-common-dir"], repo)
        if code == 0:
            c = Path(common.strip())
            c = c if c.is_absolute() else (repo / c)
            paths = [c / "fbl" / "boundary" / "events.jsonl"]
    notes = []
    for p in paths:
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") == "friction" and e.get("text"):
                    notes.append(e)
    return notes


def cmd_triage(a) -> int:
    notes = load_notes(Path(a.repo).resolve(), a.events or [])
    if not notes:
        print("no friction notes found (pass --events FILE, or run in a repo agents have filed notes in)")
        return 1
    clusters: list[dict] = []
    for n in notes:
        t = tokens(n["text"])
        best, score = None, 0.0
        for c in clusters:
            j = len(t & c["tokens"]) / max(1, len(t | c["tokens"]))
            if j > score:
                best, score = c, j
        if best is not None and score >= a.similarity:
            best["notes"].append(n)
            best["tokens"] |= t
        else:
            clusters.append({"tokens": set(t), "notes": [n]})
    rows = []
    for c in clusters:
        sessions = {(n.get("session") or {}).get("id") or n["event_id"] for n in c["notes"]}
        rep = min(c["notes"], key=lambda n: len(n["text"]))
        cause = next((cs for pat, cs, fx in FAILURES if re.search(pat, rep["text"], re.I)), "")
        fix = next((fx for pat, cs, fx in FAILURES if re.search(pat, rep["text"], re.I)), "")
        rows.append((len(sessions), len(c["notes"]), Counter(n.get("origin") or n.get("category") for n in c["notes"]).most_common(1)[0][0], sorted({n.get("harness", "?") for n in c["notes"]}), rep["text"], cause, fix))
    rows.sort(key=lambda r: (-r[0], -r[1]))
    if a.json:
        print(json.dumps([dict(zip(("sessions", "notes", "origin", "harnesses", "example", "likely_cause", "likely_fix"), r)) for r in rows[: a.top]], indent=2))
        return 0
    print(f"{len(notes)} notes in {len(rows)} groups; most-hit first\n")
    for i, (s, n, origin, harnesses, text, cause, fix) in enumerate(rows[: a.top], 1):
        print(f"{i}. {s} session{'s' if s != 1 else ''}, {n} note{'s' if n != 1 else ''} [{origin}; {', '.join(harnesses)}]")
        print(f"   {text}")
        if cause:
            print(f"   looks like: {cause}; usual fix: {fix}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "check")
    p = argparse.ArgumentParser(prog="fbl-doctor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="find what would stop an agent verifying a change")
    c.add_argument("--repo", default=".")
    c.add_argument("--run", action="store_true", help="run the test command in a throwaway clone with a minimal environment")
    c.add_argument("--command", help="the command to run (default: the first test command found)")
    c.add_argument("--timeout", type=int, default=600)
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)
    s = sub.add_parser("suggest", help="draft one script that prepares the environment and runs the tests")
    s.add_argument("--repo", default=".")
    s.add_argument("--write", metavar="PATH", help="write the script here (relative to the repo), e.g. scripts/test.sh")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_suggest)
    t = sub.add_parser("triage", help="group the friction notes into recurring problems")
    t.add_argument("--repo", default=".")
    t.add_argument("--events", nargs="*", help="events.jsonl files (default: this repository's)")
    t.add_argument("--top", type=int, default=10)
    t.add_argument("--similarity", type=float, default=0.3)
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_triage)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())

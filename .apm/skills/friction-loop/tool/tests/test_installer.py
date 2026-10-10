from __future__ import annotations

import subprocess
import sys
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[2] / "install.py"


def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def install(path: Path, tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(INSTALL), "--repo", str(path), "--domain", "t", "--home", str(tmp_path / "home"), *extra], capture_output=True, text=True)


def test_writes_instructions_for_every_harness_and_is_idempotent(tmp_path: Path):
    path = project(tmp_path)
    (path / "CLAUDE.md").write_text("# Project rules\n")
    first = install(path, tmp_path)
    assert first.returncode == 0, first.stderr
    for name in ("AGENTS.md", ".github/copilot-instructions.md", ".cursor/rules/friction-reporting.mdc"):
        text = (path / name).read_text()
        assert "## Friction reporting" in text and "--origin" in text and "Friction[<origin>]" in text, name
    assert (path / ".cursor/rules/friction-reporting.mdc").read_text().startswith("---\n")
    assert (path / "CLAUDE.md").read_text() == "# Project rules\n@AGENTS.md\n"
    snapshot = {n: (path / n).read_text() for n in ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md", ".cursor/rules/friction-reporting.mdc")}
    second = install(path, tmp_path)
    assert second.returncode == 0, second.stderr
    assert snapshot == {n: (path / n).read_text() for n in snapshot}


def test_harness_none_and_no_agents_md_write_nothing(tmp_path: Path):
    path = project(tmp_path)
    assert install(path, tmp_path, "--harness", "none", "--no-agents-md").returncode == 0
    assert not (path / "AGENTS.md").exists() and not (path / "CLAUDE.md").exists() and not (path / ".cursor").exists()


def test_unknown_harness_is_rejected(tmp_path: Path):
    path = project(tmp_path)
    result = install(path, tmp_path, "--harness", "vim")
    assert result.returncode != 0 and "unknown --harness" in result.stderr

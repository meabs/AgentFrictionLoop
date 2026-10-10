#!/usr/bin/env python3
"""Weekly job: build the review report on the insights branch and prepare an issue body when there is something to act on."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import sys
from pathlib import Path

import insights_sync
import review


def recommended_section(markdown: str) -> str:
    match = re.search(r"^## Recommended improvements\n(.*?)(?=^## |\Z)", markdown, re.S | re.M)
    return match.group(1).strip() if match else ""


def has_recommendations(section: str) -> bool:
    return any(line.startswith("- F-") for line in section.splitlines())


def run(insights: Path, push: bool, remote: str, today: str | None = None) -> dict:
    today = today or dt.date.today().isoformat()
    report = insights / "reports" / f"{today}.md"
    if review.main(["--insights", str(insights), "--out", str(report), "report"]):
        raise RuntimeError("review failed")
    section = recommended_section(report.read_text(encoding="utf-8"))
    digest = hashlib.sha256(section.encode()).hexdigest()[:16]
    marker = insights / "reports" / ".last-issue-digest"
    issue = None
    if has_recommendations(section) and (not marker.exists() or marker.read_text().strip() != digest):
        issue = insights / "reports" / f"{today}.issue.md"
        issue.write_text(
            f"Weekly agent-feedback review for {today}.\n\n"
            "These are leads from agent friction notes and session metrics, not proof. "
            f"Full report and evidence records are on the `{insights_sync.BRANCH}` branch: `reports/{today}.md`.\n\n"
            f"## Recommended improvements\n\n{section}\n",
            encoding="utf-8",
        )
        marker.write_text(digest + "\n", encoding="utf-8")
    add = ["reports/" + report.name, "reports/" + report.with_name(report.stem + ".facts.json").name]
    if marker.exists():
        add.append("reports/" + marker.name)
    insights_sync.git(insights, "add", *add)
    if insights_sync.git(insights, "diff", "--cached", "--quiet").returncode == 1:
        insights_sync.git(insights, "commit", "--quiet", "-m", f"report {today}")
    status = insights_sync.push(insights, remote) if push else "not-pushed"
    return {"report": str(report), "issue_body": str(issue) if issue else None, "push": status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--insights", required=True)
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args(argv)
    result = run(Path(args.insights).resolve(), args.push, args.remote)
    print(f"report={result['report']}")
    print(f"issue_body={result['issue_body'] or ''}")
    print(f"push={result['push']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

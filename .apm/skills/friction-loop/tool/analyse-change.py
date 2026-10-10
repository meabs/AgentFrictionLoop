#!/usr/bin/env python3
"""CLI entry point for Level 1 change analysis."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from feedback_loop.record import build_record
from feedback_loop.store import actor_salt, persist, _log
from feedback_loop.codex_adapter import enrich_record as enrich_codex
from feedback_loop.boundary_adapter import enrich_record as enrich_boundary


def _adapters(value: str) -> list[str]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(values) - {"codex", "boundary", "none"})
    if unknown:
        raise argparse.ArgumentTypeError("unknown adapter: " + ", ".join(unknown))
    return values or ["none"]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--repo", required=True)
    result.add_argument("--commit", default="HEAD")
    result.add_argument("--insights", required=True)
    result.add_argument("--domain", default="default")
    result.add_argument("--repository")
    result.add_argument("--ci-results")
    result.add_argument("--telemetry")
    result.add_argument("--adapter", type=_adapters, default=["none"])
    result.add_argument("--session")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--no-commit", action="store_true")
    result.add_argument("--fail-open", action="store_true")
    result.add_argument("--print", dest="print_record", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        salt = actor_salt(args.insights)
        record = build_record(args.repo, args.commit, args.domain, args.repository, args.ci_results, salt)
        for adapter in args.adapter:
            if adapter == "none":
                continue
            try:
                record = (enrich_codex if adapter == "codex" else enrich_boundary)(record, args.repo, args.insights, args.session)
            except Exception as error:
                _log(None, adapter + " adapter skipped: " + type(error).__name__ + ": " + str(error))
        if args.print_record or args.dry_run:
            print(json.dumps(record, sort_keys=True, indent=2))
        if not args.dry_run:
            persist(record, args.insights, Path(__file__).parent / "schemas" / "analysis-v1.schema.json", args.no_commit)
        return 0
    except Exception as error:  # fail-open is intentionally opt-in for the CLI; hooks always set it.
        if args.fail_open:
            _log(None, type(error).__name__ + ": " + str(error))
            return 0
        print(f"analyse-change: {error}", file=sys.stderr)
        if "PYTEST_CURRENT_TEST" not in __import__("os").environ:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Deterministic analysis of validated feedback records."""

from __future__ import annotations

import itertools
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError


ENTRYPOINT_GUIDANCE_FILES = {"AGENTS.md", "CLAUDE.md", "GEMINI.md", "copilot-instructions.md"}
ENVIRONMENTAL_EXIT_CODES = {126, 127}  # command not found / not executable: environment, not guidance
METRICS = ("cost_usd", "total_tokens", "api_turns", "tool_calls", "wall_seconds", "commands_failed", "discovery_steps_before_first_edit", "search_count", "repeated_reads", "first_verification_failed", "failed_verification_attempts")
FRICTION_PROPOSALS = {
    "setup": "Document setup prerequisites and provide one runnable setup command block.",
    "test-order": "Document the required test and verification order, including prerequisites.",
    "missing-doc": "Add or link the missing repository documentation from AGENTS.md.",
    "unclear-instruction": "Rewrite the unclear instruction with a concrete expected outcome and example.",
    "environment": "Record the environment prerequisite and a diagnostic remedy.",
    "tooling": "Document the supported tool invocation and its expected configuration.",
    "other": "Review the repeated friction notes and add concise repository guidance.",
    "repository-discovery": "Improve navigation: link the contract, doc or entry point from AGENTS.md or add it to an index agents already read.",
    "task-context": "Improve how tasks are briefed: acceptance criteria, constraints and inputs are missing before the agent starts. This is usually fixed in the issue or prompt template, outside the repository.",
    "execution-environment": "Fix the tooling or document the prerequisite and its order (migrate, install, build, generate) as one runnable block.",
    "verification": "Improve tests or test infrastructure so a change can be shown correct: a fast targeted command, stable fixtures, a clear pass signal.",
    "conflicting-knowledge": "Correct provenance or authority: update or remove the document that contradicts the implementation, and mark which source wins.",
}
# Origins that name a fix outside the repository's docs; reported but not turned into a docs recommendation.
NO_DOC_RECOMMENDATION = {"environment", "task-context"}


def _friction_key(item: dict[str, Any]) -> str | None:
    """Group by origin when the agent gave one, otherwise by the older category."""
    return item.get("origin") or item.get("category")


def _schema(schema: Path | None = None) -> Draft202012Validator:
    path = schema or Path(__file__).resolve().parents[1] / "schemas/analysis-v1.schema.json"
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def load_records(insights: str | Path, since: str | None = None, until: str | None = None, schema: Path | None = None) -> tuple[list[dict[str, Any]], int, int]:
    """Return records, invalid count, and incomplete-telemetry count."""
    validator = _schema(schema)
    records: list[dict[str, Any]] = []
    invalid = 0
    incomplete = 0
    root = Path(insights) / "records"
    for path in sorted(root.rglob("*.json")) if root.exists() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            validator.validate(record)
            timestamp = record["timestamp"][:10]
            if since and timestamp < since or until and timestamp > until:
                continue
            records.append(record)
            telemetry = record.get("harness_telemetry")
            if telemetry is not None and telemetry.get("session_complete") is not True:
                incomplete += 1
        except (OSError, ValueError, ValidationError):
            invalid += 1
    # One session can produce several commits. Latest record is authoritative.
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        session = record.get("harness_telemetry", {}).get("session_id")
        key = f"session:{session}" if session else f"record:{record['record_id']}"
        if key not in latest or record["timestamp"] > latest[key]["timestamp"]:
            latest[key] = record
    return sorted(latest.values(), key=lambda r: (r["timestamp"], r["record_id"])), invalid, incomplete


def group_epochs(records: list[dict[str, Any]]) -> dict[str, list[list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["repository"]].append(record)
    result: dict[str, list[list[dict[str, Any]]]] = {}
    for repository, values in grouped.items():
        epochs: list[list[dict[str, Any]]] = []
        for record in sorted(values, key=lambda r: r["timestamp"]):
            digest = record["guidance"]["digest"]
            if not epochs or epochs[-1][0]["guidance"]["digest"] != digest:
                epochs.append([])
            epochs[-1].append(record)
        result[repository] = epochs
    return result


def _sessions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r.get("harness_telemetry", {}).get("session_complete") is True]


def _attributed_actor_ids(records: list[dict[str, Any]]) -> list[str]:
    return [
        actor["id"]
        for record in records
        if isinstance(actor := record.get("actor"), dict)
        and actor.get("kind") != "bot"
        and isinstance(actor.get("id"), str)
    ]


def classify_scope(evidence_records: list[dict[str, Any]], min_actors: int) -> str:
    """Classify spread using only human-attributed evidence records."""
    ids = _attributed_actor_ids(evidence_records)
    if not ids:
        return "unattributed"
    counts = Counter(ids)
    if len(counts) >= min_actors:
        return "shared"
    if max(counts.values()) / len(ids) >= 0.70:
        return "concentrated"
    return "limited"


def _actor_summary(records: list[dict[str, Any]]) -> tuple[dict[str, Any], set[str]]:
    ids = _attributed_actor_ids(records)
    counts = Counter(ids)
    total = len(ids)
    shares = sorted((count / total for count in counts.values()), reverse=True) if total else []
    return {"distinct": len(counts), "attributed": total, "unattributed": len(records) - total, "largest_share": shares[0] if shares else 0.0}, set(counts)


def _finding(pattern: str, observation: str, records: list[dict[str, Any]], change: str, rationale: str, effect: str, metric: str = "", *, recommendation: bool = True, min_actors: int = 3) -> dict[str, Any]:
    evidence = sorted({r["record_id"] for r in records})
    repositories = sorted({r["repository"] for r in records})
    actor_mix, _ = _actor_summary(records)
    scope = classify_scope(records, min_actors)
    return {"pattern_class": pattern, "observation": observation, "evidence_count": len(evidence), "evidence": evidence, "repositories": repositories, "proposed_change": change, "rationale": rationale, "expected_effect": effect, "metric": {"name": metric, "direction": "lower"} if metric else {"name": "", "direction": "lower"}, "established": False, "recommendation": recommendation, "scope": scope, "actors": actor_mix["distinct"], "distinct_actors": actor_mix["distinct"], "attributed_records": actor_mix["attributed"], "unattributed_records": actor_mix["unattributed"], "largest_actor_share": actor_mix["largest_share"]}


def _verification_command(signature: str) -> bool:
    words = signature.lower().split()
    return any(word in {"pytest", "ruff", "mypy", "eslint", "tsc", "test", "tests", "build", "alembic", "migrate", "migration"} for word in words)


def _first_verification_failed(record: dict[str, Any]) -> bool | None:
    attempts = [a for a in record.get("execution", {}).get("verification_attempts", []) if isinstance(a.get("exit_code"), int) and a["exit_code"] not in ENVIRONMENTAL_EXIT_CODES]
    return None if not attempts else attempts[0]["exit_code"] != 0


def _friction(record: dict[str, Any], category: str | None = None) -> list[dict[str, Any]]:
    values = record.get("friction", [])
    return [item for item in values if isinstance(item, dict) and (category is None or _friction_key(item) == category)]


def _command_prefix(signature: str) -> str:
    words = signature.split()
    return " ".join(words[:3]) if len(words) >= 3 and words[:2] in (["uv", "run"], ["npm", "run"], ["bun", "run"]) else " ".join(words[:1])


def _guidance_body(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def detect_patterns(records: list[dict[str, Any]], min_evidence: int = 3, min_actors: int = 3) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    complete = _sessions(records)
    def add(finding: dict[str, Any]) -> None:
        evidence_records = [r for r in records if r["record_id"] in set(finding["evidence"])]
        actor_mix, _ = _actor_summary(evidence_records)
        finding["scope"] = classify_scope(evidence_records, min_actors)
        finding["distinct_actors"] = actor_mix["distinct"]
        finding["actors"] = actor_mix["distinct"]
        finding["attributed_records"] = actor_mix["attributed"]
        finding["unattributed_records"] = actor_mix["unattributed"]
        finding["largest_actor_share"] = actor_mix["largest_share"]
        finding["established"] = finding["evidence_count"] >= min_evidence
        if finding.get("pattern_class") in {"verification_failure", "self_reported_friction"}:
            agreeing = 0
            for record in evidence_records:
                has_note = bool(_friction(record, "test-order") or _friction(record, "setup"))
                if has_note and _first_verification_failed(record) is True:
                    agreeing += 1
            finding["agreement"] = {"agreeing_sessions": agreeing, "evidence_sessions": len(evidence_records)}
        findings.append(finding)

    # Verification telemetry is authoritative: only complete sessions, and only
    # the first known attempt of each kind, count as first-attempt failures.
    for kind in ("tests", "lint", "build", "migration"):
        failed: list[dict[str, Any]] = []
        pass_attempts: list[int] = []
        signatures: Counter[str] = Counter()
        for record in complete:
            attempts = [a for a in record.get("execution", {}).get("verification_attempts", []) if a.get("kind") == kind and isinstance(a.get("exit_code"), int) and a["exit_code"] not in ENVIRONMENTAL_EXIT_CODES]
            if not attempts or attempts[0]["exit_code"] == 0:
                continue
            failed.append(record)
            for command in record.get("execution", {}).get("failed_commands", []):
                signature = command.get("signature", "")
                if signature and ((kind == "tests" and "pytest" in signature) or (kind == "lint" and any(x in signature for x in ("ruff", "lint", "eslint"))) or (kind == "build" and "build" in signature) or (kind == "migration" and any(x in signature for x in ("alembic", "migrate")))):
                    signatures[signature] += int(command.get("count", 1) or 1)
            first_pass = next((index for index, attempt in enumerate(attempts, 1) if attempt["exit_code"] == 0), None)
            if first_pass is not None:
                pass_attempts.append(first_pass)
        if failed:
            command_note = f" Dominant failing command: {signatures.most_common(1)[0][0]!r}." if signatures else ""
            pass_note = f" Median attempts to reach a passing attempt: {statistics.median(pass_attempts):g}." if pass_attempts else " No passing attempt was observed."
            add(_finding("verification_failure", f"First {kind} verification attempt failed in {len(failed)} sessions.{command_note}{pass_note}", failed, "Document the exact ordered verification command sequence, including setup prerequisites, in the verify skill / AGENTS.md as ONE runnable command block, and state the failure symptom each prerequisite prevents.", "First-attempt verification failures recur across complete sessions.", f"Higher first-attempt verification pass rate (currently {len(complete) - len(failed)} of {len(complete)}).", kind, min_actors=min_actors))
    friction_groups: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for record in complete:
        for category in sorted({_friction_key(item) for item in _friction(record) if _friction_key(item) in FRICTION_PROPOSALS}):
            session_id = str(record.get("harness_telemetry", {}).get("session_id") or record["record_id"])
            friction_groups[(record["repository"], category)][session_id] = record
    for (repository, category), session_values in sorted(friction_groups.items()):
        values = list(session_values.values())
        samples = []
        seen_samples = set()
        for record in values:
            for item in _friction(record, category):
                text = str(item.get("text", "")).replace("\n", " ").replace("\r", " ")[:240]
                if text and text not in seen_samples:
                    seen_samples.add(text)
                    samples.append(text)
        finding = _finding("self_reported_friction", f"Agents self-reported {category!r} friction in {len(values)} sessions.", values, FRICTION_PROPOSALS[category], "Self-reported by the agent; treat as a lead, not proof unless a boundary or transcript signal agrees.", f"Fewer self-reported {category} friction notes.", "first_verification_failed" if category in {"test-order", "setup", "verification", "execution-environment"} else "commands_failed", recommendation=category not in NO_DOC_RECOMMENDATION, min_actors=min_actors)
        finding["friction_samples"] = samples[:5]
        add(finding)
    failed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    failed_occurrences: dict[str, int] = defaultdict(int)
    failed_codes: dict[str, set[int]] = defaultdict(set)
    for r in complete:
        for item in r.get("execution", {}).get("failed_commands", []):
            signature = item.get("signature", "")
            if signature and not _verification_command(signature):
                failed[signature].append(r)
                failed_occurrences[signature] += int(item.get("count", 1) or 1)
                if isinstance(item.get("exit_code"), int):
                    failed_codes[signature].add(item["exit_code"])
    for signature, values in sorted(failed.items()):
        if signature:
            codes = f" exit codes {', '.join(str(code) for code in sorted(failed_codes[signature]))}." if failed_codes[signature] else ""
            add(_finding("common_failed_command", f"Command signature {signature!r} had {failed_occurrences[signature]} occurrences in {len({r['record_id'] for r in values})} sessions.{codes}", values, "Add the supported command and failure remedy to AGENTS.md.", "A common non-verification command failure indicates discoverability or setup friction.", "Fewer failed command attempts.", "commands_failed"))

    # A recommendation is warranted only when guidance text explicitly mentions
    # a repeatedly failing command and the guidance was not opened in that same
    # guidance epoch. Missing source files simply cannot establish this signal.
    for repository, epochs in group_epochs(complete).items():
        for epoch in epochs:
            by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for record in epoch:
                for item in record.get("execution", {}).get("failed_commands", []):
                    signature = item.get("signature", "")
                    if signature:
                        by_signature[signature].append(record)
            for signature, values in by_signature.items():
                if len({r["record_id"] for r in values}) < min_evidence:
                    continue
                prefix = _command_prefix(signature)
                candidate_paths = sorted({path for r in values for path in r.get("guidance", {}).get("agents_md_referenced_paths", []) if path.endswith(("AGENTS.md", "SKILL.md"))})
                for path in candidate_paths:
                    body = _guidance_body(path)
                    if prefix not in body:
                        continue
                    opened = {r["record_id"] for r in values if path in set(r.get("execution", {}).get("files_opened", [])) or path in set(r.get("execution", {}).get("guidance_files_opened", []))}
                    if len({r["record_id"] for r in values} - opened) >= min_evidence:
                        add(_finding("guidance_trigger_gap", f"Guidance file {path!r} mentions {prefix!r}, but was not opened in {len({r['record_id'] for r in values} - opened)} sessions with that repeated failure.", values, f"Surface {path} earlier / make its trigger explicit.", "The guidance directly names the failing command but was not reached in the same epoch.", "Fewer repeated failures after earlier guidance discovery.", "commands_failed"))
    opened: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in complete:
        for path in r.get("execution", {}).get("files_opened", []):
            if (path.endswith((".md", ".yaml", ".yml", ".json")) and path not in r["guidance"].get("agents_md_referenced_paths", []) and "/skills/" not in path and path.rsplit("/", 1)[-1] not in ENTRYPOINT_GUIDANCE_FILES):
                opened[path].append(r)
    for path, values in sorted(opened.items()):
        add(_finding("documentation_gap", f"{path!r} was opened in {len(values)} sessions without being referenced by AGENTS.md.", values, f"Add a concise link to {path} from AGENTS.md.", "Agents are discovering this documentation indirectly.", "Fewer exploratory reads.", "discovery_steps_before_first_edit"))
    inefficient = [r for r in complete if r.get("execution", {}).get("repeated_searches", 0) or r.get("execution", {}).get("file_read_counts")]
    if inefficient:
        add(_finding("inefficient_discovery", f"{len(inefficient)} sessions repeated searches or file reads.", inefficient, "Add an architecture map and canonical search starting points to repository guidance.", "Repeated discovery work is evidence of unclear navigation.", "Lower repeated searches and reads.", "repeated_reads"))
    for field, label in (("service_yaml_present", "service metadata"), ("okf_present", "OKF metadata"), ("api_schema_present", "API schema")):
        missing = [r for r in records if not r["documentation"].get(field)]
        if missing:
            add(_finding("missing_metadata", f"{label} is absent in {len(missing)} records.", missing, "", "", "", recommendation=False))
    refs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in complete:
        opened_set = set(r.get("execution", {}).get("files_opened", []))
        for ref in r["guidance"].get("agents_md_referenced_paths", []):
            if ref not in opened_set:
                refs[ref].append(r)
    for ref, values in sorted(refs.items()):
        add(_finding("ineffective_agents_md", f"AGENTS.md reference {ref!r} was not opened in {len(values)} sessions.", values, "", "", "", recommendation=False))
    skill_sessions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    skill_loaded: set[tuple[str, str]] = set()
    for r in complete:
        for skill in r["guidance"].get("skills_available", []):
            skill_sessions[(r["repository"], skill)].append(r)
        for skill in r.get("execution", {}).get("skills_loaded", []):
            skill_loaded.add((r["repository"], skill))
    for (repo, skill), values in sorted(skill_sessions.items()):
        if (repo, skill) not in skill_loaded and len(values) >= min_evidence:
            add(_finding("ineffective_skills", f"Skill {skill!r} was available but loaded in 0 of {len(values)} sessions.", values, "", "", "", recommendation=False))
    token_records = [r for r in complete if isinstance(r.get("harness_telemetry", {}).get("total_tokens"), (int, float))]
    if token_records:
        median = statistics.median(r["harness_telemetry"]["total_tokens"] for r in token_records)
        high = [r for r in token_records if r["harness_telemetry"]["total_tokens"] > 1.5 * median]
        if high:
            add(_finding("high_context_consumption", f"{len(high)} sessions exceeded 1.5x the median context consumption ({median:g} tokens).", high, "Add a concise repository map and task-specific entry points.", "High context use suggests avoidable exploration.", "Lower total tokens.", "total_tokens"))
    for index, finding in enumerate(findings, 1):
        finding["id"] = f"F-{index:03d}"
    return findings


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values), "min": min(values), "max": max(values)}


def _pvalue(before: list[float], after: list[float]) -> float:
    observed = statistics.mean(after) - statistics.mean(before)
    all_values = before + after
    n = len(before)
    choices = list(itertools.combinations(range(len(all_values)), n))
    if len(choices) > 20000:
        rng = random.Random(0)
        choices = [tuple(sorted(rng.sample(range(len(all_values)), n))) for _ in range(20000)]
    extreme = 0
    for indexes in choices:
        selected = set(indexes)
        difference = statistics.mean(all_values[i] for i in indexes) - statistics.mean(all_values[i] for i in range(len(all_values)) if i not in selected)
        if abs(difference) >= abs(observed):
            extreme += 1
    return extreme / len(choices)


def effect_analysis(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def actor_mix(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
        def ids(values: list[dict[str, Any]]) -> Counter[str]:
            return Counter(_attributed_actor_ids(values))
        left_counts, right_counts = ids(left), ids(right)
        overall = set(left_counts) | set(right_counts)
        before_share = max(left_counts.values(), default=0) / sum(left_counts.values()) if left_counts else 0.0
        after_share = max(right_counts.values(), default=0) / sum(right_counts.values()) if right_counts else 0.0
        return {"before": {"distinct": len(left_counts), "largest_share": before_share}, "after": {"distinct": len(right_counts), "largest_share": after_share}, "overlap": len(set(left_counts) & set(right_counts)), "overall_distinct": len(overall), "before_distinct": len(left_counts), "after_distinct": len(right_counts), "before_largest_share": before_share, "after_largest_share": after_share}
    mix = actor_mix(before, after)
    dominated = mix["overall_distinct"] >= 2 and (mix["before_largest_share"] > 0.5 or mix["after_largest_share"] > 0.5)
    single_actor = mix["overall_distinct"] == 1
    results = []
    for metric in METRICS:
        def value(record: dict[str, Any]) -> float | None:
            if metric == "repeated_reads":
                return float(sum(record.get("execution", {}).get("file_read_counts", {}).values()))
            if metric in {"first_verification_failed", "failed_verification_attempts"}:
                attempts = [
                    attempt for attempt in record.get("execution", {}).get("verification_attempts", [])
                    if isinstance(attempt.get("exit_code"), int) and not isinstance(attempt.get("exit_code"), bool) and attempt["exit_code"] not in ENVIRONMENTAL_EXIT_CODES
                ]
                if not attempts:
                    return None
                if metric == "first_verification_failed":
                    return float(attempts[0]["exit_code"] != 0)
                return float(sum(attempt["exit_code"] != 0 for attempt in attempts))
            source = record.get("harness_telemetry", {}) if metric in {"cost_usd", "total_tokens", "api_turns", "tool_calls", "wall_seconds"} else record.get("execution", {})
            item = source.get(metric)
            return float(item) if isinstance(item, (int, float)) else None
        left = [v for r in before if (v := value(r)) is not None]
        right = [v for r in after if (v := value(r)) is not None]
        if not left or not right:
            continue
        s1, s2 = _stats(left), _stats(right)
        p = _pvalue(left, right)
        direction = "lower" if s2["mean"] < s1["mean"] else "higher" if s2["mean"] > s1["mean"] else "same"
        verdict = "no change" if direction == "same" else ("improved" if direction == "lower" and p <= .10 and len(left) >= 3 and len(right) >= 3 else "regressed" if direction == "higher" and p <= .10 and len(left) >= 3 and len(right) >= 3 else "directional")
        reasons = []
        if dominated:
            verdict = "directional"
            reasons.append("dominated by one actor")
        results.append({"metric": metric, "before": s1, "after": s2, "mean_change_percent": None if not s1["mean"] else (s2["mean"] - s1["mean"]) / s1["mean"] * 100, "median_change_percent": None if not s1["median"] else (s2["median"] - s1["median"]) / s1["median"] * 100, "p_value": p, "verdict": verdict, "reasons": reasons, "actor_mix": mix})
    return results


def read_ledger(insights: str | Path) -> list[dict[str, Any]]:
    path = Path(insights) / "config/recommendations.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def write_ledger(insights: str | Path, entry: dict[str, Any], update_id: str | None = None) -> None:
    required = {"id", "finding_pattern", "repository", "metric", "proposed_change", "status", "before_digest", "after_digest", "pr_ref", "note"}
    if set(entry) != required or entry["status"] not in {"proposed", "implemented", "reverted"}:
        raise ValueError("invalid recommendation ledger entry")
    path = Path(insights) / "config/recommendations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    values = read_ledger(insights)
    if update_id:
        for index, old in enumerate(values):
            if old.get("id") == update_id:
                values[index] = entry
                break
        else:
            raise ValueError("ledger id not found")
    else:
        if any(old.get("id") == entry["id"] for old in values):
            raise ValueError("duplicate ledger id")
        values.append(entry)
    path.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_facts(insights: str | Path, since: str | None = None, until: str | None = None, min_evidence: int = 3, min_actors: int = 3) -> dict[str, Any]:
    records, invalid, incomplete = load_records(insights, since, until)
    findings = detect_patterns(records, min_evidence, min_actors)
    epochs = group_epochs(records)
    effects = []
    for repository, groups in epochs.items():
        for left, right in zip(groups, groups[1:]):
            if len(left) >= 2 and len(right) >= 2:
                effects.extend({"repository": repository, "guidance_change": f"{left[0]['guidance']['digest'][:8]} to {right[0]['guidance']['digest'][:8]}", **item} for item in effect_analysis(_sessions(left), _sessions(right)))
    ledger = read_ledger(insights)
    for entry in ledger:
        if entry.get("status") != "implemented":
            continue
        matching = [r for r in records if r["repository"] == entry.get("repository")]
        before = [r for r in matching if r["guidance"]["digest"] == entry.get("before_digest")]
        after = [r for r in matching if r["guidance"]["digest"] == entry.get("after_digest")]
        # Keep every available metric visible for implemented recommendations.  A
        # one-record side is still useful as an outcome; effect_analysis marks its
        # verdict directional when the sample is too small for a claim.
        entry["outcome"] = effect_analysis(_sessions(before), _sessions(after))
    return {
        "records": sorted({r["record_id"] for r in records}),
        "sessions": len({r.get("harness_telemetry", {}).get("session_id") for r in records if r.get("harness_telemetry", {}).get("session_complete") is True}),
        "repositories": sorted({r["repository"] for r in records}),
        "guidance_epochs": sorted({(r["repository"], r["guidance"]["digest"]) for r in records}),
        "invalid_records": invalid,
        "incomplete_telemetry": incomplete,
        "findings": findings,
        "effects": effects,
        "ledger": ledger,
        "actor_distinct": len(set(_attributed_actor_ids(records))),
        "actor_ids": sorted(set(_attributed_actor_ids(records))),
    }

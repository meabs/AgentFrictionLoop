"""Safe deterministic Markdown rendering and validation."""

from __future__ import annotations

import json
import re
from html import escape
from typing import Any


SECTIONS = ("# Agent Engineering Review", "## Summary", "## Repeated friction", "## Documentation gaps", "## Architecture discovery issues", "## Verification failures", "## Skills / AGENTS.md effectiveness", "## Recommended improvements", "## Previous recommendation outcomes")


def _text(value: Any) -> str:
    # Evidence is data: render it as escaped text, never as Markdown/control syntax.
    return escape(str(value).replace("\n", " ").replace("\r", " "), quote=False).replace("`", "'").replace("\\", "\\\\")


def _percent(value: Any) -> str:
    try:
        return _text(f"{float(value) * 100:g}")
    except (TypeError, ValueError):
        return "0"


def _effect_line(effect: dict[str, Any]) -> str:
    marker = "REGRESSION" if effect.get("verdict") == "regressed" else effect.get("verdict", "").upper()
    before = effect.get("before", {})
    after = effect.get("after", {})
    mix = effect.get("actor_mix", {})
    reason = f" reasons={_text(', '.join(effect.get('reasons', [])))};" if effect.get("reasons") else ""
    return (
        f"- {marker} {_text(effect.get('repository', ''))} {_text(effect.get('metric', ''))}: "
        + (f"guidance {_text(effect['guidance_change'])}; " if effect.get("guidance_change") else "") +
        f"n={before.get('n')} vs n={after.get('n')}; "
        f"means={_text(before.get('mean'))} -> {_text(after.get('mean'))}; "
        f"medians={_text(before.get('median'))} -> {_text(after.get('median'))}; "
        f"p-value={_text(effect.get('p_value'))}; "
        f"median change={_text(effect.get('median_change_percent'))}%; "
        f"actor mix: before distinct={mix.get('before_distinct', 0)}, after distinct={mix.get('after_distinct', 0)}, overlap={mix.get('overlap', 0)}, "
        f"largest shares={mix.get('before_largest_share', 0) * 100:g}%/{mix.get('after_largest_share', 0) * 100:g}%.{reason}"
    )


def render_report(facts: dict[str, Any]) -> str:
    findings = facts.get("findings", [])
    lines = [SECTIONS[0], "", SECTIONS[1], ""]
    for effect in facts.get("effects", []):
        if effect.get("verdict", "").upper() in {"REGRESSED", "IMPROVED", "DIRECTIONAL"}:
            lines.append(_effect_line(effect))
    sessions = facts.get("sessions", len(facts.get("records", [])))
    repositories = facts.get("repositories", [])
    epochs = facts.get("guidance_epochs", [])
    caveats = []
    if len(repositories) <= 1 or sessions < 10:
        caveats.append("small sample: effects are directional")
    if facts.get("actor_distinct", 0) <= 1:
        caveats.append("single author: person effect cannot be separated from guidance effect")
    lines.extend([f"Records analysed: {len(facts.get('records', []))}.", f"Sessions: {sessions}.", f"Distinct repositories: {len(repositories)}.", f"Guidance digest epochs: {len(epochs)}.", *(f"Caveat: {item}." for item in caveats), f"Invalid records skipped: {facts.get('invalid_records', 0)}.", f"Incomplete telemetry: {facts.get('incomplete_telemetry', 0)}.", ""])
    buckets = {"Repeated friction": {"common_failed_command", "high_context_consumption", "self_reported_friction"}, "Documentation gaps": {"documentation_gap", "missing_metadata"}, "Architecture discovery issues": {"inefficient_discovery"}, "Verification failures": {"verification_failure"}, "Skills / AGENTS.md effectiveness": {"ineffective_skills", "ineffective_agents_md"}}
    for section in SECTIONS[2:7]:
        lines.append("## " + section[3:])
        lines.append("")
        bucket = buckets[section[3:]]
        selected = [f for f in findings if f.get("pattern_class") in bucket and f.get("established")]
        below = [f for f in findings if f.get("pattern_class") in bucket and not f.get("established")]
        if not selected and not below:
            lines.extend(["No established findings.", ""])
            continue
        for finding in selected:
            ids = ", ".join(_text(item) for item in finding.get("evidence", []))
            prefix = "Informational (no recommendation without a linked failure): " if not finding.get("recommendation", True) else ""
            lines.append(f"- {_text(finding['id'])}: {prefix}{_text(finding['observation'])} Scope: {_text(finding.get('scope', 'unattributed'))}; distinct actors: {_text(finding.get('distinct_actors', 0))}; actor share: {_percent(finding.get('largest_actor_share', 0))}%. Evidence: {finding['evidence_count']} records ({ids}).")
        if below:
            lines.extend(["Not established (below threshold):"])
            for finding in below:
                ids = ", ".join(_text(item) for item in finding.get("evidence", []))
                prefix = "Informational (no recommendation without a linked failure): " if not finding.get("recommendation", True) else ""
                lines.append(f"- {_text(finding['id'])}: {prefix}{_text(finding['observation'])} Scope: {_text(finding.get('scope', 'unattributed'))}; distinct actors: {_text(finding.get('distinct_actors', 0))}; actor share: {_percent(finding.get('largest_actor_share', 0))}%. Evidence: {finding['evidence_count']} records ({ids}).")
        lines.append("")
    lines.extend([SECTIONS[7], ""])
    recommendations = [f for f in findings if f.get("established") and f.get("recommendation", True) and f.get("scope") != "concentrated"]
    concentrated = [f for f in findings if f.get("established") and f.get("scope") == "concentrated"]
    if recommendations:
        for f in recommendations:
            ids = ", ".join(_text(item) for item in f.get("evidence", []))
            limited = " (limited spread)" if f.get("scope") == "limited" else ""
            lines.append(f"- {_text(f['id'])}: Observation: {_text(f['observation'])}{limited} Proposed change: {_text(f['proposed_change'])} Rationale: {_text(f['rationale'])} Expected effect: {_text(f['expected_effect'])}. Evidence: {f['evidence_count']} records ({ids}) across {_text(', '.join(f['repositories']))}.")
    else:
        lines.append("No established recommendations.")
    if concentrated:
        lines.append("Not recommended (concentrated in few people):")
        for f in concentrated:
            lines.append(f"- {_text(f['id'])}: {_text(f['observation'])} Concentrated: {_text(f.get('distinct_actors', 0))} distinct actors across {_text(f.get('attributed_records', 0))} attributed records; largest share {_percent(f.get('largest_actor_share', 0))}%." )
    lines.extend(["", SECTIONS[8], ""])
    ledger = facts.get("ledger", [])
    if ledger:
        for item in ledger:
            outcome = item.get("outcome")
            suffix = f" Before/after: {_text(json.dumps(outcome, sort_keys=True))}." if outcome else ""
            lines.append(f"- {_text(item.get('id'))}: {_text(item.get('status'))} ({_text(item.get('repository'))}).{suffix}")
    else:
        lines.append("No recommendation outcomes recorded.")
    return "\n".join(lines) + "\n"


MIN_LEAK_SAMPLE = 12


def validate_report(markdown: str, facts: dict[str, Any]) -> None:
    positions = [markdown.find(section) for section in SECTIONS]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        raise ValueError("required report sections missing or out of order")
    known_findings = {item["id"]: item for item in facts.get("findings", [])}
    known_records = set(facts.get("records", []))
    for actor in facts.get("actor_ids", []):
        if actor and actor in markdown:
            raise ValueError("report contains actor id")
    for finding in facts.get("findings", []):
        for sample in finding.get("friction_samples", []):
            # Very short notes ("x", "ok") match ordinary words, so only longer text counts as a leak.
            if len(sample) >= MIN_LEAK_SAMPLE and sample in markdown:
                raise ValueError("report contains friction sample text")
    for identifier in re.findall(r"\bF-\d{3}\b", markdown):
        if identifier not in known_findings:
            raise ValueError("report references unknown finding")
    for identifier in re.findall(r"\b[^\s()]+@(?:[0-9a-f]{12})\b", markdown):
        if identifier not in known_records:
            raise ValueError("report references unknown record")
    for finding in known_findings.values():
        if finding["id"] in markdown:
            expected = f"Evidence: {finding['evidence_count']} records"
            if expected not in markdown:
                raise ValueError("evidence count differs from facts")
    for finding in facts.get("findings", []):
        if finding.get("established") and finding.get("recommendation", True) and finding.get("scope") != "concentrated" and finding["id"] in markdown:
            for key in ("observation", "evidence_count", "repositories", "proposed_change", "rationale", "expected_effect"):
                if not finding.get(key):
                    raise ValueError("recommendation lacks required evidence fields")
            recommendation = next((line for line in markdown.splitlines() if finding["id"] in line and "Observation:" in line), "")
            if "Observation:" not in recommendation or "Proposed change:" not in recommendation or "Rationale:" not in recommendation or "Expected effect:" not in recommendation or "Evidence:" not in recommendation:
                raise ValueError("recommendation lacks required evidence fields")
    for marker in ("REGRESSION", "IMPROVEMENT", "DIRECTIONAL"):
        for line in (line for line in markdown.splitlines() if re.match(rf"^\s*-\s+{marker}\b", line)):
            expected_verdict = "regressed" if marker == "REGRESSION" else marker.lower()
            matches = [effect for effect in facts.get("effects", []) if effect.get("verdict") == expected_verdict and _text(effect.get("repository")) in line and f" {_text(effect.get('metric'))}: " in line and (not effect.get("guidance_change") or f"guidance {_text(effect['guidance_change'])};" in line) and f"n={effect['before']['n']} vs n={effect['after']['n']};" in line and f"means={_text(effect['before']['mean'])} -> {_text(effect['after']['mean'])};" in line]
            if not matches or len(matches) != markdown.splitlines().count(line):
                raise ValueError("effect identity differs from facts")
            effect = matches[0]
            before, after = effect["before"], effect["after"]
            required = (
                f"n={before['n']} vs n={after['n']}",
                f"means={_text(before['mean'])} -> {_text(after['mean'])}",
                f"medians={_text(before['median'])} -> {_text(after['median'])}",
                f"p-value={_text(effect['p_value'])}",
                f"median change={_text(effect['median_change_percent'])}%",
            )
            if any(value not in line for value in required):
                raise ValueError("effect number differs from facts")


def facts_json(facts: dict[str, Any]) -> str:
    safe = dict(facts)
    safe.pop("actor_ids", None)
    return json.dumps(safe, indent=2, sort_keys=True) + "\n"

"""Inspect repository guidance from a committed Git tree."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


GUIDANCE_PREFIXES = (
    ".agents/skills/",
    ".claude/skills/",
    "okf/",
    "docs/adr/",
    "docs/ard/",
)
GUIDANCE_FILES = {"AGENTS.md", "CLAUDE.md", "service.yaml", "service.yml"}


@dataclass(frozen=True)
class Tree:
    files: tuple[str, ...]
    blob_shas: dict[str, str]
    blobs: dict[str, bytes]


def _is_skill(path: str) -> bool:
    return path.startswith(".agents/skills/") or path.startswith(".claude/skills/")


def inspect(tree: Tree, changed_paths: set[str]) -> dict:
    paths = set(tree.files)
    agents = "AGENTS.md" in paths
    claude = "CLAUDE.md" in paths
    service = "service.yaml" in paths or "service.yml" in paths
    okf = any(p == "okf" or p.startswith("okf/") for p in paths)
    adr = any(p.startswith("docs/adr/") or p.startswith("docs/ard/") for p in paths)
    api_schema = any(
        (p.startswith(("api/", "schemas/", "openapi/")) and p.lower().endswith((".json", ".yaml", ".yml")))
        or p.lower().endswith(("openapi.json", "openapi.yaml", "openapi.yml", "swagger.json", "swagger.yaml"))
        for p in paths
    )
    skills = sorted({p.split("/")[2] for p in paths if _is_skill(p) and len(p.split("/")) > 3})
    relevant = {
        p for p in changed_paths
        if p.endswith((".md", ".yaml", ".yml", ".json"))
        or p in GUIDANCE_FILES
        or p.startswith(GUIDANCE_PREFIXES)
    }
    digest_items = []
    digest_paths = [
        p for p in paths
        if p in GUIDANCE_FILES or p.startswith(GUIDANCE_PREFIXES)
    ]
    for path in sorted(digest_paths):
        digest_items.append(path.encode() + b"\0" + tree.blob_shas[path].encode())

    references: set[str] = set()
    if agents:
        text = tree.blobs["AGENTS.md"].decode("utf-8", errors="replace")
        for match in re.findall(r"\[[^\]]*\]\(([^)]+)\)|`([^`]+)`", text):
            candidate = next((value for value in match if value), "").split("#", 1)[0].strip()
            if candidate.startswith("./"):
                candidate = candidate[2:]
            if candidate and not candidate.startswith(("http://", "https://", "/")) and candidate in paths:
                references.add(candidate)

    return {
        "digest": hashlib.sha256(b"\n".join(digest_items)).hexdigest(),
        "file_count": len(digest_paths),
        "skills_available": skills,
        "agents_md_referenced_paths": sorted(references),
        "guidance_changed": bool(set(digest_paths) & changed_paths),
    }, {
        "agents_md_present": agents,
        "claude_md_present": claude,
        "service_yaml_present": service,
        "okf_present": okf,
        "adr_present": adr,
        "api_schema_present": api_schema,
        "skills_present": bool(skills),
        "readme_present": "README.md" in paths,
        "relevant_docs_changed": bool(relevant),
    }

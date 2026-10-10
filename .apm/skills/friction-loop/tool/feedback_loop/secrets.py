"""Conservative secret detection for serialized derived records."""

from __future__ import annotations

import re


PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?:sk-|ghp_|xox[baprs]-)[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\b(?:password|secret|token|api_key)\s*[:=]\s*[\"']?[^\s\"']{8,}"),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)


def find(text: str) -> str | None:
    for pattern in PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return None

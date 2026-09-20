"""Redact remote-operation diagnostics before durable or browser exposure."""

from __future__ import annotations

import re


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_VALUE_RE = re.compile(
    r"(?i)\b(authorization|cookie|token|api[_-]?key|secret|password)\b"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?\b")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\(?:[^\s<>:\"|?*]+\\)*[^\s<>:\"|?*]*")


def sanitize_remote_diagnostic(value: object, *, max_length: int = 2000) -> str:
    """Return one bounded, single-line diagnostic with common secrets removed."""
    text = str(value or "")
    text = _CONTROL_RE.sub(" ", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED]", text)
    text = _KEY_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _WINDOWS_PATH_RE.sub("[REDACTED_PATH]", text)
    text = " ".join(text.split())
    if max_length <= 0:
        return ""
    if len(text) > max_length:
        return text[: max(0, max_length - 3)].rstrip() + "..."
    return text

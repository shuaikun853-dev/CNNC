"""Redact remote-operation diagnostics before durable or browser exposure."""

from __future__ import annotations

import re


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SENSITIVE_KEY = (
    r"[A-Za-z0-9_-]*token|"
    r"[A-Za-z0-9_-]*secret|"
    r"api(?:_|-)?key|authorization|(?:set-)?cookie|password"
)
_QUOTED_KEY_VALUE_RE = re.compile(
    rf'(?i)(?:"(?P<double_key>{_SENSITIVE_KEY})"|\'(?P<single_key>{_SENSITIVE_KEY})\')'
    r"\s*[:=]\s*"
    r'(?:(?:"(?:\\.|[^"\\])*")|(?:\'(?:\\.|[^\'\\])*\')|'
    r"(?:Bearer\s+)?[^\s,;]+)"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(?<![\w\"'-])(?P<key>authorization)\s*[:=]\s*"
    r"(?:[A-Za-z][A-Za-z0-9_-]*\s+)?[^\s,;]+"
)
_COOKIE_RE = re.compile(
    r"(?i)(?<![\w\"'-])(?P<key>(?:set-)?cookie)\s*[:=]\s*"
    r"(?:[^;\s,]+=[^;\s,]+(?:\s*;\s*[^;\s,]+=[^;\s,]+)*|[^\s,;]+)"
)
_KEY_VALUE_RE = re.compile(
    rf"(?i)(?<![\w-])(?P<key>{_SENSITIVE_KEY})\b"
    r"\s*[:=]\s*(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?\b")
_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![\w])(?:[A-Z]:\\)(?:[^\\\r\n<>:\"|?*]+\\)+"
    r"[^\\\r\n<>:\"|?*]*?\.[A-Za-z0-9]{1,16}(?=$|[\s,;)\]])"
)
_WINDOWS_SIMPLE_PATH_RE = re.compile(r"(?i)(?<![\w])[A-Z]:\\[^\s,;\r\n)\]]+")
_UNIX_PRIVATE_PATH_RE = re.compile(r"(?<![\w])/(?:home|tmp)/[^\s,;]+")


def sanitize_remote_diagnostic(value: object, *, max_length: int = 2000) -> str:
    """Return one bounded, single-line diagnostic with common secrets removed."""
    try:
        text = str(value or "")
    except Exception:  # noqa: BLE001 - diagnostics must never break the caller
        text = "[UNPRINTABLE]"
    text = _QUOTED_KEY_VALUE_RE.sub(
        lambda match: f"{match.group('double_key') or match.group('single_key')}=[REDACTED]",
        text,
    )
    text = _AUTHORIZATION_RE.sub(lambda match: f"{match.group('key')}=[REDACTED]", text)
    text = _COOKIE_RE.sub(lambda match: f"{match.group('key')}=[REDACTED]", text)
    text = _CONTROL_RE.sub(" ", text)
    text = _KEY_VALUE_RE.sub(lambda match: f"{match.group('key')}=[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED]", text)
    text = _WINDOWS_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _WINDOWS_SIMPLE_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _UNIX_PRIVATE_PATH_RE.sub("[REDACTED_PATH]", text)
    text = " ".join(text.split())
    if max_length <= 0:
        return ""
    if len(text) > max_length:
        if max_length <= 3:
            return "." * max_length
        return text[: max_length - 3].rstrip() + "..."
    return text

"""Redact remote-operation diagnostics before durable or browser exposure."""

from __future__ import annotations

import re


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SENSITIVE_KEY = (
    r"access(?:_|-)?token|refresh(?:_|-)?token|client(?:_|-)?secret|"
    r"api(?:_|-)?key|authorization|cookie|password|secret|token"
)
_QUOTED_KEY_VALUE_RE = re.compile(
    rf"(?i)(?P<key_quote>[\"'])(?P<key>{_SENSITIVE_KEY})(?P=key_quote)"
    r"\s*[:=]\s*"
    r"(?:(?P<value_quote>[\"'])[^\r\n]*?(?P=value_quote)|"
    r"(?:Bearer\s+)?[^\s,;]+)"
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
_UNIX_PRIVATE_PATH_RE = re.compile(r"(?<![\w])/(?:home|tmp)/[^\s,;]+")


def sanitize_remote_diagnostic(value: object, *, max_length: int = 2000) -> str:
    """Return one bounded, single-line diagnostic with common secrets removed."""
    try:
        text = str(value or "")
    except Exception:  # noqa: BLE001 - diagnostics must never break the caller
        text = "[UNPRINTABLE]"
    text = _CONTROL_RE.sub(" ", text)
    text = _QUOTED_KEY_VALUE_RE.sub(
        lambda match: f"{match.group('key')}=[REDACTED]", text
    )
    text = _KEY_VALUE_RE.sub(lambda match: f"{match.group('key')}=[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED]", text)
    text = _WINDOWS_PATH_RE.sub("[REDACTED_PATH]", text)
    text = _UNIX_PRIVATE_PATH_RE.sub("[REDACTED_PATH]", text)
    text = " ".join(text.split())
    if max_length <= 0:
        return ""
    if len(text) > max_length:
        if max_length <= 3:
            return "." * max_length
        return text[: max_length - 3].rstrip() + "..."
    return text

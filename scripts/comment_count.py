from __future__ import annotations

import math
import re


_COMMENT_COUNT_RE = re.compile(r"\d+(?:\.\d+)?")
_EMPTY_COMMENT_VALUES = {"", "未识别", "-", "--", "N/A", "NA", "NONE", "NULL"}


def normalize_comment_count(value):
    """Return a non-negative numeric comment count, or None when unavailable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "").replace("+", "")
        if text.upper() in _EMPTY_COMMENT_VALUES:
            return None
        multiplier = 10000 if text.endswith("万") else 1
        if multiplier != 1:
            text = text[:-1].strip()
        if not _COMMENT_COUNT_RE.fullmatch(text):
            return None
        number = float(text) * multiplier
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if number.is_integer() else number

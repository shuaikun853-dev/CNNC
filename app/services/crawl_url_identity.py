"""Mall-scoped, conservative URL product-identity parsing (Issue #7, Level 3).

This module is deliberately tiny and side-effect free.  Given a mall key and a
URL it either confidently extracts the product identity embedded in that
mall's canonical product URL shape, or it returns ``None`` (a conservative
MISS).  It never guesses, never fuzzy-matches, and never treats a SKU id as an
identity key: only URLs whose path verifiably ends with a mall's product
pattern may contribute an identity.

Malls without a registered parser (everything except ``ofs`` in v1) always
resolve to ``None`` so callers stay fail-closed.
"""

from __future__ import annotations

import re
from typing import Callable, Mapping, Sequence
from urllib.parse import urlparse

SKU_STATUS_CONFIRMED = "CONFIRMED"
SKU_STATUS_URL_PARSED_UNVERIFIED = "URL_PARSED_UNVERIFIED"
SKU_STATUS_UNRESOLVED = "UNRESOLVED"
SKU_STATUSES = frozenset(
    {SKU_STATUS_CONFIRMED, SKU_STATUS_URL_PARSED_UNVERIFIED, SKU_STATUS_UNRESOLVED}
)

PLACEHOLDER_PREFIXES_DEFAULT = ("zhpq-",)

# Only a path ending in /product-<digits>.html (optionally one trailing slash,
# case-insensitive) counts as an ofs product URL.  The pattern is applied to
# the parsed URL *path* only - never to the whole URL string - so a disguised
# suffix inside a query string or fragment is a conservative MISS.  Bare
# numeric paths are also rejected.  The hostname must pass the ofs.cn allow
# policy (see ofs_product_identity).
_OFS_PRODUCT_PATTERN = re.compile(r"/product-([0-9]+)\.html/?\Z", re.IGNORECASE)


def is_placeholder_code(
    value: str, prefixes: Sequence[str] = PLACEHOLDER_PREFIXES_DEFAULT
) -> bool:
    """Return True when the stripped, lowercased value starts with a placeholder prefix."""
    if not isinstance(value, str):
        return False
    stripped = value.strip().lower()
    if not stripped:
        return False
    return any(
        stripped.startswith(str(prefix).strip().lower()) for prefix in prefixes
    )


def ofs_product_identity(url: str) -> str | None:
    """Return the numeric ofs product id when the URL path ends with ``/product-<digits>.html``.

    Hostname allow policy: only ``ofs.cn``, ``www.ofs.cn``, and any real
    subdomain ``*.ofs.cn`` count as ofs URLs; other hostnames (including
    lookalikes such as ``evilofs.cn`` or ``ofs.cn.evil.com``) are a
    conservative MISS.  Only the parsed URL *path* is matched - a
    ``/product-<digits>.html`` tail inside a query string or fragment never
    contributes an identity.

    Examples:
        ``https://www.ofs.cn/product-6287660.html`` -> ``"6287660"``
        ``https://www.ofs.cn/x/product-6287660.html`` -> ``"6287660"``
        ``https://www.ofs.cn/redirect?to=/product-1.html`` -> ``None``
        ``https://www.ofs.cn/other/6287660`` -> ``None`` (conservative MISS)
    """
    if not isinstance(url, str):
        return None
    stripped = url.strip()
    if not stripped:
        return None
    try:
        parsed = urlparse(stripped)
    except ValueError:  # pragma: no cover - malformed URL, conservative MISS
        return None
    # Hostname gate first: identity extraction is only trusted on real ofs
    # hosts; urlparse already returns the hostname lowercased.
    host = (parsed.hostname or "").strip().lower()
    if host != "ofs.cn" and not host.endswith(".ofs.cn"):
        return None
    match = _OFS_PRODUCT_PATTERN.search(parsed.path or "")
    if match is None:
        return None
    return match.group(1)


MALL_IDENTITY_PARSERS: Mapping[str, Callable[[str], str | None]] = {
    "ofs": ofs_product_identity,
}


def resolve_mall_product_identity(mall: str, url: str) -> str | None:
    """Resolve the product identity for one mall-scoped URL, or ``None`` on any doubt.

    The mall must be non-empty and registered in ``MALL_IDENTITY_PARSERS``;
    unregistered malls (jd, zkh, ...) always return ``None``.  Parser
    failures are swallowed into ``None`` so identity resolution stays
    conservative by construction.
    """
    if not isinstance(mall, str):
        return None
    mall_key = mall.strip()
    if not mall_key:
        return None
    parser = MALL_IDENTITY_PARSERS.get(mall_key)
    if parser is None:
        return None
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = parser(url.strip())
    except Exception:  # noqa: BLE001 - conservative MISS on any parser failure
        return None
    if parsed is None:
        return None
    identity = str(parsed).strip()
    return identity or None

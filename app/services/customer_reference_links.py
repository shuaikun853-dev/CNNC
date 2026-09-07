"""Pure extraction of customer reference links from a detail response (Issue #9).

Semantics are byte-identical to STEP3's inline extraction in
``scripts/process_monitor_no_push_zhaoshang.py`` (lines 365-377): the same
``URL_RE`` pattern, ``data.referenceInfos[].referenceLink`` first (list order),
top-level ``data.referenceLink`` afterwards, http(s) only, dedupe preserving
first-occurrence order.  Equivalence is proven test-side against a verbatim
copy of the STEP3 block (``tests/test_customer_reference_links.py``).

The module is pure: no I/O, no network, no store access, and it never raises
on malformed payloads - every malformed field is treated as empty.  One
deliberate deviation from the raw STEP3 block: a non-dict ``referenceInfos``
entry is skipped per item, whereas STEP3's surrounding broad ``except`` would
have discarded the whole document.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

URL_RE = re.compile(r"https?://[^\s,，;；\n\r\t]+")


def extract_reference_links(detail_payload: Mapping[str, Any] | None) -> list[str]:
    """Extract unique http(s) reference links from one detail JSON response.

    ``detail_payload`` is the FULL detail response payload (a mapping with a
    ``"data"`` key).  ``None`` or any non-mapping input yields ``[]``.
    """
    if not isinstance(detail_payload, Mapping):
        return []
    data = detail_payload.get("data", {})
    if not isinstance(data, dict):
        data = {}
    links: list[str] = []
    reference_infos = data.get("referenceInfos", [])
    if isinstance(reference_infos, list):
        for item in reference_infos:
            if not isinstance(item, dict):
                # 契约:非 dict 项视为空,跳过且永不抛出(STEP3 中此类输入
                # 会让整篇文档的提取失败)。
                continue
            links.extend(_http_links(item.get("referenceLink", "")))
    links.extend(_http_links(data.get("referenceLink", "")))
    seen: set[str] = set()
    unique: list[str] = []
    for url in links:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _http_links(value: Any) -> list[str]:
    """STEP3 逐字语义:``str(field or "")`` 后按 URL_RE 提取,仅保留 http 开头。"""
    return [url for url in URL_RE.findall(str(value or "")) if url.startswith("http")]

"""Unified routing classification for customer reference URLs (Issue #10).

One pure function, :func:`classify_reference_url`, decides what a customer
reference URL means for the prewarm/dispatch pipeline:

- ``STAPLES``: the URL lives on the Staples domain (``stbchina.cn``) - it is
  never a Zhaoshang fallback target (Issue #11 owns it); decided BEFORE any
  mall mapping so a Staples URL stays Staples even if some mapping ever
  claimed the host.
- ``MANUAL_SKIP``: ``detail.tmall.com`` - Tmall detail links are handled
  manually; no inference, no crawl.
- ``ZHAOSHANG``: the deterministic-rewrite-first host resolves through the
  loaded :class:`ZhaoshangMallMapping` list; ``mall_key`` carries the mapped
  mall and ``lookup_url`` the rewritten URL (``www.jslink.com`` ->
  ``b2b.nbdeli.com``), reusing exactly the :func:`route_url` transform of
  Issue #9 - the rewrite logic is never duplicated here.
- ``UNMAPPED``: everything else.

Malformed or empty input never raises and classifies as ``UNMAPPED`` with an
empty host, mirroring :func:`route_url`'s conservative behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlparse

from app.services.customer_prewarm_discovery import route_url
from app.integrations.zhaoshang_mall_mapping import ZhaoshangMallMapping

OUTCOME_ZHAOSHANG = "ZHAOSHANG"
OUTCOME_STAPLES = "STAPLES"
OUTCOME_MANUAL_SKIP = "MANUAL_SKIP"
OUTCOME_UNMAPPED = "UNMAPPED"

_STAPLES_DOMAIN = "stbchina.cn"
_MANUAL_SKIP_DOMAIN = "detail.tmall.com"


@dataclass(frozen=True)
class ReferenceRouting:
    """Routing outcome of one customer reference URL.

    ``outcome`` is one of the ``OUTCOME_*`` constants; ``mall_key`` is the
    mapped mall for ``ZHAOSHANG`` and ``""`` otherwise; ``lookup_url`` is the
    deterministic-rewrite URL the Shared Crawl Cache should be queried with;
    ``host`` is the lowercase host of that rewritten URL (``""`` when
    unparseable).
    """

    outcome: str
    mall_key: str
    lookup_url: str
    host: str


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def classify_reference_url(
    url: str, mappings: Sequence[ZhaoshangMallMapping]
) -> ReferenceRouting:
    """Classify one customer reference URL (pure, never raises).

    Rules are applied in order to the deterministic-rewrite URL: Staples
    first, then the manual-skip Tmall detail host, then the mall mapping
    resolution via :func:`route_url`, else ``UNMAPPED``.
    """
    routed = route_url(url, mappings)
    lookup_url = routed.lookup_url
    host = ""
    try:
        host = (urlparse(lookup_url).hostname or "").lower().strip(".")
    except ValueError:  # malformed URL (e.g. bad IPv6 literal) - conservative unmapped
        host = ""
    if _host_matches(host, _STAPLES_DOMAIN):
        return ReferenceRouting(
            outcome=OUTCOME_STAPLES, mall_key="", lookup_url=lookup_url, host=host
        )
    if _host_matches(host, _MANUAL_SKIP_DOMAIN):
        return ReferenceRouting(
            outcome=OUTCOME_MANUAL_SKIP, mall_key="", lookup_url=lookup_url, host=host
        )
    if routed.mall:
        return ReferenceRouting(
            outcome=OUTCOME_ZHAOSHANG, mall_key=routed.mall, lookup_url=lookup_url, host=host
        )
    return ReferenceRouting(
        outcome=OUTCOME_UNMAPPED, mall_key="", lookup_url=lookup_url, host=host
    )

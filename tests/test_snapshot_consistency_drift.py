"""Snapshot pagination drift must void the round, not persist a mixed view.

count-change / empty-page / overrun / underrun during one pagination round
mean the authoritative source moved mid-fetch.  The whole round is discarded,
fetching restarts from page 1, at most three rounds share the original
absolute deadline, and persistent drift ends in a typed failure.
"""

import json

import pytest

from app.services.cnnc_saas_snapshot_adapter import (
    CnncSaasSnapshotAdapter,
    SnapshotFetchExhausted,
    SnapshotFetchPolicy,
    SnapshotConsistencyDrift,
)

from tests.test_cnnc_saas_snapshot_adapter import (
    _adapter,
    _customer_config,
    _ok,
    _saas_config,
)

pytestmark = pytest.mark.critical_safety


def _customer_ok(count=1):
    return _ok([{"documentId": f"c{index}"} for index in range(count)], count)


def _request_pages(opener):
    return [json.loads(request.data.decode("utf-8"))["page"] for request in opener.requests]


def test_count_drift_voids_round_and_refetches_from_page_one():
    rows = [{"vCommodityId": "a"}, {"vCommodityId": "b"}, {"vCommodityId": "c"}]
    adapter, opener = _adapter(
        [
            _ok(rows[:2], 3),        # round 1 page 1
            _ok(rows[2:], 4),        # round 1 page 2: count changed -> drift
            _ok(rows, 3),            # round 2 page 1: stable
            _customer_ok(),
        ],
        policy=SnapshotFetchPolicy(page_size=2),
    )

    snapshot = adapter.fetch()

    assert list(snapshot.saas_rows) == rows
    assert snapshot.saas_count == 3
    # The saas side restarted at page 1 after the drift, then the customer
    # side ran its own page 1.
    assert _request_pages(opener) == [1, 2, 1, 1]


def test_drift_discards_first_round_partial_rows():
    adapter, opener = _adapter(
        [
            _ok([{"vCommodityId": "stale-1"}], 2),   # round 1 page 1 (partial)
            _ok([], 2),                              # round 1 page 2: empty before count
            _ok([{"vCommodityId": "y1"}, {"vCommodityId": "y2"}], 2),  # round 2 full
            _customer_ok(),
        ],
        policy=SnapshotFetchPolicy(page_size=2),
    )

    snapshot = adapter.fetch()

    assert [row["vCommodityId"] for row in snapshot.saas_rows] == ["y1", "y2"]
    saas_evidence = [page for page in snapshot.page_evidence if page.side == "saas"]
    assert [(page.page, page.row_count) for page in saas_evidence] == [(1, 2)]


def test_continuous_drift_until_deadline_raises_exhausted():
    ticks = iter(float(value) for value in range(0, 1000, 5))
    adapter, opener = _adapter(
        # Every round: page 1 promises 2 rows, page 2 is empty -> drift.
        [_ok([{"vCommodityId": "a"}], 2), _ok([], 2)] * 6,
        policy=SnapshotFetchPolicy(page_size=1, max_elapsed_seconds=12),
        monotonic=lambda: next(ticks),
    )

    with pytest.raises(SnapshotFetchExhausted):
        adapter.fetch()


def test_three_drifting_rounds_raise_typed_drift_error():
    adapter, opener = _adapter(
        # Enough drift rounds to exceed the limit; monotonic frozen so the
        # absolute deadline never rescues the fetch.
        [_ok([{"vCommodityId": "a"}], 2), _ok([], 2)] * 4,
        policy=SnapshotFetchPolicy(page_size=1, max_elapsed_seconds=180),
        monotonic=lambda: 0.0,
    )

    with pytest.raises(SnapshotConsistencyDrift) as caught:
        adapter.fetch()

    assert caught.value.side == "saas"
    # Exactly three full rounds were attempted, then the typed error surfaced.
    assert _request_pages(opener) == [1, 2, 1, 2, 1, 2]

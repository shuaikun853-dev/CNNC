"""A nearly exhausted work budget must not shorten the lease protecting settlement."""
from datetime import UTC, datetime
from app.services.daily_audit_store import DailyAuditStore, ProcessLock
from app.workflows.incremental_price_audit import DiscoveredState, TickConfig, TickDependencies, run_incremental_tick


def test_tick_renewal_preserves_configured_lease_window(tmp_path, monkeypatch):
    store = DailyAuditStore(tmp_path / 'state.sqlite3')
    renewals = []
    original = store.renew_lease
    def record(token, ttl_seconds):
        before = store.get_run(token.business_date)['lease_expires_at']
        value = original(token, ttl_seconds)
        after = store.get_run(token.business_date)['lease_expires_at']
        renewals.append((before, after, ttl_seconds))
        return value
    monkeypatch.setattr(store, 'renew_lease', record)
    rows = [{'vCommodityId':'doc-1', 'skuId':'sku-1', 'queryStr':'test', 'skuName':'test', '__crawl_required':False}]
    deps = TickDependencies(
        discover=lambda timeout: DiscoveredState(datetime(2026,9,22,1,tzinfo=UTC), ['vCommodityId'], rows),
        detail=lambda row, timeout: {'customerPrice':'10', 'customerPriceStatus':'FOUND'},
        model=lambda row, timeout: {'match_result':'SAME'},
        monotonic=lambda: 0.0,
        wall_clock=lambda: datetime(2026,9,22,1,tzinfo=UTC))
    config = TickConfig(business_date='2026-09-22', runtime_budget_seconds=1.7,
                        heavy_work_reserve_seconds=0.5, lease_ttl_seconds=60)
    run_incremental_tick(store, ProcessLock(tmp_path/'tick.lock'), config, deps)
    assert renewals, 'Test must observe a real renewal'
    assert all(after >= before for before, after, _ in renewals), renewals

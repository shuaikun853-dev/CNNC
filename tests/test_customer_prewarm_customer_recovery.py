"""Issue #9 Review Round 3 Finding A: customer-only scanner recovery.

覆盖:
- ``CnncSaasSnapshotAdapter.fetch_customer_documents`` 只读客户侧的新增 seam
  (复用 fetch 的冻结 scope 校验与客户侧分页/重试/漂移机制;SaaS 端点一次都
  不请求,saas_rows 恒为空)。
- ``prewarm_rows_from_customer_rows`` 原始客户行 → prewarm 行整形
  (documentId first-wins 去重、缺字段容忍、空 id 跳过、永不抛错)。
- ``CustomerPrewarmStore`` 新增 ``prewarm_recovery_runs`` 节流表及其读写方法。
- ``recover_customer_documents`` 节流编排(customer_fetch 异常向上传播)。
- ``run_prewarm_scan`` Phase B2:事件侧 intake 完全失败时,纯客户侧恢复仍能
  把 customer-only 文档(以及事件侧已知文档)带进 prewarm store,且对
  state.sqlite3 严格只读;间隔节流与幂等重放。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib import error

import pytest

from app.services.cnnc_saas_snapshot_adapter import (
    CnncSaasSnapshotAdapter,
    SnapshotConsistencyDrift,
    SnapshotFetchPolicy,
)
from app.services.customer_prewarm_discovery import (
    prewarm_rows_from_customer_rows,
    recover_customer_documents,
    run_prewarm_scan,
)
from app.services.customer_prewarm_store import CustomerPrewarmStore
from app.services.shared_crawl_cache import SharedCrawlCache
from tests.test_customer_prewarm_discovery import _insert_audit_items

BUSINESS_DATE = "2026-09-07"


def _never_detail_fetch(_document_id: str, _four_category_id: str) -> dict[str, object]:
    raise AssertionError("detail scan must not run in recovery tests")


# ---- 通用测试替身(镜像 tests/test_cnnc_saas_snapshot_adapter.py) --------------------


class _Headers:
    def get_content_charset(self):
        return "utf-8"


class _Response:
    headers = _Headers()

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Opener:
    def __init__(self, events):
        self.events = list(events)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return _Response(event)


def _config(url, body):
    return {
        "url": url,
        "method": "POST",
        "headers": [],
        "cookie": "",
        "data": json.dumps(body),
    }


def _saas_config(**overrides):
    return _config(
        "https://saasportal.iio.com/improxy/saas/iio2/sameAudit/queryAll",
        {"tenantId": "188605", "commodityPoolId": "222", **overrides},
    )


def _customer_config(**overrides):
    return _config(
        "https://one.cnncecp.com/emp/onecode/improxy/iio2/quasiStandardCommodityDocumentModel/query",
        {"sassCheckStatuss": ["0"], **overrides},
    )


def _adapter(events, **kwargs):
    opener = _Opener(events)
    return CnncSaasSnapshotAdapter(
        _saas_config(),
        _customer_config(),
        opener=opener,
        monotonic=kwargs.pop("monotonic", lambda: 0.0),
        sleep=kwargs.pop("sleep", lambda _seconds: None),
        **kwargs,
    ), opener


def _ok(rows, count):
    return {"rspCode": "0000", "count": count, "rows": rows}


# ---- Finding A: adapter seam ---------------------------------------------------


def test_fetch_customer_documents_reads_customer_pages_and_never_saas():
    customer_rows = [
        {"documentId": "c1", "fourCategoryId": "k1"},
        {"documentId": "c2"},
        {"documentId": "c3"},
    ]
    adapter, opener = _adapter(
        [_ok(customer_rows[:2], 3), _ok(customer_rows[2:], 3)],
        policy=SnapshotFetchPolicy(page_size=2),
    )

    snapshot = adapter.fetch_customer_documents()

    assert list(snapshot.customer_rows) == customer_rows
    assert snapshot.saas_rows == ()
    assert snapshot.saas_count == 0
    assert snapshot.customer_count == 3
    assert [(page.side, page.page, page.row_count, page.count) for page in snapshot.page_evidence] == [
        ("customer", 1, 2, 3),
        ("customer", 2, 1, 3),
    ]
    # SaaS 端点一次都不请求;客户分页 page=1,2
    assert [str(request.full_url) for request in opener.requests]
    for request in opener.requests:
        assert "sameAudit/queryAll" not in str(request.full_url)
    assert [json.loads(request.data.decode("utf-8"))["page"] for request in opener.requests] == [1, 2]


def test_fetch_customer_documents_scope_validation_fails_before_network():
    opener = _Opener([])
    adapter = CnncSaasSnapshotAdapter(
        _saas_config(tenantId="other"),
        _customer_config(),
        opener=opener,
    )

    with pytest.raises(ValueError, match="tenant"):
        adapter.fetch_customer_documents()

    assert opener.requests == []


def test_fetch_customer_documents_rejects_invalid_deadline():
    adapter, opener = _adapter([])

    with pytest.raises(ValueError, match="max_elapsed_seconds"):
        adapter.fetch_customer_documents(max_elapsed_seconds=0)
    with pytest.raises(ValueError, match="max_elapsed_seconds"):
        adapter.fetch_customer_documents(max_elapsed_seconds=-1)
    with pytest.raises(ValueError, match="max_elapsed_seconds"):
        adapter.fetch_customer_documents(max_elapsed_seconds="boom")

    assert opener.requests == []


def test_fetch_customer_documents_reuses_drift_machinery():
    # count 逐轮变化 → 复用 fetch 的漂移机制,整轮作废并最终类型化失败
    events = [_ok([{"documentId": "a"}], 2), _ok([{"documentId": "b"}], 3)] * 3
    adapter, opener = _adapter(events, policy=SnapshotFetchPolicy(page_size=1))

    with pytest.raises(SnapshotConsistencyDrift, match="count changed"):
        adapter.fetch_customer_documents()

    assert all("quasiStandardCommodityDocumentModel/query" in str(r.full_url) for r in opener.requests)


def test_fetch_customer_documents_retryable_failure_retries_on_customer_side():
    adapter, opener = _adapter(
        [
            error.URLError("temporary"),
            _ok([{"documentId": "c1"}], 1),
        ],
    )

    snapshot = adapter.fetch_customer_documents()

    assert snapshot.customer_count == 1
    assert len(opener.requests) == 2


# ---- Finding A: 行整形 ----------------------------------------------------------


def test_prewarm_rows_from_customer_rows_dedupes_first_wins_never_raises():
    rows = [
        {"documentId": "DOC-A", "fourCategoryId": "CAT-1"},
        {"documentId": "DOC-A", "fourCategoryId": "CAT-2"},  # 重复:first-wins,不抛错
        {
            "documentId": "DOC-B",
            "fourCategoryId": "CAT-B",
            "fourCategoryName": "N",
            "commodityName": "M",
            "referenceLink": "https://x",
        },
        {"documentId": "  "},  # 空白 id 跳过
        {"documentId": None},
        {"fourCategoryId": "CAT-9"},  # 缺 id 跳过
        "not-a-mapping",
    ]

    shaped = prewarm_rows_from_customer_rows(rows)

    assert shaped == [
        {
            "documentId": "DOC-A",
            "fourCategoryId": "CAT-1",
            "fourCategoryName": "",
            "commodityName": "",
            "referenceLink": "",
        },
        {
            "documentId": "DOC-B",
            "fourCategoryId": "CAT-B",
            "fourCategoryName": "N",
            "commodityName": "M",
            "referenceLink": "https://x",
        },
    ]


def test_prewarm_rows_from_customer_rows_empty_input():
    assert prewarm_rows_from_customer_rows([]) == []


# ---- Finding A: 节流表 ------------------------------------------------------------


def test_recovery_runs_table_records_and_reads_max_ran_at(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    assert store.last_recovery_run_at() is None

    store.record_recovery_run(BUSINESS_DATE, 3)
    first = store.last_recovery_run_at()
    assert first is not None
    store.record_recovery_run("2026-09-08", 0)
    assert store.last_recovery_run_at() >= first

    with store._reader() as connection:
        rows = connection.execute(
            "SELECT business_date, ran_at, documents_seen FROM prewarm_recovery_runs ORDER BY id"
        ).fetchall()
    assert [(row["business_date"], row["documents_seen"]) for row in rows] == [
        (BUSINESS_DATE, 3),
        ("2026-09-08", 0),
    ]
    assert rows[0]["ran_at"] == first


def test_recovery_runs_table_created_on_preexisting_database(tmp_path):
    """CREATE TABLE IF NOT EXISTS:旧版(Round-1)库打开即补表,无需迁移。"""
    path = tmp_path / "pre-existing.sqlite3"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            """CREATE TABLE prewarm_documents (
                   id INTEGER PRIMARY KEY,
                   document_id TEXT NOT NULL UNIQUE,
                   business_date TEXT NOT NULL,
                   four_category_id TEXT NOT NULL DEFAULT '',
                   source TEXT NOT NULL DEFAULT 'event',
                   detail_state TEXT NOT NULL DEFAULT 'PENDING',
                   detail_attempts INTEGER NOT NULL DEFAULT 0,
                   first_seen_at TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL
               )"""
        )
        connection.commit()
    finally:
        connection.close()

    store = CustomerPrewarmStore(path)

    assert "prewarm_recovery_runs" in store.table_names()
    store.record_recovery_run(BUSINESS_DATE, 1)
    assert store.last_recovery_run_at() is not None


# ---- Finding A: recover_customer_documents 节流编排 -------------------------------


def _set_recovery_runs_ran_at(store, ran_at):
    """把 run 记录对齐到注入时钟(store 落库用真实墙钟,节流比较读 max(ran_at);
    与 tests/test_customer_prewarm_daily_sync._backdate_ledger_claims 同一做法)。"""
    connection = sqlite3.connect(str(store.path), isolation_level=None)
    try:
        connection.execute("UPDATE prewarm_recovery_runs SET ran_at=?", (ran_at,))
    finally:
        connection.close()


def test_recover_customer_documents_throttles_then_runs_at_zero_interval(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    calls = {"n": 0}

    def customer_fetch():
        calls["n"] += 1
        return [{"documentId": "DOC-1", "fourCategoryId": "C1"}]

    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    first = recover_customer_documents(
        store,
        customer_fetch=customer_fetch,
        business_date=BUSINESS_DATE,
        interval_seconds=300.0,
        now=now,
    )
    assert first == {"throttled": False, "documents_intaked": 1, "documents_seen": 1}
    assert calls["n"] == 1
    _set_recovery_runs_ran_at(store, now.isoformat())

    # 间隔内立即重跑 → 节流,不 fetch、不新增 run 记录
    second = recover_customer_documents(
        store,
        customer_fetch=customer_fetch,
        business_date=BUSINESS_DATE,
        interval_seconds=300.0,
        now=now,
    )
    assert second == {"throttled": True, "documents_intaked": 0}
    assert calls["n"] == 1
    with store._reader() as connection:
        run_count = int(connection.execute("SELECT COUNT(*) FROM prewarm_recovery_runs").fetchone()[0])
    assert run_count == 1

    # interval 0 → 每轮都跑
    third = recover_customer_documents(
        store,
        customer_fetch=customer_fetch,
        business_date=BUSINESS_DATE,
        interval_seconds=0.0,
        now=now,
    )
    assert third["throttled"] is False
    assert third["documents_intaked"] == 1
    assert calls["n"] == 2

    # 间隔过后 → 恢复执行
    _set_recovery_runs_ran_at(store, now.isoformat())
    later = recover_customer_documents(
        store,
        customer_fetch=customer_fetch,
        business_date=BUSINESS_DATE,
        interval_seconds=300.0,
        now=datetime(2026, 9, 7, 12, 5, 1, tzinfo=UTC),
    )
    assert later["throttled"] is False
    assert calls["n"] == 3


def test_recover_customer_documents_failure_is_throttled_then_retries(tmp_path):
    """失败的 customer recovery 也记一次尝试，避免每分钟重复触发浏览器认证。"""
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    calls = {"n": 0}
    now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)

    def customer_fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("customer auth refresh timed out")
        return [{"documentId": "DOC-RECOVERED", "fourCategoryId": "CAT-1"}]

    with pytest.raises(RuntimeError, match="auth refresh timed out"):
        recover_customer_documents(
            store,
            customer_fetch=customer_fetch,
            business_date=BUSINESS_DATE,
            interval_seconds=300.0,
            now=now,
        )

    with store._reader() as connection:
        failed_run = connection.execute(
            "SELECT documents_seen FROM prewarm_recovery_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert failed_run is not None
    assert failed_run["documents_seen"] == -1
    _set_recovery_runs_ran_at(store, now.isoformat())

    throttled = recover_customer_documents(
        store,
        customer_fetch=customer_fetch,
        business_date=BUSINESS_DATE,
        interval_seconds=300.0,
        now=datetime(2026, 9, 7, 12, 1, 0, tzinfo=UTC),
    )
    assert throttled == {"throttled": True, "documents_intaked": 0}
    assert calls["n"] == 1

    retried = recover_customer_documents(
        store,
        customer_fetch=customer_fetch,
        business_date=BUSINESS_DATE,
        interval_seconds=300.0,
        now=datetime(2026, 9, 7, 12, 5, 1, tzinfo=UTC),
    )
    assert retried["throttled"] is False
    assert retried["documents_intaked"] == 1
    assert calls["n"] == 2


def test_recover_customer_documents_intakes_as_scanner_recovery(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")

    def customer_fetch():
        return [
            {"documentId": "DOC-1", "fourCategoryId": "C1"},
            {"documentId": "DOC-1", "fourCategoryId": "C1-DUP"},
            {"documentId": "DOC-2", "fourCategoryId": "C2"},
        ]

    outcome = recover_customer_documents(
        store,
        customer_fetch=customer_fetch,
        business_date=BUSINESS_DATE,
        interval_seconds=300.0,
    )
    assert outcome["documents_intaked"] == 2
    assert outcome["documents_seen"] == 3
    sources = {
        row["document_id"]: row["source"]
        for row in store.pending_documents(10)
    }
    assert sources == {"DOC-1": "scanner-recovery", "DOC-2": "scanner-recovery"}


def test_recover_customer_documents_fetch_exception_propagates_and_records_failed_attempt(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")

    def customer_fetch():
        raise RuntimeError("customer endpoint down")

    with pytest.raises(RuntimeError, match="customer endpoint down"):
        recover_customer_documents(
            store,
            customer_fetch=customer_fetch,
            business_date=BUSINESS_DATE,
            interval_seconds=300.0,
        )
    # 异常仍向上传播，但失败尝试进入节流账本；不伪造任何业务文档。
    assert store.last_recovery_run_at() is not None
    with store._reader() as connection:
        row = connection.execute(
            "SELECT documents_seen FROM prewarm_recovery_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["documents_seen"] == -1
    assert store.pending_documents(10) == []


# ---- Finding A: Phase B2 场景(指定验收场景) ----------------------------------------


def test_finding_a_customer_only_recovered_when_event_intake_failed(tmp_path):
    """事件 intake 完全失败(prewarm store 从空开始),state 库 audit_items 只含
    DOC-A;DOC-B 只存在于客户源。恢复相位把两者带进 prewarm store,同时对
    state.sqlite3 严格只读(hash 不变、audit_items 无 DOC-B);interval 0 重放
    幂等(文档数不膨胀,run 记录每次新增)。"""
    from tests.test_customer_prewarm_daily_sync import make_state_db

    state_db_path = make_state_db(tmp_path, BUSINESS_DATE, rows=[])
    _insert_audit_items(
        state_db_path, BUSINESS_DATE, [{"documentId": "DOC-A", "fourCategoryId": "CAT-A"}]
    )
    state_path = Path(state_db_path)
    state_hash_before = hashlib.sha256(state_path.read_bytes()).hexdigest()

    def audit_item_document_ids():
        connection = sqlite3.connect(str(state_db_path))
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute("SELECT row_json FROM audit_items").fetchall()
        finally:
            connection.close()
        return [json.loads(row["row_json"]).get("documentId") for row in rows]

    assert audit_item_document_ids() == ["DOC-A"]  # DOC-B 不在事件侧

    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    shared_cache = SharedCrawlCache(tmp_path / "shared_cache.sqlite3")
    fetch_calls = {"n": 0}

    def customer_fetch():
        fetch_calls["n"] += 1
        return [
            {"documentId": "DOC-A", "fourCategoryId": "CAT-A"},
            {"documentId": "DOC-B", "fourCategoryId": "CAT-B"},
        ]

    first = run_prewarm_scan(
        prewarm_store=store,
        shared_cache=shared_cache,
        business_date=BUSINESS_DATE,
        state_db_path=state_db_path,
        mappings=[],
        detail_fetch=_never_detail_fetch,
        max_documents=0,
        customer_recovery_fetch=customer_fetch,
        customer_recovery_interval_seconds=300.0,
    )
    assert first["phase_errors"] == []
    assert first["customer_recovery_intaked"] == 2
    assert first["customer_recovery_throttled"] == 0
    # (1) 事件侧严格只读:audit_items 仍无 DOC-B,状态库文件 hash 不变
    assert audit_item_document_ids() == ["DOC-A"]
    assert hashlib.sha256(state_path.read_bytes()).hexdigest() == state_hash_before
    # (2) prewarm store 同时有 DOC-A 与 DOC-B
    assert store.stats()["documents"] == {"PENDING": 2}
    assert {row["document_id"] for row in store.pending_documents(10)} == {"DOC-A", "DOC-B"}
    assert store.stats()["links"] == 0
    assert store.stats()["candidates"] == {}

    # (3) interval 0 立即重放:再次记账、无重复
    second = run_prewarm_scan(
        prewarm_store=store,
        shared_cache=shared_cache,
        business_date=BUSINESS_DATE,
        state_db_path=state_db_path,
        mappings=[],
        detail_fetch=_never_detail_fetch,
        max_documents=0,
        customer_recovery_fetch=customer_fetch,
        customer_recovery_interval_seconds=0.0,
    )
    assert second["phase_errors"] == []
    assert second["customer_recovery_intaked"] == 2
    assert second["customer_recovery_throttled"] == 0
    assert fetch_calls["n"] == 2
    assert store.stats()["documents"] == {"PENDING": 2}
    assert store.stats()["links"] == 0
    assert store.stats()["candidates"] == {}
    with store._reader() as connection:
        runs = connection.execute(
            "SELECT documents_seen FROM prewarm_recovery_runs ORDER BY id"
        ).fetchall()
    assert [int(row["documents_seen"]) for row in runs] == [2]


# ---- Finding A: run_prewarm_scan Phase B2 编排 ---------------------------------------


def _scan_state_db(tmp_path, audit_items):
    from tests.test_customer_prewarm_daily_sync import make_state_db

    state_db_path = make_state_db(tmp_path, BUSINESS_DATE, rows=[])
    _insert_audit_items(state_db_path, BUSINESS_DATE, audit_items)
    return state_db_path


def _run_scan(store, shared_cache, state_db_path, *, customer_recovery_fetch=None,
              customer_recovery_interval_seconds=300.0):
    return run_prewarm_scan(
        prewarm_store=store,
        shared_cache=shared_cache,
        business_date=BUSINESS_DATE,
        state_db_path=state_db_path,
        mappings=[],
        detail_fetch=_never_detail_fetch,
        max_documents=0,
        customer_recovery_fetch=customer_recovery_fetch,
        customer_recovery_interval_seconds=customer_recovery_interval_seconds,
    )


def test_run_prewarm_scan_customer_recovery_error_isolated(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    shared_cache = SharedCrawlCache(tmp_path / "shared_cache.sqlite3")
    state_db_path = _scan_state_db(tmp_path, [{"documentId": "DOC-A", "fourCategoryId": "CAT-A"}])

    def raising_fetch():
        raise RuntimeError("customer endpoint down")

    summary = _run_scan(store, shared_cache, state_db_path, customer_recovery_fetch=raising_fetch)

    phases = {entry["phase"] for entry in summary["phase_errors"]}
    assert "customer_recovery" in phases
    assert any("customer endpoint down" in entry["error"] for entry in summary["phase_errors"])
    assert summary["customer_recovery_intaked"] == 0
    assert summary["customer_recovery_throttled"] == 0
    # 相位隔离:后续统计照常产出
    assert "stats" in summary


def test_run_prewarm_scan_without_recovery_fetch_defaults_off(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    shared_cache = SharedCrawlCache(tmp_path / "shared_cache.sqlite3")
    state_db_path = _scan_state_db(tmp_path, [{"documentId": "DOC-A", "fourCategoryId": "CAT-A"}])

    summary = _run_scan(store, shared_cache, state_db_path)

    assert summary["phase_errors"] == []
    assert summary["customer_recovery_intaked"] == 0
    assert summary["customer_recovery_throttled"] == 0


def test_run_prewarm_scan_customer_recovery_throttles_across_passes(tmp_path):
    store = CustomerPrewarmStore(tmp_path / "prewarm.sqlite3")
    shared_cache = SharedCrawlCache(tmp_path / "shared_cache.sqlite3")
    state_db_path = _scan_state_db(tmp_path, [{"documentId": "DOC-A", "fourCategoryId": "CAT-A"}])
    calls = {"n": 0}

    def customer_fetch():
        calls["n"] += 1
        return [{"documentId": "DOC-A", "fourCategoryId": "CAT-A"}]

    first = _run_scan(store, shared_cache, state_db_path, customer_recovery_fetch=customer_fetch)
    assert first["customer_recovery_intaked"] == 1
    assert first["customer_recovery_throttled"] == 0

    second = _run_scan(store, shared_cache, state_db_path, customer_recovery_fetch=customer_fetch)
    assert second["customer_recovery_throttled"] == 1
    assert second["customer_recovery_intaked"] == 0
    assert calls["n"] == 1  # 节流:第二次不再 fetch

    third = _run_scan(
        store,
        shared_cache,
        state_db_path,
        customer_recovery_fetch=customer_fetch,
        customer_recovery_interval_seconds=0.0,
    )
    assert third["customer_recovery_throttled"] == 0
    assert third["customer_recovery_intaked"] == 1
    assert calls["n"] == 2


# ---- Finding A: CLI 接线 ----------------------------------------------------------


def _run_scan_cli(runtime_root, *extra):
    from scripts import customer_prewarm_scan as cli

    return cli.main(
        ["--business-date", BUSINESS_DATE, "--runtime-root", str(runtime_root), *extra]
    )


def test_scan_cli_warns_and_runs_without_recovery_when_templates_missing(
    tmp_path, capsys, monkeypatch
):
    from scripts import customer_prewarm_scan as cli

    # 空 data 根:snapshot/detail/recovery 模板全部缺失 → 降级不致命
    monkeypatch.setattr(cli, "PROJECT_DATA_ROOT", tmp_path / "empty-data")
    runtime_root = tmp_path / "runtime"

    exit_code = _run_scan_cli(runtime_root, "--skip-detail")

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "WARNING customer recovery curl templates missing" in out
    assert out.count("WARNING customer recovery curl templates missing") == 1


def test_scan_cli_rejects_negative_recovery_interval(tmp_path, capsys):
    from scripts import customer_prewarm_scan as cli

    exit_code = cli.main(
        [
            "--business-date",
            BUSINESS_DATE,
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--customer-recovery-interval-seconds",
            "-1",
        ]
    )
    assert exit_code == 2
    assert "--customer-recovery-interval-seconds" in capsys.readouterr().out

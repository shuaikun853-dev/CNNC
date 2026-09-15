"""跨日未完成任务的 lifecycle continuation 测试(Issue #13 跨日 blocker B)。

D2 手动重爬 REUSED 跨日 D1 任务(dispatch 返回 cross_day=True +
job_business_date=D1)后,动作 worker 即 lifecycle owner:有界现有生命周期
harvest(run_manual_reexport:只读 preflight + 共享 harvest core,create_calls=0,
绝不二次 POST,只写 D1 自己的 state DB 并持 D1 日锁+租约)+
sync_daily_internet_results(幂等 ledger)镜像进共享缓存 → 既有 G2 精确关联
完成。预算耗尽保持 WAITING_REMOTE(诚实),操作员再次点击会重新探测
(无重复 POST)并重启该有界循环。

全程零真实网络/子进程:D1 远端任务用真实 DailyAuditStore 造库(fixture 形状
抄 tests/test_manual_reexport.py 的 _Seed:run+snapshot+freeze+crawl work+
remote job,BATCH_STATE.md 写 CREATED/ENABLED/PRIORITY_UPDATED 事件,招商局
adapter 注入 fake runner + 可控 fake progress_probe,mall 用 comix);dispatch
注入 fake 或真实 dispatch 薄包装(counting adapter);reexport 默认走真实
run_manual_reexport,个别测试注入记录型 fake。远端状态库最小造库方式镜像
tests/test_prewarm_manual_recrawl.py 的 _write_state_db。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from openpyxl import Workbook

from app.services.customer_prewarm_dispatch import (
    dispatch_single_candidate_exact,
    prewarm_placeholder_sku,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_runtime import runtime_paths
from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    CrawlRequest,
    LegacyPriorityOutcome,
    LegacySubmitOutcome,
    PriorityStatus,
    SubmitStatus,
)
from app.services.prewarm_manual_recrawl import ManualRecrawlRunner
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts.staples_prewarm_run import PREWARM_STORE_NAME, SHARED_CACHE_NAME
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

BUSINESS_DATE = "2026-09-14"  # 动作日 D2
DAY_1 = "2026-09-13"  # 远端任务所属业务日 D1
TASK_ID = "1246803"
TASK_NUMBER = "RW-20260913160830"
IDENTITY = f"comix|{TASK_ID}|{TASK_NUMBER}"
URL = "https://qx.com/cross-day-lifecycle.html"
BACKEND_VERSION = "legacy-v1"

COMIX = ZhaoshangMallMapping(
    mall_name="齐心商城",
    source_id="SRC-COMIX",
    mall_key="comix",
    domains=("qx.com", "www.qx.com"),
    vendor_keywords=("comix",),
)


# ---- fixture helpers ----------------------------------------------------------


def _write_result(path: Path, rows, *, task_name: str, mall_key: str):
    """结果工作簿(表头照抄 tests/test_manual_reexport.py 的 _write_result)。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "商品编码",
        "商品最小品类单元id",
        "原始链接",
        "商品价格",
        "执行编号",
        "批次号",
        "商城标识",
    ])
    for index, row in enumerate(rows):
        sheet.append([*row, f"{TASK_NUMBER}-exec-{index}", task_name, mall_key])
    workbook.save(path)


class _JobSeed:
    """D1 库 Seed 式造库(形状抄 tests/test_manual_reexport.py 的 _Seed):
    真实 DailyAuditStore run+snapshot+freeze+1 行 crawl work+remote job
    (prepare_remote_intent→claim_remote_submission→reconcile+identity),
    BATCH_STATE.md 写入 CREATED/ENABLED/PRIORITY_UPDATED 三事件;adapter 用
    真实 CnncLegacyZhaoshangAdapter + 注入 fake runner/可控 progress_probe。"""

    def __init__(self, runtime_root: Path, url: str, *, sku_id: str = "sku-0"):
        self.url = url
        self.sku_id = sku_id
        self.run_dir = runtime_root / DAY_1
        self.state_path = self.run_dir / "state.sqlite3"
        self.lock_path = self.run_dir / "incremental_price_audit.lock"
        self.batch_state = self.run_dir / "BATCH_STATE.md"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.batch_state.write_text("# state\n", encoding="utf-8")
        self.runner_calls: list[list[str]] = []
        self.probe_calls: list[tuple[str, str]] = []
        self.progress_steps: list[dict[str, int]] = []
        self.runner_steps: list = []
        self.prepared_holder: dict[str, object] = {}
        self.adapter = CnncLegacyZhaoshangAdapter(
            mappings=[],
            db_env_path=None,
            runner=self._record_runner,
            progress_probe=self._progress_probe,
        )

    def _record_runner(self, args, timeout_seconds, _environment):
        self.runner_calls.append(list(args))
        step = self.runner_steps.pop(0)
        return step(args, timeout_seconds, _environment)

    def _progress_probe(self, _job, task_id, task_number, _timeout_seconds):
        self.probe_calls.append((str(task_id), str(task_number)))
        progress = self.progress_steps.pop(0) if self.progress_steps else None
        return {
            "task_id": str(task_id),
            "task_number": str(task_number),
            "progress": progress,
            "rsp_desc": "",
            "remote_calls": 2,
        }

    def terminal_progress(self, count: int = 1) -> dict[str, int]:
        return {
            "total": count,
            "pending": 0,
            "running": 0,
            "failed": 0,
            "waiting_db": 0,
            "finished": count,
        }

    def active_progress(self, count: int = 1) -> dict[str, int]:
        return {
            "total": count,
            "pending": max(1, count - 1),
            "running": 0,
            "failed": 0,
            "waiting_db": 0,
            "finished": 1 if count > 1 else 0,
        }

    def requests(self):
        return (
            CrawlRequest(
                mall_key="comix",
                mall_source_id=COMIX.source_id,
                mall_name=COMIX.mall_name,
                sku_id=self.sku_id,
                url=self.url,
                backend_version=BACKEND_VERSION,
            ),
        )

    def seed(self, *, job_state: str = "RUNNING"):
        requests = self.requests()
        prepared = self.adapter.prepare_job(
            DAY_1,
            requests,
            self.run_dir / "input.xlsx",
            self.run_dir / "result.xlsx",
            "task-target",
            self.batch_state,
        )
        self.prepared_holder["prepared"] = prepared
        events = (
            {
                "action": "CREATED",
                "mall": "comix",
                "task_name": prepared.task_name,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
                "expected_subtask_count": len(requests),
            },
            {
                "action": "ENABLED",
                "mall": "comix",
                "task_name": prepared.task_name,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
            },
            {
                "action": "PRIORITY_UPDATED",
                "mall": "comix",
                "task_name": prepared.task_name,
                "task_id": TASK_ID,
                "task_number": TASK_NUMBER,
                "affected_rows": len(requests),
            },
        )
        with self.batch_state.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(f"- `{json.dumps(event)}`\n")

        store = DailyAuditStore(self.state_path)
        lease = store.acquire_lease(DAY_1, "seed", 60)
        rows = [
            {
                "vCommodityId": "v-0",
                "skuId": self.sku_id,
                "queryStr": "query-0",
                "extLink": self.url,
                "__crawl_required": True,
            }
        ]
        ingested = store.ingest_snapshot(lease, rows)
        snapshot_id = int(ingested["snapshot"]["id"])
        store.freeze_snapshot(lease, snapshot_id)
        payload = {
            "requests": [request.__dict__ for request in requests],
            "batch_state": str(self.batch_state),
        }
        job = store.prepare_remote_intent(
            lease,
            prepared.logical_job_key,
            prepared.task_name,
            prepared.payload_sha256,
            mall_key=prepared.mall_key,
            input_path=str(prepared.template_path),
            result_path=str(prepared.result_path),
            payload_json=payload,
        )
        store.claim_remote_submission(lease, int(job["id"]))
        store.reconcile_remote_job(
            lease,
            int(job["id"]),
            remote_identity=IDENTITY,
            state=job_state,
        )
        crawl = store.ensure_crawl_work(
            lease,
            "comix",
            self.sku_id,
            self.url,
            BACKEND_VERSION,
            self.url,
            source_id="v-0",  # 快照行 vCommodityId(冻结快照 source 集合)
        )
        store.link_crawl_work_to_remote_job(lease, int(crawl["id"]), int(job["id"]))
        store.release_lease(lease)
        return store, int(job["id"])


def _still_running_runner(seed: _JobSeed):
    """第 1 次导出调用:只返回(不写结果,模拟远端仍在跑)。"""

    def runner(args, timeout_seconds, _environment):
        assert "--wait-export-only" in args
        assert "--create-only" not in args

        class Result:
            returncode = 0

        return Result()

    return runner


def _exporting_runner(seed: _JobSeed, rows):
    """第 2 次导出调用:写 result.xlsx + 追加 EXPORTED 批事件。"""

    def runner(args, timeout_seconds, _environment):
        assert "--wait-export-only" in args
        assert "--create-only" not in args
        prepared = seed.prepared_holder["prepared"]
        _write_result(
            prepared.result_path,
            rows,
            task_name=prepared.task_name,
            mall_key=prepared.mall_key,
        )
        event = {
            "action": "EXPORTED",
            "mall": "comix",
            "task_name": prepared.task_name,
            "task_id": TASK_ID,
            "task_number": TASK_NUMBER,
            "output_path": str(prepared.result_path),
        }
        with seed.batch_state.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{json.dumps(event)}`\n")

        class Result:
            returncode = 0

        return Result()

    return runner


class _BoostCountingAdapter:
    """dispatch 用的 counting adapter:prepare_job 委托真实 adapter(保证跨日
    重建的 logical key / payload hash 与 Seed 造库证据一致),submit 默认毒化
    (意外 POST 直接炸测试),prioritize 本地返回 UPDATED weight_type=-1。"""

    def __init__(self, real, *, submit_raises: bool = True):
        self._real = real
        self.submit_raises = submit_raises
        self.prepare_calls = 0
        self.submit_calls = 0
        self.prioritize_calls = 0

    def prepare_job(self, *args, **kwargs):
        self.prepare_calls += 1
        return self._real.prepare_job(*args, **kwargs)

    def submit(self, _job, *, timeout_seconds):
        self.submit_calls += 1
        if self.submit_raises:
            raise AssertionError("manual recrawl submitted on a path that must never POST")
        return LegacySubmitOutcome(
            SubmitStatus.SUBMITTED,
            task_id="remote-1",
            task_number="number-1",
            priority_evidence="updated",
        )

    def prioritize(self, _job, *, task_id, task_number, timeout_seconds):
        self.prioritize_calls += 1
        return LegacyPriorityOutcome(PriorityStatus.UPDATED, weight_type=-1)


class _FixedDispatch:
    """RecordingDispatch 风格 fake:对任何候选返回固定的 dispatch 结果。"""

    def __init__(self, result):
        self.calls = 0
        self.seen_kwargs: list[dict] = []
        self.result = dict(result)

    def __call__(self, _prewarm_store, _shared_cache, **kwargs):
        self.calls += 1
        self.seen_kwargs.append(dict(kwargs))
        return dict(self.result)


def _seed_candidate(prewarm: CustomerPrewarmStore, url: str, *, mall="comix", document="D1"):
    """CACHE_HIT 终态候选(守卫式手动重爬的常规入口,照抄 dispatch 测试)。"""
    upsert = prewarm.upsert_link(document, url, mall=mall)
    candidate_id = int(upsert["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_CACHE_HIT)
    return candidate_id


def _make_run_db(runtime_root: Path, business_date: str):
    """真实 DailyAuditStore 的空 run 库(照抄 dispatch exact 测试的 make_state_db)。"""
    paths = runtime_paths(business_date, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(business_date)
    return paths


def _write_min_state_db(runtime_root: Path, business_date: str, jobs):
    """最小远端状态库(镜像 test_prewarm_manual_recrawl 的 _write_state_db):
    runtime_root/<business_date>/state.sqlite3,remote_jobs 只建 (id, state)。"""
    day_dir = runtime_root / business_date
    day_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(day_dir / "state.sqlite3"))
    try:
        connection.execute(
            "CREATE TABLE remote_jobs (id INTEGER PRIMARY KEY, state TEXT NOT NULL)"
        )
        for job_id, state in jobs:
            connection.execute(
                "INSERT INTO remote_jobs (id, state) VALUES (?, ?)", (job_id, state)
            )
        connection.commit()
    finally:
        connection.close()


def _state_rows(path, query, params=()):
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def _micro_utcnow() -> str:
    # 收割核心写 crawl_work.updated_at 用秒级截断,若与动作 started_at 同秒
    # 会被 G2 的 succeeded_at >= started_at 门槛误判为旧行;测试内把
    # DailyAuditStore 时钟补到微秒,保证真实时序严格递增。
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _make_runner(
    runtime_root: Path,
    *,
    dispatch_exact,
    reexport=None,
    adapter_factory=None,
    max_attempts: int = 3,
):
    return ManualRecrawlRunner(
        runtime_root=runtime_root,
        dispatch_exact=dispatch_exact,
        mappings_loader=lambda: [COMIX],
        adapter_factory=adapter_factory,
        reexport=reexport,
        cross_day_max_attempts=max_attempts,
        cross_day_interval_seconds=0.0,
        sleeper=lambda _seconds: None,
    )


# ---- 测试 1:跨日全链(零二次 POST → SUCCEEDED) ------------------------------------


def test_cross_day_full_lifecycle_zero_second_post_to_succeeded(tmp_path, monkeypatch):
    """REUSED 跨日 D1 任务后,worker 即 lifecycle owner:有界 reexport(第 1 轮
    仍在跑→STILL_RUNNING,第 2 轮终态→COMPLETED)+ sync 镜像进共享缓存 → G2
    精确关联 → 动作 SUCCEEDED;全程零二次 POST,D2 库不产生任何任务。"""
    monkeypatch.setattr(DailyAuditStore, "_utcnow", staticmethod(_micro_utcnow))
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / PREWARM_STORE_NAME)
    SharedCrawlCache(runtime_root / SHARED_CACHE_NAME)

    # D1 库:1 个 URL 的 bound RUNNING job(identity comix|...|...)+ linkage 行。
    seed = _JobSeed(runtime_root, URL)
    seed.progress_steps = [seed.active_progress(1)]
    seed.runner_steps = [_still_running_runner(seed)]
    _store1, job_id = seed.seed(job_state="RUNNING")
    candidate_id = _seed_candidate(prewarm, URL)
    prewarm.record_remote_job_link(DAY_1, candidate_id, job_id, "customer-prewarm-zhaoshang")

    # D2(动作日)空 run 库。
    paths2 = _make_run_db(runtime_root, BUSINESS_DATE)

    real_adapter = CnncLegacyZhaoshangAdapter(mappings=[COMIX], db_env_path=None)
    counting = _BoostCountingAdapter(real_adapter, submit_raises=True)

    def dispatch_wrapper(store, cache, **kw):
        return dispatch_single_candidate_exact(store, cache, adapter=counting, **kw)

    runner = _make_runner(
        runtime_root,
        dispatch_exact=dispatch_wrapper,
        reexport=None,  # 真实 run_manual_reexport
        adapter_factory=lambda: seed.adapter,
        max_attempts=4,
    )

    # 第 2 轮收割:导出真实写结果(远端终于跑完)。
    seed.runner_steps.append(_exporting_runner(seed, [["code-0", "sku-0", URL, 12.3]]))
    seed.progress_steps.append(seed.terminal_progress(1))

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    result = runner.get(handle["action_id"])

    # 有界循环第 1 轮 STILL_RUNNING、第 2 轮 COMPLETED → G2 关联 SUCCEEDED。
    assert result["status"] == "SUCCEEDED"
    assert result["outcome"]["completion_proof"] == "cache_current"
    assert result["outcome"]["cross_day_harvest"]["attempts"] == 2
    # 绝不二次 POST:counting adapter submit 毒化未被触发。
    assert counting.submit_calls == 0
    # 真实 legacy runner 只收到 --wait-export-only 导出命令,绝无 --create-only。
    assert len(seed.runner_calls) == 2
    assert all("--create-only" not in args for args in seed.runner_calls)
    assert all("--wait-export-only" in args for args in seed.runner_calls)
    # 共享缓存出现 (business_date=D1, remote_job_id=job_id) 的 current 行。
    hit = SharedCrawlCache(runtime_root / SHARED_CACHE_NAME).lookup_current("comix", URL)
    assert hit is not None
    payload = (hit.get("entry") or {}).get("payload") or {}
    assert payload.get("business_date") == DAY_1
    assert int(payload.get("remote_job_id")) == job_id
    # D1 库 job 行终态 COMPLETED。
    job_rows = _state_rows(paths2.state_path, "SELECT * FROM remote_jobs")
    assert job_rows == []  # D2 库 remote_jobs 仍空
    d1_state = _state_rows(
        seed.state_path, "SELECT state FROM remote_jobs WHERE id=?", (job_id,)
    )
    assert d1_state[0]["state"] == "COMPLETED"


# ---- 测试 2:D1 任务终态 FAILED → 动作诚实 FAILED --------------------------------


def test_cross_day_remote_job_failed_fails_action(tmp_path):
    """D1 任务已终态 FAILED:延续循环第 1 轮 reexport 后,既有 D9 只读探测把
    动作诚实翻成 FAILED(failed_by=remote_job_terminal_failed)。"""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / PREWARM_STORE_NAME)
    SharedCrawlCache(runtime_root / SHARED_CACHE_NAME)
    candidate_id = _seed_candidate(prewarm, URL)
    _write_min_state_db(runtime_root, DAY_1, [(17, "FAILED")])

    dispatch = _FixedDispatch({
        "outcome": "REUSED",
        "remote_job_id": 17,
        "remote_identity": "comix|t|n",
        "priority_proven": True,
        "cross_day": True,
        "job_business_date": DAY_1,
    })

    reexport_calls: list[dict] = []

    def fake_reexport(**kwargs):
        reexport_calls.append(dict(kwargs))
        return {
            "outcome": "REEXPORT_REJECTED",
            "error_code": "IDENTITY_MISMATCH",
            "create_calls": 0,
        }

    runner = _make_runner(
        runtime_root,
        dispatch_exact=dispatch,
        reexport=fake_reexport,
        adapter_factory=lambda: object(),
        max_attempts=3,
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    result = runner.get(handle["action_id"])

    assert result["status"] == "FAILED"
    assert result["outcome"]["failed_by"] == "remote_job_terminal_failed"
    # evaluate 设计在每轮 reexport 之后:第 1 轮 reexport 调用 1 次后即翻 FAILED。
    assert len(reexport_calls) <= 1
    assert reexport_calls[0]["business_date"] == DAY_1


# ---- 测试 3:D1 库缺失 → 同日 CREATE,跨日 daemon 从不启动 -------------------------


def test_cross_day_missing_d1_db_falls_back_to_same_day_create_no_daemon(tmp_path):
    """linkage 指向的 D1 state.sqlite3 缺失 → dispatch 诚实回退同日 CREATE
    (cross_day 非 True)→ 跨日 lifecycle 从不启动,reexport 零调用。"""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / PREWARM_STORE_NAME)
    SharedCrawlCache(runtime_root / SHARED_CACHE_NAME)
    candidate_id = _seed_candidate(prewarm, URL)
    prewarm.record_remote_job_link(DAY_1, candidate_id, 777, "customer-prewarm-zhaoshang")
    assert not (runtime_root / DAY_1 / "state.sqlite3").is_file()
    _make_run_db(runtime_root, BUSINESS_DATE)

    real_adapter = CnncLegacyZhaoshangAdapter(mappings=[COMIX], db_env_path=None)
    counting = _BoostCountingAdapter(real_adapter, submit_raises=False)

    def dispatch_wrapper(store, cache, **kw):
        return dispatch_single_candidate_exact(store, cache, adapter=counting, **kw)

    reexport_calls: list[dict] = []

    def fake_reexport(**kwargs):
        reexport_calls.append(dict(kwargs))
        return {"outcome": "REEXPORT_COMPLETED", "create_calls": 0}

    runner = _make_runner(
        runtime_root,
        dispatch_exact=dispatch_wrapper,
        reexport=fake_reexport,
        adapter_factory=lambda: object(),
        max_attempts=3,
    )

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    result = runner.get(handle["action_id"])

    assert result["status"] == "WAITING_REMOTE"
    assert result["outcome"]["dispatch"]["outcome"] == "CREATED"
    assert result["outcome"]["dispatch"].get("cross_day") is not True
    assert len(reexport_calls) == 0
    assert counting.submit_calls == 1  # 同日创建链恰好一次 POST


# ---- 测试 4:同整数 job id 只操作 D1 (date, id) 对 -------------------------------


def test_cross_day_same_integer_job_id_operates_d1_pair_only(tmp_path):
    """D1 job 17(被 linkage 指向)与 D2 job 17(COMPLETED,无关)同 id:延续
    循环每次 reexport 都只带 (D1, 17) 对;预算耗尽后动作保持 WAITING_REMOTE,
    D2 的 state.sqlite3 内容一个字节都不变。"""
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / PREWARM_STORE_NAME)
    SharedCrawlCache(runtime_root / SHARED_CACHE_NAME)
    candidate_id = _seed_candidate(prewarm, URL)
    _write_min_state_db(runtime_root, DAY_1, [(17, "RUNNING")])
    paths2 = _make_run_db(runtime_root, BUSINESS_DATE)
    # D2 也有一个同整数 id 的 job 17(COMPLETED,与跨日任务无关):完整 NOT NULL
    # 列直接插入,固定 id 制造 (D1, 17) / (D2, 17) 的整数 id 碰撞。
    connection = sqlite3.connect(str(paths2.state_path))
    try:
        run_id = connection.execute(
            "SELECT id FROM daily_runs WHERE business_date=?", (BUSINESS_DATE,)
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO remote_jobs
               (id, run_id, logical_key, task_name, payload_hash, state,
                created_at, updated_at)
               VALUES (17, ?, 'logical-d2-seventeen',
                       'cnnc_current_opt_qx_d2_17', 'hash-d2-seventeen',
                       'COMPLETED', '2026-09-14T01:00:00+00:00',
                       '2026-09-14T01:00:00+00:00')""",
            (run_id,),
        )
        connection.commit()
    finally:
        connection.close()

    dispatch = _FixedDispatch({
        "outcome": "REUSED",
        "remote_job_id": 17,
        "remote_identity": "comix|t|n",
        "priority_proven": True,
        "cross_day": True,
        "job_business_date": DAY_1,
    })

    reexport_calls: list[dict] = []

    def fake_reexport(**kwargs):
        reexport_calls.append(dict(kwargs))
        return {
            "outcome": "REEXPORT_REJECTED",
            "error_code": "REMOTE_PROGRESS_UNKNOWN",
            "create_calls": 0,
        }

    runner = _make_runner(
        runtime_root,
        dispatch_exact=dispatch,
        reexport=fake_reexport,
        adapter_factory=lambda: object(),
        max_attempts=2,
    )

    d2_rows_before = _state_rows(paths2.state_path, "SELECT * FROM remote_jobs ORDER BY id")

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    result = runner.get(handle["action_id"])

    # 每次调用都精确操作 (D1, 17) 对:state_path 在 D1 目录下、business_date=D1。
    assert len(reexport_calls) == 2
    for call in reexport_calls:
        assert call["business_date"] == DAY_1
        assert Path(call["state_path"]) == runtime_root / DAY_1 / "state.sqlite3"
        assert int(call["remote_job_id"]) == 17
        assert call["remote_identity"] == "comix|t|n"
    # 预算耗尽:WAITING_REMOTE 语义保持诚实,不翻转 FAILED。
    assert result["status"] == "WAITING_REMOTE"
    assert result["outcome"]["cross_day_harvest"]["attempts"] == 2
    # D2 的 state.sqlite3 内容不变:无关的 (D2, 17) 行从未被读改。
    d2_rows_after = _state_rows(paths2.state_path, "SELECT * FROM remote_jobs ORDER BY id")
    assert d2_rows_after == d2_rows_before
    assert [(row["id"], row["state"]) for row in d2_rows_after] == [(17, "COMPLETED")]


# ---- 测试 5:延续只碰本动作的 (date, job) 对 ------------------------------------


def test_cross_day_continuation_leaves_unrelated_jobs_untouched(tmp_path, monkeypatch):
    """测试 1 全链之上,D1 再加一个无关 RUNNING job + 无关 crawl 行:全链完成
    后无关行一字不变(收割只消费本动作 linkage 指向的任务)。"""
    monkeypatch.setattr(DailyAuditStore, "_utcnow", staticmethod(_micro_utcnow))
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / PREWARM_STORE_NAME)
    SharedCrawlCache(runtime_root / SHARED_CACHE_NAME)

    seed = _JobSeed(runtime_root, URL)
    seed.progress_steps = [seed.active_progress(1)]
    seed.runner_steps = [_still_running_runner(seed)]
    store1, job_id = seed.seed(job_state="RUNNING")

    # D1 无关工作:另一个 RUNNING job + 一行未完成 crawl(与本动作无关)。
    unrelated_url = "https://qx.com/unrelated-neighbor.html"
    unrelated_sku = prewarm_placeholder_sku("comix", unrelated_url)
    lease = store1.acquire_lease(DAY_1, "unrelated-seeder", 60)
    try:
        unrelated_crawl = store1.ensure_crawl_work(
            lease, "comix", unrelated_sku, unrelated_url, BACKEND_VERSION,
            "seed-fp", source_id=COMIX.source_id,
        )
        unrelated_job = store1.prepare_remote_intent(
            lease,
            "logical-unrelated-neighbor",
            "cnnc_current_opt_qx_unrelated",
            "hash-unrelated-neighbor",
            mall_key="comix",
            input_path="input-unrelated.xlsx",
            result_path="result-unrelated.xlsx",
            payload_json={"requests": [], "batch_state": str(seed.batch_state)},
        )
        store1.link_crawl_work_to_remote_job(
            lease, int(unrelated_crawl["id"]), int(unrelated_job["id"])
        )
    finally:
        store1.release_lease(lease)
    connection = sqlite3.connect(str(seed.state_path))
    try:
        connection.execute(
            "UPDATE remote_jobs SET state='RUNNING' WHERE id=?",
            (int(unrelated_job["id"]),),
        )
        connection.commit()
    finally:
        connection.close()
    unrelated_job_id = int(unrelated_job["id"])
    unrelated_crawl_id = int(unrelated_crawl["id"])

    candidate_id = _seed_candidate(prewarm, URL)
    prewarm.record_remote_job_link(DAY_1, candidate_id, job_id, "customer-prewarm-zhaoshang")
    _make_run_db(runtime_root, BUSINESS_DATE)

    real_adapter = CnncLegacyZhaoshangAdapter(mappings=[COMIX], db_env_path=None)
    counting = _BoostCountingAdapter(real_adapter, submit_raises=True)

    def dispatch_wrapper(store, cache, **kw):
        return dispatch_single_candidate_exact(store, cache, adapter=counting, **kw)

    runner = _make_runner(
        runtime_root,
        dispatch_exact=dispatch_wrapper,
        reexport=None,
        adapter_factory=lambda: seed.adapter,
        max_attempts=4,
    )
    seed.runner_steps.append(_exporting_runner(seed, [["code-0", "sku-0", URL, 12.3]]))
    seed.progress_steps.append(seed.terminal_progress(1))

    handle = runner.start(BUSINESS_DATE, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    result = runner.get(handle["action_id"])

    assert result["status"] == "SUCCEEDED"
    assert counting.submit_calls == 0
    # 无关 job 行 state 不变。
    unrelated_job_rows = _state_rows(
        seed.state_path, "SELECT * FROM remote_jobs WHERE id=?", (unrelated_job_id,)
    )
    assert unrelated_job_rows[0]["state"] == "RUNNING"
    # 无关 crawl 行不变(仍是未完成态,绝不被收割/同步触碰)。
    unrelated_crawl_rows = _state_rows(
        seed.state_path, "SELECT * FROM crawl_work WHERE id=?", (unrelated_crawl_id,)
    )
    assert unrelated_crawl_rows[0]["state"] != "SUCCESS"
    # sync 只镜像本动作 URL 的成功行:共享缓存没有无关 URL 的行。
    assert (
        SharedCrawlCache(runtime_root / SHARED_CACHE_NAME).lookup_current(
            "comix", unrelated_url
        )
        is None
    )

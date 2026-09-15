"""Round 6 承重测试:production-shape 跨日 prewarm 生命周期(Issue #13 Round 6 Blocker)。

D1 远端任务必须由真实 Issue #10 创建路径生成(真实
``run_prewarm_dispatch_pass`` → ``_dispatch_mall_group``),crawl_work 的
source_id 因此真实等于 ``mapping.source_id``(SRC-COMIX),SKU 是 zhpq-*
placeholder,payload / CREATED+ENABLED / PRIORITY_UPDATED 证据全部由 dispatch
正常路径写入——禁止 Round 5 ``_JobSeed`` 那种手工伪造 snapshot fan-out
(vCommodityId == crawl source_id)的承重 fixture。

场景覆盖(Round 6 review 指令 §1/§5):

- A:D1 当日 daily run 没有 authoritative snapshot(无 frozen、无 latest);
- B:D1 有无关 STEP1 冻结 snapshot,vCommodityId 与 mapping.source_id 不相等
  (后续测试补齐);

两种场景下,D2 manual recrawl 同 URL → 真实 dispatch REUSED D1 任务
(cross_day=True)→ 零二次 POST → 有界 existing-lifecycle harvest → D1
crawl_work SUCCESS → sync_daily_internet_results 镜像 → 既有 G2 精确
(business_date, remote_job_id) 关联 → action SUCCEEDED。

零真实网络/子进程:真实 CnncLegacyZhaoshangAdapter + 注入 runner
(--create-only 写 CREATED/ENABLED、--wait-export-only 按剧本导出)、
priority_runner(写 PRIORITY_UPDATED)、progress_probe(剧本进度)。
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
    run_prewarm_dispatch_pass,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_PENDING_PREWARM,
    CustomerPrewarmStore,
)
from app.services.daily_audit_store import DailyAuditStore
from app.services.incremental_audit_runtime import runtime_paths
from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter
from app.services.prewarm_manual_recrawl import ManualRecrawlRunner
from app.services.shared_crawl_cache import SharedCrawlCache
from scripts.staples_prewarm_run import PREWARM_STORE_NAME, SHARED_CACHE_NAME
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping

DAY_1 = "2026-09-13"  # D1:prewarm 任务所属业务日
DAY_2 = "2026-09-14"  # D2:manual recrawl 动作日
TASK_ID = "7731001"
TASK_NUMBER = "RW-20260913120100"
URL = "https://qx.com/prod-shape-a.html"
BACKEND_VERSION = "legacy-v1"

COMIX = ZhaoshangMallMapping(
    mall_name="齐心商城",
    source_id="SRC-COMIX",
    mall_key="comix",
    domains=("qx.com", "www.qx.com"),
    vendor_keywords=("comix",),
)


# ---- 可剧本化的"远端世界"(真实 adapter 的注入 seam) ----------------------------


class _RemoteWorld:
    """一个 D1 日的可编程远端:真实 adapter 的 runner / priority_runner /
    progress_probe 注入实现,所有证据落进真实 BATCH_STATE.md 文件。

    - ``--create-only`` runner 调用:追加 CREATED + ENABLED 事件(dispatch 的
      真实 submit 会从事件解析出 task 身份);
    - ``--wait-export-only`` runner 调用:按 ``export_steps`` 剧本出牌——
      ``None`` 表示仍在跑(不写结果),行列表表示写 result.xlsx + EXPORTED 事件;
    - priority_runner:追加 PRIORITY_UPDATED(affected_rows>0 → "updated" 证据);
    - progress_probe:默认 active 进度,可用 ``progress_steps`` 覆盖。
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.batch_state = run_dir / "BATCH_STATE.md"
        self.runner_calls: list[list[str]] = []
        self.export_steps: list = []
        self.progress_steps: list[dict[str, int]] = []

    # -- BATCH_STATE 事件(格式与 _read_batch_events 的 "- `{json}`" 行一致) --

    def _append_events(self, events: list[dict[str, object]]) -> None:
        with self.batch_state.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(f"- `{json.dumps(event)}`\n")

    def _args_value(self, args: list[str], flag: str) -> str:
        return str(args[args.index(flag) + 1])

    def _write_result(self, path: Path, rows, *, task_name: str) -> None:
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
            sheet.append([*row, f"{TASK_NUMBER}-exec-{index}", task_name, "comix"])
        workbook.save(path)

    # -- 注入 seam 实现 ----------------------------------------------------------

    def runner(self, args, timeout_seconds, _environment):
        self.runner_calls.append(list(args))
        assert ("--create-only" in args) != ("--wait-export-only" in args)
        task_name = self._args_value(args, "--task-name")
        if "--create-only" in args:
            self._append_events(
                [
                    {
                        "action": "CREATED",
                        "mall": "comix",
                        "task_name": task_name,
                        "task_id": TASK_ID,
                        "task_number": TASK_NUMBER,
                        "expected_subtask_count": 1,
                    },
                    {
                        "action": "ENABLED",
                        "mall": "comix",
                        "task_name": task_name,
                        "task_id": TASK_ID,
                        "task_number": TASK_NUMBER,
                    },
                ]
            )

            class Result:
                returncode = 0

            return Result()
        assert "--wait-export-only" in args
        step = self.export_steps.pop(0) if self.export_steps else None
        if step is None:
            # 远端仍在跑:不写结果、不写 EXPORTED 事件。

            class Result:
                returncode = 0

            return Result()
        output = Path(self._args_value(args, "--output"))
        self._write_result(output, step, task_name=task_name)
        self._append_events(
            [
                {
                    "action": "EXPORTED",
                    "mall": "comix",
                    "task_name": task_name,
                    "task_id": TASK_ID,
                    "task_number": TASK_NUMBER,
                    "output_path": str(output),
                }
            ]
        )

        class Result:
            returncode = 0

        return Result()

    def priority_runner(self, _job, _task_id, _task_number, _timeout_seconds):
        self._append_events(
            [
                {
                    "action": "PRIORITY_UPDATED",
                    "mall": "comix",
                    "task_name": _job.task_name,
                    "task_id": TASK_ID,
                    "task_number": TASK_NUMBER,
                    "affected_rows": 1,
                }
            ]
        )

    def progress_probe(self, _job, task_id, task_number, _timeout_seconds):
        progress = self.progress_steps.pop(0) if self.progress_steps else self.active_progress()
        return {
            "task_id": str(task_id),
            "task_number": str(task_number),
            "progress": progress,
            "rsp_desc": "",
            "remote_calls": 2,
        }

    @staticmethod
    def active_progress() -> dict[str, int]:
        return {
            "total": 1,
            "pending": 1,
            "running": 0,
            "failed": 0,
            "waiting_db": 0,
            "finished": 0,
        }

    @staticmethod
    def terminal_progress() -> dict[str, int]:
        return {
            "total": 1,
            "pending": 0,
            "running": 0,
            "failed": 0,
            "waiting_db": 0,
            "finished": 1,
        }

    def adapter(self) -> CnncLegacyZhaoshangAdapter:
        return CnncLegacyZhaoshangAdapter(
            mappings=[],
            db_env_path=None,
            runner=self.runner,
            priority_runner=self.priority_runner,
            progress_probe=self.progress_probe,
        )


class _NoSubmitAdapter:
    """D2 dispatch 用的包装:prepare_job / prioritize 委托真实 adapter
    (跨日重建的证据校验必须走真实逻辑),submit 毒化——REUSED 路径上任何
    二次 POST 直接炸测试。"""

    def __init__(self, real: CnncLegacyZhaoshangAdapter):
        self._real = real
        self.submit_calls = 0
        self.prioritize_calls = 0

    def prepare_job(self, *args, **kwargs):
        return self._real.prepare_job(*args, **kwargs)

    def submit(self, _job, *, timeout_seconds):
        self.submit_calls += 1
        raise AssertionError("cross-day reuse must never POST a second create")

    def prioritize(self, job, *, task_id, task_number, timeout_seconds):
        self.prioritize_calls += 1
        return self._real.prioritize(
            job, task_id=task_id, task_number=task_number, timeout_seconds=timeout_seconds
        )


# ---- 通用 helper ---------------------------------------------------------------


def _make_run_db(runtime_root: Path, business_date: str):
    paths = runtime_paths(business_date, runtime_root)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    store = DailyAuditStore(paths.state_path)
    store.ensure_run(business_date)
    return paths


def _pending_candidate(prewarm: CustomerPrewarmStore, url: str) -> int:
    upsert = prewarm.upsert_link("DOC-D1", url, mall="comix")
    candidate_id = int(upsert["candidate"]["id"])
    prewarm.record_candidate_result(candidate_id, cache_status=CACHE_STATUS_PENDING_PREWARM)
    return candidate_id


def _state_rows(path, query, params=()):
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def _micro_utcnow() -> str:
    # 收割核心写 crawl_work.updated_at 用秒级截断,若与动作 started_at 同秒
    # 会被 G2 的 succeeded_at >= started_at 门槛误判为旧行;补到微秒保证真实
    # 时序严格递增(与 tests/test_prewarm_cross_day_lifecycle.py 相同处理)。
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _export_rows(url: str):
    return [["code-prod-shape-a", prewarm_placeholder_sku("comix", url), url, 12.3]]


def _create_d1_prewarm_job(runtime_root: Path, prewarm, cache, url: str):
    """真实 Issue #10 创建路径造 D1:``run_prewarm_dispatch_pass`` 全链
    (route → create → submit → 同 pass priority confirm)。返回
    (world, d1_paths, job_id, remote_identity, candidate_id)。"""
    d1_paths = _make_run_db(runtime_root, DAY_1)
    world = _RemoteWorld(d1_paths.run_dir)
    candidate_id = _pending_candidate(prewarm, url)

    summary = run_prewarm_dispatch_pass(
        prewarm,
        cache,
        business_date=DAY_1,
        runtime_root=runtime_root,
        mappings=[COMIX],
        adapter=world.adapter(),
    )

    assert summary["outcome"] == "OK", summary
    assert summary["jobs_created"] == 1
    assert summary["submit_unknown"] == 0
    assert summary["priority_confirmed"] == 1
    # production-shape 契约:crawl source_id == mapping.source_id,placeholder SKU,
    # 任务证据齐全(SUBMITTED + 身份 + payload 携带 mapping source)。
    crawls = _state_rows(d1_paths.state_path, "SELECT * FROM crawl_work")
    assert len(crawls) == 1
    assert crawls[0]["source_id"] == COMIX.source_id
    assert crawls[0]["sku_id"] == prewarm_placeholder_sku("comix", url)
    assert crawls[0]["sku_id"].startswith("zhpq-")
    jobs = _state_rows(d1_paths.state_path, "SELECT * FROM remote_jobs")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["state"] == "SUBMITTED"
    identity = str(job["remote_identity"])
    assert identity == f"comix|{TASK_ID}|{TASK_NUMBER}"
    payload = json.loads(job["payload_json"])
    assert payload["requests"][0]["mall_source_id"] == COMIX.source_id
    assert payload["requests"][0]["sku_id"] == crawls[0]["sku_id"]
    return world, d1_paths, int(job["id"]), identity, candidate_id


# ---- 场景 A:D1 无 authoritative snapshot ---------------------------------------


def test_production_shape_cross_day_no_snapshot_harvest_to_succeeded(
    tmp_path, monkeypatch
):
    """D1 由真实 dispatch 创建(source_id=SRC-COMIX、无任何 snapshot):
    D2 manual recrawl REUSED 跨日任务后,worker 必须能收割既有生命周期到
    SUCCESS 并镜像进共享缓存,以精确 (D1, job) 对关联出 SUCCEEDED。"""
    monkeypatch.setattr(DailyAuditStore, "_utcnow", staticmethod(_micro_utcnow))
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / PREWARM_STORE_NAME)
    SharedCrawlCache(runtime_root / SHARED_CACHE_NAME)

    world, d1_paths, job_id, identity, candidate_id = _create_d1_prewarm_job(
        runtime_root, prewarm, SharedCrawlCache(runtime_root / SHARED_CACHE_NAME), URL
    )
    # 场景 A 契约:D1 run 无 frozen、无任何 observed snapshot。
    store1 = DailyAuditStore(d1_paths.state_path)
    assert store1.get_run(DAY_1)["frozen_snapshot_id"] is None
    assert store1.latest_snapshot(DAY_1) is None

    _make_run_db(runtime_root, DAY_2)

    no_submit = _NoSubmitAdapter(world.adapter())

    def dispatch_wrapper(store, cache, **kwargs):
        return dispatch_single_candidate_exact(store, cache, adapter=no_submit, **kwargs)

    runner = ManualRecrawlRunner(
        runtime_root=runtime_root,
        dispatch_exact=dispatch_wrapper,
        mappings_loader=lambda: [COMIX],
        adapter_factory=world.adapter,
        cross_day_max_attempts=4,
        cross_day_interval_seconds=0.0,
        sleeper=lambda _seconds: None,
    )

    # 剧本:第 1 轮收割仍在跑;第 2 轮导出终态结果(远端终于跑完)。
    world.export_steps = [None, _export_rows(URL)]

    handle = runner.start(DAY_2, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    result = runner.get(handle["action_id"])

    assert result["status"] == "SUCCEEDED", result
    assert result["outcome"]["dispatch"]["outcome"] == "REUSED"
    assert result["outcome"]["dispatch"]["cross_day"] is True
    assert result["outcome"]["dispatch"]["job_business_date"] == DAY_1
    assert result["outcome"]["completion_proof"] == "cache_current"
    # 零二次 POST:REUSED 路径上 submit 毒化未被触发。
    assert no_submit.submit_calls == 0
    # 真实 runner 只收到 1 次 --create-only(D1 dispatch)与若干
    # --wait-export-only(收割),绝无第二次 create。
    create_calls = [args for args in world.runner_calls if "--create-only" in args]
    assert len(create_calls) == 1
    assert all(
        "--wait-export-only" in args for args in world.runner_calls[len(create_calls) :]
    )
    # 共享缓存:current 行精确携带 (business_date=D1, remote_job_id=job)。
    hit = SharedCrawlCache(runtime_root / SHARED_CACHE_NAME).lookup_current("comix", URL)
    assert hit is not None
    payload = (hit.get("entry") or {}).get("payload") or {}
    assert payload.get("business_date") == DAY_1
    assert int(payload.get("remote_job_id")) == job_id
    # placeholder SKU 绝不成为 confirmed real SKU。
    entry = hit.get("entry") or {}
    assert entry.get("real_sku_id") is None
    assert entry.get("sku_status") == "UNRESOLVED"
    # D1 任务终态 COMPLETED;D2 库不产生任何任务。
    d1_state = _state_rows(
        d1_paths.state_path, "SELECT state FROM remote_jobs WHERE id=?", (job_id,)
    )
    assert d1_state[0]["state"] == "COMPLETED"
    d1_crawl = _state_rows(
        d1_paths.state_path, "SELECT state FROM crawl_work WHERE remote_job_id=?",
        (job_id,),
    )
    assert d1_crawl[0]["state"] == "SUCCESS"
    d2_jobs = _state_rows(
        runtime_root / DAY_2 / "state.sqlite3", "SELECT * FROM remote_jobs"
    )
    assert d2_jobs == []


# ---- 场景 B:D1 有无关 STEP1 冻结 snapshot + STEP1 门保持闭合 --------------------


def test_production_shape_cross_day_unrelated_snapshot_prewarm_harvest_and_step1_gate_stays_closed(
    tmp_path, monkeypatch
):
    """D1 冻结一个无关 snapshot(vCommodityId != mapping.source_id)后:
    普通 STEP1 ``run_manual_reexport`` 对同一 production-shape 任务必须继续
    fail-closed(fan-out 门拒绝、零 --wait-export-only runner 调用),而跨日
    worker 走 prewarm-payload 收割仍能把任务收成 SUCCESS 并以精确
    (D1, job) 对关联出 SUCCEEDED。"""
    monkeypatch.setattr(DailyAuditStore, "_utcnow", staticmethod(_micro_utcnow))
    runtime_root = tmp_path / "runtime"
    prewarm = CustomerPrewarmStore(runtime_root / PREWARM_STORE_NAME)
    SharedCrawlCache(runtime_root / SHARED_CACHE_NAME)

    url_b = "https://qx.com/prod-shape-b.html"
    world, d1_paths, job_id, identity, candidate_id = _create_d1_prewarm_job(
        runtime_root, prewarm, SharedCrawlCache(runtime_root / SHARED_CACHE_NAME), url_b
    )

    # D1 冻结一个与该 prewarm 任务无关的 snapshot:vCommodityId 与 crawl
    # source_id(SRC-COMIX)永不相等。
    store1 = DailyAuditStore(d1_paths.state_path)
    lease = store1.acquire_lease(DAY_1, "scenario-b", 60)
    rows = [
        {
            "vCommodityId": "v-unrelated-999",
            "skuId": "sku-x",
            "queryStr": "q",
            "extLink": "https://qx.com/other.html",
            "__crawl_required": True,
        }
    ]
    ingested = store1.ingest_snapshot(lease, rows)
    snapshot_id = int(ingested["snapshot"]["id"])
    store1.freeze_snapshot(lease, snapshot_id)
    store1.release_lease(lease)
    assert store1.get_run(DAY_1)["frozen_snapshot_id"] is not None

    _make_run_db(runtime_root, DAY_2)

    # 普通 STEP1 门必须继续 fail-closed:同一任务走 run_manual_reexport →
    # REJECTED / NO_SNAPSHOT_SCOPE("outside the frozen snapshot"),且该次
    # 调用零 --wait-export-only runner 调用(progress 默认 ACTIVE → 预检放行
    # → 核心 fan-out 门在任何 harvest 之前拒绝)。
    from app.services.dashboard_manual_ops import run_manual_reexport

    wait_calls_before = sum(
        1 for args in world.runner_calls if "--wait-export-only" in args
    )
    step1_outcome = run_manual_reexport(
        state_path=d1_paths.state_path,
        lock_path=d1_paths.lock_path,
        business_date=DAY_1,
        remote_job_id=job_id,
        remote_identity=identity,
        adapter=world.adapter(),
        timeout_seconds=30.0,
    )
    assert step1_outcome["outcome"] == "REEXPORT_REJECTED"
    assert step1_outcome["error_code"] == "NO_SNAPSHOT_SCOPE"
    assert "outside the frozen snapshot" in step1_outcome["reason"]
    wait_calls_after = sum(
        1 for args in world.runner_calls if "--wait-export-only" in args
    )
    assert wait_calls_after == wait_calls_before

    # 跨日 worker:与场景 A 完全一致(真实 prepare/prioritize、submit 毒化)。
    no_submit = _NoSubmitAdapter(world.adapter())

    def dispatch_wrapper(store, cache, **kwargs):
        return dispatch_single_candidate_exact(store, cache, adapter=no_submit, **kwargs)

    runner = ManualRecrawlRunner(
        runtime_root=runtime_root,
        dispatch_exact=dispatch_wrapper,
        mappings_loader=lambda: [COMIX],
        adapter_factory=world.adapter,
        cross_day_max_attempts=4,
        cross_day_interval_seconds=0.0,
        sleeper=lambda _seconds: None,
    )

    # 剧本:第 1 轮收割仍在跑;第 2 轮导出终态结果(远端终于跑完)。
    world.export_steps = [None, _export_rows(url_b)]

    handle = runner.start(DAY_2, candidate_id=candidate_id)
    runner._join_for_tests(handle["action_id"])
    result = runner.get(handle["action_id"])

    assert result["status"] == "SUCCEEDED", result
    assert result["outcome"]["dispatch"]["outcome"] == "REUSED"
    assert result["outcome"]["dispatch"]["cross_day"] is True
    assert result["outcome"]["dispatch"]["job_business_date"] == DAY_1
    assert result["outcome"]["completion_proof"] == "cache_current"
    # 零二次 POST:REUSED 路径上 submit 毒化未被触发。
    assert no_submit.submit_calls == 0
    # 真实 runner 只收到 1 次 --create-only(D1 dispatch)与若干
    # --wait-export-only(收割),绝无第二次 create。
    create_calls = [args for args in world.runner_calls if "--create-only" in args]
    assert len(create_calls) == 1
    assert all(
        "--wait-export-only" in args for args in world.runner_calls[len(create_calls):]
    )
    # 共享缓存:current 行精确携带 (business_date=D1, remote_job_id=job)。
    hit = SharedCrawlCache(runtime_root / SHARED_CACHE_NAME).lookup_current("comix", url_b)
    assert hit is not None
    payload = (hit.get("entry") or {}).get("payload") or {}
    assert payload.get("business_date") == DAY_1
    assert int(payload.get("remote_job_id")) == job_id
    # placeholder SKU 绝不成为 confirmed real SKU。
    entry = hit.get("entry") or {}
    assert entry.get("real_sku_id") is None
    assert entry.get("sku_status") == "UNRESOLVED"
    # D1 任务终态 COMPLETED;D2 库不产生任何任务。
    d1_state = _state_rows(
        d1_paths.state_path, "SELECT state FROM remote_jobs WHERE id=?", (job_id,)
    )
    assert d1_state[0]["state"] == "COMPLETED"
    d1_crawl = _state_rows(
        d1_paths.state_path, "SELECT state FROM crawl_work WHERE remote_job_id=?",
        (job_id,),
    )
    assert d1_crawl[0]["state"] == "SUCCESS"
    d2_jobs = _state_rows(
        runtime_root / DAY_2 / "state.sqlite3", "SELECT * FROM remote_jobs"
    )
    assert d2_jobs == []

"""Staples-only local prewarm service layer (Issue #11).

This module routes stbchina.cn (史泰博) prewarm candidates to a LOCAL crawl
path that is fully independent of the 招商局 (Zhaoshang) remote-task pipeline.

P0 invariant: the Staples path must never import or invoke any 招商局
capability.  This module intentionally imports only: ``json`` / ``re`` /
``sqlite3`` / ``uuid`` / ``importlib`` / ``dataclasses`` / ``datetime`` /
``pathlib`` / ``typing`` / ``urllib.parse`` plus
``app.services.crawl_url_identity``,
``app.services.customer_prewarm_store``, ``app.services.shared_crawl_cache``
and - lazily, inside a method so importing this module never costs openpyxl -
``app.integrations.monitor_stb_fallback`` (the existing, single stbchina wrapper that
must be reused instead of writing a second crawler).

Responsibilities (library code never prints):

- Domain routing: ``is_staples_url`` gates everything on the real hostname
  (``stbchina.cn`` / ``*.stbchina.cn``), never on substring matching.
- Identity: ``stbchina_product_identity`` (registered in
  ``MALL_IDENTITY_PARSERS``) provides conservative Level-3 URL identity.
- Cache recheck: before ANY crawl, ``SharedCrawlCache.lookup_current`` is
  consulted; only a real hit flips a candidate to ``CACHE_HIT``.
- Local crawl adapter: ``StaplesLocalCrawler`` is a narrow seam over
  ``run_stbchina_fallback`` (executor + failure-reader injectable for tests).
- Sidecar job state machine: ``StaplesPrewarmStore`` lives in the SAME sqlite
  file as ``CustomerPrewarmStore`` (own connections, same repo conventions)
  and tracks PENDING/RUNNING/RETRY_WAIT/SUCCESS/FAILED plus
  LOCAL_SUCCESS_CACHE_PENDING (crawl succeeded, cache write deferred).
- Bounded retry: 1m/2m/5m-cap backoff for transient failures; deterministic
  product facts (商品不存在 / 商品已下架 / URL 格式错误 / 不支持的商城域名)
  are FINAL.  Manual retry stays LOCAL staples only.
- Cache-sync retry: a deferred success payload is re-written to the shared
  cache on later passes without touching the crawler.
- Plan-only read-only helpers: ``staples_plan_readonly`` opens both databases
  in sqlite ``mode=ro`` (never constructs store classes, never creates files).

The stbchina ``uid`` comes from a URL ``itemId`` regex inside the crawler, so
it is URL-derived evidence: stored as ``URL_PARSED_UNVERIFIED`` at best and
never ``CONFIRMED``.  A zero/absent price is never treated as a success.
"""

from __future__ import annotations

import importlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import quote, urlparse

from app.services.crawl_url_identity import (
    SKU_STATUS_UNRESOLVED,
    SKU_STATUS_URL_PARSED_UNVERIFIED,
    is_placeholder_code,
    stbchina_product_identity,
)
from app.services.customer_prewarm_store import (
    CACHE_STATUS_CACHE_HIT,
    CACHE_STATUS_PENDING_PREWARM,
    CACHE_STATUS_STAPLES_PENDING,
)
from app.services.shared_crawl_cache import PRODUCER_STAPLES_LOCAL, AliasConflictError

# 既有权威 mall key(cnnc_same_product_import/config.py:186-187)。
STAPLES_CACHE_MALL_KEY = "stbchina"
STAPLES_PREWARM_SOURCE = "staples-local"

STAPLES_JOB_STATE_PENDING = "PENDING"
STAPLES_JOB_STATE_RUNNING = "RUNNING"
STAPLES_JOB_STATE_RETRY_WAIT = "RETRY_WAIT"
STAPLES_JOB_STATE_SUCCESS = "SUCCESS"
STAPLES_JOB_STATE_FAILED = "FAILED"
STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING = "LOCAL_SUCCESS_CACHE_PENDING"
STAPLES_JOB_STATES = frozenset(
    {
        STAPLES_JOB_STATE_PENDING,
        STAPLES_JOB_STATE_RUNNING,
        STAPLES_JOB_STATE_RETRY_WAIT,
        STAPLES_JOB_STATE_SUCCESS,
        STAPLES_JOB_STATE_FAILED,
        STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING,
    }
)

# 爬虫确定性商品事实(crawlers/base_result.py FailReason);其余一律瞬时。
STAPLES_FINAL_FAILURE_REASONS = (
    "商品不存在",
    "商品已下架",
    "URL 格式错误",
    "不支持的商城域名",
)

STAPLES_OUTCOME_SUCCESS = "SUCCESS"
STAPLES_OUTCOME_TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
STAPLES_OUTCOME_FINAL_FAILURE = "FINAL_FAILURE"

_DEFAULT_LEASE_SECONDS = 1500


# ---- pure helpers ------------------------------------------------------------


def is_staples_url(url: Any) -> bool:
    """True only when the URL hostname is ``stbchina.cn`` or ``*.stbchina.cn``.

    Hostname comparison only (never substring matching); never raises.
    """
    try:
        host = (urlparse(str(url or "")).hostname or "").strip().lower()
    except ValueError:  # pragma: no cover - malformed URL, conservative MISS
        return False
    return host == "stbchina.cn" or host.endswith(".stbchina.cn")


def classify_staples_failure(reason_text: Any) -> str:
    """``"FINAL"`` for deterministic product facts, else ``"TRANSIENT"``.

    Only the primary token (before any ``;`` / ``；`` separator) is compared;
    it must exactly equal or start with one of ``STAPLES_FINAL_FAILURE_REASONS``.
    Empty/None/unparseable input is TRANSIENT.
    """
    if not isinstance(reason_text, str):
        return "TRANSIENT"
    primary = re.split(r"[;；]", reason_text, maxsplit=1)[0].strip()
    if not primary:
        return "TRANSIENT"
    for reason in STAPLES_FINAL_FAILURE_REASONS:
        if primary == reason or primary.startswith(reason):
            return "FINAL"
    return "TRANSIENT"


def staples_retry_backoff_seconds(consecutive_failures: int) -> int:
    """Bounded retry schedule: 1m for the first, 2m second, then 5m cap."""
    if consecutive_failures <= 1:
        return 60
    if consecutive_failures == 2:
        return 120
    return 300


# 瞬时失败的连续次数预算:60/120/300/300 节奏下 4 次≈8 分钟窗口即稳定 FAILED,
# 绝不允许 transient→RETRY_WAIT 无限循环(Playwright 成本考量,Issue #11 Blocker A)。
STAPLES_MAX_CONSECUTIVE_TRANSIENT_FAILURES = 4


def next_transient_failure_state(
    consecutive_failures: int, diagnostic: str, now: datetime
) -> dict[str, Any]:
    """一次瞬时失败后的下一个状态判定(全部 transient 路径的唯一决策点,纯函数)。

    返回 dict:RETRY_WAIT(带 next_retry_at,store 自增 consecutive_failures)或
    FAILED(reason 形如 ``staples retry exhausted after N attempts: <diagnostic>``,
    同时含 "retry exhausted" 与原 diagnostic;store 保持 consecutive_failures)。
    """
    diagnostic_text = str(diagnostic or "").strip()
    budget = int(STAPLES_MAX_CONSECUTIVE_TRANSIENT_FAILURES)
    if int(consecutive_failures) + 1 >= budget:
        return {
            "state": STAPLES_JOB_STATE_FAILED,
            "reason": f"staples retry exhausted after {budget} attempts: {diagnostic_text}",
            "next_retry_at": None,
        }
    return {
        "state": STAPLES_JOB_STATE_RETRY_WAIT,
        "reason": diagnostic_text,
        "next_retry_at": now
        + timedelta(seconds=staples_retry_backoff_seconds(int(consecutive_failures) + 1)),
    }


@dataclass(frozen=True)
class StaplesCrawlOutcome:
    """One crawled URL's outcome, with the URL chain preserved independently.

    ``crawler_uid_raw`` / ``crawler_comment_raw`` carry the wrapper's raw
    values so evidence survives even when the uid is rejected as a
    placeholder (kept verbatim in the cache-row payload).
    """

    status: str  # SUCCESS / TRANSIENT_FAILURE / FINAL_FAILURE
    original_url: str
    submitted_url: str
    returned_url: str | None
    canonical_url: str | None
    title: str | None
    price: float | None
    comment_count: int | None
    real_sku_id: str | None
    sku_status: str
    diagnostic: str = ""
    crawler_uid_raw: str = ""
    crawler_comment_raw: str = ""


def read_stbchina_failure_reasons(path: str | Path) -> dict[str, str]:
    """失败原因 map (商品链接 -> 失败原因) for non-成功 rows of the result XLSX.

    Default ``failure_reader`` implementation.  Reads sheet1 with openpyxl
    read_only (columns 商品链接/采集状态/失败原因); 成功 rows and rows without
    a 商品链接 are skipped.  A missing/unreadable file yields ``{}``.
    """
    import openpyxl  # lazy: importing this module must not cost openpyxl

    result_path = Path(path)
    reasons: dict[str, str] = {}
    if not result_path.is_file():
        return reasons
    try:
        workbook = openpyxl.load_workbook(result_path, data_only=True, read_only=True)
    except Exception:  # noqa: BLE001 - unreadable result file, no reasons
        return reasons
    try:
        sheet = workbook.active
        headers = [str(cell.value or "").strip() for cell in sheet[1]]
        columns = {header: index for index, header in enumerate(headers)}
        if "商品链接" not in columns or "采集状态" not in columns:
            return reasons
        reason_col = columns.get("失败原因")
        for row in sheet.iter_rows(min_row=2, values_only=True):
            url = str(row[columns["商品链接"]] or "").strip()
            if not url or str(row[columns["采集状态"]] or "").strip() == "成功":
                continue
            reasons[url] = (
                str(row[reason_col] or "").strip() if reason_col is not None else ""
            )
    finally:
        workbook.close()
    return reasons


class StaplesLocalCrawler:
    """窄适配器:仅依赖 ``run_stbchina_fallback`` 契约的本地史泰博爬取封装。

    Executor contract (default: ``run_stbchina_fallback``):
    ``executor(urls, work_dir) -> dict[url, {"price", "title", "uid", "comment"}]``.
    Failure-reader contract (default: ``read_stbchina_failure_reasons``):
    ``failure_reader(xlsx_path) -> dict[url, str]`` (失败原因 of non-成功 rows).
    """

    def __init__(
        self,
        *,
        executor: Callable[[Sequence[str], Path], dict[str, dict[str, Any]]] | None = None,
        failure_reader: Callable[[Path], dict[str, str]] | None = None,
        work_dir: str | Path = "staples_prewarm",
    ) -> None:
        self.work_dir = Path(work_dir)
        self._executor = executor
        self._failure_reader = failure_reader
        self._monitor_module: Any = None

    def _default_executor(self) -> Callable[[Sequence[str], Path], dict[str, dict[str, Any]]]:
        if self._executor is not None:
            return self._executor
        # Lazy import: importing staples_prewarm must never cost openpyxl.
        if self._monitor_module is None:
            # importlib.import_module resolves via sys.modules FIRST (unlike
            # ``from scripts import ...``, which prefers an attribute already
            # bound on the package), so test injection is deterministic.
            self._monitor_module = importlib.import_module("app.integrations.monitor_stb_fallback")
        return self._monitor_module.run_stbchina_fallback

    def _default_failure_reader(self) -> Callable[[Path], dict[str, str]]:
        if self._failure_reader is not None:
            return self._failure_reader
        return read_stbchina_failure_reasons

    def _result_xlsx(self, attempt_dir: Path | None = None) -> Path:
        """结果档路径:crawl 内一律传本次 attempt 目录(读新鲜结果,不读 base 旧档);
        缺省回退 base work_dir,仅为向后兼容直接调用。"""
        return (attempt_dir or self.work_dir) / "stbchina_missing_result.xlsx"

    def crawl(self, requests: Sequence[Mapping[str, Any]]) -> dict[str, StaplesCrawlOutcome]:
        """Crawl one batch, keyed by ``original_url``.  Never raises."""
        urls: list[str] = []
        seen: set[str] = set()
        for request in requests or ():
            url = str((request or {}).get("original_url") or "").strip()
            if not url or not is_staples_url(url) or url in seen:
                continue
            seen.add(url)
            urls.append(url)
        if not urls:
            return {}
        executor = self._default_executor()
        # 每次批量爬取使用独立 attempt 目录:legacy wrapper 以"结果档已存在且
        # 覆盖全部请求 URL"为短路条件(monitor_stb_fallback.run_stbchina_fallback),
        # 共享 base 目录会让旧档吞掉新请求(Issue #11 Blocker C)。不清理,留取证。
        attempt_dir = self.work_dir / "attempts" / uuid.uuid4().hex
        attempt_dir.mkdir(parents=True, exist_ok=True)
        try:
            records = executor(urls, attempt_dir) or {}
        except Exception as exc:  # noqa: BLE001 - whole batch transient (Case 10)
            # 爬虫不可用/脚本失败都是瞬时:绝不因异常类名判 FINAL。
            diagnostic = str(exc).strip() or type(exc).__name__
            return {
                url: self._failure_outcome(url, STAPLES_OUTCOME_TRANSIENT_FAILURE, diagnostic)
                for url in urls
            }
        outcomes: dict[str, StaplesCrawlOutcome] = {}
        missing: list[str] = []
        for url in urls:
            record = records.get(url)
            if not isinstance(record, Mapping):
                missing.append(url)
                continue
            outcomes[url] = self._outcome_from_record(url, record)
        if missing:
            reasons: dict[str, str] = {}
            try:
                # 注入的 reader 也可能不依赖结果文件;默认实现自身处理缺文件。
                # 读本次 attempt 目录:base 目录旧档绝不参与本次判定(Blocker C2)。
                reasons = (
                    self._default_failure_reader()(self._result_xlsx(attempt_dir)) or {}
                )
            except Exception:  # noqa: BLE001 - unreadable reasons -> transient
                reasons = {}
            for url in missing:
                reason = str(reasons.get(url) or "").strip()
                if classify_staples_failure(reason) == "FINAL":
                    outcomes[url] = self._failure_outcome(
                        url, STAPLES_OUTCOME_FINAL_FAILURE, reason
                    )
                else:
                    outcomes[url] = self._failure_outcome(
                        url,
                        STAPLES_OUTCOME_TRANSIENT_FAILURE,
                        reason or "采集失败，无原因记录",
                    )
        return outcomes

    def _failure_outcome(self, url: str, status: str, diagnostic: str) -> StaplesCrawlOutcome:
        return StaplesCrawlOutcome(
            status=status,
            original_url=url,
            submitted_url=url,
            returned_url=None,
            canonical_url=None,
            title=None,
            price=None,
            comment_count=None,
            real_sku_id=None,
            sku_status=SKU_STATUS_UNRESOLVED,
            diagnostic=diagnostic,
        )

    def _outcome_from_record(self, url: str, record: Mapping[str, Any]) -> StaplesCrawlOutcome:
        try:
            price = float(record.get("price"))
        except (TypeError, ValueError):
            price = None
        if price is None or price <= 0:
            # 0/缺价不是成功价:绝不写成功缓存(与 STEP1 PRICE_IS_ZERO 同判)。
            return self._failure_outcome(url, STAPLES_OUTCOME_TRANSIENT_FAILURE, "价格不可见")
        raw_uid = str(record.get("uid") or "").strip()
        # stbchina uid 来自 URL itemId 正则,属 URL 证据:最多 URL_PARSED_UNVERIFIED,
        # 绝不 CONFIRMED;占位符 uid 一律丢弃。
        if raw_uid and not is_placeholder_code(raw_uid):
            real_sku_id: str | None = raw_uid
            sku_status = SKU_STATUS_URL_PARSED_UNVERIFIED
        else:
            real_sku_id = None
            sku_status = SKU_STATUS_UNRESOLVED
        raw_comment = str(record.get("comment") or "").strip()
        digits = re.sub(r"[^0-9]", "", raw_comment)
        comment_count = int(digits) if digits else None
        title_value = str(record.get("title") or "").strip() or None
        return StaplesCrawlOutcome(
            status=STAPLES_OUTCOME_SUCCESS,
            original_url=url,
            submitted_url=url,
            returned_url=url,
            canonical_url=None,
            title=title_value,
            price=price,
            comment_count=comment_count,
            real_sku_id=real_sku_id,
            sku_status=sku_status,
            crawler_uid_raw=raw_uid,
            crawler_comment_raw=raw_comment,
        )


# ---- sidecar job store ---------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS staples_prewarm_jobs (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL UNIQUE REFERENCES prewarm_candidates(id),
    state TEXT NOT NULL DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    claim_token TEXT,
    lease_expires_at TEXT,
    manual_retry_requested_at TEXT,
    success_payload_json TEXT,
    cache_result_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staples_prewarm_attempts (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES staples_prewarm_jobs(id),
    attempt_no INTEGER NOT NULL,
    submitted_url TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    reason TEXT
);
"""

_CLAIM_QUERY = """
SELECT * FROM staples_prewarm_jobs
WHERE state = 'PENDING'
   OR (state = 'RETRY_WAIT' AND (next_retry_at IS NULL OR next_retry_at <= ?))
   OR (state = 'RUNNING' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
ORDER BY id
LIMIT ?
"""


class StaplesPrewarmStore:
    """Sidecar 状态存储:与 CustomerPrewarmStore 同一 sqlite 文件。

    连接永不缓存;``PRAGMA foreign_keys=ON`` / ``busy_timeout=5000`` / WAL;
    写入走短 ``BEGIN IMMEDIATE`` 事务;读取走短连接(与仓库约定一致)。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        return connection

    @contextmanager
    def _transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._transaction(immediate=True) as connection:
            connection.executescript(_SCHEMA)

    # ---- shared helpers ------------------------------------------------------

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(UTC).isoformat(timespec="microseconds")

    @staticmethod
    def _timestamp(value: datetime | str | None) -> str | None:
        """Normalize an injectable ``now``/timestamp to store ISO-8601 UTC text."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat(timespec="microseconds")
        stripped = str(value).strip()
        return stripped or None

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    # ---- public API ------------------------------------------------------------

    def ensure_job(self, candidate_id: int, *, now: datetime | None = None) -> dict[str, Any]:
        """Insert-if-absent (state PENDING); never mutates an existing row."""
        now_text = self._timestamp(now) or self._utcnow()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM staples_prewarm_jobs WHERE candidate_id=?",
                (int(candidate_id),),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO staples_prewarm_jobs
                       (candidate_id, state, attempt_count, consecutive_failures,
                        created_at, updated_at)
                       VALUES (?, 'PENDING', 0, 0, ?, ?)""",
                    (int(candidate_id), now_text, now_text),
                )
                row = connection.execute(
                    "SELECT * FROM staples_prewarm_jobs WHERE candidate_id=?",
                    (int(candidate_id),),
                ).fetchone()
            return dict(row) if row is not None else {}

    def job_for_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM staples_prewarm_jobs WHERE candidate_id=?",
                (int(candidate_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def jobs(self, state: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM staples_prewarm_jobs"
        params: list[Any] = []
        if state is not None:
            query += " WHERE state=?"
            params.append(state)
        query += " ORDER BY id"
        with self._reader() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def claim_ready_jobs(
        self,
        limit: int,
        *,
        now: datetime,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        submitted_url_of: Callable[[int], str] | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically claim up to ``limit`` runnable jobs for one worker.

        Claimable: PENDING; RETRY_WAIT whose ``next_retry_at`` has passed;
        RUNNING with an expired lease (crashed-worker reclaim).  Each claim
        bumps ``attempt_count``, stamps a fresh ``claim_token`` and lease, and
        opens an attempts row.  ``submitted_url_of`` optionally supplies the
        candidate's original_url for the attempts row (best-effort).
        """
        if int(limit) <= 0:
            return []
        now_value = now.astimezone(UTC)
        now_text = now_value.isoformat(timespec="microseconds")
        lease_expires_at = (
            now_value + timedelta(seconds=int(lease_seconds))
        ).isoformat(timespec="microseconds")
        claimed: list[dict[str, Any]] = []
        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                _CLAIM_QUERY, (now_text, now_text, int(limit))
            ).fetchall()
            for row in rows:
                job_id = int(row["id"])
                token = uuid.uuid4().hex
                attempt_no = int(row["attempt_count"]) + 1
                connection.execute(
                    """UPDATE staples_prewarm_jobs
                       SET state='RUNNING', attempt_count=?, claim_token=?,
                           lease_expires_at=?, next_retry_at=NULL, updated_at=?
                       WHERE id=?""",
                    (attempt_no, token, lease_expires_at, now_text, job_id),
                )
                submitted_url = ""
                if submitted_url_of is not None:
                    try:
                        submitted_url = str(submitted_url_of(int(row["candidate_id"])) or "")
                    except Exception:  # noqa: BLE001 - attempts row is best-effort
                        submitted_url = ""
                connection.execute(
                    """INSERT INTO staples_prewarm_attempts
                       (job_id, attempt_no, submitted_url, started_at)
                       VALUES (?, ?, ?, ?)""",
                    (job_id, attempt_no, submitted_url, now_text),
                )
                refreshed = connection.execute(
                    "SELECT * FROM staples_prewarm_jobs WHERE id=?", (job_id,)
                ).fetchone()
                claimed.append(dict(refreshed))
        return claimed

    def complete_job(
        self,
        job_id: int,
        claim_token: str | None,
        *,
        state: str,
        reason: str | None = None,
        next_retry_at: datetime | str | None = None,
        success_payload: Mapping[str, Any] | None = None,
        cache_result_id: int | None = None,
        attempt_outcome: str | None = None,
        attempt_reason: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Complete a claimed job; fail closed on a stale claim.

        ``claim_token=None`` means "must currently be unclaimed
        (``claim_token IS NULL``)" and is used only for claim-free
        transitions (cache-hit absorption, LOCAL_SUCCESS_CACHE_PENDING sync).
        A worker whose lease expired and was reclaimed must not complete:
        its token no longer matches and :class:`ValueError` is raised.
        """
        if state not in STAPLES_JOB_STATES:
            raise ValueError(
                f"state must be one of {sorted(STAPLES_JOB_STATES)}, got {state!r}"
            )
        now_text = self._timestamp(now) or self._utcnow()
        reason_value = self._optional_text(reason)
        next_retry_value = self._timestamp(next_retry_at)
        payload_json = (
            json.dumps(
                dict(success_payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if success_payload is not None
            else None
        )
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM staples_prewarm_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            if row is None:
                raise ValueError(f"staples job {int(job_id)} does not exist")
            current_token = row["claim_token"]
            if claim_token is None:
                if current_token is not None:
                    raise ValueError(f"stale claim on staples job {int(job_id)}")
            elif current_token != claim_token:
                raise ValueError(f"stale claim on staples job {int(job_id)}")
            consecutive = int(row["consecutive_failures"])
            if state == STAPLES_JOB_STATE_RETRY_WAIT:
                consecutive += 1
            elif state in (STAPLES_JOB_STATE_PENDING, STAPLES_JOB_STATE_SUCCESS):
                consecutive = 0
            connection.execute(
                """UPDATE staples_prewarm_jobs
                   SET state=?, last_error=?, next_retry_at=?, claim_token=NULL,
                       lease_expires_at=NULL, consecutive_failures=?,
                       success_payload_json=COALESCE(?, success_payload_json),
                       cache_result_id=?, updated_at=?
                   WHERE id=?""",
                (
                    state,
                    reason_value,
                    next_retry_value,
                    consecutive,
                    payload_json,
                    cache_result_id,
                    now_text,
                    int(job_id),
                ),
            )
            # Close the attempts row opened by the matching claim (no-op when
            # the job was never claimed, attempt_no=0).
            connection.execute(
                """UPDATE staples_prewarm_attempts
                   SET finished_at=?, outcome=?, reason=?
                   WHERE job_id=? AND attempt_no=?""",
                (
                    now_text,
                    self._optional_text(attempt_outcome),
                    self._optional_text(attempt_reason),
                    int(job_id),
                    int(row["attempt_count"]),
                ),
            )
            refreshed = connection.execute(
                "SELECT * FROM staples_prewarm_jobs WHERE id=?", (int(job_id),)
            ).fetchone()
            return dict(refreshed)

    def request_staples_retry(self, candidate_id: int, *, now: datetime | None = None) -> dict[str, Any]:
        """手动重试:仅 FAILED 作业可回到 PENDING(§19,只走史泰博本地路径)。

        清零 ``consecutive_failures``(重新给满瞬时失败预算),保留
        ``attempt_count`` 累计历史;否则耗尽后的 FAILED 将在第一次瞬时失败
        时立刻再次 FAILED(Blocker A3)。
        """
        now_text = self._timestamp(now) or self._utcnow()
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM staples_prewarm_jobs WHERE candidate_id=?",
                (int(candidate_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"no staples job for candidate {int(candidate_id)}")
            if row["state"] != STAPLES_JOB_STATE_FAILED:
                raise ValueError(
                    f"staples job for candidate {int(candidate_id)} is "
                    f"{row['state']!r}; only FAILED jobs can be manually retried"
                )
            connection.execute(
                """UPDATE staples_prewarm_jobs
                   SET state='PENDING', next_retry_at=NULL, consecutive_failures=0,
                       manual_retry_requested_at=?, claim_token=NULL,
                       lease_expires_at=NULL, updated_at=?
                   WHERE id=?""",
                (now_text, now_text, int(row["id"])),
            )
            refreshed = connection.execute(
                "SELECT * FROM staples_prewarm_jobs WHERE id=?", (int(row["id"]),)
            ).fetchone()
            return dict(refreshed)

    def stats(self) -> dict[str, int]:
        """Job counts by state."""
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS n FROM staples_prewarm_jobs GROUP BY state"
            ).fetchall()
        return {row["state"]: int(row["n"]) for row in rows}


# ---- shared write/flip helpers -------------------------------------------------


def _staples_success_payload(
    outcome: StaplesCrawlOutcome, *, candidate_id: int, job_id: int, attempt_no: int
) -> dict[str, Any]:
    """Normalized evidence dict; doubles as the cache-row payload."""
    return {
        "candidate_id": int(candidate_id),
        "job_id": int(job_id),
        "attempt_no": int(attempt_no),
        "crawler_uid_raw": outcome.crawler_uid_raw,
        "crawler_comment_raw": outcome.crawler_comment_raw,
        "submitted_url": outcome.submitted_url,
        "original_url": outcome.original_url,
        "returned_url": outcome.returned_url,
        "canonical_url": outcome.canonical_url,
        "title": outcome.title,
        "price": outcome.price,
        "comment_count": outcome.comment_count,
        "real_sku_id": outcome.real_sku_id,
        "sku_status": outcome.sku_status,
        "url_evidence": {
            "original": outcome.original_url,
            "submitted": outcome.submitted_url,
            "returned": outcome.returned_url,
            "canonical": outcome.canonical_url,
        },
    }


def _write_staples_success(
    shared_cache: Any, evidence: Mapping[str, Any]
) -> dict[str, Any]:
    """Write one staples success to the shared cache from normalized evidence."""
    return shared_cache.record_success(
        STAPLES_CACHE_MALL_KEY,
        str(evidence["submitted_url"]),
        original_url=evidence.get("original_url"),
        returned_url=evidence.get("returned_url"),
        canonical_url=evidence.get("canonical_url"),
        crawl_id=None,
        real_sku_id=evidence.get("real_sku_id"),
        sku_status=evidence.get("sku_status") or SKU_STATUS_UNRESOLVED,
        title=evidence.get("title"),
        price=evidence.get("price"),
        comment_count=evidence.get("comment_count"),
        source=STAPLES_PREWARM_SOURCE,
        payload=dict(evidence),
        producer=PRODUCER_STAPLES_LOCAL,
    )


def _flip_candidate_to_cache_hit(
    prewarm_store: Any, shared_cache: Any, candidate: Mapping[str, Any], hit: Mapping[str, Any]
) -> list[str]:
    """Record CACHE_HIT and attach every candidate document; returns errors."""
    errors: list[str] = []
    candidate_id = int(candidate["id"])
    try:
        prewarm_store.record_candidate_result(
            candidate_id,
            cache_status=CACHE_STATUS_CACHE_HIT,
            match_level=hit.get("match_level"),
            resolved_identity_id=hit.get("identity_id"),
        )
    except Exception as exc:  # noqa: BLE001 - collected, never raised
        errors.append(f"candidate {candidate_id} cache-hit flip: {exc}")
    for link in prewarm_store.links_for_candidate(candidate_id):
        try:
            shared_cache.attach_document_reference(
                link["document_id"],
                mall=STAPLES_CACHE_MALL_KEY,
                url=candidate["original_url"],
                source=link.get("source") or "detail",
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, collected
            errors.append(
                f"candidate {candidate_id} attach document "
                f"{link.get('document_id')}: {exc}"
            )
    return errors


# ---- orchestration --------------------------------------------------------------


def run_staples_prewarm(
    *,
    prewarm_store: Any,
    shared_cache: Any,
    staples_store: Any,
    crawler: Any,
    work_dir: str | Path,
    route_limit: int = 200,
    max_jobs: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One staples pass: route -> cache recheck -> cache-sync retry -> crawl.

    Never raises on per-item failures (collected into ``errors``); only
    constructor-level failures propagate.  Order matters: the cache is
    rechecked BEFORE any crawl (§10) and the deferred-success cache sync runs
    BEFORE claiming new crawl work (§26), so freshly-pending syncs do not
    wait an extra pass.
    """
    now_value = (now if now is not None else datetime.now(UTC)).astimezone(UTC)
    summary: dict[str, Any] = {
        "routed": 0,
        "recheck_hits": 0,
        "cache_sync_succeeded": 0,
        "cache_sync_pending": 0,
        "jobs_claimed": 0,
        "crawl_success": 0,
        "cache_hit_after_crawl": 0,
        "transient_failures": 0,
        "final_failures": 0,
        "errors": [],
    }

    def _record_transient(job: Mapping[str, Any], diagnostic: str) -> None:
        """瞬时失败的唯一完成路径:预算内 RETRY_WAIT,第 N 次 FAILED(终态)。"""
        decision = next_transient_failure_state(
            int(job["consecutive_failures"]), diagnostic, now_value
        )
        staples_store.complete_job(
            int(job["id"]),
            job["claim_token"],
            state=decision["state"],
            reason=decision["reason"],
            next_retry_at=decision["next_retry_at"],
            attempt_outcome="TRANSIENT_FAILURE",
            now=now_value,
        )
        if decision["state"] == STAPLES_JOB_STATE_RETRY_WAIT:
            summary["transient_failures"] += 1
        else:
            summary["final_failures"] += 1

    # ---- Phase 1: route PENDING_PREWARM staples candidates to the local path.
    # route_limit 约束本 pass「被选中的 eligible Staples 候选数」(选择预算):
    # 候选一旦被选中即消耗预算,即使随后的路由失败;非 Staples 候选绝不消耗
    # 预算,否则按 id 序排在前面的非 Staples 候选(永远保持 PENDING_PREWARM)
    # 会让后面的 Staples 候选永久饿死(Issue #11)。路由失败的候选下一 pass
    # 仍是 PENDING_PREWARM,会再次参与选择。
    eligible_examined = 0
    for candidate in prewarm_store.candidates(CACHE_STATUS_PENDING_PREWARM, limit=None):
        url = str(candidate.get("original_url") or "").strip()
        if not is_staples_url(url):
            continue
        if eligible_examined >= int(route_limit):
            break
        eligible_examined += 1
        try:
            prewarm_store.record_candidate_result(
                int(candidate["id"]), cache_status=CACHE_STATUS_STAPLES_PENDING
            )
            staples_store.ensure_job(int(candidate["id"]), now=now_value)
            summary["routed"] += 1
        except Exception as exc:  # noqa: BLE001 - collected, never raised
            summary["errors"].append(f"route candidate {candidate.get('id')}: {exc}")

    # ---- Phase 2: cache recheck BEFORE any crawl (§10).
    for candidate in prewarm_store.candidates(CACHE_STATUS_STAPLES_PENDING):
        candidate_id = int(candidate["id"])
        try:
            staples_store.ensure_job(candidate_id, now=now_value)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"ensure job candidate {candidate_id}: {exc}")
        try:
            hit = shared_cache.lookup_current(STAPLES_CACHE_MALL_KEY, candidate["original_url"])
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"recheck candidate {candidate_id}: {exc}")
            continue
        if hit is None:
            continue
        summary["errors"].extend(
            _flip_candidate_to_cache_hit(prewarm_store, shared_cache, candidate, hit)
        )
        try:
            job = staples_store.job_for_candidate(candidate_id)
            if job is not None and job["state"] not in (
                STAPLES_JOB_STATE_SUCCESS,
                STAPLES_JOB_STATE_FAILED,
            ):
                # claim_token=None: absorb only when currently unclaimed.
                staples_store.complete_job(
                    int(job["id"]),
                    None,
                    state=STAPLES_JOB_STATE_SUCCESS,
                    reason="cache-hit-absorbed",
                    cache_result_id=int(hit["entry"]["id"]),
                    now=now_value,
                )
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"absorb job candidate {candidate_id}: {exc}")
        summary["recheck_hits"] += 1

    # ---- Phase 3: cache-sync retry for deferred successes (§26), crawler not involved.
    for job in staples_store.jobs(STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING):
        job_id = int(job["id"])
        candidate_id = int(job["candidate_id"])
        try:
            evidence_raw = json.loads(job.get("success_payload_json") or "")
            if not isinstance(evidence_raw, Mapping):
                raise ValueError("success payload is not an object")
            _write_staples_success(shared_cache, evidence_raw)
        except sqlite3.OperationalError as exc:
            summary["cache_sync_pending"] += 1
            summary["errors"].append(f"cache-sync job {job_id}: {exc}")
            continue
        except (ValueError, AliasConflictError) as exc:
            try:
                staples_store.complete_job(
                    job_id,
                    None,
                    state=STAPLES_JOB_STATE_FAILED,
                    reason=f"cache sync rejected: {exc}",
                    now=now_value,
                )
            except Exception as inner:  # noqa: BLE001
                summary["errors"].append(f"cache-sync fail job {job_id}: {inner}")
            continue
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"cache-sync job {job_id}: {exc}")
            continue
        try:
            hit = shared_cache.lookup_current(
                STAPLES_CACHE_MALL_KEY, str(evidence_raw["original_url"])
            )
        except Exception as exc:  # noqa: BLE001
            # §15:SUCCESS 只认真实命中;lookup 瞬断 → 保持待同步,下一 pass 重试。
            summary["cache_sync_pending"] += 1
            summary["errors"].append(f"cache-sync lookup job {job_id}: {exc}")
            continue
        if hit is None:
            # 写后 lookup 不到:绝不谎报 SUCCESS;保持待同步(success_payload 经
            # COALESCE 原样保留)并留下诊断,下一 pass 重新补同步。
            try:
                staples_store.complete_job(
                    job_id,
                    None,
                    state=STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING,
                    reason="cache lookup missed after write",
                    now=now_value,
                )
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"cache-sync keep-pending job {job_id}: {exc}")
                continue
            summary["cache_sync_pending"] += 1
            continue
        candidate = prewarm_store.candidate_by_id(candidate_id)
        if candidate is not None:
            summary["errors"].extend(
                _flip_candidate_to_cache_hit(prewarm_store, shared_cache, candidate, hit)
            )
        try:
            staples_store.complete_job(
                job_id,
                None,
                state=STAPLES_JOB_STATE_SUCCESS,
                cache_result_id=int(hit["entry"]["id"]),
                now=now_value,
            )
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"cache-sync complete job {job_id}: {exc}")
            continue
        summary["cache_sync_succeeded"] += 1

    # ---- Phase 4: bounded local crawl (one batch call).
    jobs = staples_store.claim_ready_jobs(
        int(max_jobs),
        now=now_value,
        submitted_url_of=lambda cid: (
            (prewarm_store.candidate_by_id(cid) or {}).get("original_url") or ""
        ),
    )
    summary["jobs_claimed"] = len(jobs)
    if not jobs:
        return summary

    work_path = Path(work_dir)
    work_path.mkdir(parents=True, exist_ok=True)

    prepared: list[tuple[Mapping[str, Any], str]] = []
    crawl_requests: list[dict[str, Any]] = []
    for job in jobs:
        candidate = prewarm_store.candidate_by_id(int(job["candidate_id"]))
        if candidate is None:
            try:
                staples_store.complete_job(
                    int(job["id"]),
                    job["claim_token"],
                    state=STAPLES_JOB_STATE_FAILED,
                    reason="candidate missing",
                    attempt_outcome="FINAL_FAILURE",
                    now=now_value,
                )
                summary["final_failures"] += 1
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"orphan job {job['id']}: {exc}")
            continue
        url = str(candidate.get("original_url") or "").strip()
        prepared.append((job, url))
        crawl_requests.append({"candidate_id": int(job["candidate_id"]), "original_url": url})

    try:
        outcomes = crawler.crawl(crawl_requests) or {}
    except Exception as exc:  # noqa: BLE001 - one broken crawler never kills the pass
        outcomes = {}
        crawler_error = f"crawler crashed: {exc}"
        for job, url in prepared:
            try:
                _record_transient(job, crawler_error)
            except Exception as inner:  # noqa: BLE001
                summary["errors"].append(f"crawler-crash job {job['id']}: {inner}")
        return summary

    for job, url in prepared:
        job_id = int(job["id"])
        candidate_id = int(job["candidate_id"])
        outcome = outcomes.get(url)
        try:
            if outcome is None:
                _record_transient(job, "outcome missing")
            elif outcome.status == STAPLES_OUTCOME_SUCCESS:
                evidence = _staples_success_payload(
                    outcome,
                    candidate_id=candidate_id,
                    job_id=job_id,
                    attempt_no=int(job["attempt_count"]),
                )
                try:
                    _write_staples_success(shared_cache, evidence)
                except sqlite3.OperationalError as exc:
                    # 缓存写瞬断:证据已保存,作业转本地待同步,候选保持 STAPLES_PENDING。
                    staples_store.complete_job(
                        job_id,
                        job["claim_token"],
                        state=STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING,
                        success_payload=evidence,
                        attempt_outcome="SUCCESS",
                        reason=f"cache write transient: {exc}",
                        now=now_value,
                    )
                    summary["crawl_success"] += 1
                    continue
                except (ValueError, AliasConflictError) as exc:
                    # 诚实的确定性死路:本地路径终态 FAILED,永不改道招商局。
                    staples_store.complete_job(
                        job_id,
                        job["claim_token"],
                        state=STAPLES_JOB_STATE_FAILED,
                        reason=f"cache write rejected: {exc}",
                        attempt_outcome="SUCCESS",
                        now=now_value,
                    )
                    summary["final_failures"] += 1
                    continue
                # §15: 只有真实命中才能翻转候选——写完必须 lookup 到。
                try:
                    hit = shared_cache.lookup_current(
                        STAPLES_CACHE_MALL_KEY, outcome.original_url
                    )
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"post-crawl lookup job {job_id}: {exc}")
                    hit = None
                if hit is None:
                    staples_store.complete_job(
                        job_id,
                        job["claim_token"],
                        state=STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING,
                        success_payload=evidence,
                        attempt_outcome="SUCCESS",
                        reason="cache lookup missed after write",
                        now=now_value,
                    )
                    summary["crawl_success"] += 1
                    continue
                candidate = prewarm_store.candidate_by_id(candidate_id)
                if candidate is not None:
                    summary["errors"].extend(
                        _flip_candidate_to_cache_hit(prewarm_store, shared_cache, candidate, hit)
                    )
                staples_store.complete_job(
                    job_id,
                    job["claim_token"],
                    state=STAPLES_JOB_STATE_SUCCESS,
                    cache_result_id=int(hit["entry"]["id"]),
                    success_payload=evidence,
                    attempt_outcome="SUCCESS",
                    now=now_value,
                )
                summary["crawl_success"] += 1
                summary["cache_hit_after_crawl"] += 1
            elif outcome.status == STAPLES_OUTCOME_TRANSIENT_FAILURE:
                _record_transient(job, outcome.diagnostic)
            else:
                staples_store.complete_job(
                    job_id,
                    job["claim_token"],
                    state=STAPLES_JOB_STATE_FAILED,
                    reason=outcome.diagnostic,
                    attempt_outcome="FINAL_FAILURE",
                    now=now_value,
                )
                summary["final_failures"] += 1
        except Exception as exc:  # noqa: BLE001 - e.g. stale claim, collected
            summary["errors"].append(f"complete job {job_id}: {exc}")
    return summary


# ---- plan-only read-only helpers ------------------------------------------------


def _ro_connect(path: Path) -> sqlite3.Connection:
    """Read-only sqlite connection (``mode=ro``); never creates the file."""
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _ro_scalar(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    try:
        return connection.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None


def _ro_rows(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    try:
        return connection.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _ro_cache_hit(connection: sqlite3.Connection, url: str) -> bool:
    """Read-only mini lookup mirroring SharedCrawlCache's alias->parser levels."""
    alias = _ro_scalar(
        connection,
        "SELECT identity_id FROM crawl_urls WHERE mall=? AND url=?",
        (STAPLES_CACHE_MALL_KEY, url),
    )
    identity_id = int(alias["identity_id"]) if alias is not None else None
    if identity_id is None:
        product_identity = stbchina_product_identity(url)
        if product_identity:
            identity = _ro_scalar(
                connection,
                "SELECT id FROM crawl_identities WHERE mall=? AND product_identity=?",
                (STAPLES_CACHE_MALL_KEY, product_identity),
            )
            identity_id = int(identity["id"]) if identity is not None else None
    if identity_id is None:
        return False
    current = _ro_scalar(
        connection,
        "SELECT 1 FROM crawl_results WHERE identity_id=? AND is_current=1 LIMIT 1",
        (identity_id,),
    )
    return current is not None


def _resolve_crawler_availability(crawler_available: bool | None) -> dict[str, Any]:
    """Report crawler availability; lazily imports the wrapper only when unknown."""
    module: Any = None
    if crawler_available is None:
        try:
            # Same sys.modules-first resolution as _default_executor: makes
            # monkeypatch.setitem injection deterministic; still lazy.
            module = importlib.import_module("app.integrations.monitor_stb_fallback")
        except Exception:  # noqa: BLE001 - plan mode must never crash on this
            module = None
    report: dict[str, Any] = {
        "crawler_available": bool(crawler_available) if crawler_available is not None else False,
        "crawler_script": "",
        "crawler_script_exists": False,
    }
    if module is not None:
        script = Path(getattr(module, "STB_SCRIPT", ""))
        report["crawler_script"] = str(script)
        report["crawler_script_exists"] = script.is_file()
        if crawler_available is None:
            report["crawler_available"] = report["crawler_script_exists"]
    return report


def staples_plan_readonly(
    prewarm_db_path: str | Path,
    shared_cache_db_path: str | Path,
    *,
    crawler_available: bool | None = None,
) -> dict[str, Any]:
    """TRUE read-only plan report (§28): never creates files, never constructs stores.

    Missing tables are treated as empty; missing databases are reported via
    markers (``PREWARM_STORE_MISSING`` / ``CACHE_MISSING``).
    """
    prewarm_path = Path(prewarm_db_path)
    cache_path = Path(shared_cache_db_path)
    plan: dict[str, Any] = {
        "prewarm_db": str(prewarm_path),
        "shared_cache_db": str(cache_path),
        "prewarm_store": "OK" if prewarm_path.is_file() else "PREWARM_STORE_MISSING",
        "cache": "OK" if cache_path.is_file() else "CACHE_MISSING",
        "candidates_total": 0,
        "staples_candidates": 0,
        "unique_staples_urls": 0,
        "duplicate_documents": 0,
        "cache_hit_after_recheck": 0,
        "local_retry_planned": 0,
        "local_cache_sync_pending": 0,
        "local_final_failed": 0,
        "local_crawl_planned": 0,
    }
    plan.update(_resolve_crawler_availability(crawler_available))

    staples_candidates: list[Mapping[str, Any]] = []
    job_states: dict[int, str | None] = {}
    if plan["prewarm_store"] == "OK":
        connection = None
        try:
            connection = _ro_connect(prewarm_path)
        except sqlite3.Error:
            plan["prewarm_store"] = "PREWARM_STORE_UNREADABLE"
        if connection is not None:
            try:
                total = _ro_scalar(connection, "SELECT COUNT(*) AS n FROM prewarm_candidates")
                plan["candidates_total"] = int(total["n"]) if total is not None else 0
                rows = _ro_rows(
                    connection,
                    "SELECT id, original_url, cache_status FROM prewarm_candidates "
                    "WHERE original_url LIKE '%stbchina.cn%'",
                )
                # SQL LIKE 只是预过滤;真正判定以 hostname 门为准。
                staples_candidates = [row for row in rows if is_staples_url(row["original_url"])]
                plan["staples_candidates"] = len(staples_candidates)
                plan["unique_staples_urls"] = len(
                    {row["original_url"] for row in staples_candidates}
                )
                duplicate_documents = 0
                for row in staples_candidates:
                    links = _ro_scalar(
                        connection,
                        "SELECT COUNT(DISTINCT document_id) AS n FROM prewarm_links "
                        "WHERE candidate_id=?",
                        (int(row["id"]),),
                    )
                    link_count = int(links["n"]) if links is not None else 0
                    duplicate_documents += max(0, link_count - 1)
                    job = _ro_scalar(
                        connection,
                        "SELECT state FROM staples_prewarm_jobs WHERE candidate_id=?",
                        (int(row["id"]),),
                    )
                    state = str(job["state"]) if job is not None else None
                    job_states[int(row["id"])] = state
                    if state == STAPLES_JOB_STATE_RETRY_WAIT:
                        plan["local_retry_planned"] += 1
                    elif state == STAPLES_JOB_STATE_LOCAL_SUCCESS_CACHE_PENDING:
                        plan["local_cache_sync_pending"] += 1
                    elif state == STAPLES_JOB_STATE_FAILED:
                        plan["local_final_failed"] += 1
                plan["duplicate_documents"] = duplicate_documents
            finally:
                connection.close()

    if plan["cache"] == "OK" and staples_candidates:
        connection = None
        try:
            connection = _ro_connect(cache_path)
        except sqlite3.Error:
            plan["cache"] = "CACHE_UNREADABLE"
        if connection is not None:
            try:
                hits = 0
                planned = 0
                for row in staples_candidates:
                    if _ro_cache_hit(connection, row["original_url"]):
                        hits += 1
                        continue
                    state = job_states.get(int(row["id"]))
                    if state is None or state in (
                        STAPLES_JOB_STATE_PENDING,
                        STAPLES_JOB_STATE_RETRY_WAIT,
                    ):
                        planned += 1
                plan["cache_hit_after_recheck"] = hits
                plan["local_crawl_planned"] = planned
            finally:
                connection.close()
    return plan

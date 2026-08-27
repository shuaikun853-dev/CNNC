# app/services/saas_flow_diagnostic.py
"""STEP_1 Dashboard SaaS 首次采集链路只读诊断。

契约:docs/superpowers/specs/2026-08-27-step1-saas-flow-diagnostic.md。

安全边界(极窄 SQL 例外,仅限本模块):
- 只通过指定只读账号执行本文件 DIAG_SQL_REGISTRY 内预定义、参数化 SELECT;
- tenant/pool 永久固定为 188605 / 222,不接受任何来自请求的 SQL/scope 参数;
- 绝不产生业务写操作;凭据只从进程环境变量或 ignored 的 data/CNNC_DIAG_DB.env 读取,
  不写入 Git、日志或 API response。

字段语义来源(不按字段名猜):
- docs/sql_ops KB [FRONTEND]:model_audit_result 0=互联网+非同款,1=互联网+同款,
  2=参考链接+非同款,3=参考链接+同款;sync_state 1=待审批,0=已审批-同款,2=已审批-非同款。
- docs/sql_ops KB [MANUAL]:audit_result NULL/-1/-2 为未完成,-2 同款识别中;
  s_task_info task_status 00/01/02/03、queue_status 00/01/02。
- docs/sql_ops KB [LIVE-SQL]:task_status='02'+end_time 才是完成证据(queue_status
  不必为 02);start_time 不能作为执行证据;task_table.task_status=1 不能推翻
  commodity 级 crawl_status=2。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.services.incremental_audit_runtime import (
    DEFAULT_RUNTIME_ROOT,
    SHANGHAI,
    cutoff_for_business_date,
    runtime_paths,
)

TENANT_ID = "188605"
POOL_ID = "222"

CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15
DEFAULT_GRACE_SECONDS = 1800  # callback 后暂无 search 的宽限窗口(基于入池时间近似)
DEFAULT_CACHE_TTL_SECONDS = 60.0
DEFAULT_FRESH_READ_MIN_INTERVAL_SECONDS = 30.0
MIN_CACHE_TTL_SECONDS = 60.0        # 生产要求:至少 60 秒缓存,禁止 5 秒级 DB 轮询
MIN_FRESH_READ_INTERVAL_SECONDS = 30.0  # 强制刷新间隔下限与文档/UI 声明一致,env 不可降破
MAX_CRAWL1_COMMODITIES = 2000       # 有界结果集:单日 crawl_status=1 目标商品上限
MAX_CRAWL2_COMMODITIES = 2000
MAX_CENTRAL_DETAIL_ROWS = 50        # evidence 明细展示上限

DEFAULT_DB_ENV_PATH = Path(__file__).resolve().parents[2] / "data" / "CNNC_DIAG_DB.env"

CRAWLER_BRANCH_CLASSES = (
    "TASK_NOT_CREATED",
    "TASK_CREATED_NO_CENTRAL_RECORD",
    "CENTRAL_NOT_ENQUEUED",
    "CENTRAL_ENQUEUED_WAITING",
    "CRAWLER_EXECUTING",
    "CRAWLER_FAILED",
    "CALLBACK_LAG",
    "EVIDENCE_INCOMPLETE",
)

DOWNSTREAM_BRANCH_CLASSES = (
    "CALLBACK_DONE_NO_SEARCH",
    "MODEL_PENDING",
    "INTERNET_SAME_PENDING_APPROVAL",
    "APPROVAL_OR_FINAL_CLOSURE_PENDING",
    "INTERNET_CANDIDATES_ALL_NON_SAME",
    "REFERENCE_SAME_ONLY",
    "MODEL_DONE_NO_SAME",
    "NO_INTERNET_CANDIDATE",
    "EVIDENCE_INCOMPLETE",
)

STAGE_LABELS_ZH = {
    "TASK_NOT_CREATED": "未生成task",
    "TASK_CREATED_NO_CENTRAL_RECORD": "task未进分控",
    "CENTRAL_NOT_ENQUEUED": "中控未入队",
    "CENTRAL_ENQUEUED_WAITING": "已入队等待",
    "CRAWLER_EXECUTING": "正在执行",
    "CRAWLER_FAILED": "爬虫失败",
    "CALLBACK_LAG": "callback lag",
    "CALLBACK_DONE_NO_SEARCH": "callback后暂未search",
    "MODEL_PENDING": "模型处理中",
    "INTERNET_SAME_PENDING_APPROVAL": "互联网同款待审批",
    "APPROVAL_OR_FINAL_CLOSURE_PENDING": "最终收口中",
    "INTERNET_CANDIDATES_ALL_NON_SAME": "互联网候选全部判非同款",
    "REFERENCE_SAME_ONLY": "只有参考链接同款",
    "MODEL_DONE_NO_SAME": "模型完成无同款",
    "NO_INTERNET_CANDIDATE": "无互联网候选",
    "NO_COMMODITY_LIST": "未进入采集列表",
    "EVIDENCE_INCOMPLETE": "证据不足",
}

# ---------------------------------------------------------------------------
# 预定义只读 SQL registry(唯一允许执行的语句来源)
# ---------------------------------------------------------------------------

DIAG_SQL_REGISTRY: dict[str, str] = {
    # 当日商品池 IS_SYNC_STANDARD 分布(客户商品数 / 未最终收口)
    "pool_sync_standard_counts": """
        SELECT c.IS_SYNC_STANDARD AS is_sync_standard,
               COUNT(DISTINCT c.MY_COMMODITY_ID) AS commodity_count
        FROM saas_onecode_price.commodity_pool_188605 c
        WHERE c.COMMODITY_POOL_ID = %(pool_id)s
          AND c.IS_MONITOR = 1
          AND c.CREATED_TIME >= %(day_start)s
          AND c.CREATED_TIME < %(day_end)s
        GROUP BY c.IS_SYNC_STANDARD
    """,
    # 未收口商品的 crawl_status 分布(crawl_status=1/2 路由的第一层)
    "open_crawl_status_counts": """
        SELECT cl.crawl_status AS crawl_status, COUNT(*) AS commodity_count
        FROM saas_compare_price.commodity_list_table_188605 cl
        JOIN saas_onecode_price.commodity_pool_188605 c
          ON c.MY_COMMODITY_ID = cl.commodity_id
        WHERE c.COMMODITY_POOL_ID = %(pool_id)s
          AND c.IS_MONITOR = 1
          AND c.IS_SYNC_STANDARD = 0
          AND c.CREATED_TIME >= %(day_start)s
          AND c.CREATED_TIME < %(day_end)s
        GROUP BY cl.crawl_status
    """,
    # crawl_status=1 小集合的目标商品(有界:当日范围 + LIMIT)
    "crawl1_commodity_rows": """
        SELECT cl.id AS commodity_list_id, cl.commodity_id AS commodity_id
        FROM saas_compare_price.commodity_list_table_188605 cl
        JOIN saas_onecode_price.commodity_pool_188605 c
          ON c.MY_COMMODITY_ID = cl.commodity_id
        WHERE c.COMMODITY_POOL_ID = %(pool_id)s
          AND c.IS_MONITOR = 1
          AND c.IS_SYNC_STANDARD = 0
          AND c.CREATED_TIME >= %(day_start)s
          AND c.CREATED_TIME < %(day_end)s
          AND cl.crawl_status = 1
        LIMIT 2001
    """,
    # 仅对 crawl1 小集合精确查 task(绝不与 search_table 同句 JOIN)
    "crawl1_task_rows": """
        SELECT t.reference_id AS commodity_list_id, t.task_id AS task_id,
               t.task_status AS task_status
        FROM saas_compare_price.task_table_188605 t
        WHERE t.table_name = '188605'
          AND t.reference_id IN %(commodity_list_ids)s
    """,
    # 仅对 exact batch 查分控(有界 IN,不扫全表)
    "crawl1_central_rows": """
        SELECT s.batch_num AS batch_num, s.task_status AS task_status,
               s.queue_status AS queue_status, s.enqueue_time AS enqueue_time,
               s.end_time AS end_time
        FROM central_control_sub_demo.s_task_info s
        WHERE s.is_valid = '1'
          AND s.batch_num IN %(batch_nums)s
    """,
    # crawl_status=2 商品的 search/model/审批 flag 聚合(search_table 只聚合、不与 task JOIN)
    "crawl2_search_flags": """
        SELECT cl.id AS commodity_list_id,
               COUNT(s.id) AS search_rows,
               MIN(c.CREATED_TIME) AS pool_created_at,
               MAX(s.audit_result IS NULL OR s.audit_result IN ('-1', '-2')) AS model_pending,
               MAX(s.audit_result = '2' AND s.model_audit_result = '1') AS has_internet_same,
               MAX(s.audit_result = '2' AND s.model_audit_result = '1' AND s.sync_state = 1)
                   AS internet_pending_approval,
               MAX(s.audit_result = '2' AND s.model_audit_result IN ('1', '3') AND s.sync_state = 0)
                   AS same_approved,
               MAX(s.audit_result = '2' AND s.model_audit_result IN ('1', '3') AND s.sync_state = 2)
                   AS same_rejected,
               MAX(s.audit_result = '2' AND s.model_audit_result = '3') AS has_reference_same,
               MAX(s.model_audit_result IN ('0', '1')) AS has_internet_candidate,
               MAX(s.audit_result = '2' AND s.model_audit_result = '3' AND s.sync_state = 1)
                   AS reference_pending_approval,
               MAX(s.audit_result IN ('2', '3', '0')) AS has_model_verdict
        FROM saas_compare_price.commodity_list_table_188605 cl
        JOIN saas_onecode_price.commodity_pool_188605 c
          ON c.MY_COMMODITY_ID = cl.commodity_id
        LEFT JOIN saas_compare_price.search_table_188605 s
          ON s.commodity_list_id = cl.id
        WHERE c.COMMODITY_POOL_ID = %(pool_id)s
          AND c.IS_MONITOR = 1
          AND c.IS_SYNC_STANDARD = 0
          AND c.CREATED_TIME >= %(day_start)s
          AND c.CREATED_TIME < %(day_end)s
          AND cl.crawl_status = 2
        GROUP BY cl.id
        LIMIT 2001
    """,
}

QUERY_COLUMNS: dict[str, tuple[str, ...]] = {
    "pool_sync_standard_counts": ("is_sync_standard", "commodity_count"),
    "open_crawl_status_counts": ("crawl_status", "commodity_count"),
    "crawl1_commodity_rows": ("commodity_list_id", "commodity_id"),
    "crawl1_task_rows": ("commodity_list_id", "task_id", "task_status"),
    "crawl1_central_rows": ("batch_num", "task_status", "queue_status", "enqueue_time", "end_time"),
    "crawl2_search_flags": (
        "commodity_list_id", "search_rows", "pool_created_at", "model_pending",
        "has_internet_same", "internet_pending_approval", "same_approved",
        "same_rejected", "has_reference_same", "has_internet_candidate",
        "reference_pending_approval", "has_model_verdict",
    ),
}

_ALLOWED_QUERY_PARAMS = frozenset(
    {"pool_id", "day_start", "day_end", "commodity_list_ids", "batch_nums"}
)

# ---------------------------------------------------------------------------
# SQL 安全校验
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYWORDS = (
    "UPDATE", "DELETE", "INSERT", "REPLACE", "MERGE", "TRUNCATE", "DROP",
    "ALTER", "CREATE", "GRANT", "REVOKE", "CALL", "LOCK", "UNLOCK", "SET",
    "LOAD", "INTO", "OUTFILE", "DUMPFILE",
)


def validate_readonly_statement(sql: str) -> list[str]:
    """校验单条只读语句;返回问题列表,空列表表示通过。"""
    text = sql.strip().rstrip(";").strip()
    problems: list[str] = []
    if ";" in text:
        problems.append("multi-statement")
    if not re.match(r"^(SELECT|WITH)\b", text, re.IGNORECASE):
        problems.append("not-select-or-cte")
    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", text, re.IGNORECASE):
            problems.append(f"forbidden:{keyword.lower()}")
    return problems


# ---------------------------------------------------------------------------
# 只读账号配置(值只来自进程环境或 ignored env 文件)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiagDBConfig:
    host: str
    port: int
    user: str
    password: str
    connect_timeout_seconds: int = CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: int = READ_TIMEOUT_SECONDS

    @staticmethod
    def from_env(environ: Mapping[str, str]) -> "DiagDBConfig | None":
        host = str(environ.get("CNNC_DIAG_DB_HOST") or "").strip()
        user = str(environ.get("CNNC_DIAG_DB_USER") or "").strip()
        password = str(environ.get("CNNC_DIAG_DB_PASSWORD") or "")
        if not host or not user or not password:
            return None
        try:
            port = int(str(environ.get("CNNC_DIAG_DB_PORT") or "3306"))
        except ValueError:
            return None
        return DiagDBConfig(host=host, port=port, user=user, password=password)


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            cleaned = value.strip()
            if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
                cleaned = cleaned[1:-1]
            if cleaned:
                values[key.strip()] = cleaned
    return values


def resolve_diag_db_config(
    environ: Mapping[str, str] | None = None,
    *,
    env_file: Path | None = None,
) -> DiagDBConfig | None:
    """进程环境变量优先,其次 ignored 的 data/CNNC_DIAG_DB.env;缺任一必填项即未配置。"""
    merged: dict[str, str] = {}
    merged.update(_parse_env_file(env_file or DEFAULT_DB_ENV_PATH))
    merged.update({k: v for k, v in (environ if environ is not None else os.environ).items()
                   if v})
    return DiagDBConfig.from_env(merged)


def _default_connection_factory(config: DiagDBConfig):
    import pymysql  # 延迟导入:未配置/未安装时主看板不受影响

    return pymysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        connect_timeout=config.connect_timeout_seconds,
        read_timeout=config.read_timeout_seconds,
        autocommit=True,
        charset="utf8mb4",
    )


# ---------------------------------------------------------------------------
# 纯分类函数
# ---------------------------------------------------------------------------

def _truthy_flag(value: Any) -> bool:
    return value is not None and value != 0 and value != ""


def _as_str(value: Any) -> str:
    return str(value) if value is not None else ""


def classify_crawler_branch(record: Mapping[str, Any]) -> str:
    """crawl_status=1 商品分类。证据只用 enqueue/task/queue/end 时间字段。

    决策顺序(确定性,单商品只落一桶):
    失败 > 全部子任务完成(02+end 时间,queue_status 不作要求)> 未入队 > 已入队等待
    > 执行中 > 证据不足。
    """
    if int(record.get("task_count") or 0) == 0:
        return "TASK_NOT_CREATED"
    rows = list(record.get("central") or [])
    if not rows:
        return "TASK_CREATED_NO_CENTRAL_RECORD"

    def norm(row: Mapping[str, Any]) -> tuple[str, str, Any, Any]:
        return (_as_str(row.get("task_status")), _as_str(row.get("queue_status")),
                row.get("enqueue_time"), row.get("end_time"))

    normalized = [norm(row) for row in rows]

    def has_end(end_time: Any) -> bool:
        return end_time not in (None, "")

    if any(ts == "03" for ts, _, _, _ in normalized):
        return "CRAWLER_FAILED"
    if all(ts == "02" and has_end(end) for ts, _, _, end in normalized):
        return "CALLBACK_LAG"
    if any(ts == "00" and qs == "00" and enqueue is None for ts, qs, enqueue, _ in normalized):
        return "CENTRAL_NOT_ENQUEUED"
    if any(ts == "00" for ts, _, _, _ in normalized):
        return "CENTRAL_ENQUEUED_WAITING"
    if any(ts == "01" or (ts == "02" and not has_end(end)) for ts, _, _, end in normalized):
        return "CRAWLER_EXECUTING"
    return "EVIDENCE_INCOMPLETE"


def _parse_shanghai_naive(value: Any) -> datetime | None:
    text = _as_str(value)
    if not text:
        return None
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.fromisoformat(text[:19])
        except ValueError:
            return None


def classify_downstream(
    record: Mapping[str, Any],
    *,
    now: datetime | None = None,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
) -> dict[str, Any]:
    """crawl_status=2 商品分类(单商品单桶),返回 {state, within_grace}。

    决策顺序:无 search(grace) > 互联网同款待审批 > 已审批-同款待收口
    > 已审批-非同款待收口 > 模型处理中 > 互联网候选全部判非同款
    > 只有参考链接同款 > 模型完成无同款 > 无互联网候选 > 证据不足。
    - "互联网候选全部判非同款"优先于"只有参考链接同款":2026-08-27 生产实测
      75/89 的参考链接同款商品同时存在互联网非同款候选,互联网侧证据是页面
      不增长的更直接解释。
    - sync_state=2(已审批-非同款,[FRONTEND] 前端语义)按 spec 对
      APPROVAL_OR_FINAL_CLOSURE_PENDING 的定义("审批/结果已经存在,但
      IS_SYNC_STANDARD 尚未最终关闭")处理:人工审批结果存在、收口未完成;
      不解释为任何未验证后台状态。待审批(sync_state=1)与已驳回同时存在时,
      待审批优先(它是当前可操作阶段)。
    """
    now_naive: datetime | None = None
    if now is not None:
        now_naive = now.astimezone(SHANGHAI).replace(tzinfo=None) if now.tzinfo else now
    within_grace: bool | None = None
    if not _truthy_flag(record.get("search_rows")):
        state = "CALLBACK_DONE_NO_SEARCH"
        created = _parse_shanghai_naive(record.get("pool_created_at"))
        if now_naive is not None and created is not None:
            # 近似口径:commodity_list 无已验证回调时间字段,以入池时间作宽限参照
            within_grace = (now_naive - created) <= timedelta(seconds=grace_seconds)
        return {"state": state, "within_grace": within_grace}
    if _truthy_flag(record.get("internet_pending_approval")):
        state = "INTERNET_SAME_PENDING_APPROVAL"
    elif _truthy_flag(record.get("same_approved")):
        state = "APPROVAL_OR_FINAL_CLOSURE_PENDING"
    elif _truthy_flag(record.get("same_rejected")):
        state = "APPROVAL_OR_FINAL_CLOSURE_PENDING"
    elif _truthy_flag(record.get("model_pending")):
        state = "MODEL_PENDING"
    elif _truthy_flag(record.get("has_internet_candidate")) and \
            not _truthy_flag(record.get("has_internet_same")):
        state = "INTERNET_CANDIDATES_ALL_NON_SAME"
    elif _truthy_flag(record.get("has_reference_same")) and \
            not _truthy_flag(record.get("has_internet_same")):
        state = "REFERENCE_SAME_ONLY"
    elif _truthy_flag(record.get("has_model_verdict")) and \
            not _truthy_flag(record.get("has_internet_same")) and \
            not _truthy_flag(record.get("has_reference_same")):
        state = "MODEL_DONE_NO_SAME"
    elif not _truthy_flag(record.get("has_internet_candidate")):
        state = "NO_INTERNET_CANDIDATE"
    else:
        state = "EVIDENCE_INCOMPLETE"
    return {"state": state, "within_grace": within_grace}


def evaluate_growth(
    observations: Sequence[Mapping[str, Any]],
    *,
    business_date: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """18:00(Asia/Shanghai)前的客户增长/SaaS 停滞分叉检测。

    observations 为同一 business_date 内按时间升序的权威观察序列
    (来自 state.sqlite3 snapshots.evidence_json,而非新调业务 API)。
    LAG 定义:customer_delta > 0 且 saas_delta <= 0;连续尾部区间数 >=2 → LAG_PERSISTING,
    ==1 → LAG_WATCH,否则 NORMAL。18:00 后不再评估(evals=False,永不告警)。
    """
    current = now or datetime.now(UTC)
    cutoff = cutoff_for_business_date(business_date)
    series = [
        {"observed_at": str(o.get("observed_at") or ""),
         "customer_count": int(o.get("customer_count") or 0),
         "saas_count": int(o.get("saas_count") or 0)}
        for o in observations
    ]
    result: dict[str, Any] = {
        "available": bool(series),
        "evaluated": False,
        # 明确区分"评估过=正常"与"证据不足/非评估时段",前端绝不把后者显示成"正常"
        "evaluation_reason": "NO_OBSERVATIONS" if not series else "INSUFFICIENT_OBSERVATIONS",
        "state": "NORMAL",
        "customer_count": series[-1]["customer_count"] if series else 0,
        "saas_count": series[-1]["saas_count"] if series else 0,
        "customer_delta": 0,
        "saas_delta": 0,
        "consecutive_lag_intervals": 0,
        "observation_count": len(series),
        "last_observed_at": series[-1]["observed_at"] if series else None,
        "cutoff_at": cutoff.isoformat(),
    }
    if current >= cutoff:
        result["evaluation_reason"] = "AFTER_CUTOFF"
        return result
    if len(series) < 2:
        return result
    result["evaluated"] = True
    result["evaluation_reason"] = "EVALUATED"
    intervals: list[tuple[int, int]] = []
    for previous, item in zip(series, series[1:]):
        intervals.append((item["customer_count"] - previous["customer_count"],
                          item["saas_count"] - previous["saas_count"]))
    trailing = 0
    for customer_delta, saas_delta in reversed(intervals):
        if customer_delta > 0 and saas_delta <= 0:
            trailing += 1
        else:
            break
    result["consecutive_lag_intervals"] = trailing
    result["customer_delta"], result["saas_delta"] = intervals[-1]
    if trailing >= 2:
        result["state"] = "LAG_PERSISTING"
    elif trailing == 1:
        result["state"] = "LAG_WATCH"
    return result


def read_observation_series(state_path: Path | str, business_date: str) -> list[dict[str, Any]]:
    """读取该业务日已持久化的权威观察序列(只读 URI,绝不写入)。"""
    path = Path(state_path)
    if not path.is_file():
        return []
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            """SELECT s.observed_at AS observed_at, s.evidence_json AS evidence_json
               FROM snapshots s JOIN daily_runs r ON r.id = s.run_id
               WHERE r.business_date = ?
               ORDER BY s.observed_at, s.id""",
            (business_date,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    series: list[dict[str, Any]] = []
    for observed_at, evidence_json in rows:
        try:
            evidence = json.loads(str(evidence_json or "{}"))
        except (TypeError, ValueError):
            continue
        saas_count, customer_count = evidence.get("saas_count"), evidence.get("customer_count")
        if not isinstance(saas_count, int) or not isinstance(customer_count, int):
            continue
        series.append({"observed_at": str(observed_at), "customer_count": customer_count,
                       "saas_count": saas_count})
    return series


# ---------------------------------------------------------------------------
# 诊断服务
# ---------------------------------------------------------------------------

def _empty_counts(classes: Sequence[str]) -> dict[str, int]:
    return {name: 0 for name in classes}


class SaasFlowDiagnosticService:
    """只读诊断服务:本地持久观察序列 + 固定 scope MySQL 预定义查询 + 缓存/限流。"""

    def __init__(
        self,
        *,
        config: DiagDBConfig | None,
        connection_factory: Callable[[DiagDBConfig], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        runtime_root: Path | str | None = None,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        fresh_read_min_interval_seconds: float = DEFAULT_FRESH_READ_MIN_INTERVAL_SECONDS,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._connection_factory = connection_factory or _default_connection_factory
        self._monotonic = monotonic
        self._runtime_root = runtime_root or DEFAULT_RUNTIME_ROOT
        self._grace_seconds = int(grace_seconds)
        self._cache_ttl_seconds = float(cache_ttl_seconds)
        self._fresh_min_interval = float(fresh_read_min_interval_seconds)
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._last_fresh_read_at = -float("inf")
        # single-flight:同一时间只允许一个 fresh DB diagnostic build;
        # 并发请求在锁内二次检查缓存/限流,复用已有结果(Review P1-7)。
        self._build_lock = threading.Lock()

    # -- 公共入口 -----------------------------------------------------------

    def get_diagnostic(self, business_date: str, *, force_refresh: bool = False) -> dict[str, Any]:
        payload = self._cached_or_limited(business_date, force_refresh)
        if payload is not None:
            return payload
        with self._build_lock:
            # 双检:等锁期间其他线程可能已完成 build
            payload = self._cached_or_limited(business_date, force_refresh)
            if payload is not None:
                return payload
            payload = self._build(business_date)
            payload["cached"] = False
            payload["rate_limited"] = False
            now = self._monotonic()
            self._cache[business_date] = (now, payload)
            self._last_fresh_read_at = now
            return dict(payload)

    def _cached_or_limited(self, business_date: str, force_refresh: bool) -> dict[str, Any] | None:
        """命中缓存或被限流时返回可复用 payload,需要 fresh build 时返回 None。"""
        now = self._monotonic()
        cached = self._cache.get(business_date)
        if cached is None:
            return None
        cached_at, payload = cached
        if force_refresh and now - self._last_fresh_read_at < self._fresh_min_interval:
            blocked = dict(payload)
            blocked["cached"] = True
            blocked["rate_limited"] = True
            return blocked
        if not force_refresh and now - cached_at < self._cache_ttl_seconds:
            fresh = dict(payload)
            fresh["cached"] = True
            fresh["rate_limited"] = False
            return fresh
        return None

    # -- 组装 ---------------------------------------------------------------

    def _build(self, business_date: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "business_date": business_date,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "scope": {"tenant_id": TENANT_ID, "commodity_pool_id": POOL_ID},
            "configured": self._config is not None,
            "cached": False,
            "rate_limited": False,
            "growth": self._build_growth(business_date),
            "stages": None,
            "degraded_queries": [],
            "error": None,
        }
        if self._config is None:
            payload["status"] = "unconfigured"
            return payload
        connection = None
        try:
            connection = self._connection_factory(self._config)
            stages, degraded = self._build_stages(connection, business_date)
        except Exception as exc:  # 连接层失败:只降级本面板,凭据/DSN 不进 payload
            payload["status"] = "unavailable"
            payload["error"] = {"type": type(exc).__name__}
            return payload
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        payload["stages"] = stages
        payload["degraded_queries"] = degraded
        payload["status"] = "unavailable" if stages is None else ("partial" if degraded else "ok")
        return payload

    def _build_growth(self, business_date: str) -> dict[str, Any]:
        paths = runtime_paths(business_date, self._runtime_root)
        series = read_observation_series(paths.state_path, business_date)
        return evaluate_growth(series, business_date=business_date, now=self._now_fn())

    def _query(self, connection: Any, name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        sql = DIAG_SQL_REGISTRY[name]
        assert set(params) <= _ALLOWED_QUERY_PARAMS, f"unexpected params for {name}"
        cursor = connection.cursor()
        try:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        finally:
            try:
                cursor.close()
            except Exception:
                pass
        columns = QUERY_COLUMNS[name]
        return [dict(zip(columns, row)) for row in rows]

    def _build_stages(self, connection: Any, business_date: str) -> tuple[dict[str, Any] | None, list[str]]:
        degraded: list[str] = []
        day = datetime.fromisoformat(business_date).date()
        base_params = {
            "pool_id": POOL_ID,
            "day_start": f"{business_date} 00:00:00",
            "day_end": f"{(day + timedelta(days=1)).isoformat()} 00:00:00",
        }

        def run(name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            try:
                return self._query(connection, name, {**base_params, **params})
            except Exception:
                degraded.append(name)
                return []

        sync_rows = run("pool_sync_standard_counts", {})
        crawl_rows = run("open_crawl_status_counts", {})
        # Review P1-3:查询失败 ≠ 业务 0。核心查询失败的聚合一律 None
        # (证据不可用),绝不编造 0;下游分支查询存在依赖,路由层失败即跳过。
        pool_failed = "pool_sync_standard_counts" in degraded
        crawl_failed = "open_crawl_status_counts" in degraded
        if pool_failed and crawl_failed:
            return None, degraded

        if pool_failed:
            pool_scope_total = not_closed = closed_standard = closed_no_same = None
        else:
            sync_counts = {_as_str(row.get("is_sync_standard")): int(row.get("commodity_count") or 0)
                           for row in sync_rows}
            pool_scope_total = sum(sync_counts.values())
            not_closed = sync_counts.get("0", 0)
            closed_standard = sync_counts.get("1", 0)
            closed_no_same = sync_counts.get("2", 0)
        if crawl_failed:
            crawl1_total = crawl2_total = crawl_other = None
        else:
            crawl_counts = {_as_str(row.get("crawl_status")): int(row.get("commodity_count") or 0)
                            for row in crawl_rows}
            crawl1_total = crawl_counts.get("1", 0)
            crawl2_total = crawl_counts.get("2", 0)
            crawl_other = sum(count for status, count in crawl_counts.items()
                              if status not in ("1", "2"))

        # Review P1-1:两个分支各自独立计数,同名 EVIDENCE_INCOMPLETE 不共享。
        crawler_counts: dict[str, int] | None = _empty_counts(CRAWLER_BRANCH_CLASSES)
        downstream_counts: dict[str, int] | None = _empty_counts(DOWNSTREAM_BRANCH_CLASSES)
        central_detail: dict[tuple[str, str, str], int] = {}
        reference_pending_total: int | None = 0
        callback_no_search_within_grace: int | None = 0
        now_sh = self._now_fn().astimezone(SHANGHAI).replace(tzinfo=None)

        if not crawl_failed:
            # ---- Branch A: crawl_status = 1 → task → exact batch → 分控 ----
            crawl1_rows = run("crawl1_commodity_rows", {})
            ids = [int(row["commodity_list_id"]) for row in crawl1_rows[:MAX_CRAWL1_COMMODITIES]]
            if "crawl1_commodity_rows" in degraded:
                crawler_counts["EVIDENCE_INCOMPLETE"] += crawl1_total or 0
            else:
                task_rows: list[dict[str, Any]] = []
                task_failed = False
                if ids:
                    task_rows = run("crawl1_task_rows", {"commodity_list_ids": tuple(ids)})
                    task_failed = "crawl1_task_rows" in degraded
                tasks_by_commodity: dict[int, list[dict[str, Any]]] = {}
                for row in task_rows:
                    tasks_by_commodity.setdefault(int(row["commodity_list_id"]), []).append(row)
                batches: set[str] = set()
                for row in task_rows:
                    batch = _batch_from_task_id(_as_str(row.get("task_id")))
                    if batch:
                        batches.add(batch)
                central_rows: list[dict[str, Any]] = []
                central_failed = False
                if batches:
                    central_rows = run("crawl1_central_rows", {"batch_nums": tuple(sorted(batches))})
                    central_failed = "crawl1_central_rows" in degraded
                central_by_batch: dict[str, list[dict[str, Any]]] = {}
                for row in central_rows:
                    central_by_batch.setdefault(_as_str(row.get("batch_num")), []).append(row)
                    key = (_as_str(row.get("batch_num")), _as_str(row.get("task_status")),
                           _as_str(row.get("queue_status")))
                    central_detail[key] = central_detail.get(key, 0) + 1
                for commodity_list_id in ids:
                    tasks = tasks_by_commodity.get(commodity_list_id, [])
                    # Review P1-2:task/central 查询失败只能证明证据不足,
                    # 绝不落 TASK_CREATED_NO_CENTRAL_RECORD。
                    if task_failed or central_failed:
                        crawler_counts["EVIDENCE_INCOMPLETE"] += 1
                        continue
                    commodity_batches = {_batch_from_task_id(_as_str(t.get("task_id")))
                                         for t in tasks} - {""}
                    central_for_commodity = [row for batch in commodity_batches
                                             for row in central_by_batch.get(batch, [])]
                    crawler_counts[classify_crawler_branch({
                        "task_count": len(tasks),
                        "central": central_for_commodity,
                    })] += 1
                overflow = (crawl1_total or 0) - len(ids)
                if overflow > 0:
                    crawler_counts["EVIDENCE_INCOMPLETE"] += overflow

            # ---- Branch B: crawl_status = 2 → search/model/审批 ----
            crawl2_rows = run("crawl2_search_flags", {})
            classified = 0
            for row in crawl2_rows[:MAX_CRAWL2_COMMODITIES]:
                verdict = classify_downstream(row, now=now_sh, grace_seconds=self._grace_seconds)
                downstream_counts[verdict["state"]] += 1
                classified += 1
                if verdict["state"] == "CALLBACK_DONE_NO_SEARCH" and verdict["within_grace"]:
                    callback_no_search_within_grace += 1
                if _truthy_flag(row.get("reference_pending_approval")):
                    reference_pending_total += 1
            if "crawl2_search_flags" in degraded:
                downstream_counts["EVIDENCE_INCOMPLETE"] += crawl2_total or 0
            else:
                overflow = (crawl2_total or 0) - classified
                if overflow > 0:
                    downstream_counts["EVIDENCE_INCOMPLETE"] += overflow
        else:
            crawler_counts = None
            downstream_counts = None
            reference_pending_total = None
            callback_no_search_within_grace = None

        # no_commodity_list 只在 pool 与 crawl 两层计数都可用时才可闭合
        no_commodity_list: int | None = None
        if not pool_failed and not crawl_failed:
            no_commodity_list = max(0, not_closed - (crawl1_total + crawl2_total + crawl_other))
        dominant = None
        if crawler_counts is not None and downstream_counts is not None:
            dominant = _dominant_stage(crawler_counts, downstream_counts, no_commodity_list)
        detail = [
            {"batch_num": batch, "task_status": task_status, "queue_status": queue_status,
             "count": number}
            for (batch, task_status, queue_status), number in
            sorted(central_detail.items(), key=lambda item: (-item[1], item[0]))[:MAX_CENTRAL_DETAIL_ROWS]
        ]
        stages = {
            # Review P1-6:SQL 池计数不得冒充权威客户计数(权威计数在 growth.customer_count)
            "pool_counts_available": not pool_failed,
            "crawl_status_available": not crawl_failed,
            "pool_scope_total": pool_scope_total,
            "not_closed": not_closed,
            "closed_standard": closed_standard,
            "closed_no_same": closed_no_same,
            "no_commodity_list": no_commodity_list,
            "crawl_status_1": crawl1_total,
            "crawl_status_2": crawl2_total,
            "crawl_status_other": crawl_other,
            "grace_seconds": self._grace_seconds,
            "crawler_branch": crawler_counts,
            "downstream_branch": downstream_counts,
            "callback_done_no_search_within_grace": callback_no_search_within_grace,
            "reference_pending_approval_total": reference_pending_total,
            "central_status_detail": detail,
            "dominant_stage": dominant,
            "queried_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        return stages, degraded


def _batch_from_task_id(task_id: str) -> str:
    """SAAS-188605_704827316150657024_279774 → 188605_704827316150657024(sql_ops KB §3.4)。"""
    if not task_id:
        return ""
    stripped = task_id.replace("SAAS-", "", 1)
    parts = stripped.split("_")
    if len(parts) < 2:
        return ""
    return "_".join(parts[:2])


def _dominant_stage(crawler_counts: Mapping[str, int], downstream_counts: Mapping[str, int],
                    no_commodity_list: int | None) -> dict[str, Any] | None:
    candidates: dict[str, int] = {}
    for counts in (crawler_counts, downstream_counts):
        for key, value in counts.items():
            if key != "EVIDENCE_INCOMPLETE":
                candidates[key] = value
    if no_commodity_list:
        candidates["NO_COMMODITY_LIST"] = no_commodity_list
    best_key = max(sorted(candidates), key=lambda key: candidates[key]) \
        if any(candidates.values()) else None
    if not best_key:
        return None
    return {"key": best_key, "count": candidates[best_key],
            "label_zh": STAGE_LABELS_ZH.get(best_key, best_key)}


# ---------------------------------------------------------------------------
# 端点用工厂(模块级单例;测试通过 dashboard_api monkeypatch 或重建单例)
# ---------------------------------------------------------------------------

_SERVICE_SINGLETON: SaasFlowDiagnosticService | None = None


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        value = default
    return max(minimum, value)


def get_saas_flow_diagnostic(business_date: str, *, force_refresh: bool = False) -> dict[str, Any]:
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        _SERVICE_SINGLETON = SaasFlowDiagnosticService(
            config=resolve_diag_db_config(),
            cache_ttl_seconds=_env_float("CNNC_DIAG_CACHE_SECONDS",
                                         DEFAULT_CACHE_TTL_SECONDS, MIN_CACHE_TTL_SECONDS),
            fresh_read_min_interval_seconds=_env_float(
                "CNNC_DIAG_FRESH_MIN_INTERVAL_SECONDS",
                DEFAULT_FRESH_READ_MIN_INTERVAL_SECONDS, MIN_FRESH_READ_INTERVAL_SECONDS),
        )
    return _SERVICE_SINGLETON.get_diagnostic(business_date, force_refresh=force_refresh)

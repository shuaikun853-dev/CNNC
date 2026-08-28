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

# Fix B(R1 性能修复 2026-08-28):连接死亡级 MySQL 错误。命中后当前连接必须
# 废弃并重建供后续查询使用;失败查询本身绝不重试。其余 SQL 错误(1142 权限/
# 1146 表缺失/1054 列/1064 语法等)不代表连接死亡,不得触发重建。
CONNECTION_KILLING_MYSQL_ERRNOS = frozenset({2006, 2013})

CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15
DEFAULT_GRACE_SECONDS = 1800  # callback 后暂无 search 的宽限窗口(基于入池时间近似)
DEFAULT_CACHE_TTL_SECONDS = 60.0
DEFAULT_FRESH_READ_MIN_INTERVAL_SECONDS = 30.0
MIN_CACHE_TTL_SECONDS = 60.0        # 生产要求:至少 60 秒缓存,禁止 5 秒级 DB 轮询
MIN_FRESH_READ_INTERVAL_SECONDS = 30.0  # 强制刷新间隔下限与文档/UI 声明一致,env 不可降破
MAX_CRAWL1_COMMODITIES = 2000       # 有界结果集:单日 crawl_status=1 目标商品上限
MAX_CRAWL2_COMMODITIES = 2000
MAX_CRAWL1_EXECUTION_NUMBERS = 2000  # 有界:单轮 callback 查询 execution_number 上限
# R2 Review P2:all_batches 独立硬上限(与 commodities/executions 同为 fail-safe 有界),
# 防止多 batch 商品放大 WHERE batch_num IN (...);截断按 item 级降级,见 _build_stages。
MAX_CRAWL1_BATCHES = 2000

DEFAULT_DB_ENV_PATH = Path(__file__).resolve().parents[2] / "data" / "CNNC_DIAG_DB.env"

# R2 Central Bypass(2026-08-28):crawl_status=1 主诊断链改走已实证的
# task → batch → spider_task_record → execution_number → spider_sync_log(_complated)
# callback 证据;s_task_info 不再是主诊断依赖,central 状态类全部移除。
# 未验证语义禁止臆断:无 CALLBACK_FAILED(push_state 非 0000/8888 → 证据不足),
# 无 PENDING_URL_SYNC(取证证明 CNNC 188605 无此状态值)。
CRAWLER_BRANCH_CLASSES = (
    "TASK_NOT_CREATED",
    "PUSH_RECORD_NOT_FOUND",
    "PUSH_CONFIRMED_NO_CALLBACK_RECORD",
    "CALLBACK_IN_FLIGHT",
    "CALLBACK_PARTIAL",
    "CALLBACK_DONE_SAAS_PENDING",
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
    "PUSH_RECORD_NOT_FOUND": "未发现爬虫推送记录",
    "PUSH_CONFIRMED_NO_CALLBACK_RECORD": "已推送，暂无回调记录",
    "CALLBACK_IN_FLIGHT": "回调进行中",
    "CALLBACK_PARTIAL": "部分回调完成",
    "CALLBACK_DONE_SAAS_PENDING": "回调已完成，SaaS尚未推进",
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
    # Cardinality Closeout:路由单位 = 唯一 MY_COMMODITY_ID。pool scope 先商品级
    # DISTINCT,再 LEFT JOIN 商品级预聚合(commodity_list_rows / status1_rows /
    # status2_rows),每个商品只落一个 bucket:
    #   0 条 commodity_list → no_commodity_list
    #   >1 条 commodity_list → routing_evidence_incomplete(无已验证 current-row
    #     选择规则,fail-safe,禁止 MAX/MIN(id)/最新时间/优先状态等猜测)
    #   1 条 → 按其 crawl_status 落 crawl1 / crawl2 / other
    # invariant:no_cl + c1 + c2 + other + incomplete == routing_open_total。
    "open_crawl_status_counts": """
        SELECT
            COUNT(*) AS routing_open_total,
            SUM(a.commodity_id IS NULL) AS no_commodity_list,
            SUM(a.commodity_id IS NOT NULL AND a.commodity_list_rows = 1
                AND a.status1_rows = 1) AS crawl_status_1,
            SUM(a.commodity_id IS NOT NULL AND a.commodity_list_rows = 1
                AND a.status2_rows = 1) AS crawl_status_2,
            SUM(a.commodity_id IS NOT NULL AND a.commodity_list_rows = 1
                AND a.status1_rows = 0 AND a.status2_rows = 0) AS crawl_status_other,
            SUM(a.commodity_id IS NOT NULL AND a.commodity_list_rows > 1)
                AS routing_evidence_incomplete
        FROM (
            SELECT DISTINCT c.MY_COMMODITY_ID AS MY_COMMODITY_ID
            FROM saas_onecode_price.commodity_pool_188605 c
            WHERE c.COMMODITY_POOL_ID = %(pool_id)s
              AND c.IS_MONITOR = 1
              AND c.IS_SYNC_STANDARD = 0
              AND c.CREATED_TIME >= %(day_start)s
              AND c.CREATED_TIME < %(day_end)s
        ) p
        LEFT JOIN (
            SELECT cl.commodity_id AS commodity_id,
                   COUNT(*) AS commodity_list_rows,
                   COUNT(CASE WHEN cl.crawl_status = 1 THEN 1 END) AS status1_rows,
                   COUNT(CASE WHEN cl.crawl_status = 2 THEN 1 END) AS status2_rows
            FROM saas_compare_price.commodity_list_table_188605 cl
            JOIN (
                SELECT DISTINCT c2.MY_COMMODITY_ID AS MY_COMMODITY_ID
                FROM saas_onecode_price.commodity_pool_188605 c2
                WHERE c2.COMMODITY_POOL_ID = %(pool_id)s
                  AND c2.IS_MONITOR = 1
                  AND c2.IS_SYNC_STANDARD = 0
                  AND c2.CREATED_TIME >= %(day_start)s
                  AND c2.CREATED_TIME < %(day_end)s
            ) sp ON sp.MY_COMMODITY_ID = cl.commodity_id
            GROUP BY cl.commodity_id
        ) a ON a.commodity_id = p.MY_COMMODITY_ID
    """,
    # crawl_status=1 小集合的目标商品(有界:当日范围 + LIMIT)。
    # Cardinality:EXISTS 定 pool scope(duplicate pool 行不再相乘);NOT EXISTS
    # 排除多条 commodity_list 的 ambiguous 商品(routing 层已计 incomplete,
    # 不得再进入正常 crawler branch)。每个 commodity_list_id 最多返回一次。
    "crawl1_commodity_rows": """
        SELECT cl.id AS commodity_list_id, cl.commodity_id AS commodity_id
        FROM saas_compare_price.commodity_list_table_188605 cl
        WHERE cl.crawl_status = 1
          AND EXISTS (
              SELECT 1
              FROM saas_onecode_price.commodity_pool_188605 c
              WHERE c.COMMODITY_POOL_ID = %(pool_id)s
                AND c.IS_MONITOR = 1
                AND c.IS_SYNC_STANDARD = 0
                AND c.CREATED_TIME >= %(day_start)s
                AND c.CREATED_TIME < %(day_end)s
                AND c.MY_COMMODITY_ID = cl.commodity_id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM saas_compare_price.commodity_list_table_188605 cl2
              WHERE cl2.commodity_id = cl.commodity_id
                AND cl2.id <> cl.id
          )
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
    # R2 Central Bypass:推送记录按派生 batch 精确查询(batch_num 为 varchar,
    # string 绑定;索引 index_batch_nim,生产实测 0.06~0.12s)。
    "crawl1_spider_task_rows": """
        SELECT r.batch_num AS batch_num, r.exe_number AS exe_number,
               r.is_call AS is_call
        FROM shopping_spider.spider_task_record r
        WHERE r.batch_num IN %(batch_nums)s
    """,
    # R2 Central Bypass:callback 证据。单条 UNION ALL 同时取在途/完成两表,
    # 减少两 SELECT 之间的时点漂移;execution_number 为 varchar,string 绑定,
    # 两表均走 idx_ex(生产实测 0.07~0.33s)。语义(2026-08-28 生产取证):
    # active '8888'=在途,completed '0000'=回调成功终态;其余值未验证。
    "crawl1_callback_rows": """
        SELECT l.execution_number AS execution_number, l.push_state AS push_state,
               'active' AS callback_source
        FROM shopping_spider.spider_sync_log l
        WHERE l.execution_number IN %(execution_numbers)s
        UNION ALL
        SELECT c.execution_number AS execution_number, c.push_state AS push_state,
               'completed' AS callback_source
        FROM shopping_spider.spider_sync_log_complated c
        WHERE c.execution_number IN %(execution_numbers)s
    """,
    # crawl_status=2 商品的 search/model/审批 flag 聚合(search_table 只聚合、不与 task JOIN)。
    # Cardinality:JOIN 商品级去重后的 pool 聚合(每商品一行,duplicate pool 行不再
    # 放大 search_rows);NOT EXISTS 排除多条 commodity_list 的 ambiguous 商品,
    # 与 routing 层 incomplete bucket 保持 branch closure 一致。
    "crawl2_search_flags": """
        SELECT cl.id AS commodity_list_id,
               COUNT(s.id) AS search_rows,
               MIN(pc.pool_created_at) AS pool_created_at,
               MAX(s.audit_result IS NULL OR s.audit_result IN ('-1', '-2')) AS model_pending,
               MAX(s.audit_result = '2' AND s.model_audit_result = '1') AS has_internet_same,
               MAX(s.audit_result = '2' AND s.model_audit_result = '1' AND s.sync_state = 1)
                   AS internet_pending_approval,
               MAX(s.audit_result = '2' AND s.model_audit_result IN ('1', '3') AND s.sync_state = 0)
                   AS same_approved,
               MAX(s.sync_state = 2) AS approval_non_same_exists,
               MAX(s.audit_result = '2' AND s.model_audit_result = '3') AS has_reference_same,
               MAX(s.model_audit_result IN ('0', '1')) AS has_internet_candidate,
               MAX(s.audit_result = '2' AND s.model_audit_result = '3' AND s.sync_state = 1)
                   AS reference_pending_approval,
               MAX(s.audit_result IN ('2', '3', '0')) AS has_model_verdict
        FROM saas_compare_price.commodity_list_table_188605 cl
        JOIN (
            SELECT c.MY_COMMODITY_ID AS commodity_id,
                   MIN(c.CREATED_TIME) AS pool_created_at
            FROM saas_onecode_price.commodity_pool_188605 c
            WHERE c.COMMODITY_POOL_ID = %(pool_id)s
              AND c.IS_MONITOR = 1
              AND c.IS_SYNC_STANDARD = 0
              AND c.CREATED_TIME >= %(day_start)s
              AND c.CREATED_TIME < %(day_end)s
            GROUP BY c.MY_COMMODITY_ID
        ) pc ON pc.commodity_id = cl.commodity_id
        LEFT JOIN saas_compare_price.search_table_188605 s
          ON s.commodity_list_id = cl.id
        WHERE cl.crawl_status = 2
          AND NOT EXISTS (
              SELECT 1
              FROM saas_compare_price.commodity_list_table_188605 cl2
              WHERE cl2.commodity_id = cl.commodity_id
                AND cl2.id <> cl.id
          )
        GROUP BY cl.id
        LIMIT 2001
    """,
}

QUERY_COLUMNS: dict[str, tuple[str, ...]] = {
    "pool_sync_standard_counts": ("is_sync_standard", "commodity_count"),
    "open_crawl_status_counts": ("routing_open_total", "no_commodity_list",
                                 "crawl_status_1", "crawl_status_2", "crawl_status_other",
                                 "routing_evidence_incomplete"),
    "crawl1_commodity_rows": ("commodity_list_id", "commodity_id"),
    "crawl1_task_rows": ("commodity_list_id", "task_id", "task_status"),
    "crawl1_spider_task_rows": ("batch_num", "exe_number", "is_call"),
    "crawl1_callback_rows": ("execution_number", "push_state", "callback_source"),
    "crawl2_search_flags": (
        "commodity_list_id", "search_rows", "pool_created_at", "model_pending",
        "has_internet_same", "internet_pending_approval", "same_approved",
        "approval_non_same_exists", "has_reference_same", "has_internet_candidate",
        "reference_pending_approval", "has_model_verdict",
    ),
}

_ALLOWED_QUERY_PARAMS = frozenset(
    {"pool_id", "day_start", "day_end", "commodity_list_ids", "batch_nums",
     "execution_numbers"}
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


def _is_connection_killing_error(exc: BaseException) -> bool:
    """仅凭异常 args[0] 的 MySQL errno 判定连接死亡;绝不解析错误字符串。"""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and args[0] in CONNECTION_KILLING_MYSQL_ERRNOS:
        return True
    return False


class _RecyclingConnection:
    """Fix B:连接故障隔离——正常路径全程复用一个连接;只有 2006/2013 杀死
    连接后才废弃死连接(尽力 close)并重建一个。重建失败保持 None,后续
    查询按 degraded 处理,不反复重试 factory。所有连接都保证 best-effort close。
    """

    def __init__(self, connection: Any, factory: Callable[[DiagDBConfig], Any],
                 config: DiagDBConfig | None) -> None:
        self._connection = connection
        self._factory = factory
        self._config = config

    @property
    def connection(self) -> Any:
        return self._connection

    def replace_after_connection_kill(self) -> None:
        dead, self._connection = self._connection, None
        if dead is not None:
            try:
                dead.close()
            except Exception:
                pass
        try:
            self._connection = self._factory(self._config)
        except Exception:
            self._connection = None

    def close(self) -> None:
        current, self._connection = self._connection, None
        if current is not None:
            try:
                current.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 纯分类函数
# ---------------------------------------------------------------------------

def _truthy_flag(value: Any) -> bool:
    return value is not None and value != 0 and value != ""


def _as_str(value: Any) -> str:
    return str(value) if value is not None else ""


# R2 Central Bypass:已实证的 callback 语义(2026-08-28 生产只读取证)。
CALLBACK_PUSH_STATE_SUCCESS = "0000"   # completed 表 = 回调成功终态
CALLBACK_PUSH_STATE_IN_FLIGHT = "8888"  # active 表 = 在途/尚未完成


def _normalize_is_call(raw: Any) -> int | None:
    """R2 Review P1-1:is_call 只接受明确的 0/"0"/1/"1"。

    生产语义(sql_ops KB 取证):0=未回调,1=已回调;MySQL 实际列类型只会让
    pymysql 返回 int 或 str。其余一切值(None/空串/"2"/2/-1/"abc"/bool/float)
    都是证据缺失 → None(调用方按 EVIDENCE_INCOMPLETE 处理),绝不借
    Python truthiness 或 `or 0` 兜底成业务 0/1。bool 先于 int 判断:
    isinstance(True, int) 为真,不得无意识落入 0/1 相等语义。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw in (0, 1):
        return raw
    if isinstance(raw, str) and raw in ("0", "1"):
        return int(raw)
    return None


def _normalize_execution_state(
    is_call_values: set[int],
    active_push_states: set[str],
    completed_push_states: set[str],
) -> str:
    """单个 execution_number 的状态归一化。

    返回 SUCCESS / PENDING / NO_CALLBACK_RECORD / EVIDENCE_INCOMPLETE。
    未验证语义一律 fail-safe:未知 push_state、active+completed 重叠、
    is_call 与 callback 证据矛盾、同一来源多 push_state 均不猜测。
    """
    if len(is_call_values) != 1:
        return "EVIDENCE_INCOMPLETE"
    is_call = next(iter(is_call_values))
    if active_push_states and completed_push_states:
        return "EVIDENCE_INCOMPLETE"  # 重叠语义未验证,不按"completed 优先"隐藏冲突
    if active_push_states:
        if active_push_states != {CALLBACK_PUSH_STATE_IN_FLIGHT}:
            return "EVIDENCE_INCOMPLETE"
        return "PENDING" if is_call == 0 else "EVIDENCE_INCOMPLETE"
    if completed_push_states:
        if completed_push_states != {CALLBACK_PUSH_STATE_SUCCESS}:
            return "EVIDENCE_INCOMPLETE"
        return "SUCCESS" if is_call == 1 else "EVIDENCE_INCOMPLETE"
    return "NO_CALLBACK_RECORD" if is_call == 0 else "EVIDENCE_INCOMPLETE"


def classify_crawler_callback(record: Mapping[str, Any]) -> str:
    """crawl_status=1 商品分类(R2 callback 证据链,单商品单桶)。

    输入(由 _build_stages 组装):
      task_count / task_failed / malformed / batches(派生 batch 集合) /
      spider_failed / spider_rows(本商品 batch 的推送记录) /
      batches_truncated(本商品 batch 未完整进入有界 spider 查询) /
      callback_failed / callback_rows(本商品 execution 的回调记录) /
      exes_truncated(execution 超出有界查询上限)

    决策序(逐级依赖:task → spider push → callback;R2 Review P1-2:
    callback 是下游证据,查询失败只能降级依赖 callback 的商品,不得反向
    污染上游已确定状态):
      task 查询失败/未生成task/malformed → spider 查询失败 → batch 截断
      → 推送记录缺失(全部 batch 无记录才算;部分覆盖=证据不足)
      → 字段缺失/execution 截断 → callback 查询失败 → 逐 execution 归一化
      → item 聚合。
    """
    if record.get("task_failed"):
        return "EVIDENCE_INCOMPLETE"
    if int(record.get("task_count") or 0) == 0:
        return "TASK_NOT_CREATED"  # 不依赖推送/回调查询
    if record.get("malformed"):
        return "EVIDENCE_INCOMPLETE"
    if record.get("spider_failed"):
        return "EVIDENCE_INCOMPLETE"
    batches = {str(b) for b in (record.get("batches") or ())}
    if not batches:
        return "EVIDENCE_INCOMPLETE"
    if record.get("batches_truncated"):
        # R2 Review P2:本商品有 batch 未进入有界 spider 查询,记录缺失可能
        # 是截断造成——既不得冒充 PUSH_RECORD_NOT_FOUND,也不得进入
        # "部分覆盖"业务语义
        return "EVIDENCE_INCOMPLETE"
    rows = [r for r in (record.get("spider_rows") or [])
            if _as_str(r.get("batch_num")) in batches]
    covered = {_as_str(r.get("batch_num")) for r in rows}
    if not rows:
        return "PUSH_RECORD_NOT_FOUND"
    if covered != batches:
        return "EVIDENCE_INCOMPLETE"  # 部分 batch 无推送记录,不得冒充"未发现记录"
    if any(not _as_str(r.get("exe_number")) for r in rows):
        return "EVIDENCE_INCOMPLETE"
    if record.get("exes_truncated"):
        return "EVIDENCE_INCOMPLETE"
    # R2 Review P1-2:走到这里才证明本商品存在完整 spider push 证据,
    # callback 查询失败只在此之后降级(callback 是下游证据)
    if record.get("callback_failed"):
        return "EVIDENCE_INCOMPLETE"

    is_call_by_exe: dict[str, set[int]] = {}
    for r in rows:
        # R2 Review P1-1:缺值/未知值是证据缺失,不是业务 0(禁止 `or 0` 兜底)
        call_flag = _normalize_is_call(r.get("is_call"))
        if call_flag is None:
            return "EVIDENCE_INCOMPLETE"
        is_call_by_exe.setdefault(_as_str(r["exe_number"]), set()).add(call_flag)
    active_by_exe: dict[str, set[str]] = {}
    completed_by_exe: dict[str, set[str]] = {}
    for r in record.get("callback_rows") or []:
        exe = _as_str(r.get("execution_number"))
        target = completed_by_exe if r.get("callback_source") == "completed" else active_by_exe
        target.setdefault(exe, set()).add(_as_str(r.get("push_state")))
    states = [
        _normalize_execution_state(
            is_call_by_exe.get(exe, set()),
            active_by_exe.get(exe, set()),
            completed_by_exe.get(exe, set()))
        for exe in sorted(is_call_by_exe)
    ]
    if not states or "EVIDENCE_INCOMPLETE" in states:
        return "EVIDENCE_INCOMPLETE"
    if all(state == "SUCCESS" for state in states):
        return "CALLBACK_DONE_SAAS_PENDING"
    if any(state == "SUCCESS" for state in states):
        return "CALLBACK_PARTIAL"
    if any(state == "PENDING" for state in states):
        # 纯 PENDING 或 PENDING+NO_CALLBACK_RECORD:存在真实在途回调
        return "CALLBACK_IN_FLIGHT"
    return "PUSH_CONFIRMED_NO_CALLBACK_RECORD"


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
    - approval_non_same_exists(任一 sync_state=2 行)按 [FRONTEND] 前端语义
      ("已审批-非同款")与 spec 对 APPROVAL_OR_FINAL_CLOSURE_PENDING 的定义
      ("审批/结果已经存在,但 IS_SYNC_STANDARD 尚未最终关闭")处理:覆盖
      model_audit_result=0/1/2/3 全部组合——人工审批结果已存在、收口未完成;
      不解释为任何未验证后台状态,也不新增 sync_state 3/4 逻辑。待审批
      (sync_state=1)与已审批非同款同时存在时,待审批优先(当前可操作阶段)。
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
    elif _truthy_flag(record.get("approval_non_same_exists")):
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


def read_observation_series(state_path: Path | str, business_date: str) -> dict[str, Any]:
    """读取该业务日已持久化的权威观察序列(只读 URI,绝不写入)。

    Review R2 P2-1:读取失败(损坏/schema 缺失/query error)绝不折叠成"无观察"。
    返回 {"available", "read_error", "observations"};read_error 只含安全异常
    类型名(如 DatabaseError),不含本地路径、SQL 文本或堆栈。
    文件不存在 → available=False 且 read_error=None(真的还没有观察)。
    """
    path = Path(state_path)
    if not path.is_file():
        return {"available": False, "read_error": None, "observations": []}
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                """SELECT s.observed_at AS observed_at, s.evidence_json AS evidence_json
                   FROM snapshots s JOIN daily_runs r ON r.id = s.run_id
                   WHERE r.business_date = ?
                   ORDER BY s.observed_at, s.id""",
                (business_date,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return {"available": False, "read_error": type(exc).__name__, "observations": []}
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
    return {"available": True, "read_error": None, "observations": series}


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
        pool: _RecyclingConnection | None = None
        try:
            connection = self._connection_factory(self._config)
            pool = _RecyclingConnection(connection, self._connection_factory, self._config)
            connection = None  # 所有权移交 pool(pool 可能中途换成新连接)
            stages, degraded = self._build_stages(pool, business_date)
        except Exception as exc:  # 连接层失败:只降级本面板,凭据/DSN 不进 payload
            payload["status"] = "unavailable"
            payload["error"] = {"type": type(exc).__name__}
            return payload
        finally:
            # Fix B:关闭的是"当前活跃"连接(pool 内部可能已替换),死连接早已被尽力 close
            if pool is not None:
                pool.close()
            elif connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        payload["stages"] = stages
        payload["degraded_queries"] = degraded
        payload["status"] = "unavailable" if stages is None else (
            "partial" if (degraded or stages.get("snapshot_drift")) else "ok")
        return payload

    def _build_growth(self, business_date: str) -> dict[str, Any]:
        paths = runtime_paths(business_date, self._runtime_root)
        result = read_observation_series(paths.state_path, business_date)
        if result["read_error"]:
            # 读取失败 ≠ 无观察:明确降级,不伪装 NORMAL/NO_OBSERVATIONS
            return {
                "available": False,
                "evaluated": False,
                "evaluation_reason": "OBSERVATION_READ_FAILED",
                "read_error": result["read_error"],  # 仅安全异常类型名
                "state": "NORMAL",
                "customer_count": 0,
                "saas_count": 0,
                "customer_delta": 0,
                "saas_delta": 0,
                "consecutive_lag_intervals": 0,
                "observation_count": 0,
                "last_observed_at": None,
            }
        return evaluate_growth(result["observations"], business_date=business_date,
                               now=self._now_fn())

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

    def _build_stages(self, pool: _RecyclingConnection,
                      business_date: str) -> tuple[dict[str, Any] | None, list[str]]:
        degraded: list[str] = []
        day = datetime.fromisoformat(business_date).date()
        base_params = {
            "pool_id": POOL_ID,
            "day_start": f"{business_date} 00:00:00",
            "day_end": f"{(day + timedelta(days=1)).isoformat()} 00:00:00",
        }

        def run(name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            # Fix B:连接死亡后重建失败 → pool.connection 为 None,后续查询
            # 直接降级(不重试 factory、不把未执行查询伪装成空结果)。
            connection = pool.connection
            if connection is None:
                degraded.append(name)
                return []
            try:
                return self._query(connection, name, {**base_params, **params})
            except Exception as exc:
                degraded.append(name)
                if _is_connection_killing_error(exc):
                    # 只废弃死连接并重建给"下一条"查询用;本查询绝不重试
                    pool.replace_after_connection_kill()
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
        # R3:路由层计数与 no_commodity_list 全部来自 routing query 同一语句,
        # 不再用 pool 的 not_closed 跨 SELECT 推 residual。
        if crawl_failed:
            routing_open_total = crawl1_total = crawl2_total = crawl_other = None
            no_commodity_list: int | None = None
            routing_incomplete: int | None = None
        else:
            crawl_row = crawl_rows[0] if crawl_rows else {}
            routing_open_total = int(crawl_row.get("routing_open_total") or 0)
            no_commodity_list = int(crawl_row.get("no_commodity_list") or 0)
            crawl1_total = int(crawl_row.get("crawl_status_1") or 0)
            crawl2_total = int(crawl_row.get("crawl_status_2") or 0)
            crawl_other = int(crawl_row.get("crawl_status_other") or 0)
            routing_incomplete = int(crawl_row.get("routing_evidence_incomplete") or 0)

        # Review P1-1:两个分支各自独立计数,同名 EVIDENCE_INCOMPLETE 不共享。
        crawler_counts: dict[str, int] | None = _empty_counts(CRAWLER_BRANCH_CLASSES)
        downstream_counts: dict[str, int] | None = _empty_counts(DOWNSTREAM_BRANCH_CLASSES)
        reference_pending_total: int | None = 0
        callback_no_search_within_grace: int | None = 0
        now_sh = self._now_fn().astimezone(SHANGHAI).replace(tzinfo=None)
        # Review R2 P1-1:多条 SELECT 不是同一快照;detail 行数超过路由快照 = 反向漂移,
        # 该分支本轮不发布确定性分类(不截断多出商品、不覆盖 route total)。
        # Review R3 P1:pool 与 routing 两个 SELECT 之间的计数差同样只标记 drift,
        # 绝不解释成 NO_COMMODITY_LIST / 数据丢失 / 业务异常。
        snapshot_drift: list[str] = []
        if not pool_failed and not crawl_failed and not_closed != routing_open_total:
            snapshot_drift.append("pool_vs_routing")

        if not crawl_failed:
            # ---- Branch A (R2): crawl_status=1 → task → batch → spider 推送
            # → execution_number → callback 证据。s_task_info 不再执行。----
            crawl1_rows = run("crawl1_commodity_rows", {})
            if "crawl1_commodity_rows" in degraded:
                crawler_counts["EVIDENCE_INCOMPLETE"] += crawl1_total or 0
            elif len(crawl1_rows) > (crawl1_total or 0):
                crawler_counts = None
                snapshot_drift.append("crawler_branch")
            else:
                ids = [int(row["commodity_list_id"]) for row in crawl1_rows[:MAX_CRAWL1_COMMODITIES]]
                task_rows: list[dict[str, Any]] = []
                task_failed = False
                if ids:
                    # Fix A(R1):task_table.reference_id 是 varchar(255),必须 string 绑定
                    task_rows = run("crawl1_task_rows",
                                    {"commodity_list_ids": tuple(str(i) for i in ids)})
                    task_failed = "crawl1_task_rows" in degraded
                tasks_by_commodity: dict[int, list[dict[str, Any]]] = {}
                for row in task_rows:
                    tasks_by_commodity.setdefault(int(row["commodity_list_id"]), []).append(row)
                # 每商品派生 batch 集合(支持多 batch;不假设单 batch)
                batches_by_commodity: dict[int, tuple[set[str], bool]] = {}
                all_batches: set[str] = set()
                for commodity_list_id in ids:
                    tasks = tasks_by_commodity.get(commodity_list_id, [])
                    commodity_batches: set[str] = set()
                    malformed = False
                    for t in tasks:
                        batch = _batch_from_task_id(_as_str(t.get("task_id")))
                        if batch is None:
                            malformed = True
                        else:
                            commodity_batches.add(batch)
                    batches_by_commodity[commodity_list_id] = (commodity_batches, malformed)
                    all_batches |= commodity_batches

                spider_rows: list[dict[str, Any]] = []
                spider_failed = False
                # R2 Review P2:all_batches 必须有独立硬上限,防止无界
                # WHERE batch_num IN (...)。截断不静默:batch 未完整进入
                # 有界查询的商品在 item 级降级为证据不足(见 batches_truncated),
                # 未受截断的商品照常分类;不整体放弃 crawl1 branch。
                batches_truncated = len(all_batches) > MAX_CRAWL1_BATCHES
                bounded_batches = sorted(all_batches)[:MAX_CRAWL1_BATCHES]
                bounded_batch_set = set(bounded_batches)
                if bounded_batches and not task_failed:
                    # batch_num 为 varchar,string 绑定(生产实测走 index_batch_nim)
                    spider_rows = run("crawl1_spider_task_rows",
                                      {"batch_nums": tuple(bounded_batches)})
                    spider_failed = "crawl1_spider_task_rows" in degraded
                spider_by_batch: dict[str, list[dict[str, Any]]] = {}
                for row in spider_rows:
                    spider_by_batch.setdefault(_as_str(row.get("batch_num")), []).append(row)

                all_exes: set[str] = set()
                for rows_of_batch in spider_by_batch.values():
                    for r in rows_of_batch:
                        exe = _as_str(r.get("exe_number"))
                        if exe:
                            all_exes.add(exe)
                exes_truncated = len(all_exes) > MAX_CRAWL1_EXECUTION_NUMBERS
                bounded_exes = sorted(all_exes)[:MAX_CRAWL1_EXECUTION_NUMBERS]
                callback_rows: list[dict[str, Any]] = []
                callback_failed = False
                if bounded_exes and not task_failed and not spider_failed:
                    # execution_number 为 varchar,string 绑定(两表 idx_ex)
                    callback_rows = run("crawl1_callback_rows",
                                        {"execution_numbers": tuple(bounded_exes)})
                    callback_failed = "crawl1_callback_rows" in degraded
                callback_by_exe: dict[str, list[dict[str, Any]]] = {}
                for row in callback_rows:
                    callback_by_exe.setdefault(_as_str(row.get("execution_number")), []).append(row)

                def item_record(commodity_list_id: int,
                                item_batches: set[str], malformed: bool) -> dict[str, Any]:
                    item_rows = [r for b in sorted(item_batches)
                                 for r in spider_by_batch.get(b, [])]
                    item_exes = {_as_str(r.get("exe_number")) for r in item_rows
                                 if _as_str(r.get("exe_number"))}
                    return {
                        "task_count": len(tasks_by_commodity.get(commodity_list_id, [])),
                        "task_failed": task_failed,
                        "malformed": malformed,
                        "batches": item_batches,
                        "spider_failed": spider_failed,
                        "spider_rows": item_rows,
                        "batches_truncated": batches_truncated
                        and not item_batches <= bounded_batch_set,
                        "callback_failed": callback_failed,
                        "callback_rows": [r for exe in sorted(item_exes)
                                          for r in callback_by_exe.get(exe, [])],
                        "exes_truncated": exes_truncated and not item_exes <= set(bounded_exes),
                    }

                for commodity_list_id in ids:
                    item_batches, malformed = batches_by_commodity[commodity_list_id]
                    crawler_counts[classify_crawler_callback(
                        item_record(commodity_list_id, item_batches, malformed))] += 1
                overflow = (crawl1_total or 0) - len(ids)
                if overflow > 0:
                    crawler_counts["EVIDENCE_INCOMPLETE"] += overflow

            # ---- Branch B: crawl_status = 2 → search/model/审批 ----
            crawl2_rows = run("crawl2_search_flags", {})
            if "crawl2_search_flags" in degraded:
                downstream_counts["EVIDENCE_INCOMPLETE"] += crawl2_total or 0
                # P2:查询失败 ≠ confirmed zero,派生辅助计数不可用,不得保留 0
                reference_pending_total = None
                callback_no_search_within_grace = None
            elif len(crawl2_rows) > (crawl2_total or 0):
                downstream_counts = None
                # R3 P2:drift 分支的派生计数不可用,不冒充 0
                reference_pending_total = None
                callback_no_search_within_grace = None
                snapshot_drift.append("downstream_branch")
            else:
                classified = 0
                for row in crawl2_rows[:MAX_CRAWL2_COMMODITIES]:
                    verdict = classify_downstream(row, now=now_sh, grace_seconds=self._grace_seconds)
                    downstream_counts[verdict["state"]] += 1
                    classified += 1
                    if verdict["state"] == "CALLBACK_DONE_NO_SEARCH" and verdict["within_grace"]:
                        callback_no_search_within_grace += 1
                    if _truthy_flag(row.get("reference_pending_approval")):
                        reference_pending_total += 1
                overflow = (crawl2_total or 0) - classified
                if overflow > 0:
                    downstream_counts["EVIDENCE_INCOMPLETE"] += overflow
        else:
            crawler_counts = None
            downstream_counts = None
            reference_pending_total = None
            callback_no_search_within_grace = None

        dominant = None
        if crawler_counts is not None and downstream_counts is not None:
            dominant = _dominant_stage(crawler_counts, downstream_counts, no_commodity_list)
        stages = {
            # Review P1-6:SQL 池计数不得冒充权威客户计数(权威计数在 growth.customer_count)
            "pool_counts_available": not pool_failed,
            "crawl_status_available": not crawl_failed,
            # Review R2 P1-1:多 SELECT 快照一致性显式可见;drift 是取证时点问题,不是业务异常
            "snapshot_consistent": not snapshot_drift,
            "snapshot_drift": snapshot_drift,
            "pool_scope_total": pool_scope_total,
            "not_closed": not_closed,
            "closed_standard": closed_standard,
            "closed_no_same": closed_no_same,
            # R3:routing 层权威基数(routing query 单语句自洽,不依赖 pool 时点)
            "routing_open_total": routing_open_total,
            "no_commodity_list": no_commodity_list,
            "crawl_status_1": crawl1_total,
            "crawl_status_2": crawl2_total,
            "crawl_status_other": crawl_other,
            # Cardinality:>1 条 commodity_list 的商品(无已验证 current-row 规则);
            # 只是 routing 层证据计数,不是业务故障 stage,不参与 dominant
            "routing_evidence_incomplete": routing_incomplete,
            "grace_seconds": self._grace_seconds,
            "crawler_branch": crawler_counts,
            "downstream_branch": downstream_counts,
            "callback_done_no_search_within_grace": callback_no_search_within_grace,
            "reference_pending_approval_total": reference_pending_total,
            # R2:central 诊断已退役(不再查询 s_task_info)。保留 key 以兼容
            # 现有前端/消费者,恒为 None——绝不返回 [] 冒充"查过 central 且无任务"。
            "central_status_detail": None,
            "dominant_stage": dominant,
            "queried_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        return stages, degraded


# 权威已验证 task_id 形态(sql_ops KB §3.4):SAAS-188605_<batch>_<suffix>,
# 例 SAAS-188605_704827316150657024_279774 → batch 188605_704827316150657024。
# Review R2 P1-2:tenant 固定 188605、batch/suffix 必须非空;解析不出→None,绝不产伪 batch。
_TASK_ID_RE = re.compile(r"^SAAS-188605_[^_\s]+_.+$")


def _batch_from_task_id(task_id: str) -> str | None:
    """严格校验;无法验证时返回 None(malformed 证据),而不是看似合法的 batch。"""
    if not task_id:
        return None
    if not _TASK_ID_RE.match(task_id):
        return None
    parts = task_id[len("SAAS-"):].split("_")
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

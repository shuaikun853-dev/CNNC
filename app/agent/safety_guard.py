import re

BLOCKED_KEYWORDS = [
    "UPDATE", "DELETE", "INSERT", "TRUNCATE", "DROP", "ALTER",
    "CREATE", "REPLACE", "MERGE", "CALL", "EXEC",
    "SET", "GRANT", "REVOKE",
]

BLOCKED_PATTERNS = [
    (r"\bINTO\s+OUTFILE\b", "INTO OUTFILE"),
    (r"\bINTO\s+DUMPFILE\b", "INTO DUMPFILE"),
    (r"\bLOAD\s+DATA\b", "LOAD DATA"),
]

CNNC_ALLOWED_TABLES = {
    "commodity_pool_188605",
    "commodity_pool_relation_188605",
    "standard_commodity_relation_188605",
    "standard_commodity_188605",
    "monitor_commodity_price_188605",
    "monitor_commodity_price_last_change_188605",
    "monitor_update_price_record",
    "commodity_guide_price_188605",
    "search_table_188605",
    "task_execute",
}

CNNC_ALLOWED_FQ_TABLES = {
    "saas_onecode_price.commodity_pool_188605",
    "saas_onecode_price.commodity_pool_relation_188605",
    "saas_onecode_price.standard_commodity_relation_188605",
    "saas_onecode_price.standard_commodity_188605",
    "saas_onecode_price.monitor_commodity_price_188605",
    "saas_onecode_price.monitor_commodity_price_last_change_188605",
    "saas_onecode_price.monitor_update_price_record",
    "saas_onecode_price.commodity_guide_price_188605",
    "saas_compare_price.search_table_188605",
    "onecode_base3.task_execute",
}

_ALL_ALLOWED = CNNC_ALLOWED_TABLES | CNNC_ALLOWED_FQ_TABLES

_SQL_KEYWORDS_NO_TABLE = {
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "IS", "NULL",
    "AS", "ON", "JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "CROSS",
    "GROUP", "BY", "ORDER", "HAVING", "LIMIT", "OFFSET", "UNION",
    "ALL", "DISTINCT", "EXISTS", "BETWEEN", "LIKE", "CASE", "WHEN",
    "THEN", "ELSE", "END", "WITH", "ASC", "DESC", "COUNT", "SUM",
    "AVG", "MIN", "MAX", "CONCAT", "DATE_SUB", "NOW", "INTERVAL",
    "HOUR", "DAY", "IF", "IFNULL", "COALESCE", "CAST",
}

_ALLOWED_START_RE = re.compile(r"^\s*(SELECT|WITH)\s", re.IGNORECASE)

_MYSQL_EXEC_COMMENT_RE = re.compile(r"/\*!\s*.*?\*/", re.DOTALL)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(--|#).*$", re.MULTILINE)

_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+"
    r"((?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)

_WITH_CTE_NAMES_RE = re.compile(
    r"\bWITH\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(",
    re.IGNORECASE,
)


def _strip_all_comments(sql: str) -> str:
    sql = _MYSQL_EXEC_COMMENT_RE.sub("", sql)
    sql = _BLOCK_COMMENT_RE.sub("", sql)
    sql = _LINE_COMMENT_RE.sub("", sql)
    return sql


def _has_multi_statement(sql: str) -> bool:
    cleaned = _strip_all_comments(sql)
    parts = [p.strip() for p in cleaned.split(";") if p.strip()]
    return len(parts) > 1


def _extract_table_names(sql: str) -> list[str]:
    cleaned = _strip_all_comments(sql)
    cte_names = set()
    for m in _WITH_CTE_NAMES_RE.finditer(cleaned):
        cte_names.add(m.group(1).lower())

    tables = []
    seen = set()
    for m in _TABLE_RE.finditer(cleaned):
        name = m.group(1)
        if name.lower() in cte_names:
            continue
        if name.lower() in _SQL_KEYWORDS_NO_TABLE:
            continue
        if name.lower() not in seen:
            seen.add(name.lower())
            tables.append(name)

    return tables


def _check_table_whitelist(tables: list[str]) -> tuple[bool, str]:
    for t in tables:
        if t.lower() in {a.lower() for a in _ALL_ALLOWED}:
            continue
        if re.search(r"_(\d{6})$", t):
            suffix = re.search(r"_(\d{6})$", t).group(1)
            if suffix != "188605":
                return False, f"表 {t} 属于非中核租户 {suffix}，不在允许范围内"
        else:
            return False, f"表 {t} 不在中核允许表白名单中"
    return True, ""


def validate_sql(sql: str) -> tuple[bool, str]:
    if not sql or not sql.strip():
        return False, "SQL 为空"

    if _has_multi_statement(sql):
        return False, "禁止多语句拼接，只允许单条 SQL"

    if _MYSQL_EXEC_COMMENT_RE.search(sql):
        return False, "禁止 MySQL 可执行注释 /*!...*/"

    if _BLOCK_COMMENT_RE.search(sql):
        return False, "禁止块注释 /*...*/，如需注释请移除"

    if _LINE_COMMENT_RE.search(sql):
        return False, "禁止行内注释（-- 或 #），如需注释请移除"

    cleaned = _strip_all_comments(sql)
    cleaned = cleaned.strip().rstrip(";").strip()

    if not _ALLOWED_START_RE.match(cleaned):
        return False, f"SQL 必须以 SELECT 或 WITH 开头，当前开头: {cleaned[:30]}"

    upper = cleaned.upper()
    tokens = re.findall(r"[A-Z_]+", upper)
    for kw in BLOCKED_KEYWORDS:
        if kw in tokens:
            return False, f"SQL 包含禁止关键字: {kw}"

    for pattern, label in BLOCKED_PATTERNS:
        if re.search(pattern, upper):
            return False, f"SQL 包含禁止语法: {label}"

    tables = _extract_table_names(cleaned)
    if tables:
        ok, reason = _check_table_whitelist(tables)
        if not ok:
            return False, reason
    else:
        return False, "未检测到表名，默认拒绝。请确保 SQL 中 FROM/JOIN 后包含明确的中核表名"

    return True, "安全检查通过"


def validate_and_explain(sql: str) -> dict:
    safe, reason = validate_sql(sql)
    return {"safe": safe, "reason": reason}

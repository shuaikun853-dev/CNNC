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

_TASK_EXECUTE_NAMES = {"task_execute", "onecode_base3.task_execute"}

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

_WITH_CTE_NAMES_RE = re.compile(
    r"\bWITH\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(",
    re.IGNORECASE,
)

_TABLE_REF_PATTERN = re.compile(
    r"(?:FROM|JOIN)\s+"
    r"((?:`[^`]+`\.)*`[^`]+`"
    r"|(?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)

_COMMA_TABLE_PATTERN = re.compile(
    r",\s*"
    r"((?:`[^`]+`\.)*`[^`]+`"
    r"|(?:[a-zA-Z_][a-zA-Z0-9_]*\.)*[a-zA-Z_][a-zA-Z0-9_]*)",
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


def _normalize_table_name(raw: str) -> str:
    name = raw.strip()
    if name.startswith("`") and name.endswith("`"):
        name = name[1:-1]
    parts = []
    for segment in name.split("."):
        if segment.startswith("`") and segment.endswith("`"):
            segment = segment[1:-1]
        parts.append(segment)
    return ".".join(parts)


def _extract_table_names(sql: str) -> list[str]:
    cleaned = _strip_all_comments(sql)
    cte_names = set()
    for m in _WITH_CTE_NAMES_RE.finditer(cleaned):
        cte_names.add(m.group(1).lower())

    raw_names = []
    for m in _TABLE_REF_PATTERN.finditer(cleaned):
        raw_names.append(m.group(1))

    from_clause_text = ""
    for m in re.finditer(r"\bFROM\b\s+(.*?)(?:\b(?:WHERE|GROUP|ORDER|HAVING|LIMIT|JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|UNION)\b|$)", cleaned, re.IGNORECASE | re.DOTALL):
        from_clause_text += " " + m.group(1)

    for m in _COMMA_TABLE_PATTERN.finditer(from_clause_text):
        candidate = m.group(1)
        normalized = _normalize_table_name(candidate)
        if normalized.lower() in cte_names:
            continue
        if normalized.lower() in _SQL_KEYWORDS_NO_TABLE:
            continue
        already = False
        for rn in raw_names:
            if _normalize_table_name(rn).lower() == normalized.lower():
                already = True
                break
        if not already:
            raw_names.append(candidate)

    tables = []
    seen = set()
    for raw in raw_names:
        normalized = _normalize_table_name(raw)
        if normalized.lower() in cte_names:
            continue
        if normalized.lower() in _SQL_KEYWORDS_NO_TABLE:
            continue
        if normalized.lower() not in seen:
            seen.add(normalized.lower())
            tables.append(normalized)

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


def _has_task_execute(tables: list[str]) -> bool:
    for t in tables:
        if t.lower() in {n.lower() for n in _TASK_EXECUTE_NAMES}:
            return True
    return False


def _check_task_execute_scope(sql: str) -> tuple[bool, str]:
    upper = sql.upper()
    has_tenant_188605 = bool(re.search(r"TENANT_ID\s*=?\s*'?\s*188605\s*'?", sql, re.IGNORECASE))
    has_pool_222 = bool(re.search(r"COMMODITY_POOL_ID\s*=?\s*'?222'?", sql, re.IGNORECASE))
    has_ref_subquery = (
        "REFERENCE_ID" in upper
        and "commodity_pool_188605" in upper
        and "COMMODITY_POOL_ID" in upper
        and "222" in sql
    )

    if has_tenant_188605 and (has_pool_222 or has_ref_subquery):
        return True, ""

    if not has_tenant_188605:
        return False, "task_execute 是多租户共享表，必须包含 TENANT_ID='188605'"
    if not has_pool_222 and not has_ref_subquery:
        return False, "task_execute 查询必须同时包含 COMMODITY_POOL_ID=222 或通过 reference_id 子查询关联商品池 222"
    return False, "task_execute 查询范围不足，必须限定中核租户和商品池"


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

    if _has_task_execute(tables):
        ok, reason = _check_task_execute_scope(cleaned)
        if not ok:
            return False, reason

    return True, "安全检查通过"


def validate_and_explain(sql: str) -> dict:
    safe, reason = validate_sql(sql)
    return {"safe": safe, "reason": reason}

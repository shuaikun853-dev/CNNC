MOCK_QUERY_RESULT = {
    "note": "MOCK 数据 - 未连接真实数据库",
    "rows": [],
    "row_count": 0,
    "executed": False,
    "readonly_mode": True,
}


async def execute_readonly_query(sql: str, params: dict | None = None) -> dict:
    return {
        **MOCK_QUERY_RESULT,
        "sql": sql,
        "params": params or {},
        "message": "当前为离线只读模式，不执行任何 SQL。请将 SQL 模板复制到只读副本手动执行。",
    }

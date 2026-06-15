import pytest
from unittest.mock import patch
from app.agent.sql_selector import get_sql_templates


_DANGEROUS_TEMPLATE = {
    "scenario": "test_dangerous",
    "templates": [
        {
            "name": "evil_update",
            "purpose": "dangerous test",
            "params": [],
            "sql": "UPDATE commodity_pool_188605 SET STATUS = 0 WHERE COMMODITY_POOL_ID = 222",
            "result_hint": "",
        },
        {
            "name": "safe_select",
            "purpose": "safe test",
            "params": [],
            "sql": "SELECT COUNT(*) FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 LIMIT 1",
            "result_hint": "count",
        },
    ],
}


class TestSqlSelectorSafety:
    def test_dangerous_sql_blocked_no_leak(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous")

        evil = [r for r in results if r["name"] == "evil_update"][0]
        assert evil["safe"] is False
        assert evil.get("blocked") is True
        assert "blocked_reason" in evil
        assert evil.get("sql") is None or evil.get("sql", "") == ""

    def test_safe_sql_included(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous")

        safe = [r for r in results if r["name"] == "safe_select"][0]
        assert safe["safe"] is True
        assert safe.get("sql") is not None
        assert "SELECT" in safe["sql"]

    def test_blocked_sql_has_template_name(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous")

        evil = [r for r in results if r["name"] == "evil_update"][0]
        assert "template_name" in evil
        assert evil["template_name"] == "evil_update"
        assert evil.get("sql") is None or evil.get("sql", "") == ""

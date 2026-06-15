import pytest
from unittest.mock import patch
from app.agent.sql_selector import get_sql_templates, _is_empty_value, _render_sql


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
            "params": [{"name": "commodity_id", "example": "123"}],
            "sql": "SELECT COUNT(*) FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 AND ID = :commodity_id LIMIT 1",
            "result_hint": "count",
        },
    ],
}

_RENDER_TEMPLATE = {
    "scenario": "test_render",
    "templates": [
        {
            "name": "with_colon_param",
            "purpose": "colon param render",
            "params": [{"name": "commodity_id", "example": "123"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 AND ID = :commodity_id LIMIT 1",
            "result_hint": "",
        },
        {
            "name": "with_mustache_param",
            "purpose": "mustache param render",
            "params": [{"name": "start_time", "example": "2026-01-01"}],
            "sql": "SELECT * FROM monitor_commodity_price_188605 WHERE COMMODITY_POOL_ID = 222 AND update_time >= '{{start_time}}' LIMIT 10",
            "result_hint": "",
        },
        {
            "name": "multi_param",
            "purpose": "multi param render",
            "params": [
                {"name": "commodity_id", "example": "123"},
                {"name": "status", "example": "1"},
            ],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 AND ID = :commodity_id AND STATUS = :status LIMIT 1",
            "result_hint": "",
        },
    ],
}


class TestSqlSelectorSafety:
    def test_dangerous_sql_blocked_no_leak(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous")

        evil = [r for r in results if r.get("name") == "evil_update" or r.get("template_name") == "evil_update"][0]
        assert evil.get("safe") is False or evil.get("blocked") is True
        assert evil.get("sql") is None or evil.get("sql", "") == ""

    def test_safe_sql_included(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous")

        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert safe.get("safe") is True
        assert safe.get("sql") is not None or safe.get("pending_params") is True

    def test_blocked_sql_has_template_name(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous")

        evil = [r for r in results if r.get("name") == "evil_update" or r.get("template_name") == "evil_update"][0]
        assert "template_name" in evil
        assert evil["template_name"] == "evil_update"
        assert evil.get("sql") is None or evil.get("sql", "") == ""

    def test_missing_params_detected(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous")

        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert safe.get("missing_params") is not None
        assert "commodity_id" in safe["missing_params"]
        assert safe.get("pending_params") is True

    def test_provided_params_no_missing(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous", provided_params={"commodity_id": "123"})

        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert safe.get("missing_params") is None or safe.get("missing_params") == []
        assert safe.get("pending_params") is not True

    def test_provided_params_sql_rendered(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous", provided_params={"commodity_id": "123"})

        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert safe.get("pending_params") is not True
        rendered = safe.get("sql", "")
        assert "123" in rendered
        assert ":commodity_id" not in rendered

    def test_empty_string_param_treated_as_missing(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous", provided_params={"commodity_id": ""})

        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert safe.get("pending_params") is True
        assert "commodity_id" in safe.get("missing_params", [])

    def test_none_param_treated_as_missing(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous", provided_params={"commodity_id": None})

        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert safe.get("pending_params") is True
        assert "commodity_id" in safe.get("missing_params", [])

    def test_whitespace_param_treated_as_missing(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous", provided_params={"commodity_id": "   "})

        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert safe.get("pending_params") is True
        assert "commodity_id" in safe.get("missing_params", [])

    def test_reason_field_present_in_results(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous")

        for r in results:
            assert "reason" in r, f"模板 {r.get('name')} 缺少 reason 字段"

    def test_missing_params_has_reason(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous")

        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert "缺少必要参数" in safe.get("reason", "")


_MUSTACHE_TEMPLATE = {
    "scenario": "test_mustache",
    "templates": [
        {
            "name": "mustache_select",
            "purpose": "test mustache rendering",
            "params": [{"name": "commodity_id", "example": "456"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE ID = {{commodity_id}} AND COMMODITY_POOL_ID = 222 LIMIT 1",
            "result_hint": "row",
        },
    ],
}


class TestSqlRendering:
    def test_mustache_param_rendered(self):
        with patch("app.agent.sql_selector._load_template", return_value=_MUSTACHE_TEMPLATE):
            results = get_sql_templates("test_mustache", provided_params={"commodity_id": "456"})

        tpl = results[0]
        assert tpl.get("pending_params") is not True
        rendered = tpl.get("sql", "")
        assert "456" in rendered
        assert "{{commodity_id}}" not in rendered
        assert tpl.get("safe") is True

    def test_mustache_missing_still_pending(self):
        with patch("app.agent.sql_selector._load_template", return_value=_MUSTACHE_TEMPLATE):
            results = get_sql_templates("test_mustache")

        tpl = results[0]
        assert tpl.get("pending_params") is True
        assert "commodity_id" in tpl.get("missing_params", [])

    def test_colon_param_string_value_quoted(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous", provided_params={"commodity_id": "abc"})

        safe = [r for r in results if r.get("name") == "safe_select"][0]
        rendered = safe.get("sql", "")
        assert "'abc'" in rendered

    def test_colon_param_numeric_value_not_quoted(self):
        numeric_template = {
            "scenario": "test_numeric",
            "templates": [{
                "name": "num_select",
                "purpose": "test numeric",
                "params": [{"name": "limit", "example": "10"}],
                "sql": "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 LIMIT :limit",
                "result_hint": "",
            }],
        }
        with patch("app.agent.sql_selector._load_template", return_value=numeric_template):
            results = get_sql_templates("test_numeric", provided_params={"limit": 10})

        tpl = results[0]
        rendered = tpl.get("sql", "")
        assert "10" in rendered
        assert ":limit" not in rendered


class TestEmptyValueDetection:
    def test_none_param_treated_as_missing(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous", provided_params={"commodity_id": None})
        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert "commodity_id" in safe.get("missing_params", [])

    def test_empty_string_param_treated_as_missing(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous", provided_params={"commodity_id": ""})
        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert "commodity_id" in safe.get("missing_params", [])

    def test_whitespace_param_treated_as_missing(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous", provided_params={"commodity_id": "   "})
        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert "commodity_id" in safe.get("missing_params", [])

    def test_valid_param_not_missing(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous", provided_params={"commodity_id": "123"})
        safe = [r for r in results if r.get("name") == "safe_select"][0]
        assert "commodity_id" not in safe.get("missing_params", [])
        assert safe.get("pending_params") is not True


_RENDER_TEMPLATE = {
    "scenario": "test_render",
    "templates": [
        {
            "name": "with_colon_param",
            "purpose": "colon param test",
            "params": [{"name": "commodity_id", "example": "123"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE ID = :commodity_id AND COMMODITY_POOL_ID = 222",
            "result_hint": "single row",
        },
        {
            "name": "with_mustache_param",
            "purpose": "mustache param test",
            "params": [{"name": "start_time", "example": "2026-01-01"}],
            "sql": "SELECT * FROM task_execute_188605 WHERE CREATE_TIME >= '{{start_time}}' AND TENANT_ID = '188605'",
            "result_hint": "task list",
        },
    ],
}


class TestSqlRendering:
    def test_colon_param_rendered(self):
        with patch("app.agent.sql_selector._load_template", return_value=_RENDER_TEMPLATE):
            results = get_sql_templates("test_render", provided_params={"commodity_id": "456"})
        tpl = [r for r in results if r.get("name") == "with_colon_param"][0]
        assert tpl.get("pending_params") is not True
        assert ":commodity_id" not in tpl.get("sql", "")
        assert "'456'" in tpl.get("sql", "")

    def test_mustache_param_rendered(self):
        with patch("app.agent.sql_selector._load_template", return_value=_RENDER_TEMPLATE):
            results = get_sql_templates("test_render", provided_params={"start_time": "2026-06-01"})
        tpl = [r for r in results if r.get("name") == "with_mustache_param"][0]
        assert tpl.get("pending_params") is not True
        assert "{{start_time}}" not in tpl.get("sql", "")
        assert "'2026-06-01'" in tpl.get("sql", "")

    def test_partial_params_still_pending(self):
        with patch("app.agent.sql_selector._load_template", return_value=_RENDER_TEMPLATE):
            results = get_sql_templates("test_render", provided_params={"commodity_id": "456"})
        mustache = [r for r in results if r.get("name") == "with_mustache_param"][0]
        assert mustache.get("pending_params") is True
        assert "start_time" in mustache.get("missing_params", [])

    def test_rendered_sql_passes_safety(self):
        with patch("app.agent.sql_selector._load_template", return_value=_RENDER_TEMPLATE):
            results = get_sql_templates("test_render", provided_params={"commodity_id": "456"})
        tpl = [r for r in results if r.get("name") == "with_colon_param"][0]
        assert tpl.get("safe") is True
        assert tpl.get("blocked") is not True

    def test_reason_field_present(self):
        with patch("app.agent.sql_selector._load_template", return_value=_RENDER_TEMPLATE):
            results = get_sql_templates("test_render", provided_params={"commodity_id": "456"})
        tpl = [r for r in results if r.get("name") == "with_colon_param"][0]
        assert "reason" in tpl
        assert tpl["reason"] != ""

    def test_missing_params_has_reason(self):
        with patch("app.agent.sql_selector._load_template", return_value=_RENDER_TEMPLATE):
            results = get_sql_templates("test_render")
        tpl = [r for r in results if r.get("name") == "with_colon_param"][0]
        assert "缺少必要参数" in tpl.get("reason", "")

    def test_blocked_sql_has_reason(self):
        with patch("app.agent.sql_selector._load_template", return_value=_DANGEROUS_TEMPLATE):
            results = get_sql_templates("test_dangerous")
        evil = [r for r in results if r.get("name") == "evil_update" or r.get("template_name") == "evil_update"][0]
        assert "reason" in evil
        assert evil["reason"] != ""


class TestParamTypeRestriction:
    _TPL = {
        "scenario": "test_types",
        "templates": [{
            "name": "type_test",
            "purpose": "type restriction test",
            "params": [{"name": "commodity_id", "example": "123"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE ID = :commodity_id AND COMMODITY_POOL_ID = 222 LIMIT 1",
            "result_hint": "",
        }],
    }

    def test_list_param_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TPL):
            results = get_sql_templates("test_types", provided_params={"commodity_id": [1, 2]})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True
        assert tpl.get("sql") == "" or tpl.get("sql") is None
        assert "类型" in tpl.get("reason", "") or "不支持" in tpl.get("reason", "")

    def test_dict_param_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TPL):
            results = get_sql_templates("test_types", provided_params={"commodity_id": {"x": 1}})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True
        assert tpl.get("sql") == "" or tpl.get("sql") is None

    def test_tuple_param_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TPL):
            results = get_sql_templates("test_types", provided_params={"commodity_id": (1, 2)})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_set_param_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TPL):
            results = get_sql_templates("test_types", provided_params={"commodity_id": {1, 2}})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_int_param_allowed(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TPL):
            results = get_sql_templates("test_types", provided_params={"commodity_id": 123})
        tpl = results[0]
        assert tpl.get("safe") is True
        assert tpl.get("blocked") is not True

    def test_float_param_for_commodity_id_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TPL):
            results = get_sql_templates("test_types", provided_params={"commodity_id": 1.5})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_bool_param_for_commodity_id_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TPL):
            results = get_sql_templates("test_types", provided_params={"commodity_id": True})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_empty_list_treated_as_missing(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TPL):
            results = get_sql_templates("test_types", provided_params={"commodity_id": []})
        tpl = results[0]
        assert tpl.get("pending_params") is True or tpl.get("blocked") is True


class TestParamValueSafety:
    _COMMODITY_TPL = {
        "scenario": "test_commodity",
        "templates": [{
            "name": "commodity_lookup",
            "purpose": "commodity lookup",
            "params": [{"name": "commodity_id", "example": "123"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE ID = :commodity_id AND COMMODITY_POOL_ID = 222 LIMIT 1",
            "result_hint": "",
        }],
    }

    _TIME_TPL = {
        "scenario": "test_time",
        "templates": [{
            "name": "time_range",
            "purpose": "time range",
            "params": [{"name": "start_time", "example": "2026-01-01"}, {"name": "end_time", "example": "2026-01-02"}],
            "sql": "SELECT * FROM monitor_commodity_price_188605 WHERE COMMODITY_POOL_ID = 222 AND update_time >= :start_time AND update_time < :end_time LIMIT 10",
            "result_hint": "",
        }],
    }

    _TASK_TPL = {
        "scenario": "test_task",
        "templates": [{
            "name": "task_lookup",
            "purpose": "task lookup",
            "params": [{"name": "task_type", "example": "price_push"}, {"name": "hours", "example": "24"}],
            "sql": "SELECT * FROM onecode_base3.task_execute WHERE TENANT_ID = '188605' AND COMMODITY_POOL_ID = 222 AND TASK_TYPE = :task_type AND create_time >= DATE_SUB(NOW(), INTERVAL :hours HOUR) LIMIT 50",
            "result_hint": "",
        }],
    }

    _EXEC_TPL = {
        "scenario": "test_exec",
        "templates": [{
            "name": "exec_lookup",
            "purpose": "exec lookup",
            "params": [{"name": "execution_number", "example": "TASK_20260615-001"}],
            "sql": "SELECT * FROM onecode_base3.task_execute WHERE TENANT_ID = '188605' AND COMMODITY_POOL_ID = 222 AND EXECUTION_NUMBER = :execution_number LIMIT 1",
            "result_hint": "",
        }],
    }

    def test_valid_commodity_id_passes(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._COMMODITY_TPL):
            results = get_sql_templates("test_commodity", provided_params={"commodity_id": "597894113655676928"})
        tpl = results[0]
        assert tpl.get("safe") is True
        assert tpl.get("blocked") is not True

    def test_sql_injection_commodity_id_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._COMMODITY_TPL):
            results = get_sql_templates("test_commodity", provided_params={"commodity_id": "597894113655676928 OR 1=1"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True
        assert tpl.get("sql") == "" or tpl.get("sql") is None

    def test_semicolon_injection_time_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TIME_TPL):
            results = get_sql_templates("test_time", provided_params={
                "start_time": "2026-06-01 00:00:00; DELETE FROM a",
                "end_time": "2026-06-15 00:00:00",
            })
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True
        assert tpl.get("sql") == "" or tpl.get("sql") is None

    def test_backslash_in_task_type_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TASK_TPL):
            results = get_sql_templates("test_task", provided_params={"task_type": "abc\\def", "hours": "24"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_semicolon_in_task_type_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TASK_TPL):
            results = get_sql_templates("test_task", provided_params={"task_type": "abc;drop", "hours": "24"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_valid_execution_number_passes(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._EXEC_TPL):
            results = get_sql_templates("test_exec", provided_params={"execution_number": "TASK_20260615-001"})
        tpl = results[0]
        assert tpl.get("safe") is True
        assert tpl.get("blocked") is not True

    def test_valid_datetime_passes(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TIME_TPL):
            results = get_sql_templates("test_time", provided_params={
                "start_time": "2026-06-01 00:00:00",
                "end_time": "2026-06-15 00:00:00",
            })
        tpl = results[0]
        assert tpl.get("safe") is True
        assert tpl.get("blocked") is not True

    def test_date_only_passes(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TIME_TPL):
            results = get_sql_templates("test_time", provided_params={
                "start_time": "2026-06-01",
                "end_time": "2026-06-15",
            })
        tpl = results[0]
        assert tpl.get("safe") is True

    def test_invalid_datetime_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TIME_TPL):
            results = get_sql_templates("test_time", provided_params={
                "start_time": "not-a-date",
                "end_time": "2026-06-15",
            })
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_comment_injection_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._COMMODITY_TPL):
            results = get_sql_templates("test_commodity", provided_params={"commodity_id": "123 --"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_unknown_param_with_single_quote_blocked(self):
        unknown_tpl = {
            "scenario": "test_unknown",
            "templates": [{
                "name": "unknown_param",
                "purpose": "unknown param test",
                "params": [{"name": "custom_field", "example": "abc"}],
                "sql": "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 AND TITLE = :custom_field LIMIT 1",
                "result_hint": "",
            }],
        }
        with patch("app.agent.sql_selector._load_template", return_value=unknown_tpl):
            results = get_sql_templates("test_unknown", provided_params={"custom_field": "abc' OR '1'='1"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True


class TestDoubleQuoteFix:
    _QUOTED_MUSTACHE_TPL = {
        "scenario": "test_quoted_mustache",
        "templates": [{
            "name": "quoted_mustache",
            "purpose": "quoted mustache test",
            "params": [{"name": "title", "example": "abc"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 AND TITLE = '{{title}}' LIMIT 1",
            "result_hint": "",
        }],
    }

    _QUOTED_COLON_TPL = {
        "scenario": "test_quoted_colon",
        "templates": [{
            "name": "quoted_colon",
            "purpose": "quoted colon test",
            "params": [{"name": "start_time", "example": "2026-01-01"}],
            "sql": "SELECT * FROM monitor_commodity_price_188605 WHERE COMMODITY_POOL_ID = 222 AND update_time >= ':start_time' LIMIT 1",
            "result_hint": "",
        }],
    }

    _UNQUOTED_MUSTACHE_TPL = {
        "scenario": "test_unquoted_mustache",
        "templates": [{
            "name": "unquoted_mustache",
            "purpose": "unquoted mustache test",
            "params": [{"name": "title", "example": "abc"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 AND TITLE = {{title}} LIMIT 1",
            "result_hint": "",
        }],
    }

    _UNQUOTED_COLON_TPL = {
        "scenario": "test_unquoted_colon",
        "templates": [{
            "name": "unquoted_colon",
            "purpose": "unquoted colon test",
            "params": [{"name": "start_time", "example": "2026-01-01"}],
            "sql": "SELECT * FROM monitor_commodity_price_188605 WHERE COMMODITY_POOL_ID = 222 AND update_time >= :start_time LIMIT 1",
            "result_hint": "",
        }],
    }

    def test_quoted_mustache_no_double_quotes(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._QUOTED_MUSTACHE_TPL):
            results = get_sql_templates("test_quoted_mustache", provided_params={"title": "abc"})
        tpl = results[0]
        sql = tpl.get("sql", "")
        assert "''abc''" not in sql, f"双重引号: {sql}"
        assert "TITLE = 'abc'" in sql

    def test_unquoted_mustache_gets_quotes(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._UNQUOTED_MUSTACHE_TPL):
            results = get_sql_templates("test_unquoted_mustache", provided_params={"title": "abc"})
        tpl = results[0]
        sql = tpl.get("sql", "")
        assert "'abc'" in sql

    def test_quoted_colon_no_double_quotes(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._QUOTED_COLON_TPL):
            results = get_sql_templates("test_quoted_colon", provided_params={"start_time": "2026-06-01 00:00:00"})
        tpl = results[0]
        sql = tpl.get("sql", "")
        assert "''2026" not in sql, f"双重引号: {sql}"

    def test_unquoted_colon_gets_quotes(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._UNQUOTED_COLON_TPL):
            results = get_sql_templates("test_unquoted_colon", provided_params={"start_time": "2026-06-01 00:00:00"})
        tpl = results[0]
        sql = tpl.get("sql", "")
        assert "'2026-06-01 00:00:00'" in sql

    def test_no_remaining_placeholders(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._QUOTED_COLON_TPL):
            results = get_sql_templates("test_quoted_colon", provided_params={"start_time": "2026-06-01 00:00:00"})
        tpl = results[0]
        sql = tpl.get("sql", "")
        assert ":start_time" not in sql
        assert "{{start_time}}" not in sql


class TestPostRenderChecks:
    _PLACEHOLDER_TPL = {
        "scenario": "test_placeholder",
        "templates": [{
            "name": "placeholder_test",
            "purpose": "placeholder test",
            "params": [{"name": "a", "example": "1"}, {"name": "b", "example": "2"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE ID = :a AND TITLE = {{b}} AND COMMODITY_POOL_ID = 222 LIMIT 1",
            "result_hint": "",
        }],
    }

    def test_unresolved_placeholder_blocks_sql(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._PLACEHOLDER_TPL):
            results = get_sql_templates("test_placeholder", provided_params={"a": "1"})
        tpl = results[0]
        assert tpl.get("pending_params") is True or tpl.get("blocked") is True
        assert tpl.get("sql") == "" or tpl.get("sql") is None or "{{b}}" in tpl.get("sql", "")

    def test_rendered_sql_validated_by_safety_guard(self):
        injection_tpl = {
            "scenario": "test_render_safety",
            "templates": [{
                "name": "render_injection",
                "purpose": "render injection test",
                "params": [{"name": "custom", "example": "val"}],
                "sql": "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 AND TITLE = :custom LIMIT 1",
                "result_hint": "",
            }],
        }
        with patch("app.agent.sql_selector._load_template", return_value=injection_tpl):
            results = get_sql_templates("test_render_safety", provided_params={"custom": "val'; DELETE FROM commodity_pool_188605; --"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True


class TestRemainingPlaceholderDetection:
    _EQ_TPL = {
        "scenario": "test_eq_param",
        "templates": [{
            "name": "eq_no_space",
            "purpose": "test ID=:param",
            "params": [],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE ID=:undeclared AND COMMODITY_POOL_ID = 222 LIMIT 1",
            "result_hint": "",
        }],
    }

    _SPACE_TPL = {
        "scenario": "test_space_param",
        "templates": [{
            "name": "space_param",
            "purpose": "test ID = :param",
            "params": [],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE ID = :undeclared AND COMMODITY_POOL_ID = 222 LIMIT 1",
            "result_hint": "",
        }],
    }

    _IN_TPL = {
        "scenario": "test_in_param",
        "templates": [{
            "name": "in_param",
            "purpose": "test IN (:param)",
            "params": [],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE ID IN (:undeclared) AND COMMODITY_POOL_ID = 222 LIMIT 1",
            "result_hint": "",
        }],
    }

    _MUSTACHE_TPL = {
        "scenario": "test_mustache_undeclared",
        "templates": [{
            "name": "mustache_undeclared",
            "purpose": "test {{ undeclared }}",
            "params": [],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE ID = {{ undeclared }} AND COMMODITY_POOL_ID = 222 LIMIT 1",
            "result_hint": "",
        }],
    }

    def test_eq_no_space_undeclared_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._EQ_TPL):
            results = get_sql_templates("test_eq_param")
        tpl = results[0]
        assert tpl.get("pending_params") is True or tpl.get("blocked") is True
        assert tpl.get("sql") == "" or tpl.get("sql") is None

    def test_space_undeclared_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._SPACE_TPL):
            results = get_sql_templates("test_space_param")
        tpl = results[0]
        assert tpl.get("pending_params") is True or tpl.get("blocked") is True
        assert tpl.get("sql") == "" or tpl.get("sql") is None

    def test_in_undeclared_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._IN_TPL):
            results = get_sql_templates("test_in_param")
        tpl = results[0]
        assert tpl.get("pending_params") is True or tpl.get("blocked") is True

    def test_mustache_undeclared_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._MUSTACHE_TPL):
            results = get_sql_templates("test_mustache_undeclared")
        tpl = results[0]
        assert tpl.get("pending_params") is True or tpl.get("blocked") is True

    def test_time_value_not_false_positive(self):
        time_tpl = {
            "scenario": "test_time_no_fp",
            "templates": [{
                "name": "time_check",
                "purpose": "time check",
                "params": [{"name": "start_time", "example": "2026-01-01"}, {"name": "end_time", "example": "2026-01-02"}],
                "sql": "SELECT * FROM monitor_commodity_price_188605 WHERE COMMODITY_POOL_ID = 222 AND update_time >= :start_time AND update_time < :end_time LIMIT 10",
                "result_hint": "",
            }],
        }
        with patch("app.agent.sql_selector._load_template", return_value=time_tpl):
            results = get_sql_templates("test_time_no_fp", provided_params={
                "start_time": "2026-06-01 00:00:00",
                "end_time": "2026-06-15 00:00:00",
            })
        tpl = results[0]
        assert tpl.get("safe") is True
        assert tpl.get("pending_params") is not True
        assert ":00" not in tpl.get("sql", "").replace("'2026-06-01 00:00:00'", "").replace("'2026-06-15 00:00:00'", "")


class TestDoubleQuoteDetectionFixed:
    _DOUBLE_QUOTED_MUSTACHE_TPL = {
        "scenario": "test_dq_mustache",
        "templates": [{
            "name": "dq_mustache",
            "purpose": "double quote mustache",
            "params": [{"name": "title", "example": "abc"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 AND TITLE = ''{{title}}'' LIMIT 1",
            "result_hint": "",
        }],
    }

    _DOUBLE_QUOTED_COLON_TPL = {
        "scenario": "test_dq_colon",
        "templates": [{
            "name": "dq_colon",
            "purpose": "double quote colon",
            "params": [{"name": "start_time", "example": "2026-01-01"}],
            "sql": "SELECT * FROM monitor_commodity_price_188605 WHERE COMMODITY_POOL_ID = 222 AND update_time >= '':start_time'' LIMIT 1",
            "result_hint": "",
        }],
    }

    _SINGLE_QUOTED_MUSTACHE_TPL = {
        "scenario": "test_sq_mustache",
        "templates": [{
            "name": "sq_mustache",
            "purpose": "single quote mustache",
            "params": [{"name": "title", "example": "abc"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 AND TITLE = '{{title}}' LIMIT 1",
            "result_hint": "",
        }],
    }

    _UNQUOTED_MUSTACHE_TPL = {
        "scenario": "test_uq_mustache",
        "templates": [{
            "name": "uq_mustache",
            "purpose": "unquoted mustache",
            "params": [{"name": "title", "example": "abc"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 AND TITLE = {{title}} LIMIT 1",
            "result_hint": "",
        }],
    }

    def test_double_quoted_mustache_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._DOUBLE_QUOTED_MUSTACHE_TPL):
            results = get_sql_templates("test_dq_mustache", provided_params={"title": "abc"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True
        assert tpl.get("sql") == "" or tpl.get("sql") is None

    def test_double_quoted_colon_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._DOUBLE_QUOTED_COLON_TPL):
            results = get_sql_templates("test_dq_colon", provided_params={"start_time": "2026-06-01 00:00:00"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True
        assert tpl.get("sql") == "" or tpl.get("sql") is None

    def test_single_quoted_mustache_renders_ok(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._SINGLE_QUOTED_MUSTACHE_TPL):
            results = get_sql_templates("test_sq_mustache", provided_params={"title": "abc"})
        tpl = results[0]
        assert tpl.get("safe") is True
        assert "TITLE = 'abc'" in tpl.get("sql", "")

    def test_unquoted_mustache_renders_ok(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._UNQUOTED_MUSTACHE_TPL):
            results = get_sql_templates("test_uq_mustache", provided_params={"title": "abc"})
        tpl = results[0]
        assert tpl.get("safe") is True
        assert "'abc'" in tpl.get("sql", "")


class TestNonStringScalarValidation:
    _TIME_TPL = {
        "scenario": "test_time_scalar",
        "templates": [{
            "name": "time_scalar",
            "purpose": "time scalar test",
            "params": [{"name": "start_time", "example": "2026-01-01"}, {"name": "end_time", "example": "2026-01-02"}],
            "sql": "SELECT * FROM monitor_commodity_price_188605 WHERE COMMODITY_POOL_ID = 222 AND update_time >= :start_time AND update_time < :end_time LIMIT 10",
            "result_hint": "",
        }],
    }

    _COMMODITY_TPL = {
        "scenario": "test_commodity_scalar",
        "templates": [{
            "name": "commodity_scalar",
            "purpose": "commodity scalar test",
            "params": [{"name": "commodity_id", "example": "123"}],
            "sql": "SELECT * FROM commodity_pool_188605 WHERE ID = :commodity_id AND COMMODITY_POOL_ID = 222 LIMIT 1",
            "result_hint": "",
        }],
    }

    _EXEC_TPL = {
        "scenario": "test_exec_scalar",
        "templates": [{
            "name": "exec_scalar",
            "purpose": "exec scalar test",
            "params": [{"name": "execution_number", "example": "TASK_001"}],
            "sql": "SELECT * FROM onecode_base3.task_execute WHERE TENANT_ID = '188605' AND COMMODITY_POOL_ID = 222 AND EXECUTION_NUMBER = :execution_number LIMIT 1",
            "result_hint": "",
        }],
    }

    _TASK_TPL = {
        "scenario": "test_task_scalar",
        "templates": [{
            "name": "task_scalar",
            "purpose": "task scalar test",
            "params": [{"name": "task_type", "example": "price_push"}, {"name": "hours", "example": "24"}],
            "sql": "SELECT * FROM onecode_base3.task_execute WHERE TENANT_ID = '188605' AND COMMODITY_POOL_ID = 222 AND TASK_TYPE = :task_type AND create_time >= DATE_SUB(NOW(), INTERVAL :hours HOUR) LIMIT 50",
            "result_hint": "",
        }],
    }

    def test_start_time_int_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TIME_TPL):
            results = get_sql_templates("test_time_scalar", provided_params={"start_time": 123, "end_time": "2026-06-15"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True
        assert tpl.get("sql") == "" or tpl.get("sql") is None

    def test_start_time_float_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TIME_TPL):
            results = get_sql_templates("test_time_scalar", provided_params={"start_time": 1.5, "end_time": "2026-06-15"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_start_time_bool_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TIME_TPL):
            results = get_sql_templates("test_time_scalar", provided_params={"start_time": True, "end_time": "2026-06-15"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_commodity_id_float_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._COMMODITY_TPL):
            results = get_sql_templates("test_commodity_scalar", provided_params={"commodity_id": 1.5})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_commodity_id_bool_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._COMMODITY_TPL):
            results = get_sql_templates("test_commodity_scalar", provided_params={"commodity_id": True})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_commodity_id_int_allowed(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._COMMODITY_TPL):
            results = get_sql_templates("test_commodity_scalar", provided_params={"commodity_id": 597894113655676928})
        tpl = results[0]
        assert tpl.get("safe") is True

    def test_commodity_id_digits_str_allowed(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._COMMODITY_TPL):
            results = get_sql_templates("test_commodity_scalar", provided_params={"commodity_id": "597894113655676928"})
        tpl = results[0]
        assert tpl.get("safe") is True

    def test_commodity_id_non_digits_str_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._COMMODITY_TPL):
            results = get_sql_templates("test_commodity_scalar", provided_params={"commodity_id": "abc"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_execution_number_int_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._EXEC_TPL):
            results = get_sql_templates("test_exec_scalar", provided_params={"execution_number": 123})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

    def test_execution_number_valid_str_allowed(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._EXEC_TPL):
            results = get_sql_templates("test_exec_scalar", provided_params={"execution_number": "TASK_20260615-001"})
        tpl = results[0]
        assert tpl.get("safe") is True

    def test_task_type_valid_str_allowed(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TASK_TPL):
            results = get_sql_templates("test_task_scalar", provided_params={"task_type": "abc.def_001", "hours": "24"})
        tpl = results[0]
        assert tpl.get("safe") is True

    def test_task_type_bool_blocked(self):
        with patch("app.agent.sql_selector._load_template", return_value=self._TASK_TPL):
            results = get_sql_templates("test_task_scalar", provided_params={"task_type": True, "hours": "24"})
        tpl = results[0]
        assert tpl.get("safe") is False or tpl.get("blocked") is True

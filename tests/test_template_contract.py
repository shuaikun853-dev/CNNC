import pytest
import yaml
from pathlib import Path
from app.config import TEMPLATES_DIR
from app.agent.safety_guard import validate_sql


def _load_all_templates() -> list[dict]:
    all_tpls = []
    for path in TEMPLATES_DIR.glob("*.yaml"):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        scenario = data.get("scenario", path.stem)
        for tpl in data.get("templates", []):
            all_tpls.append({"scenario": scenario, "file": path.name, **tpl})
    return all_tpls


class TestTemplateContract:
    @pytest.fixture(scope="class")
    def all_templates(self):
        return _load_all_templates()

    def test_no_874649_in_sql(self, all_templates):
        for tpl in all_templates:
            sql = tpl.get("sql", "")
            assert "874649" not in sql, f"{tpl['file']}/{tpl['name']} 包含 874649"

    def test_no_874649_in_all_text(self, all_templates):
        for tpl in all_templates:
            full_text = str(tpl)
            assert "874649" not in full_text, f"{tpl['file']}/{tpl['name']} 包含 874649"

    def test_no_hongwei_in_sql(self, all_templates):
        for tpl in all_templates:
            sql = tpl.get("sql", "")
            assert "宏伟" not in sql, f"{tpl['file']}/{tpl['name']} 包含 宏伟"
            assert "_874649" not in sql, f"{tpl['file']}/{tpl['name']} 包含 _874649 表"

    def test_task_execute_has_tenant_id(self, all_templates):
        for tpl in all_templates:
            sql = tpl.get("sql", "")
            if "task_execute" in sql:
                assert "TENANT_ID" in sql and "188605" in sql, (
                    f"{tpl['file']}/{tpl['name']} 的 task_execute SQL 必须包含 TENANT_ID='188605'"
                )

    def test_task_execute_has_pool_scope(self, all_templates):
        for tpl in all_templates:
            sql = tpl.get("sql", "")
            if "task_execute" in sql:
                has_pool_id = "COMMODITY_POOL_ID" in sql and "222" in sql
                has_ref_subquery = "REFERENCE_ID" in sql and "commodity_pool_188605" in sql
                assert has_pool_id or has_ref_subquery, (
                    f"{tpl['file']}/{tpl['name']} 的 task_execute SQL 必须包含 "
                    f"COMMODITY_POOL_ID=222 或 reference_id 子查询关联商品池 222"
                )

    def test_commodity_pool_has_pool_id(self, all_templates):
        for tpl in all_templates:
            sql = tpl.get("sql", "")
            if "commodity_pool_188605" in sql and "task_execute" not in sql:
                has_pool_constraint = (
                    "COMMODITY_POOL_ID = 222" in sql
                    or "COMMODITY_POOL_ID=222" in sql
                    or "SRC_COMMODITY_POOL_ID = 222" in sql
                    or "SRC_COMMODITY_POOL_ID=222" in sql
                )
                assert has_pool_constraint, (
                    f"{tpl['file']}/{tpl['name']} 涉及商品池必须包含 COMMODITY_POOL_ID=222 限制"
                )

    def test_monitor_has_pool_id(self, all_templates):
        for tpl in all_templates:
            sql = tpl.get("sql", "")
            if "monitor_commodity_price_188605" in sql:
                assert "COMMODITY_POOL_ID" in sql and "222" in sql, (
                    f"{tpl['file']}/{tpl['name']} 涉及 monitor 表必须包含 COMMODITY_POOL_ID=222"
                )

    def test_relation_has_pool_scope(self, all_templates):
        for tpl in all_templates:
            sql = tpl.get("sql", "")
            if "standard_commodity_relation_188605" in sql:
                has_pool = "COMMODITY_POOL_ID" in sql and "222" in sql
                has_join_pool = "commodity_pool_188605" in sql and "COMMODITY_POOL_ID" in sql
                assert has_pool or has_join_pool, (
                    f"{tpl['file']}/{tpl['name']} 涉及同款关系必须限制商品池 222"
                )

    def test_all_sql_pass_safety_guard(self, all_templates):
        for tpl in all_templates:
            sql = tpl.get("sql", "")
            safe, reason = validate_sql(sql)
            assert safe, f"{tpl['file']}/{tpl['name']} SQL 安全检查失败: {reason}"

    def test_all_sql_are_readonly(self, all_templates):
        for tpl in all_templates:
            sql = tpl.get("sql", "").upper()
            for kw in ["UPDATE", "DELETE", "INSERT", "TRUNCATE", "DROP", "ALTER", "CREATE", "REPLACE"]:
                tokens = __import__("re").findall(r"[A-Z_]+", sql)
                assert kw not in tokens, f"{tpl['file']}/{tpl['name']} 包含写库关键字 {kw}"

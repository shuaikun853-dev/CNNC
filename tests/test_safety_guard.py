import pytest
from app.agent.safety_guard import validate_sql, validate_and_explain


class TestValidateSqlBasic:
    def test_select_allowed(self):
        ok, reason = validate_sql("SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222 LIMIT 10")
        assert ok is True

    def test_with_allowed(self):
        ok, reason = validate_sql(
            "WITH x AS (SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222) SELECT * FROM x"
        )
        assert ok is True

    def test_empty_sql(self):
        ok, reason = validate_sql("")
        assert ok is False

    def test_whitespace_only(self):
        ok, reason = validate_sql("   ")
        assert ok is False


class TestBlockedKeywords:
    def test_update_blocked(self):
        ok, reason = validate_sql("UPDATE foo SET bar = 1")
        assert ok is False
        assert "UPDATE" in reason

    def test_update_lowercase_blocked(self):
        ok, reason = validate_sql("update foo set bar = 1")
        assert ok is False

    def test_delete_blocked(self):
        ok, reason = validate_sql("DELETE FROM foo WHERE id = 1")
        assert ok is False
        assert "DELETE" in reason

    def test_insert_blocked(self):
        ok, reason = validate_sql("INSERT INTO foo (a) VALUES (1)")
        assert ok is False
        assert "INSERT" in reason

    def test_drop_blocked(self):
        ok, reason = validate_sql("DROP TABLE foo")
        assert ok is False
        assert "DROP" in reason

    def test_truncate_blocked(self):
        ok, reason = validate_sql("TRUNCATE TABLE foo")
        assert ok is False
        assert "TRUNCATE" in reason

    def test_alter_blocked(self):
        ok, reason = validate_sql("ALTER TABLE foo ADD COLUMN bar INT")
        assert ok is False
        assert "ALTER" in reason

    def test_create_blocked(self):
        ok, reason = validate_sql("CREATE TABLE foo (id INT)")
        assert ok is False
        assert "CREATE" in reason

    def test_replace_blocked(self):
        ok, reason = validate_sql("REPLACE INTO foo (id) VALUES (1)")
        assert ok is False
        assert "REPLACE" in reason

    def test_merge_blocked(self):
        ok, reason = validate_sql("MERGE INTO foo USING bar ON 1=1 WHEN MATCHED THEN UPDATE SET a=1")
        assert ok is False
        assert "MERGE" in reason

    def test_call_blocked(self):
        ok, reason = validate_sql("CALL my_proc()")
        assert ok is False

    def test_exec_blocked(self):
        ok, reason = validate_sql("EXEC my_proc @p1=1")
        assert ok is False
        assert "EXEC" in reason

    def test_set_blocked(self):
        ok, reason = validate_sql("SET @x = 1")
        assert ok is False
        assert "SET" in reason

    def test_grant_blocked(self):
        ok, reason = validate_sql("GRANT ALL ON *.* TO x")
        assert ok is False
        assert "GRANT" in reason

    def test_revoke_blocked(self):
        ok, reason = validate_sql("REVOKE ALL ON *.* FROM x")
        assert ok is False
        assert "REVOKE" in reason


class TestBlockedPatterns:
    def test_into_outfile_blocked(self):
        ok, reason = validate_sql("SELECT 1 INTO OUTFILE '/tmp/x'")
        assert ok is False
        assert "INTO OUTFILE" in reason

    def test_into_dumpfile_blocked(self):
        ok, reason = validate_sql("SELECT 1 INTO DUMPFILE '/tmp/x'")
        assert ok is False
        assert "INTO DUMPFILE" in reason

    def test_load_data_blocked(self):
        ok, reason = validate_sql("LOAD DATA INFILE '/tmp/x' INTO TABLE foo")
        assert ok is False
        assert "LOAD DATA" in reason


class TestMultiStatement:
    def test_multi_statement_with_set(self):
        ok, reason = validate_sql("SELECT 1; SET @x=1")
        assert ok is False
        assert "多语句" in reason

    def test_multi_statement_with_grant(self):
        ok, reason = validate_sql("SELECT 1; GRANT ALL ON *.* TO x")
        assert ok is False
        assert "多语句" in reason

    def test_multi_statement_with_delete(self):
        ok, reason = validate_sql("SELECT * FROM a; DELETE FROM b")
        assert ok is False
        assert "多语句" in reason


class TestCommentBypass:
    def test_mysql_exec_comment_blocked(self):
        ok, reason = validate_sql("SELECT /*!50000 1 INTO OUTFILE '/tmp/x' */ 1")
        assert ok is False
        assert "可执行注释" in reason

    def test_block_comment_blocked(self):
        ok, reason = validate_sql("/* comment */ UPDATE foo SET bar = 1")
        assert ok is False
        assert "块注释" in reason

    def test_line_comment_blocked(self):
        ok, reason = validate_sql("-- fake\nUPDATE foo SET bar = 1")
        assert ok is False
        assert "行内注释" in reason

    def test_hash_comment_blocked(self):
        ok, reason = validate_sql("# fake\nUPDATE foo SET bar = 1")
        assert ok is False

    def test_select_with_update_in_subquery_blocked(self):
        ok, reason = validate_sql("SELECT * FROM (UPDATE foo SET bar = 1) AS t")
        assert ok is False
        assert "UPDATE" in reason


class TestTableWhitelist:
    def test_commodity_pool_874649_rejected(self):
        ok, reason = validate_sql("SELECT * FROM commodity_pool_874649")
        assert ok is False
        assert "874649" in reason or "非中核" in reason

    def test_fq_commodity_pool_874649_rejected(self):
        ok, reason = validate_sql("SELECT * FROM saas_onecode_price.commodity_pool_874649")
        assert ok is False
        assert "874649" in reason or "非中核" in reason

    def test_commodity_pool_123456_rejected(self):
        ok, reason = validate_sql("SELECT * FROM commodity_pool_123456")
        assert ok is False
        assert "123456" in reason or "非中核" in reason

    def test_other_tenant_table_rejected(self):
        ok, reason = validate_sql("SELECT * FROM other_tenant_table")
        assert ok is False

    def test_search_table_874649_rejected(self):
        ok, reason = validate_sql("SELECT * FROM search_table_874649")
        assert ok is False

    def test_fq_search_table_874649_rejected(self):
        ok, reason = validate_sql("SELECT * FROM saas_compare_price.search_table_874649")
        assert ok is False

    def test_with_cte_non_cnnc_table_rejected(self):
        ok, reason = validate_sql(
            "WITH x AS (SELECT * FROM commodity_pool_874649) SELECT * FROM x"
        )
        assert ok is False
        assert "874649" in reason or "非中核" in reason

    def test_cnnc_commodity_pool_allowed(self):
        ok, reason = validate_sql(
            "SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222"
        )
        assert ok is True

    def test_fq_cnnc_commodity_pool_allowed(self):
        ok, reason = validate_sql(
            "SELECT * FROM saas_onecode_price.commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222"
        )
        assert ok is True

    def test_cnnc_relation_allowed(self):
        ok, reason = validate_sql(
            "SELECT * FROM commodity_pool_relation_188605 WHERE SRC_COMMODITY_POOL_ID = 222"
        )
        assert ok is True

    def test_task_execute_allowed(self):
        ok, reason = validate_sql(
            "SELECT * FROM onecode_base3.task_execute WHERE TENANT_ID='188605' AND COMMODITY_POOL_ID=222"
        )
        assert ok is True

    def test_with_cte_cnnc_allowed(self):
        ok, reason = validate_sql(
            "WITH x AS (SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222) SELECT * FROM x"
        )
        assert ok is True

    def test_no_table_rejected(self):
        ok, reason = validate_sql("SELECT 1")
        assert ok is False
        assert "未检测到表名" in reason or "默认拒绝" in reason

    def test_dual_rejected(self):
        ok, reason = validate_sql("SELECT 1 FROM dual")
        assert ok is False

    def test_join_with_non_cnnc_rejected(self):
        ok, reason = validate_sql(
            "SELECT * FROM commodity_pool_188605 cp JOIN other_table ot ON cp.id = ot.id"
        )
        assert ok is False

    def test_multiple_cnnc_tables_allowed(self):
        ok, reason = validate_sql(
            "SELECT * FROM commodity_pool_188605 cp "
            "JOIN standard_commodity_relation_188605 scr ON cp.ID = scr.commodity_id "
            "WHERE cp.COMMODITY_POOL_ID = 222"
        )
        assert ok is True


class TestValidateAndExplain:
    def test_safe_sql(self):
        result = validate_and_explain("SELECT * FROM commodity_pool_188605 WHERE COMMODITY_POOL_ID = 222")
        assert result["safe"] is True
        assert result["reason"] == "安全检查通过"

    def test_unsafe_sql(self):
        result = validate_and_explain("DROP TABLE foo")
        assert result["safe"] is False
        assert "DROP" in result["reason"]

    def test_unsafe_sql_no_leak(self):
        result = validate_and_explain("SELECT 1 INTO OUTFILE '/tmp/x'")
        assert result["safe"] is False
        assert "INTO OUTFILE" in result["reason"]

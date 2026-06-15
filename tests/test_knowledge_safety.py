import pytest
from app.services.knowledge_loader import _sanitize_content, _contains_write_sql


class TestContainsWriteSql:
    def test_update_detected(self):
        assert _contains_write_sql("UPDATE foo SET bar = 1") is True

    def test_delete_detected(self):
        assert _contains_write_sql("DELETE FROM foo") is True

    def test_insert_detected(self):
        assert _contains_write_sql("INSERT INTO foo VALUES (1)") is True

    def test_select_not_detected(self):
        assert _contains_write_sql("SELECT * FROM foo") is False

    def test_with_select_not_detected(self):
        assert _contains_write_sql("WITH cte AS (SELECT 1) SELECT * FROM cte") is False


class TestFencedCodeBlockSanitization:
    def test_sql_lowercase_sanitized(self):
        content = "```sql\nUPDATE foo SET bar = 1;\n```"
        result = _sanitize_content(content)
        assert "UPDATE foo" not in result
        assert "高风险写库 SQL 已脱敏" in result

    def test_sql_uppercase_sanitized(self):
        content = "```SQL\nUPDATE foo SET bar = 1;\n```"
        result = _sanitize_content(content)
        assert "UPDATE foo" not in result
        assert "高风险写库 SQL 已脱敏" in result

    def test_mysql_sanitized(self):
        content = "```mysql\nINSERT INTO foo (a) VALUES (1);\n```"
        result = _sanitize_content(content)
        assert "INSERT INTO" not in result
        assert "高风险写库 SQL 已脱敏" in result

    def test_delete_sql_sanitized(self):
        content = "```sql\nDELETE FROM foo WHERE id = 1;\n```"
        result = _sanitize_content(content)
        assert "DELETE FROM" not in result
        assert "高风险写库 SQL 已脱敏" in result

    def test_no_lang_update_sanitized(self):
        content = "```\nUPDATE foo SET bar = 1;\n```"
        result = _sanitize_content(content)
        assert "UPDATE foo" not in result
        assert "高风险写库 SQL 已脱敏" in result

    def test_select_not_sanitized(self):
        content = "```sql\nSELECT * FROM foo LIMIT 10;\n```"
        result = _sanitize_content(content)
        assert "SELECT * FROM foo" in result
        assert "高风险写库 SQL 已脱敏" not in result

    def test_mixed_blocks_sanitized(self):
        content = "一些文本\n```sql\nUPDATE foo SET bar = 1;\n```\n其他文本"
        result = _sanitize_content(content)
        assert "UPDATE foo" not in result
        assert "其他文本" in result


class TestIndentedCodeBlockSanitization:
    def test_indented_update_sanitized(self):
        content = "正文\n    UPDATE foo SET bar = 1;\n后续"
        result = _sanitize_content(content)
        assert "UPDATE foo" not in result
        assert "高风险写库 SQL 已脱敏" in result

    def test_indented_select_kept(self):
        content = "正文\n    SELECT * FROM foo LIMIT 10;\n后续"
        result = _sanitize_content(content)
        assert "SELECT * FROM foo" in result


class TestHtmlPreSanitization:
    def test_pre_update_sanitized(self):
        content = "<pre>UPDATE foo SET bar = 1;</pre>"
        result = _sanitize_content(content)
        assert "UPDATE foo" not in result
        assert "高风险写库 SQL 已脱敏" in result

    def test_pre_code_insert_sanitized(self):
        content = "<pre><code>INSERT INTO foo (a) VALUES (1);</code></pre>"
        result = _sanitize_content(content)
        assert "INSERT INTO" not in result
        assert "高风险写库 SQL 已脱敏" in result

    def test_pre_select_kept(self):
        content = "<pre>SELECT * FROM foo LIMIT 10;</pre>"
        result = _sanitize_content(content)
        assert "SELECT * FROM foo" in result


class TestKnowledgeSearchNoWriteSqlLeak:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        import app.services.knowledge_loader as mod
        mod._sections = None

    def test_search_modify_price_index(self):
        from app.services.knowledge_loader import search_knowledge
        results = search_knowledge("修改价格指数 SQL", top_k=3)
        for r in results:
            content = r["content"]
            for kw in ["UPDATE", "DELETE", "INSERT"]:
                if "```" in content:
                    blocks = content.split("```")
                    for i in range(1, len(blocks), 2):
                        assert kw not in blocks[i], (
                            f"知识片段 [{r['title']}] 泄漏了写库 SQL: {kw}"
                        )

    def test_search_retry_task_sql(self):
        from app.services.knowledge_loader import search_knowledge
        results = search_knowledge("重推任务 SQL", top_k=3)
        for r in results:
            content = r["content"]
            for kw in ["UPDATE", "DELETE", "INSERT"]:
                if "```" in content:
                    blocks = content.split("```")
                    for i in range(1, len(blocks), 2):
                        assert kw not in blocks[i], (
                            f"知识片段 [{r['title']}] 泄漏了写库 SQL: {kw}"
                        )

    def test_search_high_risk_change_sql(self):
        from app.services.knowledge_loader import search_knowledge
        results = search_knowledge("高风险变更 SQL 模板", top_k=3)
        for r in results:
            content = r["content"]
            for kw in ["UPDATE", "DELETE", "INSERT"]:
                if "```" in content:
                    blocks = content.split("```")
                    for i in range(1, len(blocks), 2):
                        assert kw not in blocks[i], (
                            f"知识片段 [{r['title']}] 泄漏了写库 SQL: {kw}"
                        )

    def test_search_select_retained(self):
        from app.services.knowledge_loader import search_knowledge
        results = search_knowledge("商品基础信息查询", top_k=3)
        has_select = False
        for r in results:
            if "SELECT" in r["content"] and "UPDATE" not in r["content"].split("```")[1] if "```" in r["content"] else True:
                has_select = True
        assert has_select or len(results) == 0

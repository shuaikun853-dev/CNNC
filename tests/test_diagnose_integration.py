import pytest
from unittest.mock import patch


class TestDiagnoseWriteSqlNotLeaked:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def _get_user_content(self, data):
        parts = [str(data.get("knowledge_snippets", []))]
        parts.append(str(data.get("sql_templates", [])))
        return " ".join(parts)

    def _has_notice(self, data):
        all_text = str(data.get("knowledge_snippets", [])) + data.get("risk_notice", "")
        return "第一阶段不展示具体 SQL，需人工确认后处理" in all_text

    def test_high_risk_change_sql_no_update(self):
        r = self.client.post("/diagnose", json={"question": "高风险变更 SQL 模板"})
        assert r.status_code == 200
        data = r.json()
        user_content = self._get_user_content(data)
        for kw in ["UPDATE", "DELETE", "INSERT"]:
            assert kw not in user_content, f"/diagnose 响应泄漏了写库关键字: {kw}"
        assert self._has_notice(data)

    def test_modify_price_index_sql_no_update(self):
        r = self.client.post("/diagnose", json={"question": "修改价格指数 SQL"})
        assert r.status_code == 200
        data = r.json()
        user_content = self._get_user_content(data)
        assert "UPDATE" not in user_content, "/diagnose 响应泄漏了 UPDATE 关键字"
        assert self._has_notice(data)

    def test_manual_modify_price_index(self):
        r = self.client.post("/diagnose", json={"question": "手工修改价格指数"})
        assert r.status_code == 200
        data = r.json()
        assert self._has_notice(data)

    def test_modify_guide_price_sql(self):
        r = self.client.post("/diagnose", json={"question": "修改指导价 SQL"})
        assert r.status_code == 200
        data = r.json()
        assert self._has_notice(data)

    def test_retry_task_sql_no_update(self):
        r = self.client.post("/diagnose", json={"question": "重推任务 SQL"})
        assert r.status_code == 200
        data = r.json()
        user_content = self._get_user_content(data)
        for kw in ["UPDATE", "INSERT"]:
            assert kw not in user_content, f"/diagnose 响应泄漏了写库关键字: {kw}"
        assert self._has_notice(data)

    def test_normal_query_returns_200(self):
        r = self.client.post("/diagnose", json={"question": "商品价格不更新"})
        assert r.status_code == 200

    def test_health_check(self):
        r = self.client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestMissingParams:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_weekly_check_missing_time(self):
        r = self.client.post("/diagnose", json={"question": "中核本周巡检"})
        assert r.status_code == 200
        data = r.json()
        missing = data.get("missing_params", [])
        assert "start_time" in missing or any(
            "start_time" in str(t.get("missing_params", []))
            for t in data.get("sql_templates", [])
        )

    def test_single_item_missing_commodity_id(self):
        r = self.client.post("/diagnose", json={"question": "中核商品价格不更新"})
        assert r.status_code == 200
        data = r.json()
        missing = data.get("missing_params", [])
        assert "commodity_id" in missing or any(
            "commodity_id" in str(t.get("missing_params", []))
            for t in data.get("sql_templates", [])
        )

    def test_with_params_no_missing(self):
        r = self.client.post("/diagnose", json={
            "question": "中核本周巡检",
            "params": {"start_time": "2026-06-08", "end_time": "2026-06-15"},
        })
        assert r.status_code == 200
        data = r.json()
        missing = data.get("missing_params", [])
        assert "start_time" not in missing


class TestHealthEnhanced:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_health_ok_with_all_components(self):
        r = self.client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["knowledge_file_exists"] is True
        assert data["templates_dir_exists"] is True
        assert data["template_count"] > 0
        assert data["readonly_mode"] is True
        assert data["db_connected"] is False

    def test_health_degraded_when_knowledge_missing(self):
        with patch("app.main.KNOWLEDGE_MD_PATH", new="/nonexistent/path.md"):
            r = self.client.get("/health")
            data = r.json()
            assert data["status"] == "degraded"
            assert data["knowledge_file_exists"] is False
            assert "knowledge_file_missing" in data.get("issues", [])

    def test_health_degraded_when_templates_missing(self):
        with patch("app.main.TEMPLATES_DIR", new="/nonexistent/templates"):
            r = self.client.get("/health")
            data = r.json()
            assert data["status"] == "degraded"
            assert data["templates_dir_exists"] is False


class TestStructuredErrors:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_yaml_error_returns_structured_error(self):
        bad_yaml = "scenario: test\ntemplates: [{invalid yaml"
        with patch("app.agent.sql_selector._load_template", side_effect=Exception("YAML parse error")):
            r = self.client.post("/diagnose", json={"question": "中核本周巡检"})
            assert r.status_code == 500
            detail = r.json()["detail"]
            assert "error_code" in detail

    def test_knowledge_load_error_graceful(self):
        with patch("app.main.search_knowledge", side_effect=Exception("read error")):
            r = self.client.post("/diagnose", json={"question": "中核本周巡检"})
            assert r.status_code == 200


class TestKnowledgeStatus:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_knowledge_load_error_returns_status(self):
        import app.services.knowledge_loader as mod
        with patch("app.main.search_knowledge", side_effect=mod.KnowledgeLoadError("知识库文件不存在")):
            r = self.client.post("/diagnose", json={"question": "中核本周巡检"})
            assert r.status_code == 200
            data = r.json()
            assert data["knowledge_status"] == "error"
            assert data["knowledge_error"] != ""

    def test_knowledge_ok_status(self):
        r = self.client.post("/diagnose", json={"question": "中核本周巡检"})
        assert r.status_code == 200
        data = r.json()
        assert data["knowledge_status"] == "ok"
        assert data["knowledge_error"] == ""

    def test_health_includes_knowledge_status(self):
        r = self.client.get("/health")
        data = r.json()
        assert "knowledge_status" in data
        assert "knowledge_error" in data


class TestEmptyParamsTreatedAsMissing:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_empty_string_param_is_missing(self):
        r = self.client.post("/diagnose", json={
            "question": "中核本周巡检",
            "params": {"start_time": "", "end_time": "2026-06-15"},
        })
        assert r.status_code == 200
        data = r.json()
        missing = data.get("missing_params", [])
        assert "start_time" in missing

    def test_null_param_is_missing(self):
        r = self.client.post("/diagnose", json={
            "question": "中核本周巡检",
            "params": {"start_time": None, "end_time": "2026-06-15"},
        })
        assert r.status_code == 200
        data = r.json()
        missing = data.get("missing_params", [])
        assert "start_time" in missing

    def test_whitespace_param_is_missing(self):
        r = self.client.post("/diagnose", json={
            "question": "中核本周巡检",
            "params": {"start_time": "   ", "end_time": "2026-06-15"},
        })
        assert r.status_code == 200
        data = r.json()
        missing = data.get("missing_params", [])
        assert "start_time" in missing


class TestSqlRenderedWhenParamsComplete:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_weekly_check_with_params_renders_sql(self):
        r = self.client.post("/diagnose", json={
            "question": "中核本周巡检",
            "params": {"start_time": "2026-06-08", "end_time": "2026-06-15"},
        })
        assert r.status_code == 200
        data = r.json()
        for tpl in data.get("sql_templates", []):
            if not tpl.get("pending_params") and not tpl.get("blocked"):
                sql = tpl.get("sql", "")
                assert ":start_time" not in sql
                assert ":end_time" not in sql

    def test_sql_template_has_reason_field(self):
        r = self.client.post("/diagnose", json={"question": "中核本周巡检"})
        assert r.status_code == 200
        data = r.json()
        for tpl in data.get("sql_templates", []):
            assert "reason" in tpl, f"模板 {tpl.get('name')} 缺少 reason 字段"


class TestHealthKnowledgeStatusConsistency:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_cold_start_no_unknown(self):
        r = self.client.get("/health")
        data = r.json()
        assert data["knowledge_status"] != "unknown", "knowledge_status 不允许返回 unknown"

    def test_knowledge_ok_status_ok(self):
        r = self.client.get("/health")
        data = r.json()
        assert data["status"] == "ok"
        assert data["knowledge_status"] == "ok"

    def test_knowledge_missing_degraded(self):
        with patch("app.main.KNOWLEDGE_MD_PATH", new="/nonexistent/path.md"):
            import app.services.knowledge_loader as mod
            mod._sections = None
            mod._load_error = None
            r = self.client.get("/health")
            data = r.json()
            assert data["status"] in ("degraded", "error")
            assert data["knowledge_status"] in ("degraded", "error")
            assert len(data.get("issues", [])) > 0

    def test_knowledge_read_error_degraded(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = "知识库文件读取失败"
        with patch("app.main.KNOWLEDGE_MD_PATH", new="/nonexistent/path.md"):
            r = self.client.get("/health")
            data = r.json()
            assert data["knowledge_status"] in ("degraded", "error")

    def test_no_path_leak_in_response(self):
        with patch("app.main.KNOWLEDGE_MD_PATH", new="/some/secret/path.md"):
            import app.services.knowledge_loader as mod
            mod._sections = None
            mod._load_error = None
            r = self.client.get("/health")
            text = r.text
            assert "/some/secret/path.md" not in text
            assert "Traceback" not in text

    def test_templates_missing_degraded(self):
        with patch("app.main.TEMPLATES_DIR", new="/nonexistent/templates"):
            r = self.client.get("/health")
            data = r.json()
            assert data["status"] == "degraded"
            assert "templates_dir_missing" in data.get("issues", [])


class TestSqlParamTypeRestrictionIntegration:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_list_param_blocked_in_diagnose(self):
        r = self.client.post("/diagnose", json={
            "question": "中核商品价格不更新",
            "params": {"commodity_id": [1, 2]},
        })
        assert r.status_code == 200
        data = r.json()
        for tpl in data.get("sql_templates", []):
            if tpl.get("name") and "commodity_id" in str(tpl.get("params", [])):
                if "commodity_id" not in tpl.get("missing_params", []):
                    assert tpl.get("safe") is False or tpl.get("blocked") is True or tpl.get("sql") == ""

    def test_dict_param_blocked_in_diagnose(self):
        r = self.client.post("/diagnose", json={
            "question": "中核商品价格不更新",
            "params": {"commodity_id": {"x": 1}},
        })
        assert r.status_code == 200
        data = r.json()
        for tpl in data.get("sql_templates", []):
            if "commodity_id" not in tpl.get("missing_params", []):
                assert tpl.get("safe") is False or tpl.get("blocked") is True or tpl.get("sql") == ""

    def test_sql_injection_param_blocked_in_diagnose(self):
        r = self.client.post("/diagnose", json={
            "question": "中核商品价格不更新",
            "params": {"commodity_id": "597894113655676928 OR 1=1"},
        })
        assert r.status_code == 200
        data = r.json()
        for tpl in data.get("sql_templates", []):
            if tpl.get("sql") and "OR 1=1" not in tpl.get("missing_params", []):
                assert "OR 1=1" not in tpl.get("sql", "")


class TestSqlTemplateItemModel:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_openapi_json_accessible(self):
        r = self.client.get("/openapi.json")
        assert r.status_code == 200

    def test_sql_template_item_has_description_in_schema(self):
        r = self.client.get("/openapi.json")
        schema = r.json()
        st_schema = schema.get("components", {}).get("schemas", {}).get("SqlTemplateItem", {})
        props = st_schema.get("properties", {})
        assert "description" in props, "SqlTemplateItem 缺少 description 字段"

    def test_sql_param_spec_in_schema(self):
        r = self.client.get("/openapi.json")
        schema = r.json()
        assert "SqlParamSpec" in schema.get("components", {}).get("schemas", {})

    def test_diagnose_response_has_description(self):
        r = self.client.post("/diagnose", json={"question": "中核本周巡检"})
        assert r.status_code == 200
        data = r.json()
        for tpl in data.get("sql_templates", []):
            assert "description" in tpl


class TestRelationCheckDescription:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_relation_check_description_not_empty(self):
        r = self.client.post("/diagnose", json={"question": "中核同款关系排查"})
        assert r.status_code == 200
        data = r.json()
        for tpl in data.get("sql_templates", []):
            assert tpl.get("description", "") != "", f"模板 {tpl.get('name')} description 为空"


class TestNonStringScalarIntegration:
    @pytest.fixture(autouse=True)
    def _setup(self):
        import app.services.knowledge_loader as mod
        mod._sections = None
        mod._load_error = None
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_start_time_int_blocked(self):
        r = self.client.post("/diagnose", json={
            "question": "中核本周巡检",
            "params": {"start_time": 123, "end_time": "2026-06-15"},
        })
        assert r.status_code == 200
        data = r.json()
        for tpl in data.get("sql_templates", []):
            param_names = [p.get("name", "") for p in tpl.get("params", [])]
            if "start_time" in param_names and "start_time" not in tpl.get("missing_params", []):
                assert tpl.get("safe") is False or tpl.get("blocked") is True or tpl.get("sql") == "", f"模板 {tpl.get('name')} start_time=123 未被阻断"

    def test_commodity_id_float_blocked(self):
        r = self.client.post("/diagnose", json={
            "question": "中核商品价格不更新",
            "params": {"commodity_id": 1.5},
        })
        assert r.status_code == 200
        data = r.json()
        for tpl in data.get("sql_templates", []):
            if "commodity_id" not in tpl.get("missing_params", []):
                assert tpl.get("safe") is False or tpl.get("blocked") is True or tpl.get("sql") == ""

    def test_commodity_id_bool_blocked(self):
        r = self.client.post("/diagnose", json={
            "question": "中核商品价格不更新",
            "params": {"commodity_id": True},
        })
        assert r.status_code == 200
        data = r.json()
        for tpl in data.get("sql_templates", []):
            if "commodity_id" not in tpl.get("missing_params", []):
                assert tpl.get("safe") is False or tpl.get("blocked") is True or tpl.get("sql") == ""

    def test_commodity_id_int_allowed(self):
        r = self.client.post("/diagnose", json={
            "question": "中核商品价格不更新",
            "params": {"commodity_id": 597894113655676928},
        })
        assert r.status_code == 200
        data = r.json()
        has_rendered = False
        for tpl in data.get("sql_templates", []):
            if tpl.get("sql") and tpl.get("safe") is True:
                has_rendered = True
        assert has_rendered

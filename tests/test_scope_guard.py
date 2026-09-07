import pytest
from app.agent.router import check_scope

pytestmark = pytest.mark.critical_safety


class TestScopeGuardWhitelist:
    def test_874649_explicit_tenant_rejected(self):
        result = check_scope("租户 874649 商品价格不更新")
        assert result.in_scope is False
        assert result.error_code == "OUT_OF_SCOPE"

    def test_hongwei_rejected(self):
        result = check_scope("宏伟爬虫长时间采集中")
        assert result.in_scope is False
        assert result.error_code == "OUT_OF_SCOPE"

    def test_underscore_874649_table_rejected(self):
        result = check_scope("commodity_pool_874649 数据问题")
        assert result.in_scope is False
        assert result.error_code == "OUT_OF_SCOPE"

    def test_tenant_123456_rejected(self):
        result = check_scope("租户 123456 的推送失败")
        assert result.in_scope is False
        assert result.error_code == "OUT_OF_SCOPE"

    def test_commodity_pool_123456_rejected(self):
        result = check_scope("commodity_pool_123456 怎么查")
        assert result.in_scope is False
        assert result.error_code == "OUT_OF_SCOPE"

    def test_tenant_id_753266_rejected(self):
        result = check_scope("tenant_id=753266 的任务积压")
        assert result.in_scope is False
        assert result.error_code == "OUT_OF_SCOPE"

    def test_tenant_id_quote_874649_rejected(self):
        result = check_scope("TENANT_ID='874649' 的推送失败")
        assert result.in_scope is False
        assert result.error_code == "OUT_OF_SCOPE"

    def test_search_table_874649_rejected(self):
        result = check_scope("search_table_874649 怎么查")
        assert result.in_scope is False
        assert result.error_code == "OUT_OF_SCOPE"

    def test_nanshuibeidiao_rejected(self):
        result = check_scope("南水北调商品价格不更新")
        assert result.in_scope is False
        assert result.error_code == "OUT_OF_SCOPE"

    def test_huasheng_rejected(self):
        result = check_scope("华盛推送失败")
        assert result.in_scope is False
        assert result.error_code == "OUT_OF_SCOPE"

    def test_cnnc_normal_pass(self):
        result = check_scope("中核商品价格不更新")
        assert result.in_scope is True

    def test_188605_normal_pass(self):
        result = check_scope("188605 商品价格不更新")
        assert result.in_scope is True

    def test_cnnc_weekly_pass(self):
        result = check_scope("中核本周巡检")
        assert result.in_scope is True

    def test_generic_question_pass(self):
        result = check_scope("商品价格不更新")
        assert result.in_scope is True

    def test_empty_question_pass(self):
        result = check_scope("")
        assert result.in_scope is True

    def test_sku_six_digit_pass(self):
        result = check_scope("sku 123456 价格不更新")
        assert result.in_scope is True

    def test_sku_dash_six_digit_pass(self):
        result = check_scope("SKU-753266 状态异常")
        assert result.in_scope is True

    def test_long_commodity_id_pass(self):
        result = check_scope("商品 567840422583885824 没价格")
        assert result.in_scope is True

    def test_date_six_digit_pass(self):
        result = check_scope("20260615 中核巡检")
        assert result.in_scope is True

    def test_tenant_code_prefix_rejected(self):
        result = check_scope("租户编码 123456 推送失败")
        assert result.in_scope is False


class TestScopeGuardDiagnoseIntegration:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from fastapi.testclient import TestClient
        from app.main import app
        self.client = TestClient(app)

    def test_out_of_scope_returns_400(self):
        r = self.client.post("/diagnose", json={"question": "租户 123456 的推送失败"})
        assert r.status_code == 400

    def test_commodity_pool_123456_returns_400(self):
        r = self.client.post("/diagnose", json={"question": "commodity_pool_123456 怎么查"})
        assert r.status_code == 400

    def test_tenant_id_753266_returns_400(self):
        r = self.client.post("/diagnose", json={"question": "tenant_id=753266 的任务积压"})
        assert r.status_code == 400

    def test_nanshuibeidiao_returns_400(self):
        r = self.client.post("/diagnose", json={"question": "南水北调商品价格不更新"})
        assert r.status_code == 400

    def test_huasheng_returns_400(self):
        r = self.client.post("/diagnose", json={"question": "华盛推送失败"})
        assert r.status_code == 400

    def test_cnnc_normal_returns_200(self):
        r = self.client.post("/diagnose", json={"question": "中核商品价格不更新"})
        assert r.status_code == 200

    def test_188605_normal_returns_200(self):
        r = self.client.post("/diagnose", json={"question": "188605 商品价格不更新"})
        assert r.status_code == 200

    def test_generic_returns_200(self):
        r = self.client.post("/diagnose", json={"question": "商品价格不更新"})
        assert r.status_code == 200

    def test_sku_returns_200(self):
        r = self.client.post("/diagnose", json={"question": "sku 123456 价格不更新"})
        assert r.status_code == 200

    def test_sku_dash_returns_200(self):
        r = self.client.post("/diagnose", json={"question": "SKU-753266 状态异常"})
        assert r.status_code == 200

import pytest
from app.workflows.router import route_workflow


def test_route_work_01():
    resp = route_workflow("中核新增监控商品同款审批")
    assert resp.workflow_name == "work_01_same_product_approval"
    assert resp.confidence >= 0.8


def test_route_work_01_keyword_match():
    resp = route_workflow("同款匹配处理")
    assert resp.workflow_name == "work_01_same_product_approval"


def test_route_work_02():
    resp = route_workflow("客户反馈价格偏高")
    assert resp.workflow_name == "work_02_price_index_feedback"
    assert resp.confidence >= 0.8


def test_route_work_02_multiple_keywords():
    resp = route_workflow("价格异常勘误反馈")
    assert resp.workflow_name == "work_02_price_index_feedback"


def test_route_work_03():
    resp = route_workflow("同款不足需要补同款")
    assert resp.workflow_name == "work_03_same_product_shortage"


def test_route_work_04():
    resp = route_workflow("失败班车处理失败")
    assert resp.workflow_name == "work_04_failed_bus"


def test_route_work_07():
    resp = route_workflow("推送失败核查")
    assert resp.workflow_name == "work_07_push_check"


def test_route_work_08_ad_hoc():
    resp = route_workflow("帮我分析一下数据")
    assert resp.workflow_name == "work_08_ad_hoc"
    assert resp.confidence < 0.6


def test_route_always_returns_execution_allowed_false():
    resp = route_workflow("中核同款审批")
    assert resp.execution_allowed is False
    assert resp.executed is False

"""Issue #13 Slice C-STATIC:预爬页静态资产冒烟测试。

只验证 /dashboard/static/* 静态挂载与页面骨架(导航互链、关键 hook、零外链);
API 路由由后续切片接线,本文件不依赖任何运行时数据。
"""
from fastapi.testclient import TestClient

from app.main import app


def test_prewarm_html_served_with_key_hooks():
    client = TestClient(app)
    r = client.get("/dashboard/static/prewarm.html")
    assert r.status_code == 200
    text = r.text
    assert "客户链接预爬" in text
    assert 'data-testid="kpi-strip"' in text
    assert "重新爬取此 URL" in text
    assert 'data-testid="links-table"' in text
    assert 'data-testid="filter-bar"' in text
    assert 'data-testid="link-drawer"' in text
    assert 'data-testid="history-timeline"' in text
    assert 'data-testid="recrawl-button"' in text
    assert 'id="recrawl-route-hint"' in text
    # 多页导航:预爬页激活项指向自身
    assert '<a class="nav-link active" href="/dashboard/prewarm">客户链接预爬</a>' in text


def test_index_links_to_prewarm():
    client = TestClient(app)
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert 'href="/dashboard/prewarm"' in r.text
    # 多页外壳:shell.css 先于 app.css 加载
    assert r.text.index("/dashboard/static/shell.css") < r.text.index("/dashboard/static/app.css")


def test_shell_css_served_with_tokens_and_nav():
    client = TestClient(app)
    r = client.get("/dashboard/static/shell.css")
    assert r.status_code == 200
    text = r.text
    assert "--ok" in text and "--err" in text          # 五色令牌
    assert ".app-nav" in text and ".nav-link" in text  # 共享导航样式
    assert "http://" not in text and "https://" not in text  # 零外链


def test_prewarm_css_served_without_external_links():
    client = TestClient(app)
    r = client.get("/dashboard/static/prewarm.css")
    assert r.status_code == 200
    assert "http://" not in r.text and "https://" not in r.text


def test_prewarm_js_served_without_external_src():
    client = TestClient(app)
    r = client.get("/dashboard/static/prewarm.js")
    assert r.status_code == 200
    text = r.text
    assert "http://" not in text and "https://" not in text
    # 关键行为接线存在:KPI 标签映射 / 重爬轮询 / 操作令牌头
    assert "reference_link_count" in text
    assert "X-Dashboard-Action-Token" in text
    assert "/dashboard/api/prewarm/links" in text

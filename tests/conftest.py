"""pytest 全局配置。

让所有测试默认禁用 audit_saas_excel_file.run_main 中的"爬取模板自动导出"，
避免测试用例污染真实的 ./爬取模板/<日期>/ 目录。

测试如果**需要**验证模板导出行为，请显式传 excluded_output_dir=tmp_path 并
unset 这个环境变量，或者直接调用 export_excluded_templates() 函数。
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _suppress_excluded_export(monkeypatch):
    """默认禁用 run_main 的爬取模板自动导出。

    个别测试如需启用，可在用例内 monkeypatch.delenv("AUDIT_SUPPRESS_EXCLUDED_EXPORT", raising=False)。
    """
    monkeypatch.setenv("AUDIT_SUPPRESS_EXCLUDED_EXPORT", "1")
    yield

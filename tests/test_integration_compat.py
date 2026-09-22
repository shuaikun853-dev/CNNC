import importlib
from pathlib import Path


def test_mall_mapping_legacy_import_is_the_canonical_module():
    canonical = importlib.import_module('app.integrations.zhaoshang_mall_mapping')
    legacy = importlib.import_module('scripts.zhaoshang_mall_mapping')
    assert legacy is canonical
    assert legacy.ZhaoshangMallMapping is canonical.ZhaoshangMallMapping
    assert canonical.ROOT == Path(__file__).resolve().parents[1]
    assert canonical.DEFAULT_MAPPING_PATH == canonical.ROOT / '商城sourceId映射表' / '商城sourceId映射表.xlsx'


import pytest


@pytest.mark.parametrize('name', ['curl_utils', 'auth_manager', 'cnnc_auth_core', 'cnnc_auth_refresh'])
def test_legacy_auth_imports_resolve_to_one_canonical_module(name):
    canonical = importlib.import_module('app.integrations.' + name)
    legacy = importlib.import_module('scripts.' + name)
    assert legacy is canonical
    if hasattr(canonical, 'ROOT'):
        assert canonical.ROOT == Path(__file__).resolve().parents[1]


def test_auth_browser_path_remains_the_original_cli():
    canonical = importlib.import_module('app.integrations.cnnc_auth_refresh')
    assert canonical._BROWSER_SCRIPT == Path(__file__).resolve().parents[1] / 'scripts' / 'cnnc_auth_browser.py'


def test_auth_patch_point_is_shared_across_compatibility_imports(monkeypatch):
    canonical = importlib.import_module('app.integrations.cnnc_auth_refresh')
    legacy = importlib.import_module('scripts.cnnc_auth_refresh')
    sentinel = object()
    monkeypatch.setattr(legacy, 'ensure_daily_curl_fresh', sentinel)
    assert canonical.ensure_daily_curl_fresh is sentinel


@pytest.mark.parametrize('name', ['write_audit_workbook', '_display_header', '_format_cell_value'])
def test_workbook_functions_have_one_implementation(name):
    canonical = importlib.import_module('app.services.audit_workbook')
    legacy = importlib.import_module('scripts.audit_saas_excel_file')
    assert getattr(legacy, name) is getattr(canonical, name)


def test_detail_client_has_one_implementation():
    canonical = importlib.import_module('app.integrations.cnnc_detail')
    legacy = importlib.import_module('scripts.audit_saas_excel_file')
    assert legacy.call_detail_api is canonical.call_detail_api
    assert legacy.build_detail_request_body is canonical.build_detail_request_body


def test_remote_tool_configuration_has_one_implementation():
    canonical = importlib.import_module('app.integrations.zhaoshang_tool_config')
    legacy = importlib.import_module('scripts.monitor_stb_fallback')
    assert legacy.load_zhaoshang_db_env is canonical.load_zhaoshang_db_env


def test_staples_layout_constants_are_not_owned_by_cli():
    context = importlib.import_module('app.services.runtime_context')
    legacy = importlib.import_module('scripts.staples_prewarm_run')
    for name in ('DEFAULT_WORK_DIR_NAME','PREWARM_STORE_NAME','SHARED_CACHE_NAME','STAPLES_LOCK_NAME'):
        assert getattr(context, name) == getattr(legacy, name)


def test_staples_wrapper_preserves_legacy_module_identity():
    canonical = importlib.import_module('app.integrations.monitor_stb_fallback')
    legacy = importlib.import_module('scripts.monitor_stb_fallback')
    assert legacy is canonical
    assert legacy.run_stbchina_fallback is canonical.run_stbchina_fallback


def test_application_has_no_script_dependency_in_static_graph():
    from tools.architecture_graph import scan_repository
    graph = scan_repository(Path(__file__).resolve().parents[1])
    forbidden = [(a,b) for a,b in graph.edges if a.startswith('app.') and (b == 'scripts' or b.startswith('scripts.'))]
    assert not forbidden

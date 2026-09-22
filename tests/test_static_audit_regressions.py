"""Regression evidence for hard name errors and an unused duplicate audit engine."""
import ast
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from openpyxl import Workbook


def test_exact_export_validation_annotations_can_be_resolved():
    from app.services.legacy_zhaoshang_adapter import CnncLegacyZhaoshangAdapter
    hints = get_type_hints(CnncLegacyZhaoshangAdapter.validate_existing_export_exact_requests)
    assert 'requests' in hints and 'return' in hints


def test_legacy_excel_runner_has_no_unused_duplicate_batch_auditor():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root/'scripts/audit_saas_excel_file.py').read_text(encoding='utf-8'))
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert 'run_audit' in functions
    assert '_run_batch_model_audit' not in functions


def test_staples_adapter_reaches_injected_executor_and_reads_its_result(tmp_path, monkeypatch):
    from scripts import audit_saas_excel_file as legacy
    executable = tmp_path/'stbchina_fast_crawler.py'
    executable.write_text('# inert fixture; never executed', encoding='utf-8')
    original_path = legacy.Path
    def mapped_path(value):
        candidate = original_path(value)
        return executable if candidate.name == executable.name else candidate
    monkeypatch.setattr(legacy, 'Path', mapped_path)
    url = 'https://www.stbchina.cn/product-fixture'
    calls = []
    def fake_process(command, **options):
        calls.append(command)
        assert options['env']['PYTHONIOENCODING'] == 'utf-8'
        assert Path(command[1]) == executable
        output = Path(command[command.index('--output')+1])
        workbook = Workbook()
        workbook.active.append(['商品链接', '商品价格', '采集状态', '失败原因'])
        workbook.active.append([url, 12.5, 'SUCCESS', ''])
        workbook.save(output)
        workbook.close()
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(legacy._subprocess, 'run', fake_process)
    result = legacy.run_stbchina_crawler([url], tmp_path/'runtime')
    assert len(calls) == 1
    assert result[url]['page_price_value'] == Decimal('12.5')
    assert result[url]['page_price_status'] == 'OK'

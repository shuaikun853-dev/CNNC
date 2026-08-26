"""Regression tests for the identity-keyed Zhaoshang result reader.

P1-1 (STEP1_STABILITY_AUDIT): the identity reader's classification literals
were double-encoded mojibake, so a "成功" row with no price could never match
the success branch and every source string was corrupted.  These tests pin
the correct Chinese semantics shared with ``read_zhaoshang_result_xlsx``.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from app.services.zhaoshang_result_adapter import (
    read_zhaoshang_result_xlsx,
    read_zhaoshang_result_xlsx_by_identity,
)

RESULT_HEADERS = ["商品编码", "原始链接", "商品价格", "status", "reason"]


def _write_result_workbook(
    path: Path,
    rows: list[tuple[str, str, object, str, str]],
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(RESULT_HEADERS)
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)


def _identity_rows() -> list[tuple[str, str, object, str, str]]:
    return [
        # code, url, price, status, reason
        ("sku-ok", "https://shop.example/ok", 12.5, "成功", ""),
        ("sku-zero", "https://shop.example/zero", 0, "成功", ""),
        ("sku-invisible", "https://shop.example/invisible", None, "成功", ""),
        ("sku-fail-reason", "https://shop.example/fail-reason", None, "失败", "页面无法访问"),
        ("sku-fail-status", "https://shop.example/fail-status", None, "采集超时", ""),
        ("sku-fail-silent", "https://shop.example/fail-silent", None, "", ""),
    ]


def test_identity_reader_classifies_ok_price(tmp_path):
    result_path = tmp_path / "result.xlsx"
    _write_result_workbook(result_path, _identity_rows())

    results = read_zhaoshang_result_xlsx_by_identity(result_path)

    entry = results[("sku-ok", "https://shop.example/ok")]
    assert entry["page_price_status"] == "OK"
    assert entry["page_price_source"] == "招商局"


def test_identity_reader_classifies_real_zero_price(tmp_path):
    result_path = tmp_path / "result.xlsx"
    _write_result_workbook(result_path, _identity_rows())

    results = read_zhaoshang_result_xlsx_by_identity(result_path)

    entry = results[("sku-zero", "https://shop.example/zero")]
    assert entry["page_price_status"] == "PRICE_IS_ZERO"
    assert entry["page_price_source"] == "招商局"


def test_identity_reader_classifies_success_without_price_as_price_missing(tmp_path):
    result_path = tmp_path / "result.xlsx"
    _write_result_workbook(result_path, _identity_rows())

    results = read_zhaoshang_result_xlsx_by_identity(result_path)

    entry = results[("sku-invisible", "https://shop.example/invisible")]
    assert entry["page_price_status"] == "PRICE_MISSING"
    assert entry["page_price_source"] == "招商局-价格不可见"
    assert entry["page_price_value"] is None


def test_identity_reader_classifies_failures_with_readable_source(tmp_path):
    result_path = tmp_path / "result.xlsx"
    _write_result_workbook(result_path, _identity_rows())

    results = read_zhaoshang_result_xlsx_by_identity(result_path)

    by_reason = results[("sku-fail-reason", "https://shop.example/fail-reason")]
    assert by_reason["page_price_status"] == "FAILED"
    assert by_reason["page_price_source"] == "招商局-页面无法访问"
    by_status = results[("sku-fail-status", "https://shop.example/fail-status")]
    assert by_status["page_price_status"] == "FAILED"
    assert by_status["page_price_source"] == "招商局-采集超时"
    silent = results[("sku-fail-silent", "https://shop.example/fail-silent")]
    assert silent["page_price_status"] == "FAILED"
    assert silent["page_price_source"] == "招商局-未知"


def test_identity_reader_never_emits_mojibake_sources(tmp_path):
    result_path = tmp_path / "result.xlsx"
    _write_result_workbook(result_path, _identity_rows())

    results = read_zhaoshang_result_xlsx_by_identity(result_path)

    mojibake_fragments = ("鎷", "鎴", "灞", "浠", "鏈")
    for identity, entry in results.items():
        source = str(entry["page_price_source"])
        assert not any(fragment in source for fragment in mojibake_fragments), (
            identity,
            source,
        )


def test_identity_and_url_readers_agree_on_classification_semantics(tmp_path):
    result_path = tmp_path / "result.xlsx"
    rows = _identity_rows()
    _write_result_workbook(result_path, rows)

    by_identity = read_zhaoshang_result_xlsx_by_identity(result_path)
    by_url = read_zhaoshang_result_xlsx(result_path)

    for code, url, _price, _status, _reason in rows:
        identity_entry = by_identity[(code, url)]
        url_entry = by_url[url]
        assert identity_entry["page_price_status"] == url_entry["page_price_status"], url
        assert identity_entry["page_price_source"] == url_entry["page_price_source"], url

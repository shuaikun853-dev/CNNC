"""Business parity between the URL-keyed and identity-keyed result parsers.

The identity-keyed parser must classify every workbook row exactly like the
effective URL-keyed parser; its literals historically drifted into mojibake,
which silently pushed successful-but-priceless rows into FAILED.  These tests
assert the business classification outcome, not string encoding details.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from app.services.zhaoshang_result_adapter import (
    read_zhaoshang_result_xlsx,
    read_zhaoshang_result_xlsx_by_identity,
)


def _write_result_workbook(path: Path, rows: list[tuple[str, str, object, str, str]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["原始链接", "商品编码", "商品价格", "status", "reason"])
    for url, code, price, status, reason in rows:
        sheet.append([url, code, price, status, reason])
    workbook.save(path)


def _read_both(path: Path) -> tuple[dict, dict]:
    return (
        read_zhaoshang_result_xlsx(path),
        read_zhaoshang_result_xlsx_by_identity(path),
    )


def test_identity_parser_classifies_every_business_shape_like_the_url_parser(tmp_path):
    rows = [
        ("https://shop.example/ok", "sku-ok", 12.5, "", ""),
        ("https://shop.example/zero", "sku-zero", 0, "", ""),
        (
            "https://shop.example/missing",
            "sku-missing",
            None,
            "采集成功但价格不可见",
            "",
        ),
        ("https://shop.example/failed-reason", "sku-failed-reason", None, "失败", "链接超时"),
        ("https://shop.example/failed-status", "sku-failed-status", None, "失败", ""),
        ("https://shop.example/failed-unknown", "sku-failed-unknown", None, "", ""),
    ]
    result_path = tmp_path / "result.xlsx"
    _write_result_workbook(result_path, rows)
    url_map, identity_map = _read_both(result_path)

    assert set(url_map) == {row[0] for row in rows}
    assert set(identity_map) == {(row[1], row[0]) for row in rows}

    expected = {
        "sku-ok": ("OK", "招商局"),
        "sku-zero": ("PRICE_IS_ZERO", "招商局"),
        "sku-missing": ("PRICE_MISSING", "招商局-价格不可见"),
        "sku-failed-reason": ("FAILED", "招商局-链接超时"),
        "sku-failed-status": ("FAILED", "招商局-失败"),
        "sku-failed-unknown": ("FAILED", "招商局-未知"),
    }
    for url, code, _price, _status, _reason in rows:
        url_row = url_map[url]
        identity_row = identity_map[(code, url)]
        assert identity_row["page_price_status"] == url_row["page_price_status"]
        assert identity_row["page_price_source"] == url_row["page_price_source"]
        assert (
            identity_row["page_price_status"],
            identity_row["page_price_source"],
        ) == expected[code]


def test_identity_parser_matches_url_parser_for_price_present_rows(tmp_path):
    rows = [
        ("https://shop.example/1", "code-1", 12.5, "", ""),
        ("https://shop.example/2", "code-2", 0, "", ""),
    ]
    result_path = tmp_path / "result.xlsx"
    _write_result_workbook(result_path, rows)
    url_map, identity_map = _read_both(result_path)

    assert identity_map[("code-1", "https://shop.example/1")]["page_price_status"] == "OK"
    assert identity_map[("code-1", "https://shop.example/1")]["page_price_source"] == "招商局"
    assert url_map["https://shop.example/1"]["page_price_status"] == "OK"
    assert (
        identity_map[("code-2", "https://shop.example/2")]["page_price_status"]
        == url_map["https://shop.example/2"]["page_price_status"]
        == "PRICE_IS_ZERO"
    )
    assert (
        identity_map[("code-2", "https://shop.example/2")]["page_price_source"]
        == url_map["https://shop.example/2"]["page_price_source"]
        == "招商局"
    )


def test_identity_parser_matches_url_parser_for_price_missing_success_row(tmp_path):
    rows = [("https://shop.example/1", "code-1", None, "采集成功但价格不可见", "")]
    result_path = tmp_path / "result.xlsx"
    _write_result_workbook(result_path, rows)
    url_map, identity_map = _read_both(result_path)

    assert (
        identity_map[("code-1", "https://shop.example/1")]["page_price_status"]
        == url_map["https://shop.example/1"]["page_price_status"]
        == "PRICE_MISSING"
    )
    assert (
        identity_map[("code-1", "https://shop.example/1")]["page_price_source"]
        == url_map["https://shop.example/1"]["page_price_source"]
        == "招商局-价格不可见"
    )


def test_identity_parser_matches_url_parser_for_failed_rows(tmp_path):
    rows = [
        ("https://shop.example/reason", "code-reason", None, "失败", "链接超时"),
        ("https://shop.example/status", "code-status", None, "失败", ""),
        ("https://shop.example/unknown", "code-unknown", None, "", ""),
    ]
    result_path = tmp_path / "result.xlsx"
    _write_result_workbook(result_path, rows)
    url_map, identity_map = _read_both(result_path)

    expected = {
        "code-reason": ("FAILED", "招商局-链接超时"),
        "code-status": ("FAILED", "招商局-失败"),
        "code-unknown": ("FAILED", "招商局-未知"),
    }
    for url, code, _price, _status, _reason in rows:
        status, source = expected[code]
        assert identity_map[(code, url)]["page_price_status"] == url_map[url]["page_price_status"] == status
        assert identity_map[(code, url)]["page_price_source"] == url_map[url]["page_price_source"] == source

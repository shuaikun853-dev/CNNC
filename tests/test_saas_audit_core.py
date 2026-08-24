import ast
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import saas_audit_core as core
from app.services.zhaoshang_result_adapter import normalize_result_url
import scripts.audit_saas_excel_file as facade


def test_facade_reexports_canonical_pure_rules():
    assert facade.normalize_cell_str is core.normalize_cell_str
    assert facade.parse_decimal is core.parse_decimal
    assert facade.build_record_payload is core.build_record_payload
    assert facade.extract_customer_price_from_detail is core.extract_customer_price_from_detail
    assert facade.compute_over_30_check is core.compute_over_30_check


def test_legacy_facade_has_no_canonical_function_bodies():
    source = Path("scripts/audit_saas_excel_file.py").read_text(encoding="utf-8")
    canonical_names = (
        "normalize_cell_str", "parse_decimal", "is_valid_reference_price",
        "_saas_list_payload_rows", "_normalize_saas_list_row", "_heal_missing_closing_brace",
        "_parse_json_segments", "_load_json_file_segments", "_collect_rows_from_segments",
        "read_saas_list_json", "_normalize_saas_pending_row", "read_saas_pending_json",
        "build_customer_index", "join_saas_with_customer", "is_jd_link", "build_record_payload",
        "extract_customer_price_from_detail", "build_records", "compute_over_30_check",
        "compute_saas_price_vs_customer", "compute_price_changed", "compute_final_decision",
    )
    for name in canonical_names:
        assert f"def {name}(" not in source
        assert source.count(f"{name} = _audit_core.") == 1


def test_concatenated_json_decimal_join_and_detail_are_stable(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(
        '{"rows":[{"vCommodityId":"doc","skuId":"sku","price":"1.20"}]}\n'
        '{"rows":[{"vCommodityId":"doc","skuId":"sku","price":"1.20"}]}',
        encoding="utf-8",
    )
    _, rows = core.read_saas_pending_json(source)
    assert [row["price"] for row in rows] == [Decimal("1.20"), Decimal("1.20")]
    joined = core.join_saas_with_customer(rows, {"doc": {"fourCategoryId": "c", "fourCategoryName": "n", "list_reference_price": Decimal("9")}})
    assert [row["fourCategoryId"] for row in joined] == ["c", "c"]
    detail = core.extract_customer_price_from_detail({"data": {"referenceInfos": [{"referencePrice": "10"}, {"referencePrice": "11"}]}})
    assert detail["customerPrice"] == Decimal("11")


def test_detail_business_error_envelope_is_request_failed_not_not_found():
    payload = {
        "success": False,
        "rspCode": "8888",
        "rspDesc": "潜在标准商品数据详情异常:Index: 1, Size: 0",
        "data": "",
    }
    result = core.extract_customer_price_from_detail(payload)
    assert result["customerPrice"] is None
    assert result["customerPriceStatus"] == "DETAIL_REQUEST_FAILED"
    assert "rspCode=8888" in result["customerPriceWarning"]
    assert "潜在标准商品数据详情异常:Index: 1, Size: 0" in result["customerPriceWarning"]


def test_detail_success_envelope_keeps_max_reference_price():
    payload = {
        "success": True,
        "rspCode": "0000",
        "data": {
            "referenceInfos": [{"referencePrice": "500"}, {"referencePrice": "550"}],
            "referencePrice": "500",
        },
    }
    result = core.extract_customer_price_from_detail(payload)
    assert result["customerPrice"] == Decimal("550")
    assert result["customerPriceStatus"] == "FOUND"


def test_detail_success_without_references_stays_not_found():
    payload = {"success": True, "rspCode": "0000", "data": {}}
    result = core.extract_customer_price_from_detail(payload)
    assert result["customerPrice"] is None
    assert result["customerPriceStatus"] == "NOT_FOUND"


def test_identity_and_fingerprint_keep_duplicate_snapshot_members_and_exact_urls_distinct():
    row = {"vCommodityId": "document", "skuId": "sku", "extLink": " https://a.example/item?a=1 "}
    source = core.source_fingerprint(row)
    assert core.snapshot_fingerprint([source, source]) != core.snapshot_fingerprint([source])
    assert core.crawl_request_identity(row, "mall", "v1") == core.crawl_request_identity({**row, "extLink": "https://a.example/item?a=1"}, "mall", "v1")
    assert core.crawl_request_identity(row, "mall", "v1") != core.crawl_request_identity({**row, "extLink": "https://a.example/item?a=2"}, "mall", "v1")
    assert core.crawl_request_identity(row, "mall", "v1") != core.crawl_request_identity({**row, "vCommodityId": "other"}, "mall", "v1")
    assert normalize_result_url(" https://a.example/item?a=1 ") == "https://a.example/item?a=1"
    with pytest.raises(ValueError):
        core.relationship_identity({"vCommodityId": "document", "skuId": ""})


def test_customer_fingerprint_uses_joined_list_reference_price_alias():
    base = {"fourCategoryId": "c", "fourCategoryName": "n", "commodityName": "item", "referenceLink": "https://x"}
    assert core.customer_fingerprint({**base, "list_reference_price": Decimal("10")}) != core.customer_fingerprint(
        {**base, "list_reference_price": Decimal("11")}
    )


def test_source_fingerprint_includes_resolved_crawl_policy():
    row = {"vCommodityId": "v", "skuId": "sku", "extLink": "https://x"}
    disabled = core.source_fingerprint({**row, "__crawl_required": False})
    enabled = core.source_fingerprint(
        {
            **row,
            "__crawl_required": True,
            "__crawl_mall_key": "mall-a",
            "__crawl_mall_source_id": "source-a",
            "__crawl_backend_version": "legacy-v1",
        }
    )
    remapped = core.source_fingerprint(
        {
            **row,
            "__crawl_required": True,
            "__crawl_mall_key": "mall-b",
            "__crawl_mall_source_id": "source-b",
            "__crawl_backend_version": "legacy-v2",
        }
    )

    assert len({disabled, enabled, remapped}) == 3


def test_strict_customer_index_rejects_duplicate_document_ids(tmp_path):
    path = tmp_path / "customer.json"
    path.write_text(json.dumps({"rows": [{"documentId": "d"}, {"documentId": "d"}]}), encoding="utf-8")
    assert core.build_customer_index(path)["d"]
    with pytest.raises(ValueError, match="duplicate"):
        core.build_strict_customer_index(path)


def test_legacy_module_has_one_effective_duplicate_prone_definition():
    path = Path("scripts/audit_saas_excel_file.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in ("run_zhaoshang_tasks", "read_zhaoshang_result_xlsx", "apply_zhaoshang_results_to_rows"):
        assert sum(isinstance(node, ast.FunctionDef) and node.name == name for node in tree.body) == 1
    assert sum(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(isinstance(target, ast.Name) and target.id == "VALUE_DISPLAY_NAMES" for target in (node.targets if isinstance(node, ast.Assign) else [node.target]))
        for node in tree.body
    ) == 1
    assert "_shadowed_" not in source
    assert "_LEGACY_VALUE_DISPLAY_NAMES_SHADOWED" not in source
    assert "_read_zhaoshang_template_code_url_map" not in source
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "read_zhaoshang_result_xlsx")
    assert any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "read_zhaoshang_result_xlsx"
        for node in reader.body
    )

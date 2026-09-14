"""Read-only SaaS check for documents with multiple product codes in one mall."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from scripts.auth_manager import AuthManager
    from scripts.curl_utils import apply_curl_headers, load_curl_command
except ModuleNotFoundError:  # Direct invocation from scripts/.
    from auth_manager import AuthManager
    from curl_utils import apply_curl_headers, load_curl_command


ROOT = Path(__file__).resolve().parents[1]
TENANT_ID = "188605"
COMMODITY_POOL_ID = "222"
MONITOR_BATCH_SIZE = 100


def parse_document_ids(text: str) -> list[str]:
    """Extract document IDs and preserve their first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for document_id in re.findall(r"\d{15,20}", text or ""):
        if document_id not in seen:
            seen.add(document_id)
            result.append(document_id)
    return result


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    """Split items into fixed-size batches without reordering them."""
    return [items[index:index + size] for index in range(0, len(items), size)]


def response_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data", response.get("rows", []))
    if isinstance(data, dict):
        rows = data.get("rows", data.get("records", []))
    else:
        rows = data
    return rows if isinstance(rows, list) else []


def request_json(auth: AuthManager, curl: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    def factory() -> urllib.request.Request:
        request = urllib.request.Request(curl["url"], data=body, method=curl["method"])
        apply_curl_headers(request, curl["headers"], curl["cookie"])
        return request

    response = auth.request_json("saas", factory, read_only=True, timeout=60)
    if str(response.get("rspCode")) != "0000":
        raise RuntimeError(f"SaaS query failed: rspCode={response.get('rspCode')}")
    return response


def fetch_relations(auth: AuthManager, curl: dict[str, Any], my_commodity_id: str) -> list[dict[str, Any]]:
    payload = dict(curl["body"])
    payload.update({"myCommodityId": my_commodity_id, "page": 1, "limit": 100})
    first = request_json(auth, curl, payload)
    relations = response_rows(first)
    total = int(first.get("count") or len(relations))
    for page in range(2, math.ceil(total / 100) + 1):
        payload["page"] = page
        relations.extend(response_rows(request_json(auth, curl, payload)))
    if len(relations) != total:
        raise RuntimeError(f"relation pagination mismatch for {my_commodity_id}: {len(relations)}/{total}")
    return relations


def find_multi_code_malls(relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return malls that contain at least two distinct valid SKU/link pairs."""
    grouped: dict[str, dict[str, Any]] = defaultdict(dict)
    for relation in relations:
        if str(relation.get("isValid")) != "1":
            continue
        shop_code = str(relation.get("shopCode") or "").strip().lower()
        shop_name = str(relation.get("shopName") or "").strip()
        mall_key = shop_code or shop_name.lower() or "unknown"
        sku_link = (str(relation.get("skuId") or "").strip(), str(relation.get("linkUrl") or "").strip())
        grouped[mall_key].setdefault("mall", shop_name or shop_code or "unknown")
        grouped[mall_key].setdefault("items", set()).add(sku_link)
    return [
        {"mall": group["mall"], "count": len(group["items"])}
        for group in grouped.values()
        if len(group["items"]) >= 2
    ]


def query_document(
    document_id: str,
    my_commodity_id: str,
    auth: AuthManager,
    relation_curl: dict[str, Any],
) -> dict[str, Any]:
    repeats = find_multi_code_malls(fetch_relations(auth, relation_curl, my_commodity_id))
    return {
        "document_id": document_id,
        "my_commodity_id": my_commodity_id,
        "repeat_malls": repeats,
    }


def load_body(curl: dict[str, Any]) -> dict[str, Any]:
    body = json.loads(curl["body"])
    if not isinstance(body, dict):
        raise RuntimeError("cURL request body must be a JSON object")
    return body


def query_multi_code_document_ids(document_ids: list[str], workers: int = 8) -> dict[str, Any]:
    """Run the read-only SaaS multi-code query for already-normalized document IDs."""
    if not document_ids:
        raise ValueError("未识别到单据 ID")
    if workers < 1:
        raise ValueError("workers 必须大于 0")

    monitor_curl = load_curl_command(ROOT / "data" / "saas_zhuangtai.curl")
    relation_curl = load_curl_command(ROOT / "data" / "sass-same-lianjie.curl")
    monitor_body = load_body(monitor_curl)
    relation_body = load_body(relation_curl)
    scope = (
        str(monitor_body.get("tenantId")),
        str(monitor_body.get("commodityPoolId")),
        str(relation_body.get("tenantId")),
        str(relation_body.get("commodityPoolId")),
    )
    if scope != (TENANT_ID, COMMODITY_POOL_ID, TENANT_ID, COMMODITY_POOL_ID):
        raise RuntimeError(f"unexpected tenant/pool scope: {scope}")

    auth = AuthManager()
    mapping: dict[str, str] = {}
    for document_batch in chunked(document_ids, MONITOR_BATCH_SIZE):
        monitor_payload = dict(monitor_body)
        monitor_payload.update({"commodityId": " ".join(document_batch), "page": 1, "limit": len(document_batch)})
        monitor_rows = response_rows(request_json(auth, monitor_curl, monitor_payload))
        mapping.update({
            str(row.get("commodityId")): str(row.get("myCommodityId"))
            for row in monitor_rows
            if row.get("commodityId") and row.get("myCommodityId")
        })
    missing = [document_id for document_id in document_ids if document_id not in mapping]

    relation_curl = {**relation_curl, "body": relation_body}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(query_document, document_id, mapping[document_id], auth, relation_curl)
            for document_id in document_ids
            if document_id in mapping
        ]
        results = [future.result() for future in futures]
    flagged = [result for result in results if result["repeat_malls"]]
    return {
        "input_unique_count": len(document_ids),
        "queried_count": len(results),
        "unmapped_document_ids": missing,
        "flagged_count": len(flagged),
        "flagged": flagged,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="只读查询 SaaS 单据是否存在同商城一物多码")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--ids", help="单据 ID 文本；可直接粘贴，重复值会自动去重")
    input_group.add_argument("--ids-file", type=Path, help="包含单据 ID 的文本文件")
    parser.add_argument("--output", type=Path, required=True, help="结果 JSON 文件路径")
    parser.add_argument("--workers", type=int, default=8, help="关系查询并发数，默认 8")
    args = parser.parse_args()

    input_text = args.ids if args.ids is not None else args.ids_file.read_text(encoding="utf-8")
    document_ids = parse_document_ids(input_text)
    if args.workers < 1:
        raise SystemExit("--workers 必须大于 0")
    output = query_multi_code_document_ids(document_ids, args.workers)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

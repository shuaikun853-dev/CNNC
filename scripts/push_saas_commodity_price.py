from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Sequence
import urllib.request
from urllib.parse import urlparse, urlunparse

try:
    from scripts.auth_manager import AuthManager
    from scripts.curl_utils import (
        apply_curl_headers,
        internal_direct_opener,
        load_curl_command,
    )
except ModuleNotFoundError:  # Direct invocation: python scripts/push_saas_commodity_price.py
    from auth_manager import AuthManager
    from curl_utils import apply_curl_headers, internal_direct_opener, load_curl_command


ROOT = Path(__file__).resolve().parents[1]
TENANT_ID = "188605"
COMMODITY_POOL_ID = "222"
QUERY_PATH = "/improxy/saas/iio2/commodityMonitorResult/query"
PUSH_PATH = "/improxy/saas/iio2/commodityPoolRelation/pushCommodityPrice"
DOCUMENT_ID_PATTERN = re.compile(r"^[0-9]{15,20}$")


@dataclass(frozen=True)
class PushPlanItem:
    document_id: str
    relation_id: str
    my_commodity_id: str
    is_sync: str
    is_sync_standard: str
    sync_price_status: str
    action: str

    def as_manifest_item(self) -> dict[str, str]:
        return {
            "documentId": self.document_id,
            "relationId": self.relation_id,
            "myCommodityId": self.my_commodity_id,
            "isSync": self.is_sync,
            "isSyncStandard": self.is_sync_standard,
            "syncPriceStatus": self.sync_price_status,
            "action": self.action,
        }


def load_document_ids(path: Path) -> list[str]:
    values = path.read_text(encoding="utf-8").split()
    if not values:
        raise ValueError("Document ID file is empty")
    invalid = [value for value in values if not DOCUMENT_ID_PATTERN.fullmatch(value)]
    if invalid:
        raise ValueError(f"Invalid document ID(s): {invalid[:5]}")
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"Duplicate document ID(s): {duplicates[:5]}")
    if len(values) > 500:
        raise ValueError("At most 500 document IDs are allowed per controlled batch")
    return values


def validate_query_curl(parsed_curl: dict[str, Any]) -> dict[str, Any]:
    parsed_url = urlparse(str(parsed_curl.get("url") or ""))
    if parsed_url.hostname != "saasportal.iio.com":
        raise ValueError("cURL host must be saasportal.iio.com")
    if parsed_url.scheme not in {"http", "https"}:
        raise ValueError("cURL scheme must be HTTP or HTTPS")
    if parsed_url.path != QUERY_PATH:
        raise ValueError("cURL path must be the commodity monitor query endpoint")
    if str(parsed_curl.get("method") or "POST").upper() != "POST":
        raise ValueError("cURL method must be POST")
    if not str(parsed_curl.get("cookie") or "").strip():
        raise ValueError("cURL must contain a Cookie header")
    raw_body = parsed_curl.get("body", parsed_curl.get("data"))
    try:
        body = json.loads(str(raw_body or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("cURL body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("cURL body must be a JSON object")
    if (str(body.get("tenantId")), str(body.get("commodityPoolId"))) != (
        TENANT_ID,
        COMMODITY_POOL_ID,
    ):
        raise ValueError("cURL tenantId or commodityPoolId is outside the project scope")
    return body


def push_url_from_query_curl(parsed_curl: dict[str, Any]) -> str:
    validate_query_curl(parsed_curl)
    parsed_url = urlparse(str(parsed_curl["url"]))
    return urlunparse(parsed_url._replace(path=PUSH_PATH))


def _decode_json_response(response: Any) -> dict[str, Any]:
    charset = response.headers.get_content_charset() or "utf-8"
    payload = json.loads(response.read().decode(charset, errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("SaaS response must be a JSON object")
    return payload


def _request_json(
    parsed_curl: dict[str, Any],
    url: str,
    payload: dict[str, Any],
    *,
    opener: Any | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        method="POST",
    )
    apply_curl_headers(
        request,
        parsed_curl.get("headers") or [],
        str(parsed_curl.get("cookie") or ""),
    )
    with (opener or internal_direct_opener()).open(request, timeout=timeout) as response:
        result = _decode_json_response(response)
    if str(result.get("rspCode")) != "0000":
        raise RuntimeError(
            f"SaaS request failed: rspCode={result.get('rspCode')}, "
            f"rspDesc={result.get('rspDesc')}"
        )
    return result


def fetch_push_plan(
    parsed_curl: dict[str, Any],
    document_ids: Sequence[str],
    *,
    already_attempted_relation_ids: set[str] | None = None,
    opener: Any | None = None,
) -> list[PushPlanItem]:
    base_body = validate_query_curl(parsed_curl)
    payload = {
        **base_body,
        "tenantId": TENANT_ID,
        "commodityPoolId": COMMODITY_POOL_ID,
        "commodityId": " ".join(document_ids),
        "page": 1,
        "limit": 500,
    }
    result = _request_json(
        parsed_curl,
        str(parsed_curl["url"]),
        payload,
        opener=opener,
    )
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("SaaS query response rows must be a list")
    if int(result.get("count", -1)) != len(rows):
        raise RuntimeError("SaaS query count does not match returned rows")

    targets = set(document_ids)
    by_document: dict[str, list[dict[str, Any]]] = {value: [] for value in document_ids}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("SaaS query returned a non-object row")
        document_id = str(row.get("commodityId") or "").strip()
        if document_id in targets:
            by_document[document_id].append(row)

    missing = [value for value, matches in by_document.items() if not matches]
    duplicates = [value for value, matches in by_document.items() if len(matches) > 1]
    if missing or duplicates:
        raise RuntimeError(
            f"SaaS relation mapping is not one-to-one: missing={missing[:10]}, "
            f"duplicates={duplicates[:10]}"
        )

    attempted = already_attempted_relation_ids or set()
    plan: list[PushPlanItem] = []
    matched_attempted: set[str] = set()
    relation_ids: set[str] = set()
    for document_id in document_ids:
        row = by_document[document_id][0]
        relation_id = str(row.get("id") or "").strip()
        if not relation_id:
            raise RuntimeError(f"SaaS row for {document_id} has no relation id")
        if relation_id in relation_ids:
            raise RuntimeError(f"Duplicate SaaS relation id: {relation_id}")
        relation_ids.add(relation_id)
        action = "SKIP_MANUAL_ATTEMPT" if relation_id in attempted else "PUSH"
        if action == "SKIP_MANUAL_ATTEMPT":
            matched_attempted.add(relation_id)
        plan.append(
            PushPlanItem(
                document_id=document_id,
                relation_id=relation_id,
                my_commodity_id=str(row.get("myCommodityId") or "").strip(),
                is_sync=str(row.get("isSync") or "").strip(),
                is_sync_standard=str(row.get("isSyncStandard") or "").strip(),
                sync_price_status=str(row.get("syncPriceStatus") or "").strip(),
                action=action,
            )
        )
    unmatched_attempted = attempted - matched_attempted
    if unmatched_attempted:
        raise RuntimeError(
            f"Already-attempted relation id(s) are not in the target batch: "
            f"{sorted(unmatched_attempted)}"
        )
    return plan


def execution_plan_sha256(plan: Sequence[PushPlanItem]) -> str:
    encoded = json.dumps(
        [item.as_manifest_item() for item in plan],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_execution_confirmation(
    plan: Sequence[PushPlanItem],
    *,
    expected_count: int,
    expected_sha256: str,
) -> None:
    actual_count = sum(item.action == "PUSH" for item in plan)
    if actual_count != expected_count:
        raise ValueError(
            f"Push count changed: expected {expected_count}, actual {actual_count}"
        )
    actual_sha256 = execution_plan_sha256(plan)
    if actual_sha256.lower() != expected_sha256.strip().lower():
        raise ValueError(
            f"Execution plan SHA-256 changed: expected {expected_sha256}, "
            f"actual {actual_sha256}"
        )


def execute_push_plan(
    parsed_curl: dict[str, Any],
    plan: Sequence[PushPlanItem],
    *,
    auth_manager: AuthManager,
    opener: Any | None = None,
    timeout: int = 60,
    on_success: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    push_url = push_url_from_query_curl(parsed_curl)
    events: list[dict[str, Any]] = []
    for item in plan:
        if item.action != "PUSH":
            continue

        def request_factory() -> urllib.request.Request:
            request = urllib.request.Request(
                push_url,
                data=json.dumps(
                    {
                        "tenantId": TENANT_ID,
                        "commodityPoolId": COMMODITY_POOL_ID,
                        "id": item.relation_id,
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                method="POST",
            )
            apply_curl_headers(
                request,
                parsed_curl.get("headers") or [],
                str(parsed_curl.get("cookie") or ""),
            )
            return request

        result = auth_manager.request_json(
            "saas",
            request_factory,
            read_only=False,
            opener=opener,
            timeout=timeout,
        )
        rsp_code = str(result.get("rspCode"))
        if rsp_code != "0000":
            raise RuntimeError(
                f"Push failed for documentId={item.document_id}, "
                f"relationId={item.relation_id}: rspCode={rsp_code}, "
                f"rspDesc={result.get('rspDesc')}"
            )
        event = {
            "sequence": len(events) + 1,
            "documentId": item.document_id,
            "relationId": item.relation_id,
            "rspCode": rsp_code,
            "rspDesc": str(result.get("rspDesc") or "").strip(),
        }
        events.append(event)
        if on_success is not None:
            on_success(event)
    return events


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or execute a locked SaaS commodity price push batch."
    )
    parser.add_argument("--ids-file", type=Path, required=True)
    parser.add_argument(
        "--query-curl",
        type=Path,
        default=ROOT / "data" / "saas_zhuangtai.curl",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--already-attempted-relation-id", action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--expected-sha256")
    return parser


def run_main(
    argv: Sequence[str] | None = None,
    *,
    auth_manager: AuthManager | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    document_ids = load_document_ids(args.ids_file)
    parsed_curl = load_curl_command(args.query_curl)
    validate_query_curl(parsed_curl)
    manager = auth_manager or AuthManager()
    parsed_curl = manager.prepare_curl("saas", parsed_curl, refresh=False)
    plan = fetch_push_plan(
        parsed_curl,
        document_ids,
        already_attempted_relation_ids=set(args.already_attempted_relation_id),
    )
    push_count = sum(item.action == "PUSH" for item in plan)
    skipped_count = len(plan) - push_count
    digest = execution_plan_sha256(plan)
    print(f"target_count={len(plan)}")
    print(f"push_candidate_count={push_count}")
    print(f"manual_attempt_skip_count={skipped_count}")
    print(f"execution_plan_sha256={digest}")

    manifest = {
        "tenantId": TENANT_ID,
        "commodityPoolId": COMMODITY_POOL_ID,
        "idsFile": str(args.ids_file.resolve()),
        "targetCount": len(plan),
        "pushCandidateCount": push_count,
        "manualAttemptSkipCount": skipped_count,
        "executionPlanSha256": digest,
        "plan": [item.as_manifest_item() for item in plan],
    }
    if not args.execute:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = args.output_dir / "push_manifest.json"
        if manifest_path.exists():
            raise RuntimeError("Preview manifest already exists; use a new batch directory")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0
    if args.expected_count is None or not args.expected_sha256:
        raise ValueError("--execute requires --expected-count and --expected-sha256")
    validate_execution_confirmation(
        plan,
        expected_count=args.expected_count,
        expected_sha256=args.expected_sha256,
    )

    manifest_path = args.output_dir / "push_manifest.json"
    try:
        preview_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("A valid preview push_manifest.json is required") from exc
    if preview_manifest != manifest:
        raise RuntimeError("Current execution plan does not match the saved preview manifest")

    parsed_curl = manager.prepare_curl("saas", parsed_curl, refresh=True)
    refreshed_plan = fetch_push_plan(
        parsed_curl,
        document_ids,
        already_attempted_relation_ids=set(args.already_attempted_relation_id),
    )
    validate_execution_confirmation(
        refreshed_plan,
        expected_count=args.expected_count,
        expected_sha256=args.expected_sha256,
    )
    plan = refreshed_plan

    events_path = args.output_dir / "push_events.jsonl"
    summary_path = args.output_dir / "push_summary.json"
    if events_path.exists() or summary_path.exists():
        raise RuntimeError(
            "Execution evidence already exists; refusing to risk a duplicate push"
        )
    events: list[dict[str, Any]] = []

    def record_success(event: dict[str, Any]) -> None:
        events.append(event)
        with events_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())

    try:
        execute_push_plan(
            parsed_curl,
            plan,
            auth_manager=manager,
            on_success=record_success,
        )
        summary = {
            "status": "COMPLETED",
            "targetCount": len(plan),
            "pushCandidateCount": push_count,
            "manualAttemptSkipCount": skipped_count,
            "confirmedPushCount": len(events),
            "executionPlanSha256": digest,
        }
    except Exception as exc:
        summary = {
            "status": "BLOCKED",
            "targetCount": len(plan),
            "pushCandidateCount": push_count,
            "manualAttemptSkipCount": skipped_count,
            "confirmedPushCount": len(events),
            "executionPlanSha256": digest,
            "errorType": type(exc).__name__,
            "error": str(exc),
        }
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"status={summary['status']}")
    print(f"confirmed_push_count={summary['confirmedPushCount']}")
    print(f"output_dir={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_main())

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas.workflow import InternalCrawlResult, SaasSameProductApprovalItem
from app.workflows.work_01_saas_approval_json_audit import audit_saas_approval_records


DEFAULT_SCOPE = {
    "project": "CNNC",
    "tenant_id": 188605,
    "commodity_pool_id": 222,
}


def _ensure_json_path(path: Path) -> None:
    if path.suffix.lower() != ".json":
        raise ValueError(f"只支持 JSON 文件，不读取 Excel/CSV：{path}")


def _read_json(path: Path):
    _ensure_json_path(path)
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_records(path: Path) -> list[SaasSameProductApprovalItem]:
    payload = _read_json(path)
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("records"), list):
        records = payload["records"]
    else:
        raise ValueError("records-json 必须是数组，或包含 records 数组的对象")
    return [SaasSameProductApprovalItem(**record) for record in records]


def load_crawl_results(path: Path | None) -> list[InternalCrawlResult]:
    if path is None:
        return []
    payload = _read_json(path)
    if isinstance(payload, list):
        results = payload
    elif isinstance(payload, dict) and isinstance(payload.get("crawl_results"), list):
        results = payload["crawl_results"]
    else:
        raise ValueError("crawl-results-json 必须是数组，或包含 crawl_results 数组的对象")
    return [InternalCrawlResult(**result) for result in results]


def write_result(path: Path, result: dict) -> None:
    _ensure_json_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)


def print_summary(result: dict, output_path: Path) -> None:
    summary = result.get("summary", {})
    print(f"总记录数: {summary.get('total_records', 0)}")
    print(f"建议通过数量: {summary.get('approve_suggestion_count', 0)}")
    print(f"人工核查数量: {summary.get('manual_check_count', 0)}")
    print(f"建议驳回数量: {summary.get('reject_suggestion_count', 0)}")
    print(f"客户价高于30%数量: {summary.get('higher_than_customer_price_30_count', 0)}")
    print(f"缺少爬取结果数量: {summary.get('crawl_result_missing_count', 0)}")
    print(f"页面下架数量: {summary.get('crawl_off_shelf_count', 0)}")
    print(f"价格不一致数量: {summary.get('price_mismatch_count', 0)}")
    print(f"输出文件路径: {output_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地 SaaS 待审批 JSON 审查脚本")
    parser.add_argument("--records-json", required=True, type=Path)
    parser.add_argument("--crawl-results-json", type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = load_records(args.records_json)
    crawl_results = load_crawl_results(args.crawl_results_json)
    response = audit_saas_approval_records(
        records=records,
        use_model=True,
        use_link_audit=True,
        crawl_results=crawl_results,
        use_crawl_result=args.crawl_results_json is not None,
    )
    result = response.model_dump(mode="json")
    result["scope"] = DEFAULT_SCOPE
    write_result(args.output_json, result)
    print_summary(result, args.output_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

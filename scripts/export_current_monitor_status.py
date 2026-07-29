from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.request import Request

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from curl_utils import (
    apply_curl_headers,
    apply_internal_no_proxy_env,
    internal_direct_opener,
    load_curl_command,
)


ROOT = Path(__file__).resolve().parents[1]


def _is_valid_new_price(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    text = str(value).strip()
    if not text or text.lower() in {"null", "none"}:
        return False
    try:
        price = Decimal(text)
        return price.is_finite() and price > 0
    except InvalidOperation:
        return False


def _request(config: dict, payload: dict) -> dict:
    request = Request(config["url"], data=json.dumps(payload, separators=(",", ":")).encode(), method=config["method"])
    apply_curl_headers(request, config["headers"], config["cookie"])
    with internal_direct_opener().open(request, timeout=60) as response:
        data = json.loads(response.read().decode(response.headers.get_content_charset() or "utf-8"))
    if str(data.get("rspCode")) != "0000":
        raise RuntimeError(f"rspCode={data.get('rspCode')}, rspDesc={data.get('rspDesc')}")
    return data


def _rows(data: dict) -> list[dict]:
    value = data.get("data", data.get("rows", []))
    if isinstance(value, dict):
        value = value.get("rows", value.get("records", []))
    return value if isinstance(value, list) else []


def _status(row: dict) -> tuple[str, str, str, str, str, str]:
    sync, standard, price = (str(row.get(key) or "") for key in ("isSync", "isSyncStandard", "syncPriceStatus"))
    collect = {"1": "采集成功", "0": "采集失败", "2": "采集中"}.get(sync, "未知")
    same = {"1": "同款匹配成功", "0": "同款匹配中", "2": "同款匹配异常/未成功"}.get(standard, "未知")
    price_text = {"1": "价格计算成功", "2": "价格计算中/未完成", "5": "价格计算失败", "": "无价格"}.get(price, "未知")
    if sync != "1":
        problem = "采集未成功"
    elif standard == "0":
        problem = "同款匹配中"
    elif standard != "1":
        problem = "同款匹配异常/未成功"
    elif price == "5":
        problem = "价格检测校验失败"
    elif not _is_valid_new_price(row.get("newPrice")):
        problem = "推送价格为空或无效"
    else:
        return collect, same, price_text, "是", "可推送", ""
    return collect, same, price_text, "否", problem, problem


def main() -> None:
    apply_internal_no_proxy_env()
    cnnc = load_curl_command(ROOT / "data/latest_cnnc_query.curl")
    saas = load_curl_command(ROOT / "data/saas_zhuangtai.curl")
    cnnc_body, saas_body = json.loads(cnnc["data"]), json.loads(saas["data"])
    if cnnc_body.get("sassCheckStatuss") != ["0"] or (str(saas_body.get("tenantId")), str(saas_body.get("commodityPoolId"))) != ("188605", "222"):
        raise RuntimeError("租户、商品池或中核待处理筛选不符合第二步范围")
    cnnc_rows: list[dict] = []
    page, total = 1, None
    while True:
        data = _request(cnnc, {**cnnc_body, "page": page, "limit": 200})
        expected, page_rows = int(data["count"]), _rows(data)
        total = expected if total is None else total
        if expected != total or not page_rows and len(cnnc_rows) < total:
            raise RuntimeError("中核分页 count 异常")
        cnnc_rows.extend(page_rows)
        if len(cnnc_rows) >= total:
            break
        page += 1
    if len(cnnc_rows) != total:
        raise RuntimeError("中核实际行数与 count 不一致")
    ids = [str(row.get("documentId") or "").strip() for row in cnnc_rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise RuntimeError("documentId 为空或重复")
    saas_rows: list[dict] = []
    for start in range(0, len(ids), 200):
        data = _request(saas, {**saas_body, "page": 1, "limit": 200, "commodityId": " ".join(ids[start:start + 200])})
        batch = _rows(data)
        if len(batch) != int(data["count"]):
            raise RuntimeError("SaaS 分批返回 count 异常")
        saas_rows.extend(batch)
    index = {str(row.get("commodityId") or "").strip(): row for row in saas_rows}
    headers = ["文档ID", "中核商品名称", "中核类目名称", "四级类目ID", "四级类目名称", "客户参考价", "参考链接", "物料编码", "物料描述", "中核创建时间", "中核更新时间", "SaaS校验状态", "准标准商品编码", "采集状态", "同款匹配状态", "价格计算状态", "是否可推送", "最终分类", "问题说明", "SaaS监控状态"]
    keys = ["documentId", "commodityName", "categoryName", "fourCategoryId", "fourCategoryName", "referencePrice", "referenceLink", "materialCode", "materialDesc", "createTime", "updateTime", "sassCheckStatus", "quasiStandardCommodityCode"]
    records = []
    for row in cnnc_rows:
        monitor = index.get(str(row["documentId"]).strip())
        records.append([row.get(key, "") for key in keys] + (list(_status(monitor)) + ["已匹配"] if monitor else ["", "", "", "否", "SaaS监控缺失", "中核存在但SaaS监控未返回", "缺失"]))
    pushable = [row for row in records if row[16] == "是"]
    missing = [row for row in records if row[19] == "缺失"]
    needed = [row for row in records if row[16] == "否" and row[19] != "缺失"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = ROOT / "runtime" / "saas状态查询" / timestamp
    output_dir.mkdir(parents=True)
    output = output_dir / f"中核待处理_SaaS监控状态_{timestamp}.xlsx"
    workbook = Workbook(); workbook.remove(workbook.active)
    stats = [["指标", "分类", "数量"], ["中核待处理总数", "", len(records)], ["SaaS匹配数", "", len(records) - len(missing)], ["SaaS监控缺失数", "", len(missing)], ["可推送数", "", len(pushable)], ["需处理数", "", len(needed)]] + [["最终分类", key, value] for key, value in Counter(row[17] for row in records).most_common()]
    for name, data in (("全量监控结果", [headers, *records]), ("可推送数据", [headers, *pushable]), ("需处理数据", [headers, *needed]), ("SaaS监控缺失", [headers, *missing]), ("状态统计", stats)):
        sheet = workbook.create_sheet(name)
        for row in data: sheet.append(row)
        for cell in sheet[1]: cell.font = Font(bold=True, color="FFFFFF"); cell.fill = PatternFill("solid", fgColor="1F4E78")
        sheet.freeze_panes = "A2"; sheet.auto_filter.ref = sheet.dimensions
        for col in range(1, sheet.max_column + 1): sheet.column_dimensions[get_column_letter(col)].width = min(max(10, max(len(str(sheet.cell(row, col).value or "")) for row in range(1, sheet.max_row + 1)) + 2), 50)
    workbook.save(output)
    print(json.dumps({"output": str(output), "cnnc_total": len(records), "saas_matched": len(records) - len(missing), "saas_missing": len(missing), "pushable": len(pushable), "needs_processing": len(needed), "categories": dict(Counter(row[17] for row in records))}, ensure_ascii=False))


if __name__ == "__main__": main()

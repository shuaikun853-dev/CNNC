import os
import re
from decimal import Decimal
from urllib.parse import urlparse

from pydantic import Field

from app.schemas.workflow import StrictModel


PRICE_TOLERANCE = Decimal("0.01")
JD_SKU_RE = re.compile(r"/([0-9]+)\.html(?:$|[?#])")


class LinkAuditRequest(StrictModel):
    ext_link: str
    sku_id: str = ""
    vendor_name: str = ""
    shop_name: str = ""
    json_price: Decimal | None = None
    page_actual_price: Decimal | None = None


class LinkAuditResult(StrictModel):
    reachable: bool | None = None
    off_shelf: bool | None = None
    sku_link_check: str = ""
    page_title: str = ""
    page_price: Decimal | None = None
    price_check: str = ""
    screenshot_path: str = ""
    error_code: str = ""
    page_status: str = ""
    crawl_status: str = ""
    error_message: str = ""
    price_source: str = ""
    raw_price_candidates: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


def check_sku_link_consistency(sku_id: str, ext_link: str) -> tuple[str, list[str]]:
    sku = (sku_id or "").strip()
    link = (ext_link or "").strip()
    if not sku or not link:
        return "UNKNOWN", ["缺少 skuId 或 extLink，无法核验"]

    host = urlparse(link).netloc.lower()
    if "jd.com" in host:
        match = JD_SKU_RE.search(link)
        if not match:
            return "UNKNOWN", ["京东链接未识别到 SKU，需人工核验"]
        link_sku = match.group(1)
        if link_sku == sku:
            return "MATCH", ["京东链接 SKU 与 skuId 一致"]
        return "MISMATCH", ["京东链接 SKU 与 skuId 不一致"]

    return "UNKNOWN", ["暂不支持该商城链接 SKU 解析，需人工核验"]


def check_price_consistency(json_price: Decimal | None, page_actual_price: Decimal | None) -> tuple[str, list[str]]:
    if json_price is None:
        return "JSON_PRICE_MISSING", ["SaaS JSON 价格为空"]
    if page_actual_price is None:
        return "WAIT_PAGE_PRICE_CHECK", ["缺少页面实际价格，需人工打开链接核验"]
    if abs(json_price - page_actual_price) <= PRICE_TOLERANCE:
        return "MATCH", ["SaaS JSON 价格与页面实际价格一致"]
    return "MISMATCH", ["SaaS JSON 价格与页面实际价格不一致"]


def audit_link_with_browser(request: LinkAuditRequest) -> LinkAuditResult:
    if os.environ.get("ENABLE_BROWSER_LINK_AUDIT", "").lower() != "true":
        sku_check, sku_evidence = check_sku_link_consistency(request.sku_id, request.ext_link)
        price_check, price_evidence = check_price_consistency(request.json_price, request.page_actual_price)
        return LinkAuditResult(
            reachable=None,
            off_shelf=None,
            sku_link_check=sku_check,
            page_price=request.page_actual_price,
            price_check=price_check,
            error_code="BROWSER_AUDIT_DISABLED",
            evidence=[
                *sku_evidence,
                *price_evidence,
                "浏览器链接核验默认关闭；不登录 SaaS、不点击审批、不提交表单、不写入外部系统",
                "价格识别优先级：页面结构化数据/DOM/接口字段 > 页面文本提取 > 截图留证据 > OCR 兜底",
            ],
        )
    sku_check, sku_evidence = check_sku_link_consistency(request.sku_id, request.ext_link)
    price_check, price_evidence = check_price_consistency(request.json_price, request.page_actual_price)
    return LinkAuditResult(
        reachable=None,
        off_shelf=None,
        sku_link_check=sku_check,
        page_price=request.page_actual_price,
        price_check=price_check,
        error_code="BROWSER_AUDIT_NOT_IMPLEMENTED",
        evidence=[*sku_evidence, *price_evidence, "浏览器核验接口已预留，R3.6 不执行真实页面操作"],
    )


def audit_link(request: LinkAuditRequest) -> LinkAuditResult:
    return audit_link_with_browser(request)

# -*- coding: utf-8 -*-
"""OFS/officemate.cn fallback crawler.

Purpose:
  Crawl OFS product pages when 招商局 does not return a result.

Design:
  - Uses Playwright so the site can pass its JavaScript WAF challenge normally.
  - Defaults to one browser page and a per-request delay to reduce IP-ban risk.
  - Extracts: url, skuid, product_title, price, status, fail_reason.

Example:
  python scripts/crawl_ofs_fallback.py --urls urls.txt --output ofs_result.xlsx
  python scripts/crawl_ofs_fallback.py --urls urls.txt --workers 2 --delay 3
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import openpyxl
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)


@dataclass
class OfsRecord:
    url: str
    skuid: str = ""
    product_title: str = ""
    price: object = ""
    status: str = "失败"
    fail_reason: str = ""


def extract_ofs_sku(url: str) -> str:
    parsed = urlparse(str(url).strip())
    qs = parse_qs(parsed.query)
    for key in ("sku", "skuId", "skuNo", "itemId", "id", "goods_id", "productId"):
        if key in qs and qs[key]:
            return str(qs[key][0]).strip()
    m = re.search(r"/product-(\d+)\.html", parsed.path or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r"/(\d+)\.html", parsed.path or "", re.I)
    if m:
        return m.group(1)
    return ""


def read_urls(path: Path) -> list[str]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        headers = [str(ws.cell(1, c).value or "").strip().lower() for c in range(1, ws.max_column + 1)]
        url_col = None
        for name in ("url", "链接", "商品链接", "原始链接"):
            if name.lower() in headers:
                url_col = headers.index(name.lower()) + 1
                break
        if url_col is None:
            url_col = 1
        urls = []
        for r in range(2, ws.max_row + 1):
            v = str(ws.cell(r, url_col).value or "").strip()
            if v:
                urls.append(v)
        return list(dict.fromkeys(urls))

    urls = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "," in s and not s.startswith("http"):
            # Allow simple CSV with URL in first or url-named column.
            row = next(csv.reader([s]))
            s = next((x for x in row if x.startswith("http")), row[0]).strip()
        urls.append(s)
    return list(dict.fromkeys(urls))


def parse_price(text: str) -> object:
    # Prefer the product detail price after "销售价", not recommendation prices.
    matches = re.findall(r"销售价\s*[:：]\s*￥?\s*([0-9]+(?:\.[0-9]+)?)", text)
    if matches:
        try:
            return float(matches[-1])
        except Exception:
            return matches[-1]
    matches = re.findall(r"￥\s*([0-9]+(?:\.[0-9]+)?)", text)
    if matches:
        try:
            return float(matches[-1])
        except Exception:
            return matches[-1]
    return ""


def parse_title(text: str, page_title: str) -> str:
    # Page title is usually: "<product_title>_<brand>"
    title = (page_title or "").strip()
    if title and "404" not in title and "登录" not in title:
        title = re.sub(r"_[^_]{1,40}$", "", title).strip()
        if title:
            return title

    lines = [x.strip() for x in text.splitlines() if x.strip()]
    marker = "您当前的位置"
    for i, line in enumerate(lines):
        if marker in line:
            for cand in lines[i + 1 : i + 8]:
                if cand not in ("猜你喜欢", "最近您浏览过"):
                    return cand
    return ""


def crawl_one(context, url: str, timeout_ms: int, wait_ms: int) -> OfsRecord:
    rec = OfsRecord(url=url, skuid=extract_ofs_sku(url))
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(wait_ms)
        # If WAF challenge reload is still happening, give it one more short window.
        html = page.content()
        if "aliyun_waf" in html or "acw_sc__v2" in html:
            page.wait_for_timeout(3000)
        text = page.locator("body").inner_text(timeout=timeout_ms)
        rec.product_title = parse_title(text, page.title())
        rec.price = parse_price(text)
        if not rec.product_title:
            rec.fail_reason = "未解析到商品标题"
        elif rec.price in ("", None):
            rec.fail_reason = "未解析到商品价格"
        else:
            rec.status = "成功"
            rec.fail_reason = ""
        return rec
    except PlaywrightTimeoutError as exc:
        rec.fail_reason = f"页面超时: {exc}"
        return rec
    except Exception as exc:
        rec.fail_reason = f"{type(exc).__name__}: {exc}"
        return rec
    finally:
        page.close()


def write_xlsx(records: Iterable[OfsRecord], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ofs_result"
    headers = ["url", "skuid", "商品标题", "商品价格", "采集状态", "失败原因"]
    ws.append(headers)
    for rec in records:
        ws.append([rec.url, rec.skuid, rec.product_title, rec.price, rec.status, rec.fail_reason])
    for col, width in {"A": 70, "B": 18, "C": 60, "D": 12, "E": 12, "F": 40}.items():
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    wb.save(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--urls", required=True, help="txt/csv/xlsx file containing OFS URLs")
    parser.add_argument("--output", required=True, help="output xlsx path")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between pages, default 2")
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--wait-ms", type=int, default=3500)
    parser.add_argument("--headful", action="store_true", help="show browser window")
    args = parser.parse_args()

    urls = [u for u in read_urls(Path(args.urls)) if "officemate.cn" in u or "ofs.cn" in u]
    print(f"[ofs] urls={len(urls)} delay={args.delay}s")
    records: list[OfsRecord] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        context = browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1365, "height": 768})
        for i, url in enumerate(urls, 1):
            print(f"[ofs] {i}/{len(urls)} {url}")
            rec = crawl_one(context, url, args.timeout_ms, args.wait_ms)
            records.append(rec)
            print(f"[ofs]   {rec.status} sku={rec.skuid} price={rec.price} title={rec.product_title[:50]}")
            if i < len(urls):
                time.sleep(args.delay)
        context.close()
        browser.close()

    write_xlsx(records, Path(args.output))
    ok = sum(1 for r in records if r.status == "成功")
    print(f"[ofs] done ok={ok}/{len(records)} output={args.output}")
    return 0 if ok == len(records) else 2


if __name__ == "__main__":
    raise SystemExit(main())

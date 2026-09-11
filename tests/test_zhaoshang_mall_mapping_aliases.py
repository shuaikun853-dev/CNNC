"""Zhaoshang mall mapping EXTRA_DOMAIN_ALIASES tests (Issue #10 Part A).

``comix``/``ofs`` 在映射表中没有 qx.com / officemate.cn 域名条目,别名必须
 additive 地并入 loader 域名;既有别名(jd/zjmi/nbdeli/epec)保持不变。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from openpyxl import Workbook

from scripts.zhaoshang_mall_mapping import (
    EXTRA_DOMAIN_ALIASES,
    load_zhaoshang_mall_mappings,
)


def _mall_for_host(mappings, url):
    """与 route_url 相同的域名匹配规则(host == domain 或 endswith("." + domain))。"""
    host = (urlparse(url).hostname or "").lower().strip(".")
    for mapping in mappings:
        for domain in mapping.domains:
            normalized = str(domain or "").strip().lower().strip(".")
            if not normalized:
                continue
            if host == normalized or host.endswith("." + normalized):
                return mapping.mall_key
    return ""


def _write_mapping_workbook(path: Path) -> Path:
    """最小映射表:仅 loader 必需的三列(商城名称/sourceId/商城标识 identification)。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["商城名称", "sourceId", "商城标识 identification"])
    sheet.append(["齐心商城", "SRC-COMIX", "comix"])
    sheet.append(["欧菲斯商城", "SRC-OFS", "ofs"])
    workbook.save(path)
    return path


def test_extra_domain_aliases_resolve_comix_and_ofs(tmp_path):
    mappings = load_zhaoshang_mall_mappings(_write_mapping_workbook(tmp_path / "mapping.xlsx"))
    by_key = {mapping.mall_key: mapping for mapping in mappings}
    assert set(by_key) == {"comix", "ofs"}

    comix = by_key["comix"]
    assert comix.source_id == "SRC-COMIX"
    assert "qx.com" in comix.domains
    for url in (
        "https://qx.com/product-1.html",
        "https://www.qx.com/product-1.html",
        "https://sub.qx.com/product-1.html",
    ):
        assert _mall_for_host(mappings, url) == "comix", url

    ofs = by_key["ofs"]
    assert ofs.source_id == "SRC-OFS"
    assert "officemate.cn" in ofs.domains
    for url in (
        "https://officemate.cn/product-1.html",
        "https://www.officemate.cn/product-1.html",
    ):
        assert _mall_for_host(mappings, url) == "ofs", url


def test_existing_extra_domain_aliases_unchanged():
    assert EXTRA_DOMAIN_ALIASES == {
        "jd": ("item.jd.com",),
        "zjmi": ("zjmi.com",),
        "nbdeli": ("nbdeli.cn", "delib2b.com"),
        "epec": ("emall.epec.com", "lt95.com"),
        "comix": ("qx.com",),
        "ofs": ("officemate.cn",),
    }

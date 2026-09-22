"""Strict URL identity rules shared by Zhaoshang create and result paths."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse


def canonicalize_new_zhaoshang_url(value: object) -> str:
    """Map the retired jslink host to the canonical nbdeli host for new work."""
    text = str(value or "").strip()
    if not text:
        return text
    try:
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower().lstrip(".")
        port = parsed.port
        default_port = port is None or (
            parsed.scheme == "http" and port == 80
        ) or (parsed.scheme == "https" and port == 443)
        if (
            parsed.scheme in {"http", "https"}
            and (host == "jslink.com" or host.endswith(".jslink.com"))
            and parsed.username is None
            and parsed.password is None
            and default_port
        ):
            return parsed._replace(netloc="b2b.nbdeli.com").geturl()
    except ValueError:
        return text
    return text


def is_absolute_http_url(value: object) -> bool:
    """Return whether a value is an absolute HTTP(S) URL with a safe host."""
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _strict_qstbg_legacy_rewrite(
    durable_url: str, exported_url: str
) -> bool:
    durable = urlparse(durable_url)
    exported = urlparse(exported_url)
    durable_query = parse_qsl(durable.query, keep_blank_values=True)
    exported_query = parse_qsl(exported.query, keep_blank_values=True)
    if not (
        durable.scheme == exported.scheme == "https"
        and durable.netloc == exported.netloc == "www.qstbg.com"
        and durable.path == "/gd/detail.do"
        and exported.path == "/gd/initParameter.do"
        and not durable.fragment
        and exported.fragment == "title_detail"
        and len({key for key, _value in durable_query}) == len(durable_query)
        and len({key for key, _value in exported_query}) == len(exported_query)
    ):
        return False
    durable_params = dict(durable_query)
    exported_params = dict(exported_query)
    goods_id = durable_params.get("goods_id", "")
    return bool(
        goods_id
        and set(durable_params).issubset({"goods_id", "formid"})
        and durable_params.get("formid", "searchform") == "searchform"
        and exported_params == {"goods_id": goods_id, "formid": "form1"}
    )


def strict_legacy_export_url_match(durable_url: str, exported_url: str) -> bool:
    """Match exact URLs plus the existing directional legacy rewrites."""
    if durable_url == exported_url:
        return True
    if not durable_url or not exported_url:
        return False
    durable = urlparse(durable_url)
    exported = urlparse(exported_url)
    if _strict_qstbg_legacy_rewrite(durable_url, exported_url):
        return True
    jd_hosts = {"i-item.jd.com", "item.jd.com"}
    durable_jd_product = re.fullmatch(r"/([0-9]+)\.html", durable.path)
    exported_jd_product = re.fullmatch(r"/([0-9]+)\.html", exported.path)
    if (
        durable.scheme == exported.scheme == "https"
        and {durable.netloc, exported.netloc} == jd_hosts
        and not durable.query
        and not exported.query
        and not durable.fragment
        and not exported.fragment
        and durable_jd_product is not None
        and exported_jd_product is not None
        and durable_jd_product.group(1) == exported_jd_product.group(1)
    ):
        return True
    if not durable.path.endswith("/detail/inner"):
        return False
    canonical_path = durable.path[: -len("/inner")]
    return exported == durable._replace(path=canonical_path)


def _strict_qstbg_count_variant(left_url: str, right_url: str) -> bool:
    """Accept only the two evidenced qstbg detail URLs carrying count=1."""
    left = urlparse(left_url)
    right = urlparse(right_url)
    for detail, initialized in ((left, right), (right, left)):
        detail_query = parse_qsl(detail.query, keep_blank_values=True)
        initialized_query = parse_qsl(initialized.query, keep_blank_values=True)
        if (
            detail.scheme == initialized.scheme == "https"
            and detail.netloc == initialized.netloc == "www.qstbg.com"
            and detail.path == "/gd/detail.do"
            and initialized.path == "/gd/initParameter.do"
            and not detail.fragment
            and initialized.fragment == "title_detail"
            and len({key for key, _value in detail_query}) == len(detail_query)
            and len({key for key, _value in initialized_query})
            == len(initialized_query)
        ):
            detail_params = dict(detail_query)
            initialized_params = dict(initialized_query)
            goods_id = detail_params.get("goods_id", "")
            if (
                goods_id
                and set(detail_params).issubset(
                    {"goods_id", "formid", "count"}
                )
                and detail_params.get("formid", "searchform") == "searchform"
                and detail_params.get("count") == "1"
                and initialized_params
                == {"goods_id": goods_id, "formid": "form1"}
            ):
                return True
    return False


def _strict_lxwlmall_inner_variant(left_url: str, right_url: str) -> bool:
    left = urlparse(left_url)
    right = urlparse(right_url)
    for inner, detail in ((left, right), (right, left)):
        if (
            inner.scheme == detail.scheme == "https"
            and inner.netloc == detail.netloc == "www.lxwlmall.com"
            and inner.path == "/detail/inner"
            and detail.path == "/detail"
            and not inner.fragment
            and not detail.fragment
            and inner.query == detail.query
        ):
            return True
    return False


def strict_batch_result_url_match(mall: str, left_url: str, right_url: str) -> bool:
    """Apply only production-proven, mall-specific Batch URL equivalence."""
    if left_url == right_url:
        return bool(left_url)
    if mall == "lxwlmall":
        return _strict_lxwlmall_inner_variant(left_url, right_url)
    if mall == "qstbg":
        return (
            _strict_qstbg_legacy_rewrite(left_url, right_url)
            or _strict_qstbg_legacy_rewrite(right_url, left_url)
            or _strict_qstbg_count_variant(left_url, right_url)
        )
    return False

# Canonical reusable capability; original script retains compatibility imports.
from __future__ import annotations
import json
import urllib.error
import urllib.request
from typing import Any
from app.integrations.curl_utils import apply_curl_headers, internal_direct_opener

def build_detail_request_body(template: str, document_id: str, four_category_id: str) -> bytes:
    """根据 cURL 模板构造单行详情接口请求体。"""
    template = (template or "").strip()
    if template:
        try:
            payload = json.loads(template)
            if isinstance(payload, dict):
                if "vCommodityId" in payload and "documentId" not in payload:
                    payload["vCommodityId"] = document_id
                else:
                    payload["documentId"] = document_id
                payload["fourCategoryId"] = four_category_id
                return json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except json.JSONDecodeError:
            pass
    payload = {"documentId": document_id, "fourCategoryId": four_category_id}
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")

def call_detail_api(
    curl_template: dict[str, Any],
    document_id: str,
    four_category_id: str,
    timeout: float = 15.0,
    opener: Any | None = None,
) -> dict[str, Any]:
    """调用中核详情接口。返回结构 {ok, payload, error, http_status}。

    - http_status 仅在 HTTPError 分支携带真实状态码，其余分支为 None。
    - opener 仅用于测试注入；默认使用 urllib.request.urlopen。
    - 不打印任何敏感请求头/Cookie 内容。
    """
    body = build_detail_request_body(curl_template.get("data", ""), document_id, four_category_id)
    request = urllib.request.Request(
        url=curl_template["url"],
        data=body,
        method=curl_template.get("method", "POST") or "POST",
    )
    has_content_type = any(name.lower() == "content-type" for name, _ in curl_template.get("headers", []))
    apply_curl_headers(request, curl_template.get("headers", []), curl_template.get("cookie") or "")
    if not has_content_type:
        request.headers["Content-Type"] = "application/json"

    try:
        if opener is None:
            response = internal_direct_opener().open(request, timeout=timeout)
        else:
            response = opener(request, timeout=timeout)
        with response:
            raw = response.read()
        text = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return {"ok": False, "payload": None, "http_status": None, "error": f"详情接口响应不是 JSON：{exc}"}
        return {"ok": True, "payload": payload, "http_status": None, "error": ""}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "payload": None, "http_status": exc.code, "error": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "payload": None, "http_status": None, "error": f"URL 错误：{exc.reason}"}
    except TimeoutError:
        return {"ok": False, "payload": None, "http_status": None, "error": "详情接口超时"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "payload": None, "http_status": None, "error": f"详情接口异常：{type(exc).__name__}"}

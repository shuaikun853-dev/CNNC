from __future__ import annotations

import os
import shlex
import urllib.request
from pathlib import Path
from typing import Any


INTERNAL_NO_PROXY_HOSTS = (
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "saasportal.iio.com",
    "zhaosj.iioprod.com",
    "www.cnncmall.com",
    "cnncmall.com",
    "one.cnncecp.com",
    "172.168.1.245",
    "192.168.10.239",
    "10.100.12.250",
)


def strip_curl_line_continuations(text: str) -> str:
    parts: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line:
            continue
        while line.endswith("\\") or line.endswith("^") or line.endswith("`"):
            line = line[:-1].rstrip()
        parts.append(line)
    return " ".join(parts).strip()


def parse_curl_command(curl_text: str) -> dict[str, Any]:
    cleaned = strip_curl_line_continuations(curl_text).lstrip("\ufeff")
    cleaned = cleaned.replace('\\"', '"')
    if cleaned.lower().startswith("curl "):
        cleaned = cleaned[5:].strip()
    if not cleaned:
        raise ValueError("cURL content is empty")
    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError as exc:
        raise ValueError(f"Unable to parse cURL content: {exc}") from exc

    url = ""
    headers_list: list[tuple[str, str]] = []
    headers_dict: dict[str, str] = {}
    cookie = ""
    data = ""
    method = ""

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-H", "--header") and index + 1 < len(tokens):
            value = tokens[index + 1]
            if ":" in value:
                name, _, raw_value = value.partition(":")
                name = name.strip()
                raw_value = raw_value.strip()
                headers_list.append((name, raw_value))
                headers_dict[name] = raw_value
            index += 2
            continue
        if token in ("-b", "--cookie") and index + 1 < len(tokens):
            cookie = tokens[index + 1]
            headers_dict["Cookie"] = cookie
            index += 2
            continue
        if token in ("--data-raw", "--data", "--data-binary", "--data-ascii", "-d") and index + 1 < len(tokens):
            data = tokens[index + 1]
            index += 2
            continue
        if token in ("-X", "--request") and index + 1 < len(tokens):
            method = tokens[index + 1]
            index += 2
            continue
        if token in (
            "--compressed",
            "-L",
            "--location",
            "-k",
            "--insecure",
            "-s",
            "--silent",
            "-S",
            "--show-error",
            "-v",
            "--verbose",
        ):
            index += 1
            continue
        if token.startswith("-"):
            index += 2 if (index + 1 < len(tokens) and not tokens[index + 1].startswith("-")) else 1
            continue
        if not url:
            url = token
        index += 1

    if not url:
        raise ValueError("No target URL found in cURL content")
    if not method:
        method = "POST" if data else "GET"
    return {
        "url": url,
        "headers": headers_list,
        "headers_dict": headers_dict,
        "cookie": cookie,
        "data": data,
        "body": data,
        "method": method.upper(),
    }


def load_curl_command(path: Path) -> dict[str, Any]:
    return parse_curl_command(path.read_text(encoding="utf-8"))


def collect_curl_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for raw_line in (text or "").splitlines():
        stripped = raw_line.strip().lstrip("\ufeff")
        if stripped.startswith("curl "):
            if current:
                blocks.append("\n".join(current))
            current = [raw_line]
            continue
        if current:
            if not stripped:
                blocks.append("\n".join(current))
                current = []
            else:
                current.append(raw_line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def no_proxy_value(extra_hosts: list[str] | tuple[str, ...] | None = None) -> str:
    hosts: list[str] = []
    for host in [*INTERNAL_NO_PROXY_HOSTS, *(extra_hosts or [])]:
        if host and host not in hosts:
            hosts.append(host)
    return ",".join(hosts)


def apply_internal_no_proxy_env(env: dict[str, str] | None = None) -> dict[str, str]:
    target = os.environ if env is None else env
    existing = target.get("NO_PROXY") or target.get("no_proxy") or ""
    extra = tuple(p.strip() for p in existing.split(",") if p.strip())
    value = no_proxy_value(extra)
    target["NO_PROXY"] = value
    target["no_proxy"] = value
    return target


def internal_direct_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def apply_curl_headers(request: urllib.request.Request, headers: list[tuple[str, str]] | dict[str, str], cookie: str = "") -> None:
    """Apply captured cURL headers without urllib's header-name normalization."""
    items = headers.items() if isinstance(headers, dict) else headers
    for name, value in items:
        request.headers[str(name)] = str(value)
    if cookie:
        request.headers["Cookie"] = cookie


def internal_subprocess_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    apply_internal_no_proxy_env(env)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    return env

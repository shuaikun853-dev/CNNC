from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class QueueOpener:
    def __init__(self, *items):
        self.items = list(items)
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float = 0):
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _write_curl(path: Path, host: str, access: str, refresh: str, *, blade: bool) -> bytes:
    blade_header = f" -H 'Blade-Auth: bearer {access}'" if blade else ""
    content = (
        f"curl 'http://{host}/improxy/read' -H 'Content-Type: application/json'"
        f" -b 'theme=dark; x-access-token={access}; refreshToken={refresh}'"
        f"{blade_header} --data-raw '{{\"tenantId\":\"188605\"}}'"
    )
    path.write_text(content, encoding="utf-8")
    return path.read_bytes()


def _sources(tmp_path: Path) -> tuple[dict[str, tuple[Path, ...]], dict[Path, bytes]]:
    saas = tmp_path / "saas.curl"
    zhaoshang = tmp_path / "zhaoshang.curl"
    originals = {
        saas: _write_curl(saas, "saasportal.iio.com", "saas-a", "saas-r", blade=False),
        zhaoshang: _write_curl(
            zhaoshang,
            "zhaosj.iioprod.com",
            "zs-a",
            "zs-r",
            blade=True,
        ),
    }
    return {"saas": (saas,), "zhaoshang": (zhaoshang,)}, originals


def _state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _http_401(url: str = "http://saasportal.iio.com/improxy/read") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))


def test_refresh_bootstraps_both_systems_and_persists_both_rotated_tokens_atomically(tmp_path):
    from scripts.auth_manager import AuthManager

    sources, originals = _sources(tmp_path)
    state_path = tmp_path / "auth_state.json"
    opener = QueueOpener(
        FakeResponse(
            {"data": {"accessToken": "saas-b", "refreshToken": "saas-r2"}}
        )
    )
    manager = AuthManager(state_path=state_path, curl_sources=sources, opener=opener)

    tokens = manager.refresh("saas")

    assert (tokens.access_token, tokens.refresh_token) == ("saas-b", "saas-r2")
    assert _state(state_path) == {
        "saas": {"access_token": "saas-b", "refresh_token": "saas-r2"},
        "zhaoshang": {"access_token": "zs-a", "refresh_token": "zs-r"},
    }
    assert "refreshToken=saas-r" in opener.requests[0].full_url
    assert opener.requests[0].get_header("Authorization") == "Basic c2FiZXI6c2FiZXJfc2VjcmV0"
    assert not list(tmp_path.glob("auth_state.json.*.tmp"))
    assert {path: path.read_bytes() for path in originals} == originals


def test_existing_state_is_exclusive_and_never_falls_back_to_curl(tmp_path):
    from scripts.auth_manager import AuthManager, AuthStateError

    sources, _ = _sources(tmp_path)
    state_path = tmp_path / "auth_state.json"
    state_path.write_text(
        json.dumps(
            {"saas": {"access_token": "state-a", "refresh_token": "state-r"}}
        ),
        encoding="utf-8",
    )
    manager = AuthManager(state_path=state_path, curl_sources=sources)

    with pytest.raises(AuthStateError, match="zhaoshang"):
        manager.get_tokens("zhaoshang")


def test_prepare_curl_changes_only_authentication_fields(tmp_path):
    from scripts.auth_manager import AuthManager
    from scripts.curl_utils import load_curl_command

    sources, _ = _sources(tmp_path)
    state_path = tmp_path / "auth_state.json"
    state_path.write_text(
        json.dumps(
            {
                "saas": {"access_token": "saas-new", "refresh_token": "saas-new-r"},
                "zhaoshang": {"access_token": "zs-new", "refresh_token": "zs-new-r"},
            }
        ),
        encoding="utf-8",
    )
    original = load_curl_command(sources["zhaoshang"][0])
    manager = AuthManager(state_path=state_path, curl_sources=sources)

    prepared = manager.prepare_curl("zhaoshang", original, refresh=False)

    assert prepared["url"] == original["url"]
    assert prepared["method"] == original["method"]
    assert prepared["data"] == original["data"]
    assert "theme=dark" in prepared["cookie"]
    assert "x-access-token=zs-new" in prepared["cookie"]
    assert "refreshToken=zs-new-r" in prepared["cookie"]
    assert prepared["headers_dict"]["Blade-Auth"] == "bearer zs-new"
    assert "zs-a" in original["cookie"]
    assert original["headers_dict"]["Blade-Auth"] == "bearer zs-a"

    environment = manager.export_environment("zhaoshang", refresh=False)
    assert "theme=dark" in environment["COOKIE"]
    assert "x-access-token=zs-new" in environment["COOKIE"]
    assert environment["BLADE_AUTH"] == "bearer zs-new"


def test_concurrent_stale_refresh_consumes_refresh_token_once(tmp_path):
    from scripts.auth_manager import AuthManager

    sources, _ = _sources(tmp_path)
    state_path = tmp_path / "auth_state.json"
    state_path.write_text(
        json.dumps(
            {
                "saas": {"access_token": "saas-a", "refresh_token": "saas-r"},
                "zhaoshang": {"access_token": "zs-a", "refresh_token": "zs-r"},
            }
        ),
        encoding="utf-8",
    )
    opener = QueueOpener(
        FakeResponse(
            {"data": {"accessToken": "saas-b", "refreshToken": "saas-r2"}}
        )
    )
    manager = AuthManager(state_path=state_path, curl_sources=sources, opener=opener)
    barrier = threading.Barrier(3)
    results = []

    def refresh():
        barrier.wait()
        results.append(manager.refresh("saas", stale_access_token="saas-a"))

    threads = [threading.Thread(target=refresh) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(opener.requests) == 1
    assert {(item.access_token, item.refresh_token) for item in results} == {
        ("saas-b", "saas-r2")
    }


def test_refresh_failure_is_fail_closed_and_does_not_expose_tokens(tmp_path):
    from scripts.auth_manager import AuthManager, RefreshTokenExpiredError

    sources, _ = _sources(tmp_path)
    state_path = tmp_path / "auth_state.json"
    opener = QueueOpener(_http_401("http://saasportal.iio.com/api/blade-auth/token"))
    manager = AuthManager(state_path=state_path, curl_sources=sources, opener=opener)

    with pytest.raises(RefreshTokenExpiredError) as caught:
        manager.refresh("saas")

    message = str(caught.value)
    assert "人工重新登录 SaaS" in message
    assert "saas-r" not in message
    assert _state(state_path)["saas"] == {
        "access_token": "saas-a",
        "refresh_token": "saas-r",
    }


def test_read_only_request_refreshes_and_retries_once_after_http_401(tmp_path):
    from scripts.auth_manager import AuthManager

    sources, _ = _sources(tmp_path)
    state_path = tmp_path / "auth_state.json"
    refresh_opener = QueueOpener(
        FakeResponse(
            {"data": {"accessToken": "saas-b", "refreshToken": "saas-r2"}}
        )
    )
    business_opener = QueueOpener(
        _http_401(),
        FakeResponse({"rspCode": "0000", "rows": []}),
    )
    manager = AuthManager(
        state_path=state_path,
        curl_sources=sources,
        opener=refresh_opener,
    )

    result = manager.request_json(
        "saas",
        lambda: urllib.request.Request(
            "http://saasportal.iio.com/improxy/read",
            data=b"{}",
            method="POST",
        ),
        read_only=True,
        opener=business_opener,
    )

    assert result == {"rspCode": "0000", "rows": []}
    assert len(business_opener.requests) == 2
    assert "x-access-token=saas-a" in business_opener.requests[0].get_header("Cookie")
    assert "x-access-token=saas-b" in business_opener.requests[1].get_header("Cookie")


def test_read_only_request_stops_after_second_401(tmp_path):
    from scripts.auth_manager import AuthManager

    sources, _ = _sources(tmp_path)
    manager = AuthManager(
        state_path=tmp_path / "auth_state.json",
        curl_sources=sources,
        opener=QueueOpener(
            FakeResponse(
                {"data": {"accessToken": "saas-b", "refreshToken": "saas-r2"}}
            )
        ),
    )
    business_opener = QueueOpener(_http_401(), _http_401())

    with pytest.raises(urllib.error.HTTPError) as caught:
        manager.request_json(
            "saas",
            lambda: urllib.request.Request("http://saasportal.iio.com/improxy/read"),
            read_only=True,
            opener=business_opener,
        )

    assert caught.value.code == 401
    assert len(business_opener.requests) == 2


def test_read_only_request_retries_once_after_explicit_login_payload(tmp_path):
    from scripts.auth_manager import AuthManager

    sources, _ = _sources(tmp_path)
    manager = AuthManager(
        state_path=tmp_path / "auth_state.json",
        curl_sources=sources,
        opener=QueueOpener(
            FakeResponse(
                {"data": {"accessToken": "saas-b", "refreshToken": "saas-r2"}}
            )
        ),
    )
    business_opener = QueueOpener(
        FakeResponse({"rspCode": "3333", "rspDesc": "session ended"}),
        FakeResponse({"rspCode": "0000", "rows": []}),
    )

    result = manager.request_json(
        "saas",
        lambda: urllib.request.Request("http://saasportal.iio.com/improxy/read"),
        read_only=True,
        opener=business_opener,
    )

    assert result["rspCode"] == "0000"
    assert len(business_opener.requests) == 2


def test_non_idempotent_request_never_retries_after_401(tmp_path):
    from scripts.auth_manager import AuthManager

    sources, _ = _sources(tmp_path)
    refresh_opener = QueueOpener(
        FakeResponse(
            {"data": {"accessToken": "unused", "refreshToken": "unused-r"}}
        )
    )
    manager = AuthManager(
        state_path=tmp_path / "auth_state.json",
        curl_sources=sources,
        opener=refresh_opener,
    )
    business_opener = QueueOpener(_http_401())

    with pytest.raises(urllib.error.HTTPError):
        manager.request_json(
            "zhaoshang",
            lambda: urllib.request.Request(
                "http://zhaosj.iioprod.com/improxy/iio/crawl/crawlTaskManage/add",
                data=b"{}",
                method="POST",
            ),
            read_only=False,
            opener=business_opener,
        )

    assert len(business_opener.requests) == 1
    assert refresh_opener.requests == []

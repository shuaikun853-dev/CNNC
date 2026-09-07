import json
from urllib import error

import pytest

import app.services.title_match_model_adapter as adapter
from app.workflows.incremental_price_audit import _safe_error

pytestmark = pytest.mark.critical_safety


def _clear_retry_policy(monkeypatch):
    for name in (
        "TITLE_MATCH_MODEL_MAX_ATTEMPTS",
        "TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS",
        "TITLE_MATCH_MODEL_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)


def test_retry_policy_defaults_are_finite(monkeypatch):
    _clear_retry_policy(monkeypatch)

    assert adapter._model_retry_limit() == 3
    assert adapter._model_retry_elapsed_limit_seconds() == 300.0


def test_cache_contract_versions_are_content_addressed():
    assert adapter.TITLE_MATCH_PROMPT_VERSION.startswith("sha256:")
    assert adapter.TITLE_MATCH_SCHEMA_VERSION.startswith("sha256:")
    assert len(adapter.TITLE_MATCH_PROMPT_VERSION) == len("sha256:") + 64
    assert len(adapter.TITLE_MATCH_SCHEMA_VERSION) == len("sha256:") + 64


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TITLE_MATCH_MODEL_MAX_ATTEMPTS", "0"),
        ("TITLE_MATCH_MODEL_MAX_ATTEMPTS", "not-an-int"),
        ("TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS", "0"),
        ("TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS", "not-a-number"),
    ],
)
def test_retry_policy_invalid_values_fall_back_to_finite_defaults(monkeypatch, name, value):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv(name, value)

    assert adapter._model_retry_limit() == 3
    assert adapter._model_retry_elapsed_limit_seconds() == 300.0


def test_legacy_max_retries_alias_is_used_when_max_attempts_is_absent(monkeypatch):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_RETRIES", "2")

    assert adapter._model_retry_limit() == 2


def test_retry_policy_has_absolute_caps_even_when_environment_is_unbounded(monkeypatch):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ATTEMPTS", "999999999")
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS", "inf")

    assert adapter._model_retry_limit() == adapter.DEFAULT_MODEL_MAX_ATTEMPTS
    assert adapter._model_retry_elapsed_limit_seconds() == adapter.DEFAULT_MODEL_MAX_ELAPSED_SECONDS


def test_retry_succeeds_after_retry(monkeypatch):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS", "30")
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    calls = []

    def call(remaining_seconds):
        calls.append(remaining_seconds)
        if len(calls) == 1:
            raise TimeoutError("temporary")
        return "ok"

    assert adapter._retry_model_call("test operation", call) == "ok"
    assert len(calls) == 2


def test_retry_stops_at_elapsed_deadline_and_surfaces_context(monkeypatch):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS", "1")
    ticks = iter((100.0, 100.0, 101.1))
    monkeypatch.setattr(adapter.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)

    with pytest.raises(adapter.ModelCallRetryExhaustedError) as caught:
        adapter._retry_model_call("test operation", lambda _remaining: (_ for _ in ()).throw(TimeoutError("slow")))

    exc = caught.value
    assert exc.operation == "test operation"
    assert exc.attempts == 1
    assert exc.elapsed_seconds == pytest.approx(1.1)
    assert isinstance(exc.last_cause, TimeoutError)


def test_post_timeout_is_clamped_to_remaining_elapsed_budget(monkeypatch):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TITLE_MATCH_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("TITLE_MATCH_MODEL_NAME", "test-model")
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS", "1")
    monkeypatch.setenv("TITLE_MATCH_MODEL_TIMEOUT", "30")
    ticks = iter((100.0, 100.75))
    monkeypatch.setattr(adapter.time, "monotonic", lambda: next(ticks))
    timeouts = []

    def fake_post(_payload, timeout_seconds=None):
        timeouts.append(timeout_seconds)
        return json.dumps({"is_same": "是"})

    monkeypatch.setattr(adapter, "_post_model_payload", fake_post)

    result = adapter.call_title_match_model(
        adapter.TitleMatchModelRequest(query_str="阀门 DN20", sku_name="阀门 DN20")
    )

    assert result.match_result == "SAME"
    assert timeouts == [pytest.approx(0.25)]


def test_single_call_retries_invalid_json_inside_the_bounded_policy(monkeypatch):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TITLE_MATCH_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("TITLE_MATCH_MODEL_NAME", "test-model")
    monkeypatch.setenv("TITLE_MATCH_MODEL_RETRY_SLEEP", "0.001")
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    responses = iter(("not-json", json.dumps({"is_same": "是"})))
    calls = []

    def fake_post(_payload, timeout_seconds=None):
        calls.append(timeout_seconds)
        return next(responses)

    monkeypatch.setattr(adapter, "_post_model_payload", fake_post)

    result = adapter.call_title_match_model(
        adapter.TitleMatchModelRequest(query_str="阀门 DN20", sku_name="阀门 DN20"),
        max_attempts=2,
        max_elapsed_seconds=10,
    )

    assert result.match_result == "SAME"
    assert len(calls) == 2


def test_deterministic_http_4xx_is_not_retried(monkeypatch):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: pytest.fail("must not retry"))
    calls = []
    http_error = error.HTTPError("https://model.invalid", 400, "bad request", {}, None)

    def call(_remaining_seconds):
        calls.append(1)
        raise http_error

    with pytest.raises(error.HTTPError) as caught:
        adapter._retry_model_call("test operation", call)

    assert caught.value is http_error
    assert calls == [1]


def test_retry_diagnostics_do_not_expose_request_urls_or_query_secrets(monkeypatch, capsys):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("TITLE_MATCH_MODEL_RETRY_SLEEP", "0.001")
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    sensitive_url = "https://model.invalid/v1?api_key=do-not-print"

    def fail(_remaining_seconds):
        raise error.HTTPError(sensitive_url, 503, "temporary", {}, None)

    with pytest.raises(adapter.ModelCallRetryExhaustedError) as caught:
        adapter._retry_model_call("test operation", fail)

    output = capsys.readouterr().out
    assert "do-not-print" not in output
    assert sensitive_url not in output
    assert "do-not-print" not in str(caught.value)
    assert sensitive_url not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        (
            TimeoutError("https://model.invalid/?token=do-not-store"),
            "ModelCallRetryExhaustedError(attempts=2,last_cause=TimeoutError)",
        ),
        (
            error.HTTPError("https://model.invalid/?token=do-not-store", 503, "temporary", {}, None),
            "ModelCallRetryExhaustedError(attempts=2,last_cause=HTTPError,status=503)",
        ),
    ],
)
def test_persisted_retry_exhaustion_keeps_safe_root_cause(cause, expected):
    exc = adapter.ModelCallRetryExhaustedError(
        "batch title match model call",
        attempts=2,
        elapsed_seconds=60,
        last_cause=cause,
    )

    message = _safe_error(exc)

    assert message == expected
    assert "do-not-store" not in message


def test_call_specific_policy_can_clamp_attempts_and_total_elapsed(monkeypatch):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ATTEMPTS", "9")
    monkeypatch.setenv("TITLE_MATCH_MODEL_MAX_ELAPSED_SECONDS", "900")
    calls = []

    def fail(remaining_seconds):
        calls.append(remaining_seconds)
        raise TimeoutError("temporary")

    with pytest.raises(adapter.ModelCallRetryExhaustedError) as caught:
        adapter._retry_model_call(
            "bounded tick operation",
            fail,
            max_attempts=1,
            max_elapsed_seconds=12,
        )

    assert caught.value.attempts == 1
    assert calls == [pytest.approx(12, rel=0.01)]


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("network timeout"),
        error.URLError("connection reset"),
        error.HTTPError("https://model.invalid", 429, "rate limited", {}, None),
        error.HTTPError("https://model.invalid", 503, "unavailable", {}, None),
        json.JSONDecodeError("bad json", "{", 1),
        ValueError("response schema mismatch"),
    ],
    ids=("timeout", "url-error", "http-429", "http-503", "bad-json", "bad-schema"),
)
def test_transient_and_response_shape_failures_retry_within_finite_cap(
    monkeypatch,
    failure,
):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    calls = 0

    def fail_once(_remaining_seconds):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return "ok"

    assert adapter._retry_model_call(
        "classified retry",
        fail_once,
        max_attempts=2,
        max_elapsed_seconds=10,
    ) == "ok"
    assert calls == 2


@pytest.mark.parametrize(
    ("first_response", "failure_name"),
    [
        ("not-json", "bad json"),
        (json.dumps({"results": []}), "missing indexes"),
    ],
)
def test_batch_model_retries_parse_and_schema_failures(
    monkeypatch,
    first_response,
    failure_name,
):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TITLE_MATCH_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("TITLE_MATCH_MODEL_NAME", "test-model")
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    valid = json.dumps(
        {
            "results": [
                {
                    "index": 0,
                    "match_result": "SAME",
                    "model_match": "exact",
                    "confidence": 0.9,
                }
            ]
        }
    )
    responses = iter((first_response, valid))
    calls = []

    def fake_post(_payload, timeout_seconds=None):
        calls.append(timeout_seconds)
        return next(responses)

    monkeypatch.setattr(adapter, "_post_model_payload", fake_post)
    request = adapter.TitleMatchModelRequest(query_str="query", sku_name="sku")

    result = adapter.call_title_match_model_batch(
        [request],
        max_attempts=2,
        max_elapsed_seconds=10,
    )

    assert result[0].match_result == "SAME", failure_name
    assert len(calls) == 2


def test_single_model_retries_parse_failure_inside_same_finite_policy(monkeypatch):
    _clear_retry_policy(monkeypatch)
    monkeypatch.setenv("TITLE_MATCH_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TITLE_MATCH_MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("TITLE_MATCH_MODEL_NAME", "test-model")
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    responses = iter((
        "not-json",
        json.dumps({
            "match_result": "SAME",
            "model_match": "exact",
            "confidence": 0.9,
        }),
    ))
    calls = 0

    def fake_post(_payload, timeout_seconds=None):
        nonlocal calls
        calls += 1
        return next(responses)

    monkeypatch.setattr(adapter, "_post_model_payload", fake_post)
    result = adapter.call_title_match_model(
        adapter.TitleMatchModelRequest(query_str="query", sku_name="sku"),
        max_attempts=2,
        max_elapsed_seconds=10,
    )

    assert result.match_result == "SAME"
    assert calls == 2

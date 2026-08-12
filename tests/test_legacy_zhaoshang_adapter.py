import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from app.services.legacy_zhaoshang_adapter import (
    CrawlRequest,
    CnncLegacyZhaoshangAdapter,
    HarvestStatus,
    ReconcileStatus,
    SubmitStatus,
    TemplateConflictError,
)
from app.services import legacy_zhaoshang_adapter as legacy_module
from scripts.zhaoshang_mall_mapping import ZhaoshangMallMapping


MAPPING = ZhaoshangMallMapping(
    mall_name="京东",
    source_id="1",
    mall_key="jd",
    domains=("jd.com",),
    vendor_keywords=("jd", "京东"),
)


def _adapter(**kwargs):
    return CnncLegacyZhaoshangAdapter(mappings=[MAPPING], **kwargs)


def _request(sku_id="sku-1", url="https://item.jd.com/a", backend_version="legacy-v1"):
    return CrawlRequest(
        mall_key="jd",
        mall_source_id="1",
        mall_name="京东",
        sku_id=sku_id,
        url=url.strip(),
        backend_version=backend_version,
    )


def _state_file(tmp_path):
    path = tmp_path / "BATCH_STATE.md"
    path.write_text("# state\n", encoding="utf-8")
    return path


def _prepare(tmp_path, requests=(_request(),), **kwargs):
    return _adapter(**kwargs).prepare_job(
        business_date="2026-08-11",
        requests=requests,
        template_path=tmp_path / "template.xlsx",
        result_path=tmp_path / "result.xlsx",
        task_name="cnnc_current_opt_京东_20260811180000000000",
        batch_state_path=_state_file(tmp_path),
    )


def _append_event(path, action, **details):
    event = {"mall": "jd", "task_name": "cnnc_current_opt_京东_20260811180000000000", "action": action, "task_id": "id-default", "task_number": "no-default", **details}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"- `{json.dumps(event, ensure_ascii=False)}`\n")


def _write_result(path: Path, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["商品编码", "原始链接", "商品价格"])
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def test_mapping_prefers_url_domain_and_uses_vendor_only_for_blank_url():
    adapter = _adapter()

    from_url = adapter.request_from_saas_row({"skuId": "s", "extLink": "https://item.jd.com/a", "vendorName": "other"}, backend_version="v1")
    nonblank_unknown = adapter.request_from_saas_row({"skuId": "s", "extLink": "https://other.example/a", "vendorName": "京东"}, backend_version="v1")
    from_vendor = adapter.request_from_saas_row({"skuId": "s", "extLink": "", "vendorName": "京东"}, backend_version="v1")

    assert from_url is not None and from_url.mall_key == "jd"
    assert nonblank_unknown is None
    assert from_vendor is not None and from_vendor.url == ""


def test_mapping_parses_each_row_url_once_and_caches_repeated_host(monkeypatch):
    calls = []
    original = legacy_module.urlparse

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(legacy_module, "urlparse", counted)
    adapter = _adapter()

    assert adapter.request_from_saas_row(
        {"skuId": "s1", "extLink": "https://item.jd.com/a"}, backend_version="v1"
    ) is not None
    assert adapter.request_from_saas_row(
        {"skuId": "s2", "extLink": "https://item.jd.com/b"}, backend_version="v1"
    ) is not None

    assert calls == ["https://item.jd.com/a", "https://item.jd.com/b"]
    assert list(adapter._host_mapping_cache) == ["item.jd.com"]


def test_request_identity_keeps_same_url_with_two_skus_and_hashes_are_order_independent(tmp_path):
    first = _request("sku-1")
    second = _request("sku-2")
    adapter = _adapter()
    job_one = adapter.prepare_job(
        "2026-08-11", (first, second), tmp_path / "one.xlsx", tmp_path / "one-result.xlsx", "task-one", _state_file(tmp_path)
    )
    job_two = adapter.prepare_job(
        "2026-08-11", (second, first), tmp_path / "two.xlsx", tmp_path / "two-result.xlsx", "task-two", _state_file(tmp_path)
    )

    assert len(job_one.requests) == 2
    assert job_one.logical_job_key == job_two.logical_job_key
    assert job_one.payload_sha256 == job_two.payload_sha256


def test_immutable_template_reuses_matching_payload_and_rejects_conflict(tmp_path):
    adapter = _adapter()
    state = _state_file(tmp_path)
    first = adapter.prepare_job("2026-08-11", (_request(),), tmp_path / "template.xlsx", tmp_path / "result.xlsx", "task", state)
    second = adapter.prepare_job("2026-08-11", (_request(),), tmp_path / "template.xlsx", tmp_path / "result.xlsx", "task", state)

    assert first.payload_sha256 == second.payload_sha256
    with pytest.raises(TemplateConflictError):
        adapter.prepare_job("2026-08-11", (_request("sku-2"),), tmp_path / "template.xlsx", tmp_path / "result.xlsx", "task", state)


def test_template_requires_embedded_payload_evidence_and_never_overwrites_unreadable_file(tmp_path):
    adapter = _adapter()
    state = _state_file(tmp_path)
    path = tmp_path / "template.xlsx"
    adapter.prepare_job("2026-08-11", (_request(),), path, tmp_path / "result.xlsx", "task", state)
    workbook = load_workbook(path)
    try:
        workbook.properties.description = "legacy_zhaoshang_payload_sha256=wrong"
        workbook.save(path)
    finally:
        workbook.close()
    with pytest.raises(TemplateConflictError):
        adapter.prepare_job("2026-08-11", (_request(),), path, tmp_path / "result.xlsx", "task", state)

    unreadable = tmp_path / "unreadable.xlsx"
    unreadable.write_bytes(b"")
    with pytest.raises(TemplateConflictError):
        adapter.prepare_job("2026-08-11", (_request(),), unreadable, tmp_path / "result.xlsx", "other-task", state)
    assert unreadable.read_bytes() == b""


def test_submit_uses_exact_create_only_cli_once_and_requires_priority_evidence(tmp_path):
    calls = []

    def runner(args, timeout_seconds, env):
        calls.append((args, timeout_seconds, env))
        _append_event(Path(args[args.index("--batch-state-file") + 1]), "PRIORITY_UPDATED", task_id="id-1", task_number="no-1", affected_rows=1)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    adapter = _adapter(runner=runner)
    job = _prepare(tmp_path)
    outcome = adapter.submit(job, timeout_seconds=30)

    assert outcome.status is SubmitStatus.SUBMITTED
    assert outcome.task_id == "id-1"
    assert len(calls) == 1
    args = calls[0][0]
    assert "--create-only" in args
    assert "--wait-export-only" not in args
    assert args[args.index("--task-name") + 1] == job.task_name
    assert args[args.index("--task-weight") + 1] == "0"


@pytest.mark.parametrize(
    "action, details, expected",
    [
        ("PRIORITY_UPDATED", {"affected_rows": 2}, "updated"),
        ("PRIORITY_ALREADY_HIGHEST", {"already_highest_count": 2}, "already_highest_priority"),
        ("PRIORITY_UNCONFIRMED", {}, "completed_unconfirmed"),
    ],
)
def test_submit_accepts_only_supported_priority_evidence(tmp_path, action, details, expected):
    def runner(args, _timeout, _env):
        _append_event(Path(args[args.index("--batch-state-file") + 1]), action, **details)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    outcome = _adapter(runner=runner).submit(_prepare(tmp_path), timeout_seconds=30)

    assert outcome.status is SubmitStatus.SUBMITTED
    assert outcome.priority_evidence == expected


def test_submit_binds_priority_evidence_to_identity_from_the_same_event(tmp_path):
    def runner(args, _timeout, _env):
        state = Path(args[args.index("--batch-state-file") + 1])
        _append_event(state, "PRIORITY_UPDATED", task_id="priority-id", task_number="priority-number", affected_rows=1)
        _append_event(state, "EXPORTED", task_id="later-id", task_number="later-number")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    outcome = _adapter(runner=runner).submit(_prepare(tmp_path), timeout_seconds=30)

    assert outcome.status is SubmitStatus.SUBMITTED
    assert (outcome.task_id, outcome.task_number) == ("priority-id", "priority-number")


def test_submit_fails_closed_on_conflicting_accepted_priority_identities(tmp_path):
    def runner(args, _timeout, _env):
        state = Path(args[args.index("--batch-state-file") + 1])
        _append_event(state, "PRIORITY_UPDATED", task_id="first-id", task_number="first-number", affected_rows=1)
        _append_event(
            state,
            "PRIORITY_ALREADY_HIGHEST",
            task_id="second-id",
            task_number="second-number",
            already_highest_count=1,
        )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    outcome = _adapter(runner=runner).submit(_prepare(tmp_path), timeout_seconds=30)

    assert outcome.status is SubmitStatus.SUBMIT_UNKNOWN
    assert "conflict" in outcome.diagnostic


def test_production_runner_shares_deadline_and_strips_model_secrets(tmp_path, monkeypatch):
    tool = tmp_path / "auto_crawl_task.py"
    tool.write_text("# test tool\n", encoding="utf-8")
    monkeypatch.setenv("TITLE_MATCH_MODEL_API_KEY", "must-not-leak")
    monkeypatch.setenv("TITLE_MATCH_MODEL_NAME", "must-not-leak")
    captured = {}

    class Auth:
        def export_environment(self, *_args, **_kwargs):
            return {
                "BLADE_AUTH": "blade",
                "COOKIE": "cookie",
                "TITLE_MATCH_MODEL_API_KEY": "must-still-not-leak",
            }

    ticks = iter((100.0, 104.0))
    adapter = _adapter(
        auth_manager=Auth(),
        external_tool_path=tool,
        monotonic=lambda: next(ticks),
    )

    def run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(legacy_module.subprocess, "run", run)
    adapter._production_runner([sys.executable, str(legacy_module.EXTERNAL_TOOL_PATH)], 10)

    assert captured["timeout"] == pytest.approx(6)
    assert captured["env"]["BLADE_AUTH"] == "blade"
    assert not any(name.startswith("TITLE_MATCH_MODEL_") for name in captured["env"])
    assert os.environ["TITLE_MATCH_MODEL_API_KEY"] == "must-not-leak"


def test_production_runner_never_starts_subprocess_after_auth_uses_deadline(tmp_path, monkeypatch):
    tool = tmp_path / "auto_crawl_task.py"
    tool.write_text("# test tool\n", encoding="utf-8")

    class Auth:
        def export_environment(self, *_args, **_kwargs):
            return {"BLADE_AUTH": "blade", "COOKIE": "cookie"}

    ticks = iter((100.0, 111.0))
    adapter = _adapter(
        auth_manager=Auth(),
        external_tool_path=tool,
        monotonic=lambda: next(ticks),
    )
    monkeypatch.setattr(
        legacy_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not start after deadline"),
    )

    with pytest.raises(TimeoutError):
        adapter._production_runner([sys.executable, str(legacy_module.EXTERNAL_TOOL_PATH)], 10)


def test_default_reconciler_shares_auth_deadline_with_all_read_requests(tmp_path, monkeypatch):
    tool = tmp_path / "auto_crawl_task.py"
    tool.write_text("# test tool\n", encoding="utf-8")
    request_timeouts = []

    class Auth:
        def export_environment(self, *_args, **_kwargs):
            return {"BLADE_AUTH": "blade", "COOKIE": "cookie"}

    class Session:
        def get(self, *_args, **kwargs):
            request_timeouts.append(kwargs["timeout"])
            return object()

    class Loader:
        def exec_module(self, module):
            module.build_session = lambda: Session()
            module.priority_mall_code_for_key = lambda mall_key: mall_key

            def check_existing_task(session, *_args):
                session.get("https://read-only.invalid", timeout=60)
                return {"taskId": "id", "taskNumber": "number"}

            module.check_existing_task = check_existing_task

    class Spec:
        loader = Loader()

    class Module:
        pass

    monkeypatch.setattr(legacy_module.importlib.util, "spec_from_file_location", lambda *_args: Spec())
    monkeypatch.setattr(legacy_module.importlib.util, "module_from_spec", lambda _spec: Module())
    ticks = iter((100.0, 104.0, 105.0))
    adapter = _adapter(
        auth_manager=Auth(),
        external_tool_path=tool,
        monotonic=lambda: next(ticks),
    )

    remote = adapter._default_reconciler("task", "jd", 10)

    assert remote == {"taskId": "id", "taskNumber": "number"}
    assert request_timeouts == [pytest.approx(5)]


def test_default_reconciler_never_builds_session_after_auth_exhausts_deadline(tmp_path, monkeypatch):
    tool = tmp_path / "auto_crawl_task.py"
    tool.write_text("# test tool\n", encoding="utf-8")

    class Auth:
        def export_environment(self, *_args, **_kwargs):
            return {"BLADE_AUTH": "blade"}

    class Loader:
        def exec_module(self, module):
            module.build_session = lambda: pytest.fail("session must not be built after deadline")
            module.priority_mall_code_for_key = lambda mall_key: mall_key
            module.check_existing_task = lambda *_args: None

    class Spec:
        loader = Loader()

    class Module:
        pass

    monkeypatch.setattr(legacy_module.importlib.util, "spec_from_file_location", lambda *_args: Spec())
    monkeypatch.setattr(legacy_module.importlib.util, "module_from_spec", lambda _spec: Module())
    ticks = iter((100.0, 111.0))
    adapter = _adapter(
        auth_manager=Auth(),
        external_tool_path=tool,
        monotonic=lambda: next(ticks),
    )

    with pytest.raises(TimeoutError):
        adapter._default_reconciler("task", "jd", 10)


def test_submit_timeout_is_unknown_and_never_retried(tmp_path):
    calls = []

    def runner(args, _timeout, _env):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 30)

    outcome = _adapter(runner=runner).submit(_prepare(tmp_path), timeout_seconds=30)

    assert outcome.status is SubmitStatus.SUBMIT_UNKNOWN
    assert len(calls) == 1


def test_reconcile_unknown_uses_exact_identity_without_write_calls(tmp_path):
    calls = []

    def reconciler(task_name, mall_key, timeout_seconds):
        calls.append((task_name, mall_key, timeout_seconds))
        return {"taskId": "id-1", "taskNumber": "no-1", "mallLogo": "jd"}

    job = _prepare(tmp_path)
    _append_event(
        job.batch_state_path,
        "PRIORITY_ALREADY_HIGHEST",
        task_id="id-1",
        task_number="no-1",
        already_highest_count=1,
    )
    outcome = _adapter(reconciler=reconciler).reconcile_unknown(job, timeout_seconds=12)

    assert outcome.status is ReconcileStatus.FOUND
    assert outcome.task_id == "id-1"
    assert calls == [("cnnc_current_opt_京东_20260811180000000000", "jd", 12)]


def test_reconcile_unknown_rejects_priority_evidence_for_another_remote_identity(tmp_path):
    job = _prepare(tmp_path)
    _append_event(job.batch_state_path, "PRIORITY_ALREADY_HIGHEST", already_highest_count=1)
    adapter = _adapter(
        reconciler=lambda *_args: {"taskId": "different-id", "taskNumber": "different-number"}
    )

    outcome = adapter.reconcile_unknown(job, timeout_seconds=12)

    assert outcome.status is ReconcileStatus.MANUAL


def test_transient_unknown_reconciliation_stays_recoverable(tmp_path):
    job = _prepare(tmp_path)
    adapter = _adapter(reconciler=lambda *_args: (_ for _ in ()).throw(TimeoutError("temporary")))

    outcome = adapter.reconcile_unknown(job, timeout_seconds=12)

    assert outcome.status is ReconcileStatus.NOT_FOUND


def test_harvest_timeout_is_running_and_uses_wait_export_only(tmp_path):
    calls = []

    def runner(args, _timeout, _env):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 15)

    outcome = _adapter(runner=runner).harvest(_prepare(tmp_path), timeout_seconds=15)

    assert outcome.status is HarvestStatus.RUNNING
    assert len(calls) == 1
    assert "--wait-export-only" in calls[0]
    assert "--create-only" not in calls[0]


def test_harvest_reuses_valid_export_without_runner(tmp_path):
    job = _prepare(tmp_path)
    _write_result(job.result_path, [["sku-1", "https://item.jd.com/a", "12.5"]])
    calls = []

    outcome = _adapter(runner=lambda *_args: calls.append(1)).harvest(job, timeout_seconds=15)

    assert outcome.status is HarvestStatus.EXPORTED
    assert outcome.reused_existing is True
    assert outcome.result_map["https://item.jd.com/a"]["page_price_value"] == 12.5
    assert calls == []


def test_harvest_preserves_two_skus_sharing_one_url_as_distinct_results(tmp_path):
    job = _prepare(tmp_path, requests=(_request("sku-1"), _request("sku-2")))
    _write_result(
        job.result_path,
        [["sku-1", "https://item.jd.com/a", "12.5"], ["sku-2", "https://item.jd.com/a", "19.5"]],
    )

    outcome = _adapter().harvest(job, timeout_seconds=15)

    assert outcome.status is HarvestStatus.EXPORTED
    assert outcome.result_by_identity is not None
    assert outcome.result_by_identity[("sku-1", "https://item.jd.com/a")]["page_price_value"] == 12.5
    assert outcome.result_by_identity[("sku-2", "https://item.jd.com/a")]["page_price_value"] == 19.5


def test_harvest_allows_unique_sku_blank_request_url_when_export_supplies_url(tmp_path):
    job = _prepare(tmp_path, requests=(_request("sku-blank", ""),))
    _write_result(job.result_path, [["sku-blank", "https://item.jd.com/discovered", "12.5"]])

    outcome = _adapter().harvest(job, timeout_seconds=15)

    assert outcome.status is HarvestStatus.EXPORTED
    assert outcome.result_by_identity is not None
    assert outcome.result_by_identity[("sku-blank", "")]["page_price_value"] == 12.5


def test_harvest_rejects_mismatched_existing_export(tmp_path):
    job = _prepare(tmp_path, requests=(_request("sku-1"), _request("sku-2")))
    _write_result(job.result_path, [["sku-1", "https://item.jd.com/a", "12.5"]])
    calls = []

    outcome = _adapter(runner=lambda *_args: calls.append(1)).harvest(job, timeout_seconds=15)

    assert outcome.status is HarvestStatus.FAILED
    assert calls == []


def test_harvest_rejects_same_count_export_with_wrong_crawl_identity(tmp_path):
    job = _prepare(tmp_path, requests=(_request("sku-1"), _request("sku-2", "https://item.jd.com/b")))
    _write_result(
        job.result_path,
        [["sku-1", "https://item.jd.com/a", "12.5"], ["sku-2", "https://item.jd.com/wrong", "13"]],
    )

    outcome = _adapter().harvest(job, timeout_seconds=15)

    assert outcome.status is HarvestStatus.FAILED


def test_read_only_session_clamps_legacy_helper_timeout():
    from app.services.legacy_zhaoshang_adapter import _BoundedReadOnlySession

    calls = []

    class Session:
        def post(self, *_args, **kwargs):
            calls.append(kwargs["timeout"])
            return object()

    _BoundedReadOnlySession(Session(), 3).post("https://read-only.invalid", timeout=60)
    assert len(calls) == 1
    assert 0 < calls[0] <= 3


def test_read_only_session_uses_one_aggregate_deadline_across_requests():
    from app.services.legacy_zhaoshang_adapter import _BoundedReadOnlySession

    calls = []

    class Session:
        def post(self, *_args, **kwargs):
            calls.append(kwargs["timeout"])
            return object()

    ticks = iter((100.0, 101.0, 104.0, 111.0))
    session = _BoundedReadOnlySession(Session(), 10, monotonic=lambda: next(ticks))

    session.post("https://read-only.invalid/1", timeout=60)
    session.post("https://read-only.invalid/2", timeout=60)
    with pytest.raises(TimeoutError):
        session.post("https://read-only.invalid/3", timeout=60)

    assert calls == [pytest.approx(9), pytest.approx(6)]

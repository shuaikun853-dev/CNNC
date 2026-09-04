"""Unit tests for the adapter's read-only progress query and stop seams.

Covers the injectable callbacks (progress_probe / stop_runner), the STOPPED
outcome, and the production paths exercised through the same fake-tool /
fake-session pattern used by tests/test_remote_running_recovery.py (no real
network is ever touched; ``_load_module`` is monkeypatched at class level).
"""

from __future__ import annotations

import json

import pytest

from app.services.legacy_zhaoshang_adapter import (
    CnncLegacyZhaoshangAdapter,
    LegacyRestartOutcome,
    LegacyZhaoshangAdapterError,
    RemoteTaskIdentityConflictError,
    RestartStatus,
)

BUSINESS_DATE = "2026-08-28"
TASK_NAME = "cnnc_current_opt_领先未来商城_61284593895349"
TASK_ID = "1269504"
TASK_NUMBER = "RW-20260825150224"

_ACTIVE_PROGRESS_RSP = "总任务量：10条，待执行：4条，执行中：3条，失败：0条，待入库：1条，执行完成：2条"


class _FakeRestartResponse:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {}
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    """Stateful fake: updateState flips the exact task's enabled flag."""

    def __init__(
        self,
        task: dict,
        *,
        fail_disable: bool = False,
        fail_lookup: bool = False,
        status_rsp_desc: str = "执行中",
        progress_rsp_desc: str = _ACTIVE_PROGRESS_RSP,
        progress_payload: object = None,
    ) -> None:
        self.task = task
        self.fail_disable = fail_disable
        self.fail_lookup = fail_lookup
        self.status_rsp_desc = status_rsp_desc
        self.progress_rsp_desc = progress_rsp_desc
        self.progress_payload = progress_payload
        self.update_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.disable_calls = 0
        self.enable_calls = 0

    def post(self, url, json=None, timeout=None):
        if "updateState" in url:
            self.update_calls.append(dict(json))
            if json.get("state") == "0":
                self.disable_calls += 1
                if self.fail_disable:
                    raise TimeoutError("disable response lost")
                self.task["enabled"] = False
                return _FakeRestartResponse({"rspCode": "0000", "success": True})
            if json.get("state") == "1":
                self.enable_calls += 1
                self.task["enabled"] = True
                return _FakeRestartResponse({"rspCode": "0000", "success": True})
        if "queryTaskStatus" in url:
            return _FakeRestartResponse({"rspDesc": self.status_rsp_desc, "success": True})
        if "queryList" in url:
            if self.fail_lookup:
                raise TimeoutError("lookup response lost")
            return _FakeRestartResponse({"data": [dict(self.task)]})
        return _FakeRestartResponse({"data": [dict(self.task)]})

    def get(self, url, params=None, timeout=None):
        self.get_calls.append(
            {"url": url, "params": dict(params) if params is not None else None}
        )
        if self.progress_payload is not None:
            return _FakeRestartResponse(self.progress_payload)
        return _FakeRestartResponse({"rspDesc": self.progress_rsp_desc, "success": True})


def _fake_tool(
    task: dict,
    session: _FakeSession,
    *,
    lookup_error: Exception | None = None,
    lookup_none: bool = False,
    identity_error: Exception | None = None,
):
    class _Module:
        EXPORT_TASK_URL = "https://platform.invalid/exportTask"
        QUERY_PROGRESS_URL = "https://platform.invalid/queryTaskProgress"

        @staticmethod
        def build_session():
            return session

        @staticmethod
        def priority_mall_code_for_key(mall_key):
            return "lxwl"

        @staticmethod
        def build_browser_task_identifiers(task_name, mall_key):
            return task_name, task_name

        @staticmethod
        def check_existing_task(session, task_name, mall_code):
            if lookup_error is not None:
                raise lookup_error
            if lookup_none:
                return None
            payload = session.post(
                "https://platform.invalid/queryList",
                json={"taskName": task_name},
                timeout=60,
            ).json()
            return payload["data"][0]

        @staticmethod
        def get_task_identity(task, mall_code):
            if identity_error is not None:
                raise identity_error
            return task["taskId"], task["taskNumber"], mall_code

        @staticmethod
        def query_task_status(session, task_id):
            return session.post(
                f"https://platform.invalid/queryTaskStatus?taskId={task_id}", timeout=60
            ).json()

        @staticmethod
        def query_task_progress(session, batch_number):
            return session.get(
                "https://platform.invalid/queryTaskProgress",
                params={"batchNumber": batch_number},
                timeout=60,
            ).json()

        @staticmethod
        def parse_task_progress_desc(rsp_desc):
            values = {
                "总任务量": "total",
                "待执行": "pending",
                "执行中": "running",
                "失败": "failed",
                "待入库": "waiting_db",
                "执行完成": "finished",
            }
            summary = {}
            for label, key in values.items():
                marker = f"{label}："
                if marker in rsp_desc:
                    head = rsp_desc.split(marker, 1)[1]
                    summary[key] = int(head.split("条", 1)[0])
            return summary

        @staticmethod
        def disable_task(session, task_id):
            return session.post(
                "https://platform.invalid/updateState",
                json={"taskId": task_id, "state": "0"},
                timeout=60,
            ).json()

        @staticmethod
        def enable_task(session, task_id):
            return session.post(
                "https://platform.invalid/updateState",
                json={"taskId": task_id, "state": "1"},
                timeout=60,
            ).json()

    return _Module


class _FakeAuthManager:
    def export_environment(
        self, system, refresh=False, timeout_seconds=None, deadline=None, monotonic=None
    ):
        return {"BLADE_AUTH": "bearer fake", "COOKIE": "x-access-token=fake; refreshToken=fake"}


def _running_task_record(*, task_id: str = TASK_ID, task_number: str = TASK_NUMBER, enabled: bool = True) -> dict:
    return {
        "taskName": TASK_NAME,
        "mallLogo": "lxwl",
        "taskId": task_id,
        "taskNumber": task_number,
        "batchNumber": TASK_NAME,
        "enabled": enabled,
    }


def _prepared_job(
    tmp_path,
    monkeypatch,
    task: dict,
    session: _FakeSession,
    *,
    auth_manager=None,
    monotonic=None,
    **tool_kwargs,
):
    run_dir = tmp_path / "zhaoshang"
    run_dir.mkdir(parents=True, exist_ok=True)
    batch_state = run_dir / "BATCH_STATE.md"
    batch_state.write_text("# batch\n", encoding="utf-8")
    adapter_kwargs = {"auth_manager": auth_manager or _FakeAuthManager()}
    if monotonic is not None:
        adapter_kwargs["monotonic"] = monotonic
    adapter = CnncLegacyZhaoshangAdapter(mappings=[], db_env_path=None, **adapter_kwargs)
    prepared = adapter.prepare_job(
        BUSINESS_DATE,
        [
            type(
                "Request",
                (),
                {
                    "mall_key": "lxwl",
                    "mall_source_id": "34",
                    "mall_name": "领先未来商城",
                    "sku_id": "sku-1",
                    "url": "https://www.66123123.com/item/1",
                    "backend_version": "legacy-v1",
                    "identity": ("lxwl", "34", "sku-1", "https://www.66123123.com/item/1", "legacy-v1"),
                },
            )()
        ],
        run_dir / "input.xlsx",
        run_dir / "result.xlsx",
        TASK_NAME,
        batch_state,
    )
    monkeypatch.setattr(
        type(adapter),
        "_load_module",
        classmethod(lambda cls, path, name: _fake_tool(task, session, **tool_kwargs)),
    )
    return adapter, prepared


# ---------------------------------------------------------------------------
# query_task_progress_for_job
# ---------------------------------------------------------------------------


def test_progress_probe_injection_passthrough() -> None:
    calls: list[tuple[object, str, str, float]] = []

    def fake_probe(job, task_id, task_number, timeout_seconds):
        calls.append((job, task_id, task_number, timeout_seconds))
        return {
            "task_id": task_id,
            "task_number": task_number,
            # taskName == batchNumber semantics: the resolved browser task
            # name is the batch key when the record carries no batchNumber.
            "batch_number": job.task_name,
            "progress": {"total": 18, "pending": 18, "running": 0, "failed": 0, "waiting_db": 0, "finished": 0},
            "rsp_desc": "总任务量：18条...",
            "enabled": True,
            "remote_calls": 2,
        }

    adapter = CnncLegacyZhaoshangAdapter(mappings=[], db_env_path=None, progress_probe=fake_probe)
    job = type("Job", (), {"task_name": TASK_NAME, "mall_key": "lxwl"})()

    result = adapter.query_task_progress_for_job(job, TASK_ID, TASK_NUMBER, 5.0)

    assert calls == [(job, TASK_ID, TASK_NUMBER, 5.0)]
    assert result["batch_number"] == job.task_name
    assert result["progress"]["total"] == 18
    assert result["enabled"] is True
    assert result["remote_calls"] == 2


def test_query_task_progress_business_fail_returns_none_progress(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(
        task,
        progress_payload={"rspCode": "9999", "success": False, "rspDesc": "查询任务进度失败"},
    )
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    result = adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert result["progress"] is None
    assert result["rsp_desc"] == "查询任务进度失败"
    assert result["batch_number"] == TASK_NAME
    assert result["enabled"] is True
    assert result["remote_calls"] == 2
    # Read-only: no state write ever left.
    assert session.update_calls == []


def test_query_task_progress_success_parses_counters(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    result = adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert result["progress"] == {
        "total": 10, "pending": 4, "running": 3, "failed": 0, "waiting_db": 1, "finished": 2,
    }
    assert result["task_id"] == TASK_ID
    assert result["task_number"] == TASK_NUMBER
    assert session.update_calls == []


def test_query_task_progress_identity_mismatch_raises_conflict(tmp_path, monkeypatch) -> None:
    task = _running_task_record(task_id="9999999")
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    with pytest.raises(RemoteTaskIdentityConflictError):
        adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)


# ---------------------------------------------------------------------------
# query_task_progress_for_job — characterization tests
#
# These pin the CURRENT production behavior (fallbacks, exception types and
# message fragments) as a safety net before the progress-query capability is
# extracted out of the adapter.  They assert what the implementation does
# today, not what it ideally should do.
# ---------------------------------------------------------------------------


def test_progress_lookup_runtime_error_maps_to_identity_conflict(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(
        tmp_path,
        monkeypatch,
        task,
        session,
        lookup_error=RuntimeError("queryList exact match is ambiguous"),
    )

    with pytest.raises(RemoteTaskIdentityConflictError) as excinfo:
        adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert "queryList exact match is ambiguous" in str(excinfo.value)
    assert session.update_calls == []


def test_progress_identity_mall_marker_mismatch_maps_to_identity_conflict(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(
        tmp_path,
        monkeypatch,
        task,
        session,
        identity_error=RuntimeError("任务商城标识不一致: 期望 lxwl, 实际 other"),
    )

    with pytest.raises(RemoteTaskIdentityConflictError) as excinfo:
        adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert "商城标识不一致" in str(excinfo.value)


def test_progress_identity_other_runtime_error_propagates_untyped(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(
        tmp_path,
        monkeypatch,
        task,
        session,
        identity_error=RuntimeError("身份字段缺失"),
    )

    with pytest.raises(RuntimeError) as excinfo:
        adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    # Malformed payloads stay untyped: NOT a RemoteTaskIdentityConflictError.
    assert not isinstance(excinfo.value, RemoteTaskIdentityConflictError)
    assert "身份字段缺失" in str(excinfo.value)


def test_progress_lookup_none_raises_no_exact_existing_task(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session, lookup_none=True)

    with pytest.raises(LegacyZhaoshangAdapterError) as excinfo:
        adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert not isinstance(excinfo.value, RemoteTaskIdentityConflictError)
    assert "no exact existing task" in str(excinfo.value)
    assert session.update_calls == []


def test_progress_missing_batch_number_falls_back_to_task_name(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    del task["batchNumber"]
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    result = adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    # batchNumber fallback semantics: the resolved browser task name is the key.
    assert result["batch_number"] == TASK_NAME
    assert [call["params"]["batchNumber"] for call in session.get_calls] == [TASK_NAME]


def test_progress_disabled_task_record_reports_enabled_false(tmp_path, monkeypatch) -> None:
    task = _running_task_record(enabled=False)
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    result = adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    # Success path still runs; only the enabled flag mirrors the remote record.
    assert result["progress"] is not None
    assert result["enabled"] is False


def test_progress_rejects_non_positive_timeout(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    with pytest.raises(TimeoutError) as excinfo:
        adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=0.0)

    assert "timeout must be positive" in str(excinfo.value)
    assert session.update_calls == []
    assert session.get_calls == []


def test_progress_missing_blade_auth_raises_session_auth_unavailable(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)

    class _CookieOnlyAuthManager:
        def export_environment(
            self, system, refresh=False, timeout_seconds=None, deadline=None, monotonic=None
        ):
            return {"COOKIE": "x-access-token=fake; refreshToken=fake"}

    adapter, prepared = _prepared_job(
        tmp_path, monkeypatch, task, session, auth_manager=_CookieOnlyAuthManager()
    )

    with pytest.raises(LegacyZhaoshangAdapterError) as excinfo:
        adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert not isinstance(excinfo.value, RemoteTaskIdentityConflictError)
    assert "session auth is unavailable" in str(excinfo.value)
    assert session.get_calls == []


def test_progress_success_payload_key_set_is_pinned(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    result = adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert set(result) == {
        "task_id",
        "task_number",
        "batch_number",
        "progress",
        "rsp_desc",
        "enabled",
        "remote_calls",
    }
    assert result["remote_calls"] == 2


def test_progress_deadline_exhausted_before_query_times_out(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)
    # Stepped clock clamped at 60.0 == deadline (timeout_seconds=60.0): auth
    # refresh and the exact-task lookup still fit, then the remaining-time
    # check right before the progress query sees remaining == 0.
    values = [0.0, 1.0, 2.0, 3.0, 60.0]
    state = {"index": 0}

    def stepped_monotonic() -> float:
        value = values[min(state["index"], len(values) - 1)]
        state["index"] += 1
        return value

    adapter, prepared = _prepared_job(
        tmp_path, monkeypatch, task, session, monotonic=stepped_monotonic
    )

    with pytest.raises(TimeoutError) as excinfo:
        adapter.query_task_progress_for_job(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert "deadline exhausted before progress query" in str(excinfo.value)
    # The progress GET never left after the deadline hit.
    assert session.get_calls == []


# ---------------------------------------------------------------------------
# stop_running_task + RestartStatus.STOPPED
# ---------------------------------------------------------------------------


def test_restart_status_has_stopped_member() -> None:
    assert RestartStatus.STOPPED.value == "STOPPED"
    # Existing members stay untouched.
    assert RestartStatus.RESTARTED.value == "RESTARTED"
    assert RestartStatus.DISABLE_UNCONFIRMED.value == "DISABLE_UNCONFIRMED"
    assert RestartStatus.ENABLE_UNCONFIRMED.value == "ENABLE_UNCONFIRMED"


def test_stop_runner_injection_passthrough() -> None:
    calls: list[tuple[object, str, str, float, str | None]] = []
    expected = LegacyRestartOutcome(RestartStatus.STOPPED, disable_confirmed=True, observed_enabled=False)

    def fake_stop_runner(job, task_id, task_number, timeout_seconds, *, resume_phase=None):
        calls.append((job, task_id, task_number, timeout_seconds, resume_phase))
        return expected

    adapter = CnncLegacyZhaoshangAdapter(mappings=[], db_env_path=None, stop_runner=fake_stop_runner)
    job = type("Job", (), {"task_name": TASK_NAME, "mall_key": "lxwl"})()

    outcome = adapter.stop_running_task(job, TASK_ID, TASK_NUMBER, 5.0, resume_phase="DISABLE_SENT")

    assert outcome is expected
    assert calls == [(job, TASK_ID, TASK_NUMBER, 5.0, "DISABLE_SENT")]


def test_production_stop_confirms_disable_without_enable(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.stop_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status is RestartStatus.STOPPED
    assert outcome.status.value == "STOPPED"
    assert outcome.disable_confirmed is True
    assert outcome.observed_enabled is False
    assert outcome.enable_confirmed is False
    # Exactly one disable write, never an enable.
    assert session.disable_calls == 1
    assert session.enable_calls == 0
    disable_calls = [call for call in session.update_calls if call.get("state") == "0"]
    assert disable_calls == [{"taskId": TASK_ID, "state": "0"}]


def test_production_stop_already_disabled_is_externally_disabled(tmp_path, monkeypatch) -> None:
    task = _running_task_record(enabled=False)
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.stop_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status is RestartStatus.EXTERNALLY_DISABLED
    assert outcome.observed_enabled is False
    # Zero remote writes.
    assert session.update_calls == []


def test_production_stop_resume_disable_sent_never_rewrites(tmp_path, monkeypatch) -> None:
    task = _running_task_record(enabled=False)
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.stop_running_task(
        prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0, resume_phase="DISABLE_SENT"
    )

    assert outcome.status is RestartStatus.STOPPED
    assert outcome.disable_confirmed is True
    assert outcome.observed_enabled is False
    assert session.disable_calls == 0
    assert session.enable_calls == 0


def test_production_stop_declines_when_no_longer_active(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(
        task,
        status_rsp_desc="本次任务执行完毕",
        progress_rsp_desc="总任务量：10条，待执行：0条，执行中：0条，失败：0条，待入库：0条，执行完成：10条",
    )
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.stop_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status is RestartStatus.NO_LONGER_RUNNING
    # Write-time revalidation declined with zero remote writes.
    assert session.update_calls == []


def test_production_stop_disable_outcome_unknown_reobserves(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task, fail_disable=True)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    outcome = adapter.stop_running_task(prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0)

    assert outcome.status is RestartStatus.DISABLE_UNKNOWN
    assert outcome.observed_enabled is True
    assert session.enable_calls == 0


def test_stop_rejects_unrecognized_resume_phase(tmp_path, monkeypatch) -> None:
    task = _running_task_record()
    session = _FakeSession(task)
    adapter, prepared = _prepared_job(tmp_path, monkeypatch, task, session)

    with pytest.raises(ValueError):
        adapter.stop_running_task(
            prepared, TASK_ID, TASK_NUMBER, timeout_seconds=60.0, resume_phase="ENABLE_SENT"
        )

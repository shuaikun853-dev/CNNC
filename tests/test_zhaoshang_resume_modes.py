import importlib.util
import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import ModuleType

import openpyxl
import pytest


EXTERNAL_TOOL = Path(
    r"E:\.A2026\招商局自动建任务\external_tools\crawl_task_tool\auto_crawl_task.py"
)
EXTERNAL_WRAPPER = Path(
    r"E:\.A2026\招商局自动建任务\agent\tools\crawl_task_tool.py"
)


def _external_tool_loadable() -> bool:
    if not EXTERNAL_WRAPPER.is_file():
        return False
    try:
        spec = importlib.util.spec_from_file_location(
            "_governance_availability_probe", EXTERNAL_TOOL
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    except Exception:
        return False
    return hasattr(module, "run_auto_task")


if not _external_tool_loadable():
    pytest.skip(
        "zhaoshang external tool not loadable (PRODUCTION_MACHINE_ONLY)",
        allow_module_level=True,
    )


def load_external_tool():
    tool_dir = str(EXTERNAL_TOOL.parent)
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)
    spec = importlib.util.spec_from_file_location("zhaoshang_resume_tool", EXTERNAL_TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_external_wrapper():
    spec = importlib.util.spec_from_file_location(
        "zhaoshang_agent_wrapper", EXTERNAL_WRAPPER
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def existing_task():
    return {
        "taskId": 1227009,
        "taskNumber": "RW-20260809171239",
        "mallLogo": "colipu",
        "batchNumber": "cnnc_current_opt_科力普商城_20260809154847",
        "taskName": "cnnc_current_opt_科力普商城_20260809154847",
    }


def forbid(name):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError(f"{name} must not be called")

    return _forbidden


def configure_existing_active(monkeypatch, module):
    monkeypatch.setattr(module, "build_session", lambda: object())
    monkeypatch.setattr(module, "check_existing_task", lambda *_args: existing_task())
    monkeypatch.setattr(module, "query_task_status", lambda *_args: {"rspDesc": "任务执行中"})
    monkeypatch.setattr(module, "create_single_mall_task", forbid("create_single_mall_task"))
    monkeypatch.setattr(module, "query_task_by_name", forbid("query_task_by_name"))
    monkeypatch.setattr(module, "enable_task", forbid("enable_task"))


def test_create_only_existing_active_task_without_priority_evidence_fails_closed(
    monkeypatch, tmp_path
):
    module = load_external_tool()
    configure_existing_active(monkeypatch, module)
    monkeypatch.setattr(module, "wait_crawl_finished", forbid("wait_crawl_finished"))
    monkeypatch.setattr(module, "export_task_with_retry", forbid("export_task_with_retry"))

    with pytest.raises(RuntimeError, match="活动任务.*未取得提优证据"):
        module.run_auto_task(
            task_name="cnnc_current_opt_科力普商城_20260809154847",
            excel_path=tmp_path / "input.xlsx",
            mall_key="colipu",
            output_path=tmp_path / "result.xlsx",
            create_only=True,
        )


def test_wait_export_only_existing_active_task_only_waits_and_exports(monkeypatch, tmp_path):
    module = load_external_tool()
    configure_existing_active(monkeypatch, module)
    calls = []
    monkeypatch.setattr(
        module,
        "wait_crawl_finished",
        lambda **kwargs: calls.append(("wait", kwargs["batch_number"])),
    )
    monkeypatch.setattr(
        module,
        "export_task_with_retry",
        lambda **kwargs: calls.append(("export", kwargs["task_id"])),
    )

    module.run_auto_task(
        task_name="cnnc_current_opt_科力普商城_20260809154847",
        excel_path=tmp_path / "input.xlsx",
        mall_key="colipu",
        output_path=tmp_path / "result.xlsx",
        wait_export_only=True,
    )

    assert calls == [
        ("wait", "cnnc_current_opt_科力普商城_20260809154847"),
        ("export", 1227009),
    ]


def test_full_run_existing_completed_task_emits_unconfirmed_priority_evidence(
    monkeypatch, tmp_path, capsys
):
    module = load_external_tool()
    state = tmp_path / "BATCH_STATE.md"
    state.write_text("状态：ACTIVE\n", encoding="utf-8")
    events = []
    monkeypatch.setattr(module, "build_session", lambda: object())
    monkeypatch.setattr(module, "check_existing_task", lambda *_args: existing_task())
    monkeypatch.setattr(
        module,
        "query_task_status",
        lambda *_args: {"rspDesc": "本次任务执行完毕"},
    )
    monkeypatch.setattr(module, "create_single_mall_task", forbid("create_single_mall_task"))
    monkeypatch.setattr(module, "enable_task", forbid("enable_task"))
    monkeypatch.setattr(module, "wait_crawl_finished", forbid("wait_crawl_finished"))
    monkeypatch.setattr(module, "export_task_with_retry", lambda **_kwargs: None)
    monkeypatch.setattr(
        module,
        "record_batch_state_event",
        lambda _path, **event: events.append(event),
    )

    module.run_auto_task(
        task_name="cnnc_current_opt_科力普商城_20260809154847",
        excel_path=tmp_path / "input.xlsx",
        mall_key="colipu",
        output_path=tmp_path / "result.xlsx",
        batch_state_path=state,
    )

    assert "COMPLETED_UNCONFIRMED" in capsys.readouterr().out
    assert [event["action"] for event in events] == [
        "PRIORITY_UNCONFIRMED",
        "EXPORTED",
    ]


def test_wait_export_only_existing_completed_task_preserves_priority_evidence(
    monkeypatch, tmp_path, capsys
):
    module = load_external_tool()
    state = tmp_path / "BATCH_STATE.md"
    state.write_text("状态：ACTIVE\n", encoding="utf-8")
    events = []
    monkeypatch.setattr(module, "build_session", lambda: object())
    monkeypatch.setattr(module, "check_existing_task", lambda *_args: existing_task())
    monkeypatch.setattr(
        module,
        "query_task_status",
        lambda *_args: {"rspDesc": "本次任务执行完毕"},
    )
    monkeypatch.setattr(module, "create_single_mall_task", forbid("create_single_mall_task"))
    monkeypatch.setattr(module, "enable_task", forbid("enable_task"))
    monkeypatch.setattr(module, "wait_crawl_finished", forbid("wait_crawl_finished"))
    monkeypatch.setattr(module, "export_task_with_retry", lambda **_kwargs: None)
    monkeypatch.setattr(
        module,
        "record_batch_state_event",
        lambda _path, **event: events.append(event),
    )

    module.run_auto_task(
        task_name="cnnc_current_opt_科力普商城_20260809154847",
        excel_path=tmp_path / "input.xlsx",
        mall_key="colipu",
        output_path=tmp_path / "result.xlsx",
        batch_state_path=state,
        wait_export_only=True,
    )

    assert "COMPLETED_UNCONFIRMED" not in capsys.readouterr().out
    assert [event["action"] for event in events] == ["EXPORTED"]


def test_wait_export_only_missing_task_fails_closed_without_creating(monkeypatch, tmp_path):
    module = load_external_tool()
    monkeypatch.setattr(module, "build_session", lambda: object())
    monkeypatch.setattr(module, "check_existing_task", lambda *_args: None)
    monkeypatch.setattr(module, "create_single_mall_task", forbid("create_single_mall_task"))

    with pytest.raises(RuntimeError, match="拒绝创建"):
        module.run_auto_task(
            task_name="cnnc_current_opt_京东_20260809154847",
            excel_path=tmp_path / "input.xlsx",
            mall_key="jd",
            output_path=tmp_path / "result.xlsx",
            wait_export_only=True,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"create_only": True, "wait_export_only": True},
        {"wait_export_only": True, "skip_if_exists": False},
    ],
)
def test_invalid_resume_mode_combinations_fail_before_session_creation(
    monkeypatch, tmp_path, kwargs
):
    module = load_external_tool()
    monkeypatch.setattr(module, "build_session", forbid("build_session"))

    with pytest.raises(ValueError):
        module.run_auto_task(
            task_name="cnnc_current_opt_京东_20260809154847",
            excel_path=tmp_path / "input.xlsx",
            mall_key="jd",
            output_path=tmp_path / "result.xlsx",
            **kwargs,
        )


def test_external_cli_exposes_mutually_exclusive_wait_export_mode():
    result = subprocess.run(
        [sys.executable, str(EXTERNAL_TOOL), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "--wait-export-only" in result.stdout
    assert "--batch-state-file" in result.stdout


def test_agent_wrapper_passes_batch_state_and_returns_priority_evidence(
    monkeypatch, tmp_path
):
    module = load_external_wrapper()
    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self):
            return (
                "task_id: 1227009\n"
                "task_number: RW-20260809171239\n"
                "[priority] already -1 count=0\n"
                "[priority] affected_rows=39\n"
                "[priority] state=updated\n"
                "结果文件: result.xlsx\n",
                "",
            )

    def fake_popen(command, **_kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module, "build_internal_subprocess_env", lambda: {})
    batch_state = tmp_path / "BATCH_STATE.md"

    result = module.run_crawl_task(
        task_name="cnnc_current_opt_科力普商城_20260809154847",
        mall_key="colipu",
        excel_path=str(tmp_path / "input.xlsx"),
        batch_state_path=batch_state,
    )

    state_arg = captured["command"].index("--batch-state-file")
    assert captured["command"][state_arg + 1] == str(batch_state)
    assert result["main_task_id"] == 1227009
    assert result["task_number"] == "RW-20260809171239"
    assert result["priority_state"] == "updated"
    assert result["affected_rows"] == 39


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("[priority] state=already_highest_priority", "already_highest_priority"),
        ("[优先级调整] COMPLETED_UNCONFIRMED: done", "task_completed_unconfirmed"),
        ("[优先级调整] ACTIVE_UNCONFIRMED: active", "active_unconfirmed"),
    ],
)
def test_agent_wrapper_parses_non_updated_priority_states(output, expected):
    module = load_external_wrapper()

    assert module.parse_crawl_task_output(output)["priority_state"] == expected


def test_batch_state_event_is_appended_as_safe_json(tmp_path):
    module = load_external_tool()
    state = tmp_path / "BATCH_STATE.md"
    state.write_text("状态：ACTIVE\n", encoding="utf-8")

    module.record_batch_state_event(
        state,
        mall_key="jd",
        task_name="task-jd",
        action="ENABLED",
        task_id=123,
        task_number="RW-123",
    )

    event_line = state.read_text(encoding="utf-8").split("`", 2)[1]
    event = json.loads(event_line)
    assert event["action"] == "ENABLED"
    assert event["task_id"] == 123
    assert "Cookie" not in event_line


def test_prior_add_intent_blocks_another_add_when_query_finds_nothing(
    monkeypatch, tmp_path
):
    module = load_external_tool()
    state = tmp_path / "BATCH_STATE.md"
    state.write_text("状态：ACTIVE\n", encoding="utf-8")
    module.record_batch_state_event(
        state,
        mall_key="jd",
        task_name="cnnc_current_opt_京东_20260809154847",
        action="ADD_INTENT",
    )
    monkeypatch.setattr(module, "build_session", lambda: object())
    monkeypatch.setattr(module, "check_existing_task", lambda *_args: None)
    monkeypatch.setattr(module, "create_single_mall_task", forbid("create_single_mall_task"))

    with pytest.raises(RuntimeError, match="已有 /add 记录"):
        module.run_auto_task(
            task_name="cnnc_current_opt_京东_20260809154847",
            excel_path=tmp_path / "input.xlsx",
            mall_key="jd",
            output_path=tmp_path / "result.xlsx",
            create_only=True,
            batch_state_path=state,
        )


def test_new_create_records_each_write_boundary(monkeypatch, tmp_path):
    module = load_external_tool()
    state = tmp_path / "BATCH_STATE.md"
    state.write_text("状态：ACTIVE\n", encoding="utf-8")
    events = []
    monkeypatch.setattr(module, "build_session", lambda: object())
    monkeypatch.setattr(module, "check_existing_task", lambda *_args: None)
    monkeypatch.setattr(module, "has_batch_state_add_attempt", lambda *_args: False)
    monkeypatch.setattr(
        module,
        "record_batch_state_event",
        lambda _path, **event: events.append(event),
    )

    def create_task(*_args, before_add=None, after_add=None, **_kwargs):
        before_add()
        after_add()
        return {"local_subtask_count": 39}

    monkeypatch.setattr(module, "create_single_mall_task", create_task)
    monkeypatch.setattr(module, "query_task_by_name", lambda *_args: existing_task())
    monkeypatch.setattr(module, "enable_task", lambda *_args: {"rspCode": "0000"})
    priority = ModuleType("zhaoshang_priority_boost")

    class PriorityUpdateZeroRowsError(RuntimeError):
        pass

    priority.PriorityUpdateZeroRowsError = PriorityUpdateZeroRowsError
    priority_calls = []

    def boost_priority(**kwargs):
        priority_calls.append(kwargs)
        return {"affected_rows": 39, "priority_state": "updated"}

    priority.boost_zhaoshang_task_priority_after_create = boost_priority
    monkeypatch.setitem(sys.modules, "zhaoshang_priority_boost", priority)

    module.run_auto_task(
        task_name="cnnc_current_opt_科力普商城_20260809154847",
        excel_path=tmp_path / "input.xlsx",
        mall_key="colipu",
        output_path=tmp_path / "result.xlsx",
        create_only=True,
        batch_state_path=state,
    )

    assert [event["action"] for event in events] == [
        "ADD_INTENT",
        "ADD_ACCEPTED",
        "CREATED",
        "ENABLED",
        "PRIORITY_UPDATED",
    ]
    assert priority_calls == [
        {
            "task_name": "cnnc_current_opt_科力普商城_20260809154847",
            "task_number": "RW-20260809171239",
            "mall_code": "colipu",
            "expected_subtask_count": 39,
        }
    ]


class QueryResponse:
    def __init__(self, records):
        self.records = records
        self.status_code = 200
        self.text = json.dumps({"data": {"records": records}}, ensure_ascii=False)

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": {"records": self.records}}


class QuerySession:
    def __init__(self, records):
        self.records = records

    def post(self, *_args, **_kwargs):
        return QueryResponse(self.records)


def test_query_task_by_name_never_falls_back_to_first_fuzzy_record():
    module = load_external_tool()
    session = QuerySession([
        {
            "taskName": "cnnc_current_opt_京东_20260811181149",
            "taskNumber": "RW-wrong",
            "mallLogo": "jd",
        }
    ])

    with pytest.raises(RuntimeError, match="精确任务"):
        module.query_task_by_name(
            session,
            "cnnc_current_opt_广博商城_20260811181149",
            expected_mall_code="guangbo",
            retry=1,
            sleep_seconds=0,
        )


def test_query_task_by_name_requires_one_exact_name_and_mall_match():
    module = load_external_tool()
    task_name = "cnnc_current_opt_广博商城_20260811181149"
    session = QuerySession([
        {"taskName": task_name, "taskNumber": "RW-shared", "mallLogo": "jd"},
        {
            "taskName": task_name,
            "taskNumber": "RW-shared",
            "mallLogo": "guangbo",
        },
    ])

    task = module.query_task_by_name(
        session,
        task_name,
        expected_mall_code="guangbo",
        retry=1,
        sleep_seconds=0,
    )

    assert task["mallLogo"] == "guangbo"


def test_query_task_by_name_rejects_duplicate_exact_identity():
    module = load_external_tool()
    task_name = "cnnc_current_opt_广博商城_20260811181149"
    session = QuerySession([
        {"taskName": task_name, "taskNumber": "RW-shared", "mallLogo": "guangbo"},
        {"taskName": task_name, "taskNumber": "RW-shared", "mallLogo": "guangbo"},
    ])

    with pytest.raises(RuntimeError, match="不唯一"):
        module.query_task_by_name(
            session,
            task_name,
            expected_mall_code="guangbo",
            retry=1,
            sleep_seconds=0,
        )


def test_task_identity_does_not_default_or_cross_malls():
    module = load_external_tool()

    with pytest.raises(RuntimeError, match="商城标识"):
        module.get_task_identity(
            {"taskId": 1, "taskNumber": "RW-1"},
            expected_mall_code="guangbo",
        )
    with pytest.raises(RuntimeError, match="商城标识不一致"):
        module.get_task_identity(
            {"taskId": 1, "taskNumber": "RW-1", "mallLogo": "jd"},
            expected_mall_code="guangbo",
        )


def test_missing_configured_mall_code_fails_closed(monkeypatch):
    module = load_external_tool()
    monkeypatch.setitem(module.MALL_CONFIG, "broken", {"sourceName": "未映射商城"})

    with pytest.raises(ValueError, match="mall code"):
        module.priority_mall_code_for_key("broken")


def test_already_highest_priority_records_idempotent_state(monkeypatch, tmp_path):
    module = load_external_tool()
    state = tmp_path / "BATCH_STATE.md"
    state.write_text("状态：ACTIVE\n", encoding="utf-8")
    events = []
    monkeypatch.setattr(module, "build_session", lambda: object())
    monkeypatch.setattr(module, "check_existing_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "has_batch_state_add_attempt", lambda *_args: False)
    monkeypatch.setattr(
        module,
        "record_batch_state_event",
        lambda _path, **event: events.append(event),
    )

    def create_task(*_args, before_add=None, after_add=None, **_kwargs):
        before_add()
        after_add()
        return {"local_subtask_count": 12}

    monkeypatch.setattr(module, "create_single_mall_task", create_task)
    monkeypatch.setattr(module, "query_task_by_name", lambda *_args, **_kwargs: existing_task())
    monkeypatch.setattr(module, "enable_task", lambda *_args: {"rspCode": "0000"})
    priority = ModuleType("zhaoshang_priority_boost")

    class PriorityUpdateZeroRowsError(RuntimeError):
        pass

    priority.PriorityUpdateZeroRowsError = PriorityUpdateZeroRowsError
    priority.boost_zhaoshang_task_priority_after_create = lambda **_kwargs: {
        "affected_rows": 0,
        "priority_state": "already_highest_priority",
        "already_highest_count": 12,
    }
    monkeypatch.setitem(sys.modules, "zhaoshang_priority_boost", priority)

    module.run_auto_task(
        task_name="cnnc_current_opt_科力普商城_20260809154847",
        excel_path=tmp_path / "input.xlsx",
        mall_key="colipu",
        output_path=tmp_path / "result.xlsx",
        create_only=True,
        batch_state_path=state,
    )

    assert events[-1]["action"] == "PRIORITY_ALREADY_HIGHEST"
    assert events[-1]["already_highest_count"] == 12


def test_atomic_export_rejects_invalid_bytes_without_replacing_target(tmp_path):
    module = load_external_tool()
    target = tmp_path / "result.xlsx"
    target.write_bytes(b"previous valid placeholder")

    with pytest.raises(RuntimeError, match="不是有效的 XLSX"):
        module.write_export_file_atomic(target, b"partial download")

    assert target.read_bytes() == b"previous valid placeholder"


def test_atomic_export_replaces_target_after_xlsx_validation(tmp_path):
    module = load_external_tool()
    target = tmp_path / "result.xlsx"
    target.write_bytes(b"old")
    stream = BytesIO()
    workbook = openpyxl.Workbook()
    workbook.active.append(["商品编码", "商品价格"])
    workbook.save(stream)

    module.write_export_file_atomic(target, stream.getvalue())

    loaded = openpyxl.load_workbook(target, read_only=True)
    loaded.close()

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import incremental_local_fallback as fallback
from app.services import incremental_audit_runtime as runtime
from scripts import customer_prewarm_dispatch as dispatch


def test_fallback_empty_budget_creates_nothing_before_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(fallback, "verify_crawler_revision", lambda *a: pytest.fail("revision verification creates temporary files"))
    result = fallback.execute_pinned_crawler(
        [{"id": 1, "mall_key": "nbdeli", "exact_url": "https://b2b.nbdeli.com/item/1"}],
        run_dir=tmp_path / "run", tick_id="empty", timeout_seconds=59,
    )
    assert result == {1: {"deferred": True}}
    assert list(tmp_path.rglob("*")) == []


def test_dispatch_daily_jsonl_has_two_complete_records(tmp_path):
    target = tmp_path / "summary_2026-09-26.jsonl"
    for counter in (1, 2):
        dispatch._write_json_output(target, {"business_date": "2026-09-26", "outcome": "OK", "jobs_created": counter, "duration": 1.5})
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert len(list(tmp_path.iterdir())) == 1
    for row in rows:
        assert {"timestamp", "business_date", "outcome", "counters", "errors", "duration"} <= row.keys()
    assert [row["counters"]["jobs_created"] for row in rows] == [1, 2]


def test_dispatch_legacy_json_is_still_one_document(tmp_path):
    target = tmp_path / "legacy.json"
    dispatch._write_json_output(target, {"outcome": "OK"})
    assert json.loads(target.read_text(encoding="utf-8"))["outcome"] == "OK"


def test_batch_snapshots_bound_repeats_and_preserve_transitions(tmp_path):
    target = tmp_path / "BATCH_STATE.md"
    first = None
    transition = None
    for index in range(30):
        status = "BLOCKED" if index == 10 else "ACTIVE"
        snapshot = runtime._write_batch_state_snapshot(
            target, managed_block=f"status: {status}",
            event_line='- event: {"command":"tick", "outcome":"WAITING_REMOTE"}',
            alias_status="updated",
        )
        if index == 0:
            first = snapshot
        if index == 10:
            transition = snapshot
    assert first.exists() and transition.exists() and snapshot.exists()
    assert len(list(snapshot.parent.glob("BATCH_STATE_*.md"))) <= 9


def test_fallback_child_environment_shares_bytecode_cache(tmp_path):
    first = fallback._child_env(tmp_path / "a")
    second = fallback._child_env(tmp_path / "b")
    assert first["PYTHONPYCACHEPREFIX"] == second["PYTHONPYCACHEPREFIX"]
    assert not list(tmp_path.rglob("_pycache"))


def _owned_artifacts(tmp_path, tick):
    folder = tmp_path / "local_fallback" / tick
    (folder / "_tmp").mkdir(parents=True)
    (folder / "manifest.xlsx").write_bytes(b"manifest")
    (folder / "nbdeli_input.xlsx").write_bytes(b"input")
    (folder / "nbdeli_output.xlsx").write_bytes(b"output")
    (folder / "_tmp" / "scratch").write_bytes(b"scratch")
    result = fallback.CrawlerOutcomes()
    result.artifact_dir = folder
    return result


def test_durable_success_removes_only_owned_temporary_files(tmp_path):
    outcomes = _owned_artifacts(tmp_path, "success")
    extra = outcomes.artifact_dir / "operator-evidence.txt"
    extra.write_text("keep")
    fallback._cleanup_settled_artifacts(outcomes, ["SUCCESS"])
    assert list(outcomes.artifact_dir.iterdir()) == [extra]


def test_unfinished_artifacts_never_enter_retention(tmp_path):
    outcomes = _owned_artifacts(tmp_path, "unfinished")
    before = sorted(str(p) for p in outcomes.artifact_dir.rglob("*"))
    fallback._cleanup_settled_artifacts(outcomes, ["RETRYABLE_FAILED"])
    assert sorted(str(p) for p in outcomes.artifact_dir.rglob("*")) == before
    for index in range(8):
        failed = _owned_artifacts(tmp_path, f"failed-{index}")
        fallback._cleanup_settled_artifacts(failed, ["TERMINAL_FAILED"])
    # Terminal crawler errors still need manual diagnosis, not automatic expiry.
    assert len(list((tmp_path / "local_fallback").glob("*/.durably-settled.json"))) == 8
    assert sorted(str(p) for p in outcomes.artifact_dir.rglob("*")) == before


def test_durable_commit_failure_keeps_all_crawler_evidence(tmp_path, monkeypatch):
    from tests.test_incremental_local_fallback import setup_work, at, checked_jobs, success_result

    store, token, rows, job = setup_work(tmp_path)
    outcomes = _owned_artifacts(tmp_path, "commit-failure")
    before = sorted(str(p) for p in outcomes.artifact_dir.rglob("*"))

    def execute(claims):
        outcomes.update({int(claim["id"]): {"result": success_result("18.80")} for claim in claims})
        return outcomes

    def fail_commit(*args, **kwargs):
        raise OSError("simulated durable commit failure")

    monkeypatch.setattr(store, "complete_local_fallback", fail_commit)
    with pytest.raises(OSError, match="durable commit"):
        fallback.run_incremental_local_fallback(store, token, now=at(8, 30), tick_id="commit-failure", final_remote_check=checked_jobs, execute=execute)
    assert sorted(str(p) for p in outcomes.artifact_dir.rglob("*")) == before


def test_artifacts_clean_only_after_durable_completion_returns(tmp_path, monkeypatch):
    from tests.test_incremental_local_fallback import setup_work, at, checked_jobs, success_result

    store, token, rows, job = setup_work(tmp_path)
    outcomes = _owned_artifacts(tmp_path, "success-commit")
    original = store.complete_local_fallback
    observed = []

    def complete(*args, **kwargs):
        assert (outcomes.artifact_dir / "manifest.xlsx").exists()
        result = original(*args, **kwargs)
        observed.append(result["state"])
        assert (outcomes.artifact_dir / "manifest.xlsx").exists()
        return result

    def execute(claims):
        outcomes.update({int(claim["id"]): {"result": success_result("18.80")} for claim in claims})
        return outcomes

    monkeypatch.setattr(store, "complete_local_fallback", complete)
    fallback.run_incremental_local_fallback(store, token, now=at(8, 30), tick_id="success-commit", final_remote_check=checked_jobs, execute=execute)
    assert observed == ["SUCCESS"]
    assert not outcomes.artifact_dir.exists()


def test_reviewed_diagnostics_are_bounded_but_unreviewed_are_not(tmp_path):
    for index in range(8):
        outcomes = _owned_artifacts(tmp_path, f"reviewed-{index}")
        fallback._cleanup_settled_artifacts(outcomes, ["TERMINAL_FAILED"])
        marker = outcomes.artifact_dir / ".durably-settled.json"
        proof = json.loads(marker.read_text(encoding="utf-8"))
        proof["diagnostics_reviewed"] = True
        marker.write_text(json.dumps(proof), encoding="utf-8")
    current = _owned_artifacts(tmp_path, "unreviewed")
    fallback._cleanup_settled_artifacts(current, ["TERMINAL_FAILED"])
    assert len(list((tmp_path / "local_fallback").glob("*/.durably-settled.json"))) == 6
    assert (current.artifact_dir / "manifest.xlsx").exists()


def test_cleanup_refuses_reparse_ancestor_or_file(tmp_path, monkeypatch):
    outcomes = _owned_artifacts(tmp_path, "reparse")
    original = fallback.is_reparse_point
    for forbidden in (outcomes.artifact_dir.parent, outcomes.artifact_dir / "manifest.xlsx", outcomes.artifact_dir / "_tmp"):
        with monkeypatch.context() as patch:
            patch.setattr(fallback, "is_reparse_point", lambda path: path == forbidden or original(path))
            assert fallback._remove_known_artifacts(outcomes.artifact_dir) is False
    assert (outcomes.artifact_dir / "manifest.xlsx").exists()
    assert (outcomes.artifact_dir / "_tmp" / "scratch").exists()


def test_repeated_heartbeat_still_updates_alias_without_repeated_event(tmp_path):
    paths = runtime.runtime_paths("2026-09-26", tmp_path)
    result = runtime.RuntimeResult(runtime.TickSummary("WAITING_REMOTE", (), {}, {"pending": 1}, 1))
    for index in range(10):
        runtime._record_batch_result(paths, result, command="tick", enable_zhaoshang=False, output_path=paths.final_output_path)
    alias = (paths.run_dir / "BATCH_STATE.md").read_text(encoding="utf-8")
    assert alias.count('- event: {') == 1
    assert "status: ACTIVE" in alias
    latest = runtime.latest_batch_state_snapshot_path(paths)
    assert latest and '"WAITING_REMOTE"' in latest.read_text(encoding="utf-8")
    assert len(list(latest.parent.glob("BATCH_STATE_*.md"))) == 6


@pytest.mark.parametrize("script", ["run_customer_prewarm_scan_scheduled.ps1", "run_customer_prewarm_dispatch_scheduled.ps1", "run_incremental_price_audit_scheduled.ps1"])
def test_scheduler_preview_has_no_files_or_environment_setup(tmp_path, script):
    root = Path(__file__).resolve().parents[1]
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    assert pwsh, "PowerShell parser/runtime required for scheduler tests"
    text = (root / "scripts" / script).read_text(encoding="utf-8")
    assert text.index("if ($Preview)") < text.index('scheduler_e_environment.ps1') < text.index("Add-Type")
    result = subprocess.run([pwsh, "-NoProfile", "-File", str(root / "scripts" / script), "-RepoRoot", str(root), "-RuntimeRoot", str(tmp_path / "absent-runtime"), "-Preview"], capture_output=True, text=True, check=True)
    preview = json.loads(result.stdout)
    assert preview["Preview"] is True
    assert list(tmp_path.rglob("*")) == []
    if "dispatch" in script:
        assert preview["JsonOutputPath"].endswith(f'summary_{preview["BusinessDate"]}.jsonl')


@pytest.mark.parametrize("initial", ["", '{"invocation_id":"different","outcome":"OK"}\n{"interrupted":', '{"invocation_id":"mine","outcome":"OK"}', b'{"interrupted":"\xe4'])
def test_scheduler_summary_recovers_missing_partial_and_deduplicates(tmp_path, initial):
    root = Path(__file__).resolve().parents[1]
    target = tmp_path / "summary_2026-09-26.jsonl"
    initial_bytes = initial if isinstance(initial, bytes) else initial.encode("utf-8")
    target.write_bytes(initial_bytes)
    script = tmp_path / "invoke-summary.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f". '{root / 'scripts' / 'dispatch_summary.ps1'}'\n"
        f"Ensure-CnncDispatchSummary -Path '{target}' -InvocationId 'mine' -BusinessDate '2026-09-26' -ExitCode 124 -DurationSeconds 300\n"
        f"Ensure-CnncDispatchSummary -Path '{target}' -InvocationId 'mine' -BusinessDate '2026-09-26' -ExitCode 124 -DurationSeconds 300\n",
        encoding="utf-8",
    )
    subprocess.run([shutil.which("pwsh.exe") or shutil.which("pwsh"), "-NoProfile", "-File", str(script)], capture_output=True, text=True, encoding="utf-8", check=True)
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    owned = [row for row in rows if row["invocation_id"] == "mine"]
    assert len(owned) == 1
    if b'"mine"' not in initial_bytes:
        assert owned[0]["outcome"] == "DISPATCH_TIMEOUT"
        assert {"timestamp", "business_date", "counters", "errors", "duration", "exit_code"} <= owned[0].keys()
    else:
        assert owned[0]["outcome"] == "OK"
    assert target.read_bytes().endswith(b"\n")
    fragments = list(tmp_path.glob("*.partial_*"))
    assert len(fragments) == int(b'"interrupted"' in initial_bytes)
    if fragments:
        assert fragments[0].read_bytes() == initial_bytes.split(b"\n")[-1]


def test_child_jsonl_summary_has_scheduler_invocation_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "_INVOCATION_ID", "mine")
    target = tmp_path / "summary.jsonl"
    dispatch._write_json_output(target, {"outcome": "OK"})
    assert json.loads(target.read_text(encoding="utf-8"))["invocation_id"] == "mine"


@pytest.mark.skipif(os.name != "nt", reason="production Windows runtime junction")
def test_successful_fallback_cleanup_through_configured_runtime_junction(tmp_path, monkeypatch):
    from openpyxl import load_workbook
    from tests.test_incremental_local_fallback import write_output

    target = tmp_path / "physical-runtime"
    target.mkdir()
    alias = tmp_path / "runtime-alias"
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(alias), str(target)], capture_output=True, check=True)
    monkeypatch.setattr(fallback, "verify_crawler_revision", lambda root: fallback.EXPECTED_CRAWLER_REVISION)
    monkeypatch.setenv("CNNC_PYTHON_CACHE_ROOT", str(tmp_path / "shared-pyc"))
    url = "https://b2b.nbdeli.com/item/1"

    def fake_child(command, **kwargs):
        book = load_workbook(command[command.index("--manifest") + 1], data_only=True)
        output = list(book.active.values)[1][2]
        book.close()
        write_output(Path(output), [["1", "title", "18.80", url, "SUCCESS", ""]])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(fallback.subprocess, "run", fake_child)
    outcomes = fallback.execute_pinned_crawler([{"id": 1, "mall_key": "nbdeli", "exact_url": url}], run_dir=alias / "2026-09-26", tick_id="done")
    assert "result" in outcomes[1]
    fallback._cleanup_settled_artifacts(outcomes, ["SUCCESS"])
    assert not (target / "2026-09-26" / "local_fallback" / "done").exists()

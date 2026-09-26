import json
import os
import runpy
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts import cnnc_auth_browser as browser


def test_unavailable_root_never_falls_back(monkeypatch, tmp_path):
    root = tmp_path / "blocked"
    root.write_text("not a directory")
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", root)
    calls = []
    monkeypatch.setattr(browser.tempfile, "mkdtemp", lambda **kw: calls.append(kw) or str(tmp_path))
    with pytest.raises(OSError):
        browser._make_browser_profile_dir("cnnc_harvest_")
    assert calls == []


def test_old_unknown_profile_is_not_deleted(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", tmp_path)
    old = tmp_path / "cnnc_harvest_legacy"
    old.mkdir()
    os.utime(old, (time.time() - 86400,) * 2)
    browser._make_browser_profile_dir("cnnc_harvest_")
    assert old.exists()


@pytest.mark.parametrize("condition,deleted", [
    ("dead", True), ("live_owner", False), ("reused_pid", False),
    ("chrome", False), ("unknown_inventory", False), ("fresh", False),
    ("bad_marker", False), ("bad_identity", False),
])
def test_orphan_cleanup_requires_all_evidence(monkeypatch, tmp_path, condition, deleted):
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", tmp_path)
    profile = browser._make_browser_profile_dir("cnnc_harvest_")
    metadata_path = profile / browser.PROFILE_OWNER_FILE
    metadata = json.loads(metadata_path.read_text())
    assert {"purpose", "run_id", "pid", "created_at", "owner_created_at"} <= metadata.keys()
    metadata.update(pid=789123, created_at=time.time() - 1000)
    records = []
    if condition in ("live_owner", "reused_pid"):
        records = [{"pid": 789123, "created_at": 0, "cmdline": []}]
    if condition == "chrome":
        records = [{"pid": 1, "cmdline": ["chrome.exe", "--user-data-dir=" + str(profile)]}]
    if condition == "unknown_inventory":
        records = None
    if condition == "fresh":
        metadata["created_at"] = time.time()
    if condition == "bad_marker":
        metadata["marker"] = "foreign"
    if condition == "bad_identity":
        metadata.pop("owner_created_at")
    metadata_path.write_text(json.dumps(metadata))
    monkeypatch.setattr(browser, "_process_inventory", lambda: records)
    assert browser._remove_browser_profile(profile, orphan=True) is deleted
    assert profile.exists() is not deleted


def test_non_e_root_rejected_before_mkdir(monkeypatch):
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", Path("C:/blocked/profile"))
    with pytest.raises(OSError, match="E-drive"):
        browser._make_browser_profile_dir("cnnc_harvest_")


def test_mkdtemp_failure_has_no_second_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "BROWSER_TMP_ROOT", tmp_path)
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise OSError("disk full")
    monkeypatch.setattr(browser.tempfile, "mkdtemp", fail)
    with pytest.raises(OSError, match="disk full"):
        browser._make_browser_profile_dir("cnnc_harvest_")
    assert len(calls) == 1
    assert calls[0]["dir"] == str(tmp_path)


@pytest.mark.parametrize("reused", [False, True])
def test_owned_chrome_tree_termination_checks_identity(monkeypatch, reused):
    calls = []
    class Process:
        def __init__(self, pid):
            self.pid = pid
        def create_time(self):
            return 11 if reused and self.pid == 1 else 10
        def children(self, recursive):
            assert recursive
            return [Process(2)]
        def terminate(self):
            calls.append(("terminate", self.pid))
        def kill(self):
            calls.append(("kill", self.pid))
    monkeypatch.setattr(browser.psutil, "Process", Process)
    monkeypatch.setattr(browser.psutil, "wait_procs", lambda ps, timeout: ([], ps))
    browser._kill_chrome(SimpleNamespace(pid=1, _cnnc_identity=10, poll=lambda: None))
    assert calls == ([] if reused else [
        ("terminate", 1), ("terminate", 2), ("kill", 1), ("kill", 2)])


def test_inventory_access_denied_fails_closed(monkeypatch):
    def denied():
        raise browser.psutil.AccessDenied(42)
    monkeypatch.setattr(browser.psutil, "process_iter", denied)
    assert browser._process_inventory() is None


def test_profile_reference_with_spaces_and_split_argument(tmp_path):
    path = tmp_path / "with spaces"
    records = [{"cmdline": ["chrome.exe", "--user-data-dir", str(path)]}]
    assert browser._profile_referenced(path.resolve(), records)


def test_missing_process_dependency_stops_before_any_creation(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    with pytest.raises(ModuleNotFoundError, match="psutil"):
        runpy.run_path(browser.__file__, run_name="dependency_check")


@pytest.mark.parametrize("identity,child_reused,expected", [
    (10, False, [("terminate", 2), ("kill", 2)]),
    (10, True, []), (None, False, []), (99, False, []),
])
def test_close_cdp_cleans_captured_children_after_root_exit(
        monkeypatch, identity, child_reused, expected):
    calls = []
    state = {"root_exited": False}
    class Process:
        def __init__(self, pid):
            self.pid = pid
            if pid == 1 and state["root_exited"]:
                raise browser.psutil.NoSuchProcess(pid)
        def create_time(self):
            if self.pid == 2 and state["root_exited"] and child_reused:
                return 20
            return 10
        def children(self, recursive):
            return [Process(2)]
        def terminate(self):
            calls.append(("terminate", self.pid))
        def kill(self):
            calls.append(("kill", self.pid))
    def close():
        state["root_exited"] = True
    monkeypatch.setattr(browser.psutil, "Process", Process)
    monkeypatch.setattr(browser.psutil, "wait_procs", lambda ps, timeout: ([], ps))
    proc = SimpleNamespace(pid=1, _cnnc_identity=identity,
                           poll=lambda: 0 if state["root_exited"] else None)
    browser._close_cdp(proc, SimpleNamespace(close=close), None)
    assert calls == expected

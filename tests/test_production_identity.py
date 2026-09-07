r"""生产运行版本(Production Deployment Identity)服务 + 只读 API 契约测试。

边界纪律:
- 单元用例全部用 FakeRunner(dict 按 argv 关键字匹配脚本化响应)或抛异常
  runner,绝不触真实 git/网络/调度器;
- 集成用例只用 tmp_path 下的迷你 git 仓库 + 不存在的计划任务名
  (CNNC-Test-Nonexistent-XYZ,只读 Get-ScheduledTask,查无此任务 → registered
  False),不碰生产任务 E:\.A中核运维\cnnc-ops-agent;
- API 用例 monkeypatch dashboard_api.get_production_identity_service,
  不触发真实单例的 subprocess。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.production_identity import (
    DEFAULT_PROD_BRANCH,
    CommandResult,
    ProductionIdentityService,
    _reset_default_service,
    build_comparison,
    get_production_identity_service,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
TASKS = ("CNNC-PriceAudit-Incremental", "CNNC-Dashboard-ReadOnly")


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(0, stdout, "")


def fail(stderr: str = "", rc: int = 128) -> CommandResult:
    return CommandResult(rc, "", stderr)


class FakeRunner:
    """按 argv 关键字子串匹配脚本化响应。

    scripts: dict[keyword, CommandResult | list[CommandResult] | Exception]
      - CommandResult: 每次命中返回同一响应(粘性)
      - list: 按序逐次消耗;耗尽再命中 → AssertionError(暴露意外多余调用)
      - Exception 子类: 命中即抛
    未匹配 → default(默认 rc=1)。
    """

    def __init__(self, scripts=None, default=None):
        self.scripts = dict(scripts or {})
        self.default = default if default is not None else CommandResult(1, "", "unscripted")
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        key = " ".join(str(a) for a in argv)
        for kw, resp in self.scripts.items():
            if kw in key:
                if isinstance(resp, list):
                    if not resp:
                        raise AssertionError("script sequence exhausted for: " + kw)
                    return resp.pop(0)
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return self.default

    def calls_matching(self, keyword: str):
        return [c for c in self.calls if keyword in " ".join(c)]


def make_service(scripts=None, **kwargs):
    kwargs.setdefault("scheduler_task_names", TASKS)
    runner = FakeRunner(scripts)
    svc = ProductionIdentityService(repo_root=REPO_ROOT, command_runner=runner, **kwargs)
    return svc, runner


def base_scripts(sha=SHA_A, remote_sha=SHA_B):
    return {
        "rev-parse HEAD": ok(sha + "\n"),
        "branch --show-current": ok("main\n"),
        "status --porcelain": ok(""),
        "ls-remote": ok(f"{remote_sha}\trefs/heads/{DEFAULT_PROD_BRANCH}\n"),
        "query_scheduled_task_json": ok("null"),
    }


# ---- Case 1:startup == local == remote → version_status == MATCH ----

def test_case1_all_equal_match():
    svc, _ = make_service(base_scripts(SHA_A, remote_sha=SHA_A))
    snap = svc.snapshot()
    assert snap["startup"]["status"] == "OK"
    assert snap["startup"]["git_sha"] == SHA_A
    assert snap["local"]["status"] == "OK"
    assert snap["local"]["working_tree"] == "CLEAN"
    assert snap["local"]["dirty"]["classification"] == "CLEAN"
    assert snap["remote"]["status"] == "OK" and snap["remote"]["git_sha"] == SHA_A
    assert snap["scheduler"]["status"] == "OK"
    cmp = snap["comparison"]
    assert cmp["process_vs_local"] == "MATCH"
    assert cmp["local_vs_remote"] == "MATCH"
    assert cmp["version_status"] == "MATCH"
    assert cmp["alerts"] == []


# ---- Case 2:local != startup → CODE_CHANGED_AFTER_PROCESS_START ----

def test_case2_code_changed_after_process_start():
    scripts = base_scripts(SHA_A, remote_sha=SHA_C)
    # startup 捕获吃掉第一个 SHA_A;local 快照刷新得到 SHA_C(代码已前进而进程未重启)
    scripts["rev-parse HEAD"] = [ok(SHA_A + "\n"), ok(SHA_C + "\n")]
    svc, _ = make_service(scripts)
    snap = svc.snapshot()
    assert snap["startup"]["git_sha"] == SHA_A
    assert snap["local"]["git_sha"] == SHA_C
    cmp = snap["comparison"]
    assert cmp["process_vs_local"] == "CODE_CHANGED_AFTER_PROCESS_START"
    assert cmp["version_status"] == "CODE_CHANGED_AFTER_PROCESS_START"
    assert "代码已更新但进程未重启" in cmp["alerts"]


# ---- Case 3:ls-remote 失败 → 只降级 remote 块,结构完整不抛 ----

def test_case3_ls_remote_failure_degrades_remote_only():
    scripts = base_scripts(SHA_A, remote_sha=SHA_A)
    scripts["ls-remote"] = fail("fatal: 'origin' does not appear to be a git repository\n")
    svc, _ = make_service(scripts)
    snap = svc.snapshot()
    assert snap["remote"]["status"] == "UNKNOWN"
    assert snap["remote"]["git_sha"] is None
    assert "fatal" in snap["remote"]["error_code"]
    assert len(snap["remote"]["error_code"]) <= 200
    assert snap["remote"]["branch"] == DEFAULT_PROD_BRANCH
    # 其余块不受拖垮
    assert snap["local"]["status"] == "OK"
    assert snap["startup"]["status"] == "OK"
    assert snap["scheduler"]["status"] == "OK"
    assert snap["comparison"]["version_status"] == "REMOTE_UNKNOWN"
    assert snap["comparison"]["local_vs_remote"] == "REMOTE_UNKNOWN"
    assert "GitHub 生产分支 HEAD 暂不可达" in snap["comparison"]["alerts"]


# ---- Case 4:dirty 分类(真代码改动 vs 仅运行产物)+ detached HEAD + rename 原样保留 ----

def test_case4_dirty_with_code_changes():
    scripts = base_scripts()
    scripts["status --porcelain"] = ok(" M app/x.py\n?? __pycache__/m.pyc\n")
    svc, _ = make_service(scripts)
    local = svc.snapshot()["local"]
    assert local["working_tree"] == "DIRTY"
    assert local["dirty"]["total"] == 2
    assert local["dirty"]["artifact"] == 1
    assert local["dirty"]["code"] == 1
    assert local["dirty"]["classification"] == "HAS_CODE_CHANGES"
    assert local["dirty"]["entries"] == ["app/x.py", "__pycache__/m.pyc"]
    assert "工作区存在代码改动" in svc.snapshot()["comparison"]["alerts"]


def test_case4_dirty_artifacts_only():
    scripts = base_scripts()
    scripts["status --porcelain"] = ok(
        "?? _run/log.txt\n?? runtime/cache\n?? data/x.bak\n"
        "?? b.pyc\n?? t.pyo\n?? .pytest_cache/v\n?? __pycache__/y\n")
    svc, _ = make_service(scripts)
    local = svc.snapshot()["local"]
    assert local["working_tree"] == "DIRTY"
    assert local["dirty"]["total"] == 7 and local["dirty"]["code"] == 0
    assert local["dirty"]["classification"] == "ALL_ARTIFACTS"
    assert local["dirty"]["entries"] == [
        "_run/log.txt", "runtime/cache", "data/x.bak",
        "b.pyc", "t.pyo", ".pytest_cache/v", "__pycache__/y"]


def test_case4_data_bak_is_artifact_but_plain_data_is_code():
    scripts = base_scripts()
    scripts["status --porcelain"] = ok("?? data/a.bak\n?? data/keep.json\n")
    svc, _ = make_service(scripts)
    dirty = svc.snapshot()["local"]["dirty"]
    assert dirty["artifact"] == 1 and dirty["code"] == 1
    assert dirty["classification"] == "HAS_CODE_CHANGES"


def test_case4_rename_entry_kept_verbatim():
    scripts = base_scripts()
    scripts["status --porcelain"] = ok("R  old_name.py -> new_name.py\n")
    svc, _ = make_service(scripts)
    dirty = svc.snapshot()["local"]["dirty"]
    assert dirty["entries"] == ["old_name.py -> new_name.py"]
    assert dirty["classification"] == "HAS_CODE_CHANGES"


def test_case4_detached_head_when_branch_empty():
    scripts = base_scripts()
    scripts["branch --show-current"] = ok("\n")
    svc, _ = make_service(scripts)
    local = svc.snapshot()["local"]
    assert local["detached_head"] is True
    assert local["git_branch"] is None


# ---- Case 5:git 全失败(runner 抛异常 / rc=-1)→ 各块 UNKNOWN,snapshot 不抛 ----

def test_case5_runner_raises_everything():
    runner = FakeRunner(default=FileNotFoundError("git missing"))
    svc = ProductionIdentityService(repo_root=REPO_ROOT, command_runner=runner,
                                    scheduler_task_names=TASKS)
    snap = svc.snapshot()  # 绝不抛
    assert snap["startup"]["status"] == "UNKNOWN"
    assert snap["startup"]["git_sha"] is None
    assert snap["local"]["status"] == "UNKNOWN"
    assert snap["local"]["working_tree"] == "UNKNOWN"
    assert snap["remote"]["status"] == "UNKNOWN"
    assert snap["scheduler"]["status"] == "UNKNOWN"
    assert snap["comparison"]["process_vs_local"] == "UNKNOWN"


def test_case5_runner_returns_minus_one_everywhere():
    runner = FakeRunner(default=CommandResult(-1, "", "<FileNotFoundError>"))
    svc = ProductionIdentityService(repo_root=REPO_ROOT, command_runner=runner,
                                    scheduler_task_names=TASKS)
    snap = svc.snapshot()
    assert snap["startup"]["status"] == "UNKNOWN"
    assert snap["local"]["status"] == "UNKNOWN"
    assert snap["remote"]["status"] == "UNKNOWN"
    assert snap["remote"]["error_code"] == "<FileNotFoundError>"
    assert snap["scheduler"]["status"] == "UNKNOWN"


# ---- Case 6:调度器查询失败(垃圾输出/超时/error payload)→ scheduler UNKNOWN 不抛;"null" → registered False ----

def test_case6_scheduler_garbage_output_is_unknown():
    scripts = base_scripts(SHA_A, remote_sha=SHA_A)
    scripts["query_scheduled_task_json"] = ok("this is {{{ not json")
    svc, _ = make_service(scripts)
    snap = svc.snapshot()  # 不抛
    assert snap["scheduler"]["status"] == "UNKNOWN"
    assert [t["name"] for t in snap["scheduler"]["tasks"]] == list(TASKS)


def test_case6_scheduler_timeout_is_unknown():
    scripts = base_scripts(SHA_A, remote_sha=SHA_A)
    scripts["query_scheduled_task_json"] = subprocess.TimeoutExpired(
        cmd="powershell.exe", timeout=15)
    svc, _ = make_service(scripts)
    snap = svc.snapshot()  # 不抛
    assert snap["scheduler"]["status"] == "UNKNOWN"


def test_case6_scheduler_null_means_unregistered():
    scripts = base_scripts(SHA_A, remote_sha=SHA_A)
    scripts["query_scheduled_task_json"] = ok("null")
    svc, _ = make_service(scripts)
    snap = svc.snapshot()
    assert snap["scheduler"]["status"] == "OK"
    for task in snap["scheduler"]["tasks"]:
        assert task["registered"] is False
        assert task["state"] is None


def test_case6_scheduler_registered_task_fields_mapped():
    payload = {"task_name": "CNNC-PriceAudit-Incremental", "state": "Ready",
               "last_run_time": "2026-09-04T08:00:00", "next_run_time": "2026-09-04T18:00:00",
               "last_task_result": 0, "action": "C:\\Windows\\SYSTEM32\\cmd.exe",
               "arguments": "/c echo hi"}
    scripts = base_scripts(SHA_A, remote_sha=SHA_A)
    scripts["query_scheduled_task_json"] = ok(json.dumps(payload))
    svc, _ = make_service(scripts)
    task = svc.snapshot()["scheduler"]["tasks"][0]
    assert task["registered"] is True
    assert task["state"] == "Ready"
    assert task["last_run_time"] == "2026-09-04T08:00:00"
    assert task["next_run_time"] == "2026-09-04T18:00:00"
    assert task["last_task_result"] == 0
    assert task["action"] == "C:\\Windows\\SYSTEM32\\cmd.exe"


def test_case6_scheduler_error_payload_degrades_block():
    scripts = base_scripts(SHA_A, remote_sha=SHA_A)
    scripts["query_scheduled_task_json"] = ok('{"error":"Something failed"}')
    svc, _ = make_service(scripts)
    snap = svc.snapshot()  # 不抛
    # error payload = 查询失败,整块降级
    assert snap["scheduler"]["status"] == "UNKNOWN"
    assert snap["scheduler"]["tasks"][0]["registered"] is False
    assert snap["scheduler"]["tasks"][0]["state"] == "UNKNOWN"


# ---- BEHIND / AHEAD / REMOTE_DIFFERS:merge-base --is-ancestor rc 组合 ----

def test_remote_ahead_of_local_is_behind():
    scripts = base_scripts(SHA_A, remote_sha=SHA_B)
    scripts[f"is-ancestor {SHA_A} {SHA_B}"] = ok()      # local 是 remote 祖先 → 落后
    scripts[f"is-ancestor {SHA_B} {SHA_A}"] = fail("", rc=1)
    svc, _ = make_service(scripts)
    cmp = svc.snapshot()["comparison"]
    assert cmp["local_vs_remote"] == "BEHIND"
    assert cmp["version_status"] == "BEHIND"


def test_local_ahead_of_remote_is_ahead():
    scripts = base_scripts(SHA_A, remote_sha=SHA_B)
    scripts[f"is-ancestor {SHA_A} {SHA_B}"] = fail("", rc=1)
    scripts[f"is-ancestor {SHA_B} {SHA_A}"] = ok()      # remote 是 local 祖先 → 领先
    svc, _ = make_service(scripts)
    assert svc.snapshot()["comparison"]["local_vs_remote"] == "AHEAD"


def test_ancestor_both_ways_fail_is_remote_differs():
    scripts = base_scripts(SHA_A, remote_sha=SHA_B)
    scripts[f"is-ancestor {SHA_A} {SHA_B}"] = fail("", rc=1)
    scripts[f"is-ancestor {SHA_B} {SHA_A}"] = fail("", rc=1)
    svc, _ = make_service(scripts)
    cmp = svc.snapshot()["comparison"]
    assert cmp["local_vs_remote"] == "REMOTE_DIFFERS"
    assert cmp["version_status"] == "REMOTE_DIFFERS"
    assert "本地与 GitHub 生产分支不一致(方向未知)" in cmp["alerts"]


def test_ancestor_missing_objects_rc128_falls_to_remote_differs():
    scripts = base_scripts(SHA_A, remote_sha=SHA_B)
    scripts[f"is-ancestor {SHA_A} {SHA_B}"] = fail("fatal: Not all commits exist\n", rc=128)
    scripts[f"is-ancestor {SHA_B} {SHA_A}"] = fail("fatal: Not all commits exist\n", rc=128)
    svc, _ = make_service(scripts)
    cmp = svc.snapshot()["comparison"]
    assert cmp["local_vs_remote"] == "REMOTE_DIFFERS"
    assert cmp["local_vs_remote"] not in ("MATCH", "BEHIND", "AHEAD")
    assert "本地与 GitHub 生产分支不一致(方向未知)" in cmp["alerts"]


def test_code_changed_priority_over_remote_differs():
    """CODE_CHANGED_AFTER_PROCESS_START 优先级最高:local 与 remote 也不一致
    (双向 is-ancestor 失败)时,version_status 仍是进程未重启,不被改写。"""
    scripts = base_scripts(SHA_A, remote_sha=SHA_B)
    # startup 捕获吃掉第一个 SHA_A;local 快照刷新得到 SHA_C
    scripts["rev-parse HEAD"] = [ok(SHA_A + "\n"), ok(SHA_C + "\n")]
    scripts[f"is-ancestor {SHA_C} {SHA_B}"] = fail("", rc=1)
    scripts[f"is-ancestor {SHA_B} {SHA_C}"] = fail("", rc=1)
    svc, _ = make_service(scripts)
    cmp = svc.snapshot()["comparison"]
    assert cmp["process_vs_local"] == "CODE_CHANGED_AFTER_PROCESS_START"
    assert cmp["version_status"] == "CODE_CHANGED_AFTER_PROCESS_START"
    assert cmp["local_vs_remote"] == "REMOTE_DIFFERS"


def test_build_comparison_pure_function_without_runner():
    """纯函数 seam:注入 is_ancestor 谓词即可单测,不依赖任何 subprocess。"""
    startup = {"git_sha": SHA_A}
    local = {"git_sha": SHA_A, "dirty": {"classification": "CLEAN"}}
    remote = {"status": "OK", "git_sha": SHA_B}
    cmp = build_comparison(startup, local, remote,
                           is_ancestor=lambda a, b: a == SHA_A and b == SHA_B)
    assert cmp == {"process_vs_local": "MATCH", "local_vs_remote": "BEHIND",
                   "version_status": "BEHIND", "alerts": []}


# ---- TTL 缓存:窗口内二次 snapshot 不再触发 git/ls-remote/powershell ----

def test_ttl_cache_suppresses_repeat_subprocess():
    svc, runner = make_service(base_scripts(SHA_A, remote_sha=SHA_A),
                               local_ttl=60.0, remote_ttl=60.0, scheduler_ttl=60.0)
    svc.snapshot()
    svc.snapshot()
    assert len(runner.calls_matching("rev-parse HEAD")) == 2   # startup 1 + local 1
    assert len(runner.calls_matching("branch --show-current")) == 2
    assert len(runner.calls_matching("status --porcelain")) == 1
    assert len(runner.calls_matching("ls-remote")) == 1
    assert len(runner.calls_matching("query_scheduled_task_json")) == len(TASKS)


def test_ttl_expiry_forces_refresh():
    svc, runner = make_service(base_scripts(SHA_A, remote_sha=SHA_A),
                               local_ttl=0.0, remote_ttl=0.0, scheduler_ttl=0.0)
    svc.snapshot()
    svc.snapshot()
    assert len(runner.calls_matching("status --porcelain")) == 2
    assert len(runner.calls_matching("ls-remote")) == 2


# ---- 单例:get 两次同对象;_reset 后新建 ----

def test_singleton_created_at_import_time():
    """dashboard_api 模块 import 时必须急切创建单例(uvicorn 启动即捕获
    startup 身份);退回懒创建会把 startup SHA 推迟到首访,掩盖
    “代码已更新但进程没重启”。"""
    import importlib

    from app import dashboard_api as dash
    from app.services import production_identity as pi

    pi._reset_default_service()
    importlib.reload(dash)
    assert pi._default_service is not None
    pi._reset_default_service()


def test_singleton_identity_and_reset():
    _reset_default_service()
    try:
        first = get_production_identity_service()
        second = get_production_identity_service()
        assert first is second
        _reset_default_service()
        third = get_production_identity_service()
        assert third is not first
    finally:
        _reset_default_service()


# ---- prod 分支环境变量覆盖 ----

def test_prod_branch_env_override(monkeypatch):
    monkeypatch.setenv("CNNC_IDENTITY_PROD_BRANCH", "prod/hotfix-test")
    scripts = base_scripts()
    scripts["ls-remote"] = ok(f"{SHA_B}\trefs/heads/prod/hotfix-test\n")
    svc, _ = make_service(scripts)
    snap = svc.snapshot()
    assert snap["remote"]["branch"] == "prod/hotfix-test"


# ---- 快照结构完整性(任何降级下五块+generated_at 齐全)----

def test_snapshot_structure_keys_complete():
    svc, _ = make_service(base_scripts(SHA_A, remote_sha=SHA_B))
    snap = svc.snapshot()
    assert set(snap.keys()) == {"generated_at", "startup", "local", "remote",
                                "scheduler", "comparison"}
    assert set(snap["startup"].keys()) == {"status", "git_sha", "git_branch",
                                           "captured_at", "python", "process"}
    assert set(snap["startup"]["python"].keys()) == {"version", "executable", "virtualenv"}
    assert set(snap["startup"]["process"].keys()) == {"pid", "started_at", "started_at_source"}
    assert set(snap["local"].keys()) == {"status", "git_sha", "git_branch",
                                         "detached_head", "working_tree", "dirty"}
    assert set(snap["local"]["dirty"].keys()) == {"total", "artifact", "code",
                                                  "classification", "entries"}
    assert set(snap["remote"].keys()) == {"status", "git_sha", "branch",
                                          "checked_at", "error_code"}
    assert set(snap["scheduler"].keys()) == {"status", "checked_at", "tasks"}
    assert set(snap["comparison"].keys()) == {"process_vs_local", "local_vs_remote",
                                              "version_status", "alerts"}
    assert snap["startup"]["process"]["pid"] > 0
    assert snap["startup"]["python"]["virtualenv"] in (True, False)
    assert snap["generated_at"].endswith("+08:00")
    assert snap["remote"]["checked_at"].endswith("+08:00")
    assert snap["scheduler"]["checked_at"].endswith("+08:00")


def test_dirty_entries_capped_at_ten():
    scripts = base_scripts()
    scripts["status --porcelain"] = ok("".join(
        f"?? file_{i}.py\n" for i in range(15)))
    svc, _ = make_service(scripts)
    dirty = svc.snapshot()["local"]["dirty"]
    assert dirty["total"] == 15
    assert len(dirty["entries"]) == 10


# ---- API 契约:GET 只读 + 400 未知参数 + 503 兜底 ----

@pytest.fixture()
def client():
    return TestClient(app)


class _StubService:
    def snapshot(self):
        return {"generated_at": "2026-09-04T10:00:00+08:00",
                "startup": {"status": "OK"}, "local": {"status": "OK"},
                "remote": {"status": "OK"}, "scheduler": {"status": "OK"},
                "comparison": {"version_status": "MATCH", "alerts": []}}


def test_api_production_identity_200_shape(client, monkeypatch):
    from app import dashboard_api
    monkeypatch.setattr(dashboard_api, "get_production_identity_service",
                        lambda: _StubService())
    r = client.get("/dashboard/api/production-identity")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    body = r.json()
    for key in ("startup", "local", "remote", "scheduler", "comparison"):
        assert key in body, key


def test_api_production_identity_rejects_any_query_param(client):
    r = client.get("/dashboard/api/production-identity?date=2026-09-04")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "UNKNOWN_QUERY_PARAM"
    r = client.get("/dashboard/api/production-identity?refresh=1")
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "UNKNOWN_QUERY_PARAM"


def test_api_production_identity_503_on_snapshot_breach(client, monkeypatch):
    """snapshot 契约永不抛;若仍意外抛出 → 503 降级,不裸 500。"""
    from app import dashboard_api

    def boom():
        raise RuntimeError("unreachable")

    monkeypatch.setattr(dashboard_api, "get_production_identity_service", boom)
    r = client.get("/dashboard/api/production-identity")
    assert r.status_code == 503
    assert r.json()["detail"]["error_code"] == "PRODUCTION_IDENTITY_READ_FAILED"


def test_api_production_identity_get_only(client):
    for method in ("post", "put", "delete", "patch"):
        r = getattr(client, method)("/dashboard/api/production-identity")
        assert r.status_code == 405, method


# ---- 集成(真实 subprocess):tmp 迷你 git 仓库 + 不存在的计划任务名 ----

@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_integration_real_git_repo_and_absent_task(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copyfile(REPO_ROOT / "scripts" / "query_scheduled_task_json.ps1",
                    scripts_dir / "query_scheduled_task_json.ps1")
    for argv in (["git", "init", "-q"],
                 ["git", "add", "-A"],
                 ["git", "-c", "user.email=probe@test", "-c", "user.name=probe",
                  "commit", "-q", "-m", "init"]):
        proc = subprocess.run(argv, cwd=str(tmp_path), capture_output=True, text=True)
        assert proc.returncode == 0, argv + proc.stderr
    svc = ProductionIdentityService(
        repo_root=tmp_path,
        scheduler_task_names=("CNNC-Test-Nonexistent-XYZ",))
    snap = svc.snapshot()
    assert snap["startup"]["status"] == "OK"
    assert snap["local"]["status"] == "OK"
    assert snap["local"]["git_branch"]                     # master/main 均可
    assert snap["local"]["working_tree"] == "CLEAN"
    assert snap["startup"]["git_sha"] == snap["local"]["git_sha"]
    assert snap["remote"]["status"] == "UNKNOWN"           # 无 origin,快速失败
    assert snap["comparison"]["version_status"] == "REMOTE_UNKNOWN"
    task = snap["scheduler"]["tasks"][0]
    assert task["name"] == "CNNC-Test-Nonexistent-XYZ"
    assert task["registered"] is False                     # 真实只读查询:查无此任务

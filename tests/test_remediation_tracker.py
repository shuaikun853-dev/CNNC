import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.remediation_tracker import (
    CommandResult,
    RemediationTrackerService,
)


class FakeRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv, timeout=None):
        key = tuple(str(x) for x in argv)
        self.calls.append((key, timeout))
        return self.responses.get(key, CommandResult(1, "", "missing fake"))


class StubProductionIdentity:
    def snapshot(self):
        return {
            "startup": {"git_sha": "prod-runtime-sha"},
            "remote": {"git_sha": "prod-remote-sha", "status": "OK"},
        }


def write_ledger(path: Path):
    path.write_text(json.dumps({
        "version": 1,
        "production_branch": "prod/closeout-20260818",
        "items": [
            {
                "id": "F01", "severity": "P0", "title": "收口数据不一致",
                "chatgpt": {"title": "稳定性审计"},
                "codex": {"title": "closeout evidence"},
                "branch": "fix/F01-closeout",
                "pr": None,
                "review": {"status": "PENDING"},
                "next_action": "创建 PR",
            },
            {
                "id": "F02", "severity": "P0", "title": "UNKNOWN 自动重派",
                "chatgpt": {"title": "稳定性审计"},
                "codex": {"title": "cross-day reconcile"},
                "branch": "fix/F02-reconcile",
                "pr": 26,
                "review": {"status": "PARTIAL"},
                "next_action": "补门禁",
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")


def test_snapshot_enriches_branch_pr_and_production_state(tmp_path):
    ledger = tmp_path / "tracker.json"
    write_ledger(ledger)
    repo = tmp_path / "repo"
    repo.mkdir()

    def cmd(*parts):
        return ("git", "-C", str(repo), *parts)

    runner = FakeRunner({
        cmd("rev-parse", "--verify", "fix/F01-closeout^{commit}"):
            CommandResult(0, "f01-sha\n", ""),
        cmd("rev-parse", "--verify", "fix/F02-reconcile^{commit}"):
            CommandResult(0, "f02-sha\n", ""),
        cmd("rev-parse", "--verify", "prod/closeout-20260818^{commit}"):
            CommandResult(0, "prod-remote-sha\n", ""),
        cmd("merge-base", "--is-ancestor", "f01-sha", "prod-remote-sha"):
            CommandResult(1, "", ""),
        cmd("merge-base", "--is-ancestor", "f02-sha", "prod-remote-sha"):
            CommandResult(1, "", ""),
        cmd("merge-base", "--is-ancestor", "f01-sha", "prod-runtime-sha"):
            CommandResult(1, "", ""),
        cmd("merge-base", "--is-ancestor", "f02-sha", "prod-runtime-sha"):
            CommandResult(1, "", ""),
    })

    svc = RemediationTrackerService(
        repo_root=repo,
        ledger_path=ledger,
        command_runner=runner,
        production_identity=StubProductionIdentity(),
        pr_reader=lambda number: {
            "number": number, "state": "OPEN", "url": "https://example/pr/26",
            "merged_at": None, "merge_commit_sha": None,
        },
        branch_pr_reader=lambda branch: None,
    )
    snap = svc.snapshot()

    assert snap["summary"]["total"] == 2
    assert snap["summary"]["p0"] == 2
    assert snap["items"][0]["development"]["state"] == "BRANCH_ONLY"
    assert snap["items"][1]["development"]["state"] == "PR_OPEN"
    assert snap["items"][1]["pr"]["url"] == "https://example/pr/26"
    assert snap["items"][0]["production"]["state"] == "NOT_MERGED"


def test_api_and_page_contract(monkeypatch):
    from app import dashboard_api

    payload = {
        "generated_at": "2026-09-18T10:00:00+08:00",
        "summary": {
            "total": 27, "p0": 4, "in_development": 3,
            "pending_review": 2, "merged_not_deployed": 1,
            "production_verified": 0,
        },
        "items": [{"id": "F01", "severity": "P0", "title": "收口数据不一致"}],
    }

    class Stub:
        def snapshot(self):
            return payload

    monkeypatch.setattr(
        dashboard_api, "get_remediation_tracker_service", lambda: Stub()
    )
    client = TestClient(app)

    page = client.get("/dashboard/remediation")
    assert page.status_code == 200
    assert 'id="remediation-app"' in page.text
    assert "整改追踪" in page.text

    api = client.get("/dashboard/api/remediation")
    assert api.status_code == 200
    assert api.headers["Cache-Control"] == "no-store"
    assert api.json() == payload

    bad = client.get("/dashboard/api/remediation?refresh=1")
    assert bad.status_code == 400
    assert bad.json()["detail"]["error_code"] == "UNKNOWN_QUERY_PARAM"


def test_branch_can_auto_discover_pr_once_for_shared_branch(tmp_path):
    ledger = tmp_path / "tracker.json"
    write_ledger(ledger)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["items"][1]["branch"] = data["items"][0]["branch"]
    data["items"][1]["pr"] = None
    ledger.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()

    def cmd(*parts):
        return ("git", "-C", str(repo), *parts)

    runner = FakeRunner({
        cmd("rev-parse", "--verify", "prod/closeout-20260818^{commit}"):
            CommandResult(0, "prod-remote-sha\n", ""),
        cmd("rev-parse", "--verify", "fix/F01-closeout^{commit}"):
            CommandResult(0, "feature-sha\n", ""),
        cmd("merge-base", "--is-ancestor", "feature-sha", "prod-runtime-sha"):
            CommandResult(1, "", ""),
        cmd("merge-base", "--is-ancestor", "feature-sha", "prod-remote-sha"):
            CommandResult(1, "", ""),
    })
    calls = []

    def find_pr(branch):
        calls.append(branch)
        return {"number": 99, "state": "OPEN", "url": "https://example/pr/99"}

    snap = RemediationTrackerService(
        repo_root=repo, ledger_path=ledger, command_runner=runner,
        production_identity=StubProductionIdentity(),
        pr_reader=lambda number: None, branch_pr_reader=find_pr,
    ).snapshot()

    assert calls == ["fix/F01-closeout"]
    branch_remote_calls = [
        call for call, _timeout in runner.calls
        if call[-1] == "refs/heads/fix/F01-closeout"
    ]
    assert len(branch_remote_calls) == 1
    assert [item["development"]["state"] for item in snap["items"]] == [
        "PR_OPEN", "PR_OPEN"
    ]
    assert all(item["pr"]["number"] == 99 for item in snap["items"])


def test_remote_tracking_branch_is_used_when_local_branch_is_absent(tmp_path):
    ledger = tmp_path / "tracker.json"
    write_ledger(ledger)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["items"] = [data["items"][0]]
    ledger.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()

    def cmd(*parts):
        return ("git", "-C", str(repo), *parts)

    runner = FakeRunner({
        cmd("rev-parse", "--verify", "prod/closeout-20260818^{commit}"):
            CommandResult(0, "prod-remote-sha\n", ""),
        cmd("rev-parse", "--verify", "fix/F01-closeout^{commit}"):
            CommandResult(1, "", ""),
        cmd("rev-parse", "--verify", "origin/fix/F01-closeout^{commit}"):
            CommandResult(0, "remote-feature-sha\n", ""),
        cmd("merge-base", "--is-ancestor", "remote-feature-sha", "prod-runtime-sha"):
            CommandResult(1, "", ""),
        cmd("merge-base", "--is-ancestor", "remote-feature-sha", "prod-remote-sha"):
            CommandResult(1, "", ""),
    })

    snap = RemediationTrackerService(
        repo_root=repo, ledger_path=ledger, command_runner=runner,
        production_identity=StubProductionIdentity(),
        branch_pr_reader=lambda branch: None,
    ).snapshot()

    assert snap["items"][0]["branch_status"]["head_sha"] == "remote-feature-sha"
    assert snap["items"][0]["development"]["state"] == "BRANCH_ONLY"


def test_unknown_remote_identity_does_not_override_local_production_branch(tmp_path):
    ledger = tmp_path / "tracker.json"
    write_ledger(ledger)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["items"] = [data["items"][0]]
    ledger.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()

    class RemoteUnknown:
        def snapshot(self):
            return {
                "startup": {"git_sha": "runtime-old"},
                "remote": {
                    "status": "UNKNOWN",
                    "git_sha": "stale-remote-sha",
                },
            }

    def cmd(*parts):
        return ("git", "-C", str(repo), *parts)

    runner = FakeRunner({
        cmd("rev-parse", "--verify", "prod/closeout-20260818^{commit}"):
            CommandResult(0, "local-prod-sha\n", ""),
        cmd("rev-parse", "--verify", "fix/F01-closeout^{commit}"):
            CommandResult(0, "feature-sha\n", ""),
        cmd("merge-base", "--is-ancestor", "feature-sha", "runtime-old"):
            CommandResult(1, "", ""),
        cmd("merge-base", "--is-ancestor", "feature-sha", "local-prod-sha"):
            CommandResult(0, "", ""),
    })

    snap = RemediationTrackerService(
        repo_root=repo, ledger_path=ledger, command_runner=runner,
        production_identity=RemoteUnknown(),
        branch_pr_reader=lambda branch: None,
    ).snapshot()

    item = snap["items"][0]
    assert item["production"]["production_branch_sha"] == "local-prod-sha"
    assert item["production"]["state"] == "MERGED_NOT_DEPLOYED"


def test_checked_in_ledger_has_all_27_findings():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "docs" / "stability" / "remediation_tracker.json"
    )
    data = json.loads(ledger.read_text(encoding="utf-8"))
    items = data["items"]

    assert [item["id"] for item in items] == [
        f"F{index:02d}" for index in range(1, 28)
    ]
    assert sum(item["severity"] == "P0" for item in items) == 4


def test_known_pr_with_unavailable_github_state_is_not_downgraded_to_branch_only(
    tmp_path,
):
    ledger = tmp_path / "tracker.json"
    write_ledger(ledger)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["items"] = [data["items"][1]]
    ledger.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()

    def cmd(*parts):
        return ("git", "-C", str(repo), *parts)

    runner = FakeRunner({
        cmd("rev-parse", "--verify", "prod/closeout-20260818^{commit}"):
            CommandResult(0, "prod-remote-sha\n", ""),
        cmd("rev-parse", "--verify", "fix/F02-reconcile^{commit}"):
            CommandResult(0, "feature-sha\n", ""),
        cmd("merge-base", "--is-ancestor", "feature-sha", "prod-runtime-sha"):
            CommandResult(1, "", ""),
        cmd("merge-base", "--is-ancestor", "feature-sha", "prod-remote-sha"):
            CommandResult(1, "", ""),
    })

    snap = RemediationTrackerService(
        repo_root=repo, ledger_path=ledger, command_runner=runner,
        production_identity=StubProductionIdentity(),
        pr_reader=lambda number: {
            "number": number, "state": "UNKNOWN", "url": None,
            "merged_at": None, "merge_commit_sha": None,
        },
    ).snapshot()

    assert snap["items"][0]["development"]["state"] == "PR_UNKNOWN"


def test_branch_pr_lookup_failure_is_reported_as_unknown_not_no_pr(tmp_path):
    ledger = tmp_path / "tracker.json"
    write_ledger(ledger)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["items"] = [data["items"][0]]
    ledger.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()

    def cmd(*parts):
        return ("git", "-C", str(repo), *parts)

    runner = FakeRunner({
        cmd("rev-parse", "--verify", "prod/closeout-20260818^{commit}"):
            CommandResult(0, "prod-remote-sha\n", ""),
        cmd("rev-parse", "--verify", "fix/F01-closeout^{commit}"):
            CommandResult(0, "feature-sha\n", ""),
        cmd("merge-base", "--is-ancestor", "feature-sha", "prod-runtime-sha"):
            CommandResult(1, "", ""),
        cmd("merge-base", "--is-ancestor", "feature-sha", "prod-remote-sha"):
            CommandResult(1, "", ""),
    })

    snap = RemediationTrackerService(
        repo_root=repo, ledger_path=ledger, command_runner=runner,
        production_identity=StubProductionIdentity(),
        branch_pr_reader=lambda branch: {
            "number": None, "state": "UNKNOWN", "url": None,
            "lookup_failed": True,
        },
    ).snapshot()

    assert snap["items"][0]["development"]["state"] == "PR_UNKNOWN"
    assert snap["items"][0]["pr"]["state"] == "UNKNOWN"


def test_branch_already_in_production_is_development_merged_without_pr(tmp_path):
    ledger = tmp_path / "tracker.json"
    write_ledger(ledger)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["items"] = [data["items"][0]]
    ledger.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()

    def cmd(*parts):
        return ("git", "-C", str(repo), *parts)

    runner = FakeRunner({
        cmd("rev-parse", "--verify", "prod/closeout-20260818^{commit}"):
            CommandResult(0, "prod-new\n", ""),
        cmd("rev-parse", "--verify", "fix/F01-closeout^{commit}"):
            CommandResult(0, "feature-sha\n", ""),
        cmd("merge-base", "--is-ancestor", "feature-sha", "prod-runtime-sha"):
            CommandResult(1, "", ""),
        cmd("merge-base", "--is-ancestor", "feature-sha", "prod-new"):
            CommandResult(0, "", ""),
    })

    class ProdNew:
        def snapshot(self):
            return {
                "startup": {"git_sha": "prod-runtime-sha"},
                "remote": {"status": "OK", "git_sha": "prod-new"},
            }

    item = RemediationTrackerService(
        repo_root=repo, ledger_path=ledger, command_runner=runner,
        production_identity=ProdNew(), branch_pr_reader=lambda branch: None,
    ).snapshot()["items"][0]

    assert item["production"]["state"] == "MERGED_NOT_DEPLOYED"
    assert item["development"]["state"] == "MERGED"

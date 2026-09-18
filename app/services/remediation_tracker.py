"""Read-only remediation tracker projection for the CNNC dashboard."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.production_identity import get_production_identity_service

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_REPOSITORY = "shuaikun853-dev/CNNC"
DEFAULT_PROD_BRANCH = "prod/closeout-20260818"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _default_runner(cwd: Path):
    def run(argv, timeout=None):
        kwargs = {"cwd": str(cwd)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                [str(part) for part in argv], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, **kwargs
            )
            return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(-1, "", f"<{type(exc).__name__}>")
    return run


class RemediationTrackerService:
    def __init__(
        self, repo_root=None, ledger_path=None, *, command_runner=None,
        production_identity=None, pr_reader=None, branch_pr_reader=None,
        ttl_seconds=15.0,
    ):
        self.repo_root = (
            Path(repo_root) if repo_root is not None
            else Path(__file__).resolve().parents[2]
        )
        self.ledger_path = (
            Path(ledger_path) if ledger_path is not None
            else self.repo_root / "docs" / "stability" / "remediation_tracker.json"
        )
        self.runner = command_runner or _default_runner(self.repo_root)
        self.production_identity = (
            production_identity or get_production_identity_service()
        )
        self.pr_reader = pr_reader or self._read_pr_with_gh
        self.branch_pr_reader = branch_pr_reader or self._find_pr_with_gh
        self.ttl_seconds = float(ttl_seconds)
        self._cache = None
        self._lock = threading.Lock()

    def _git(self, *parts, timeout=5.0):
        return self.runner(
            ["git", "-C", str(self.repo_root), *parts], timeout=timeout
        )

    def _resolve_ref(self, ref):
        if not ref:
            return None
        for candidate in (str(ref), f"origin/{ref}"):
            result = self._git(
                "rev-parse", "--verify", f"{candidate}^{{commit}}"
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        return None

    def _resolve_remote_branch(self, branch):
        if not branch:
            return None
        result = self.runner(
            [
                "git", "-C", str(self.repo_root), "ls-remote", "origin",
                f"refs/heads/{branch}",
            ],
            timeout=5.0,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.split()[0]
        return self._resolve_ref(branch)

    def _is_ancestor(self, ancestor, descendant):
        if not ancestor or not descendant:
            return False
        return self._git(
            "merge-base", "--is-ancestor", ancestor, descendant
        ).returncode == 0


    def _read_pr_with_gh(self, number):
        result = self.runner(
            [
                "gh", "pr", "view", str(int(number)), "--repo",
                DEFAULT_REPOSITORY, "--json",
                "number,state,url,mergedAt,mergeCommit,headRefName,baseRefName,headRefOid",
            ],
            timeout=4.0,
        )
        if result.returncode != 0:
            return {
                "number": int(number), "state": "UNKNOWN", "url": None,
                "merged_at": None, "merge_commit_sha": None,
            }
        try:
            data = json.loads(result.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {
                "number": int(number), "state": "UNKNOWN", "url": None,
                "merged_at": None, "merge_commit_sha": None,
            }
        merge_commit = data.get("mergeCommit") or {}
        return {
            "number": int(data.get("number") or number),
            "state": str(data.get("state") or "UNKNOWN").upper(),
            "url": data.get("url"),
            "merged_at": data.get("mergedAt"),
            "merge_commit_sha": merge_commit.get("oid"),
            "head_ref": data.get("headRefName"),
            "head_sha": data.get("headRefOid"),
            "base_ref": data.get("baseRefName"),
        }

    def _find_pr_with_gh(self, branch):
        if not branch:
            return None
        result = self.runner(
            [
                "gh", "pr", "list", "--repo", DEFAULT_REPOSITORY,
                "--head", str(branch), "--state", "all", "--limit", "1",
                "--json",
                "number,state,url,mergedAt,mergeCommit,headRefName,baseRefName,headRefOid",
            ],
            timeout=4.0,
        )
        if result.returncode != 0:
            return {
                "number": None, "state": "UNKNOWN", "url": None,
                "lookup_failed": True,
            }
        try:
            rows = json.loads(result.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {
                "number": None, "state": "UNKNOWN", "url": None,
                "lookup_failed": True,
            }
        if not isinstance(rows, list):
            return {
                "number": None, "state": "UNKNOWN", "url": None,
                "lookup_failed": True,
            }
        if not rows:
            return None
        data = rows[0]
        merge_commit = data.get("mergeCommit") or {}
        return {
            "number": int(data["number"]),
            "state": str(data.get("state") or "UNKNOWN").upper(),
            "url": data.get("url"),
            "merged_at": data.get("mergedAt"),
            "merge_commit_sha": merge_commit.get("oid"),
            "head_ref": data.get("headRefName"),
            "head_sha": data.get("headRefOid"),
            "base_ref": data.get("baseRefName"),
        }

    def _load_ledger(self):
        data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError("remediation tracker items must be a list")
        ids = [str(item.get("id") or "") for item in items]
        if len(ids) != len(set(ids)) or any(not value for value in ids):
            raise ValueError("remediation tracker ids must be unique and non-empty")
        return data


    @staticmethod
    def _development_state(item, branch_sha, pr):
        state = str((pr or {}).get("state") or "").upper()
        if (pr or {}).get("merged_at") or state == "MERGED":
            return "MERGED"
        if state == "OPEN":
            return "PR_OPEN"
        if state == "DRAFT":
            return "PR_DRAFT"
        if pr and state == "UNKNOWN" and (
            pr.get("number") or pr.get("lookup_failed")
        ):
            return "PR_UNKNOWN"
        if branch_sha:
            return "BRANCH_ONLY"
        if (item.get("codex") or {}).get("title"):
            return "ASSIGNED"
        return "NOT_STARTED"

    def _production_state(self, anchor_sha, prod_sha, runtime_sha, merged):
        if not anchor_sha:
            return "NOT_STARTED"
        if runtime_sha and self._is_ancestor(anchor_sha, runtime_sha):
            return "DEPLOYED"
        in_prod = bool(prod_sha and self._is_ancestor(anchor_sha, prod_sha))
        if merged or in_prod:
            return "MERGED_NOT_DEPLOYED"
        return "NOT_MERGED"

    def _build_snapshot(self):
        ledger = self._load_ledger()
        prod_branch = str(ledger.get("production_branch") or DEFAULT_PROD_BRANCH)
        prod_sha = self._resolve_remote_branch(prod_branch)
        identity = self.production_identity.snapshot()
        runtime_sha = ((identity.get("startup") or {}).get("git_sha"))
        remote_block = identity.get("remote") or {}
        remote_prod_sha = remote_block.get("git_sha")
        if remote_block.get("status") == "OK" and remote_prod_sha:
            prod_sha = remote_prod_sha

        projected = []
        discovered_prs = {}
        resolved_branches = {}
        for raw in ledger["items"]:
            item = dict(raw)
            branch = item.get("branch")
            if branch and branch not in resolved_branches:
                resolved_branches[branch] = self._resolve_remote_branch(branch)
            branch_sha = resolved_branches.get(branch) if branch else None
            if item.get("pr"):
                pr = self.pr_reader(item["pr"])
            elif branch:
                if branch not in discovered_prs:
                    discovered_prs[branch] = self.branch_pr_reader(branch)
                pr = discovered_prs[branch]
            else:
                pr = None
            if not branch_sha and pr and pr.get("head_sha"):
                branch_sha = pr["head_sha"]
            merged = bool(
                pr and (
                    pr.get("merged_at")
                    or str(pr.get("state") or "").upper() == "MERGED"
                )
            )
            anchor_sha = (
                ((pr or {}).get("merge_commit_sha") if merged else None)
                or branch_sha
            )

            production_state = self._production_state(
                anchor_sha, prod_sha, runtime_sha, merged
            )
            development_state = (
                "MERGED"
                if production_state in {"MERGED_NOT_DEPLOYED", "DEPLOYED"}
                else self._development_state(item, branch_sha, pr)
            )
            item["branch_status"] = {
                "name": branch, "exists": bool(branch_sha), "head_sha": branch_sha,
            }
            item["pr"] = pr or (
                {"number": item.get("pr"), "state": "NONE", "url": None}
                if item.get("pr") else None
            )
            item["development"] = {"state": development_state}
            item["production"] = {
                "state": production_state,
                "production_branch": prod_branch,
                "production_branch_sha": prod_sha,
                "runtime_sha": runtime_sha,
            }
            projected.append(item)

        summary = {
            "total": len(projected),
            "p0": sum(item.get("severity") == "P0" for item in projected),
            "in_development": sum(
                item["development"]["state"] in {
                    "ASSIGNED", "BRANCH_ONLY", "PR_OPEN",
                    "PR_DRAFT", "PR_UNKNOWN"
                }
                for item in projected
            ),
            "pending_review": sum(
                str((item.get("review") or {}).get("status") or "").upper()
                in {"PENDING", "PARTIAL"}
                and item["development"]["state"] != "NOT_STARTED"
                for item in projected
            ),
            "merged_not_deployed": sum(
                item["production"]["state"] == "MERGED_NOT_DEPLOYED"
                for item in projected
            ),
            "production_verified": sum(
                str((item.get("production_verification") or {}).get("status") or "").upper()
                == "VERIFIED"
                for item in projected
            ),
        }
        return {
            "generated_at": _now_iso(), "summary": summary, "items": projected,
            "production_identity": {
                "runtime_sha": runtime_sha, "production_branch_sha": prod_sha,
            },
        }


    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cache[0] <= self.ttl_seconds:
                return self._cache[1]
            value = self._build_snapshot()
            self._cache = (now, value)
            return value


_default_service = None
_default_lock = threading.Lock()


def get_remediation_tracker_service():
    global _default_service
    if _default_service is None:
        with _default_lock:
            if _default_service is None:
                _default_service = RemediationTrackerService()
    return _default_service

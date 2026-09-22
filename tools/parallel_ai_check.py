"""Read-only worksite check for parallel CNNC development tasks."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import sys


DEFAULT_PROD_BRANCH = "prod/closeout-20260818"


class CheckError(RuntimeError):
    """Raised when a required local Git fact cannot be read."""


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    head: str
    branch: str
    annotations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    title: str
    url: str
    branch: str
    files: tuple[str, ...]
    from_fork: bool = False
    head: str | None = None


@dataclass(frozen=True)
class OverlapInfo:
    source: str
    path: str
    reason: str


@dataclass(frozen=True)
class WorktreeStatusInfo:
    worktree: WorktreeInfo
    paths: tuple[str, ...] = ()
    available: bool = True


def _run(command: list[str], *, cwd: Path, timeout: float) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
            timeout=timeout,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"command unavailable: {command[0]}") from exc
    if result.returncode != 0:
        raise CheckError(f"command failed ({result.returncode}): {Path(command[0]).name}")
    return result.stdout


def _git(repo: Path, *args: str, timeout: float) -> str:
    return _run(["git", "--no-optional-locks", "-C", str(repo), *args], cwd=repo, timeout=timeout)


def _current_prod_sha(repo: Path, prod_branch: str, timeout: float) -> str:
    output = _git(
        repo,
        "ls-remote",
        "--heads",
        "origin",
        f"refs/heads/{prod_branch}",
        timeout=timeout,
    )
    fields = output.strip().split()
    if len(fields) != 2 or fields[1] != f"refs/heads/{prod_branch}" or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]):
        raise CheckError(f"origin branch not found: {prod_branch}")
    return fields[0]


def _cached_prod_sha(repo: Path, prod_branch: str, timeout: float) -> str | None:
    try:
        return _git(
            repo,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{prod_branch}^{{commit}}",
            timeout=timeout,
        ).strip()
    except CheckError:
        return None


def _parse_worktrees(output: str) -> list[WorktreeInfo]:
    worktrees = []
    for block in output.split("\0\0"):
        if not block:
            continue
        fields = {}; annotations = []
        for record in block.split("\0"):
            key, _, value = record.partition(" ")
            if key in {"worktree", "HEAD", "branch"}:
                fields[key] = value
            elif key:
                annotations.append(record)
        if "bare" in annotations:
            continue
        if "worktree" not in fields or "HEAD" not in fields:
            raise CheckError("WORKTREE_LIST_INCOMPLETE")
        branch = fields.get("branch", "").removeprefix("refs/heads/") or "DETACHED"
        worktrees.append(WorktreeInfo(Path(fields["worktree"]).resolve(), fields["HEAD"], branch, tuple(annotations)))
    return worktrees


def _github_repo_slug(repo: Path, timeout: float) -> str:
    remote = _git(repo, "remote", "get-url", "origin", timeout=timeout).strip()
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", remote)
    if match is None:
        raise CheckError("origin is not a GitHub repository")
    return f"{match.group(1)}/{match.group(2)}"


def _open_pull_requests(repo: Path, timeout: float) -> tuple[PullRequestInfo, ...]:
    gh = shutil.which("gh")
    if gh is None:
        raise CheckError("gh is not available")
    slug = _github_repo_slug(repo, timeout)
    output = _run([gh, "pr", "list", "--repo", slug, "--state", "open", "--limit", "100",
                   "--json", "number,title,url,headRefName,headRefOid,files,changedFiles,isCrossRepository"], cwd=repo, timeout=timeout)
    try:
        payload = json.loads(output)
        if not isinstance(payload, list) or len(payload) >= 100:
            raise CheckError("GITHUB_PR_LIST_INCOMPLETE")
        results = []
        for item in payload:
            files = item.get("files")
            count = item.get("changedFiles")
            if not isinstance(files, list) or type(count) is not int or len(files) != count:
                raise CheckError("GITHUB_PR_FILES_INCOMPLETE")
            paths = tuple(file["path"] for file in files)
            if not all(isinstance(path, str) and path for path in paths):
                raise CheckError("GITHUB_PR_FILES_INVALID")
            head = item.get("headRefOid")
            head = head.lower() if isinstance(head, str) and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head) else None
            results.append(PullRequestInfo(int(item["number"]), str(item["title"]), str(item["url"]),
                           str(item["headRefName"]), paths, item.get("isCrossRepository") is not False, head=head))
        return tuple(results)
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckError("gh returned an invalid pull request payload") from exc


def _normalized_repo_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return PurePosixPath(normalized).as_posix().rstrip("/")


def _status_paths(repo: Path, timeout: float) -> tuple[str, ...]:
    output = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", timeout=timeout)
    if output and not output.endswith("\0"):
        raise CheckError("STATUS_OUTPUT_INCOMPLETE")
    records = output.split("\0"); paths = set(); index = 0
    while index < len(records):
        record = records[index]; index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise CheckError("STATUS_OUTPUT_INVALID")
        paths.add(_normalized_repo_path(record[3:]))
        if any(code in record[:2] for code in ("R", "C")):
            if index >= len(records) or not records[index]:
                raise CheckError("STATUS_RENAME_SOURCE_MISSING")
            paths.add(_normalized_repo_path(records[index])); index += 1
    return tuple(sorted(paths))


def _committed_paths(repo: Path, base: str, head: str, timeout: float) -> tuple[str, ...]:
    output = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                  "--name-only", "-z", f"{base}...{head}", "--", timeout=timeout)
    if output and not output.endswith("\0"):
        raise CheckError("DIFF_OUTPUT_INCOMPLETE")
    return tuple(sorted({_normalized_repo_path(path) for path in output.split("\0") if path}))


def _worktree_statuses(
    worktrees: list[WorktreeInfo], timeout: float
) -> tuple[WorktreeStatusInfo, ...]:
    def inspect(worktree: WorktreeInfo) -> WorktreeStatusInfo:
        try:
            if not worktree.path.is_dir():
                return WorktreeStatusInfo(worktree=worktree, available=False)
            return WorktreeStatusInfo(
                worktree=worktree,
                paths=_status_paths(worktree.path, timeout),
            )
        except (CheckError, OSError):
            return WorktreeStatusInfo(worktree=worktree, available=False)

    if not worktrees:
        return ()
    with ThreadPoolExecutor(max_workers=min(8, len(worktrees))) as executor:
        return tuple(executor.map(inspect, worktrees))


def _scope_matches(scope: str, changed_path: str) -> bool:
    if os.name == "nt":
        scope, changed_path = scope.casefold(), changed_path.casefold()
    return changed_path == scope or changed_path.startswith(f"{scope}/")


def _find_overlaps(
    repo: Path,
    current_prod_sha: str,
    fallback_base_sha: str,
    current_branch: str,
    task_paths: tuple[str, ...],
    worktree_statuses: tuple[WorktreeStatusInfo, ...],
    pull_requests: tuple[PullRequestInfo, ...],
    timeout: float,
    comparisons_unavailable: list[str] | None = None,
    *,
    current_head: str | None = None,
) -> tuple[OverlapInfo, ...]:
    overlaps: set[tuple[str, str, str]] = set()
    for status_info in worktree_statuses:
        worktree = status_info.worktree
        committed_source = f"worktree {worktree.branch} ({worktree.path})"
        uncommitted_source = f"dirty worktree {worktree.branch} ({worktree.path})"
        try:
            committed = _committed_paths(repo, current_prod_sha, worktree.head, timeout)
        except CheckError:
            try:
                committed = _committed_paths(repo, fallback_base_sha, worktree.head, timeout)
            except CheckError:
                committed = ()
                if comparisons_unavailable is not None:
                    comparisons_unavailable.append(worktree.branch)
        for changed_path in committed:
            if any(_scope_matches(scope, changed_path) for scope in task_paths):
                overlaps.add(("FILE_OVERLAP", committed_source, changed_path))
        for changed_path in status_info.paths:
            if any(_scope_matches(scope, changed_path) for scope in task_paths):
                overlaps.add(
                    ("FILE_OVERLAP_UNCOMMITTED", uncommitted_source, changed_path)
                )

    for pull_request in pull_requests:
        if pull_request.branch == current_branch and not pull_request.from_fork:
            pr_head = getattr(pull_request, "head", None)
            if current_head and pr_head == current_head:
                continue
            if (not current_head or not pr_head) and comparisons_unavailable is not None:
                comparisons_unavailable.append(f"PR #{pull_request.number} same-branch HEAD")
        for changed_path in pull_request.files:
            normalized = _normalized_repo_path(changed_path)
            if any(_scope_matches(scope, normalized) for scope in task_paths):
                overlaps.add(
                    ("FILE_OVERLAP", f"PR #{pull_request.number} {pull_request.branch}", normalized)
                )
    return tuple(
        OverlapInfo(reason=reason, source=source, path=path)
        for reason, source, path in sorted(overlaps)
    )


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only BASE_SHA, prod, branch, and worktree check."
    )
    parser.add_argument("--base-sha", required=True, help="Prod SHA recorded at task start")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--prod-branch", default=DEFAULT_PROD_BRANCH)
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        help="Planned repository-relative file or directory (repeatable)",
    )
    parser.add_argument("--no-github", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    _configure_console()

    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("STATUS: STOP_AND_REVIEW\nREASON: INVALID_TIMEOUT"); return 2
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", args.base_sha):
        print("STATUS: STOP_AND_REVIEW\nREASON: INVALID_BASE_SHA"); return 2
    for value in args.scope:
        path = _normalized_repo_path(value)
        if not path.strip() or "\0" in path or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive or PureWindowsPath(path).root or ".." in PurePosixPath(path).parts or path == ".":
            print("STATUS: STOP_AND_REVIEW\nREASON: INVALID_SCOPE"); return 2
    lexical_root = args.repo_root.absolute()
    repo = args.repo_root.resolve()
    try:
        repo = Path(_git(repo, "rev-parse", "--show-toplevel", timeout=args.timeout).strip()).resolve()
        base_sha = _git(repo, "rev-parse", f"{args.base_sha}^{{commit}}", timeout=args.timeout).strip()
        head = _git(repo, "rev-parse", "HEAD", timeout=args.timeout).strip()
        try:
            _git(repo, "merge-base", "--is-ancestor", base_sha, head, timeout=args.timeout)
            base_in_head = True
        except CheckError:
            base_in_head = False
        branch = _git(repo, "branch", "--show-current", timeout=args.timeout).strip() or "DETACHED"
        current_dirty_paths = _status_paths(repo, args.timeout)
        dirty = bool(current_dirty_paths)
        worktrees = _parse_worktrees(
            _git(repo, "worktree", "list", "--porcelain", "-z", timeout=args.timeout)
        )
    except CheckError as exc:
        print("STATUS: STOP_AND_REVIEW")
        print(f"ERROR: {exc}")
        print("READ_ONLY: no changes were made")
        return 2

    cached_prod_sha = _cached_prod_sha(repo, args.prod_branch, args.timeout)
    try:
        current_prod_sha: str | None = _current_prod_sha(
            repo, args.prod_branch, args.timeout
        )
        remote_prod_status = "AVAILABLE"
    except CheckError:
        current_prod_sha = None
        remote_prod_status = "UNAVAILABLE"

    if args.no_github:
        github_state = "SKIPPED"
        pull_requests: tuple[PullRequestInfo, ...] = ()
    else:
        try:
            pull_requests = _open_pull_requests(repo, args.timeout)
            github_state = "AVAILABLE"
        except CheckError:
            pull_requests = ()
            github_state = "UNAVAILABLE"

    other_worktrees = [item for item in worktrees if item.path != repo]
    worktree_statuses = _worktree_statuses(other_worktrees, args.timeout)
    unavailable_worktrees = tuple(
        item.worktree for item in worktree_statuses if not item.available
    )
    declared_scope = tuple(
        normalized
        for value in args.scope
        if (normalized := _normalized_repo_path(value))
    )
    comparisons_unavailable: list[str] = []
    comparison_prod_sha = current_prod_sha or cached_prod_sha or base_sha
    try:
        committed_task_paths = _committed_paths(
            repo, comparison_prod_sha, head, args.timeout
        )
    except CheckError:
        try:
            committed_task_paths = _committed_paths(repo, base_sha, head, args.timeout)
        except CheckError:
            committed_task_paths = ()
            comparisons_unavailable.append("CURRENT_WORKTREE")
    observed_task_paths = tuple(
        sorted(set(committed_task_paths) | set(current_dirty_paths))
    )
    task_paths = tuple(sorted(set(declared_scope) | set(observed_task_paths)))
    overlaps = _find_overlaps(
        repo,
        comparison_prod_sha,
        base_sha,
        branch,
        task_paths,
        worktree_statuses,
        pull_requests,
        args.timeout,
        comparisons_unavailable,
        current_head=head,
    )

    prod_same = current_prod_sha is not None and base_sha == current_prod_sha
    reasons: list[str] = []
    if not base_in_head:
        reasons.append("BASE_NOT_IN_CURRENT_HEAD")
    if branch == args.prod_branch or any("cnnc-prod-runtime" in {part.casefold() for part in root.parts} for root in (repo, lexical_root)):
        reasons.append("PRODUCTION_WORKTREE")
    if comparisons_unavailable:
        reasons.append("COMPARISON_UNAVAILABLE")
    if current_prod_sha is None:
        reasons.append("REMOTE_PROD_UNAVAILABLE")
    elif not prod_same:
        reasons.append("PRODUCTION_MOVED")
    if dirty:
        reasons.append("CURRENT_WORKTREE_DIRTY")
    if not declared_scope:
        reasons.append("SCOPE_NOT_DECLARED")
    if unavailable_worktrees:
        reasons.append("WORKTREE_STATUS_UNAVAILABLE")
    if github_state == "UNAVAILABLE":
        reasons.append("GITHUB_UNAVAILABLE")
    if any(item.reason == "FILE_OVERLAP" for item in overlaps):
        reasons.append("FILE_OVERLAP")
    if any(item.reason == "FILE_OVERLAP_UNCOMMITTED" for item in overlaps):
        reasons.append("FILE_OVERLAP_UNCOMMITTED")
    status = (
        "STOP_AND_REVIEW"
        if any(
            reason in reasons
            for reason in (
                "REMOTE_PROD_UNAVAILABLE",
                "BASE_NOT_IN_CURRENT_HEAD",
                "PRODUCTION_WORKTREE",
                "PRODUCTION_MOVED",
                "FILE_OVERLAP",
                "FILE_OVERLAP_UNCOMMITTED",
            )
        )
        else "WARNING"
        if reasons
        else "SAFE_TO_START"
    )
    print("PARALLEL_AI_CHECK")
    print(f"STATUS: {status}")
    print(f"BASE_SHA: {base_sha}")
    print(f"REMOTE_PROD_STATUS: {remote_prod_status}")
    print(f"CURRENT_PROD_SHA: {current_prod_sha or 'UNAVAILABLE'}")
    print(f"CACHED_PROD_SHA: {cached_prod_sha or 'UNAVAILABLE'}")
    print(
        "PROD_STATUS: "
        + ("UNKNOWN" if current_prod_sha is None else "SAME" if prod_same else "PRODUCTION_MOVED")
    )
    print("CURRENT_WORKTREE:")
    print(f"  PATH: {repo}")
    print(f"  BRANCH: {branch}")
    print(f"  HEAD: {head}")
    print(f"  CLEAN: {'NO' if dirty else 'YES'}")
    for reason in reasons:
        print(f"REASON: {reason}")
    print("DECLARED_SCOPE:")
    for scope in declared_scope or ["(not declared)"]:
        print(f"  {scope}")
    print("OBSERVED_TASK_PATHS:")
    for path in observed_task_paths or ["(none)"]:
        print(f"  {path}")
    print(f"OTHER_WORKTREES: {len(other_worktrees)}")
    for status_info in worktree_statuses:
        item = status_info.worktree
        print(f"  PATH: {item.path}")
        print(f"  BRANCH: {item.branch}")
        print(f"  HEAD: {item.head}")
        if not status_info.available:
            print(f"  WORKTREE_STATUS_UNAVAILABLE: {item.path}")
        for annotation in item.annotations:
            print(f"  NOTE: {annotation}")
    print(f"FILE_OVERLAPS: {len(overlaps)}")
    for overlap in overlaps:
        print(f"  {overlap.path} <- {overlap.source} [{overlap.reason}]")
    for unavailable in sorted(set(comparisons_unavailable)):
        print(f"COMPARISON_UNAVAILABLE: {unavailable}")
    print("GUARANTEE: snapshot of observable facts, not a lock or future safety guarantee")
    print(f"GITHUB: {github_state}")
    if github_state == "AVAILABLE":
        print(f"OPEN_PULL_REQUESTS: {len(pull_requests)}")
        for pull_request in pull_requests:
            print(
                f"  #{pull_request.number} {pull_request.branch} "
                f"({len(pull_request.files)} files) {pull_request.url}"
            )
    if current_prod_sha is not None and not prod_same:
        try:
            new_commits = _git(
                repo,
                "log",
                "--oneline",
                "--no-decorate",
                f"{base_sha}..{current_prod_sha}",
                timeout=args.timeout,
            ).strip()
        except CheckError:
            new_commits = "unavailable locally; run git fetch origin and rerun"
        print("NEW_PROD_COMMITS:")
        for line in new_commits.splitlines() or ["(none)"]:
            print(f"  {line}")
        print("ACTION: read new prod commits, assess overlap, and re-verify before integration")
    print("READ_ONLY: no changes were made")
    return 0 if status == "SAFE_TO_START" else 1 if status == "WARNING" else 2


if __name__ == "__main__":
    raise SystemExit(main())

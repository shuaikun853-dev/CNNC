"""Read-only worksite check for parallel CNNC development tasks."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
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


@dataclass(frozen=True)
class OverlapInfo:
    source: str
    path: str


def _run(command: list[str], *, cwd: Path, timeout: float) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"command unavailable: {command[0]}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise CheckError(f"command failed ({result.returncode}): {detail}")
    return result.stdout


def _git(repo: Path, *args: str, timeout: float) -> str:
    return _run(["git", "-C", str(repo), *args], cwd=repo, timeout=timeout)


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
    if len(fields) < 2:
        raise CheckError(f"origin branch not found: {prod_branch}")
    return fields[0]


def _parse_worktrees(output: str) -> list[WorktreeInfo]:
    worktrees: list[WorktreeInfo] = []
    for block in output.strip().split("\n\n"):
        fields: dict[str, str] = {}
        annotations: list[str] = []
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key in {"worktree", "HEAD", "branch"}:
                fields[key] = value
            elif key:
                annotations.append(line)
        if "worktree" not in fields or "HEAD" not in fields:
            continue
        branch_ref = fields.get("branch", "")
        branch = branch_ref.removeprefix("refs/heads/") if branch_ref else "DETACHED"
        worktrees.append(
            WorktreeInfo(
                path=Path(fields["worktree"]).resolve(),
                head=fields["HEAD"],
                branch=branch,
                annotations=tuple(annotations),
            )
        )
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
    output = _run(
        [
            gh,
            "pr",
            "list",
            "--repo",
            slug,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,url,headRefName,files",
        ],
        cwd=repo,
        timeout=timeout,
    )
    try:
        payload = json.loads(output)
        return tuple(
            PullRequestInfo(
                number=int(item["number"]),
                title=str(item["title"]),
                url=str(item["url"]),
                branch=str(item["headRefName"]),
                files=tuple(str(file_item["path"]) for file_item in item.get("files", [])),
            )
            for item in payload
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CheckError("gh returned an invalid pull request payload") from exc


def _normalized_repo_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def _status_paths(repo: Path, timeout: float) -> tuple[str, ...]:
    output = _git(repo, "status", "--porcelain", "--untracked-files=all", timeout=timeout)
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        normalized = _normalized_repo_path(value.strip('"'))
        if normalized:
            paths.append(normalized)
    return tuple(sorted(set(paths)))


def _committed_paths(repo: Path, base: str, head: str, timeout: float) -> tuple[str, ...]:
    output = _git(
        repo,
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
        f"{base}...{head}",
        timeout=timeout,
    )
    return tuple(
        sorted(
            {
                normalized
                for line in output.splitlines()
                if (normalized := _normalized_repo_path(line))
            }
        )
    )


def _scope_matches(scope: str, changed_path: str) -> bool:
    return changed_path == scope or changed_path.startswith(f"{scope}/")


def _find_overlaps(
    repo: Path,
    current_prod_sha: str,
    fallback_base_sha: str,
    current_branch: str,
    task_paths: tuple[str, ...],
    other_worktrees: list[WorktreeInfo],
    pull_requests: tuple[PullRequestInfo, ...],
    timeout: float,
) -> tuple[OverlapInfo, ...]:
    overlaps: set[tuple[str, str]] = set()
    for worktree in other_worktrees:
        try:
            committed = _committed_paths(repo, current_prod_sha, worktree.head, timeout)
        except CheckError:
            try:
                committed = _committed_paths(repo, fallback_base_sha, worktree.head, timeout)
            except CheckError:
                committed = ()
        for changed_path in committed:
            if any(_scope_matches(scope, changed_path) for scope in task_paths):
                overlaps.add((f"worktree {worktree.branch} ({worktree.path})", changed_path))

    for pull_request in pull_requests:
        if pull_request.branch == current_branch:
            continue
        for changed_path in pull_request.files:
            normalized = _normalized_repo_path(changed_path)
            if any(_scope_matches(scope, normalized) for scope in task_paths):
                overlaps.add((f"PR #{pull_request.number} {pull_request.branch}", normalized))
    return tuple(OverlapInfo(source=source, path=path) for source, path in sorted(overlaps))


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

    repo = args.repo_root.resolve()
    try:
        repo = Path(_git(repo, "rev-parse", "--show-toplevel", timeout=args.timeout).strip()).resolve()
        base_sha = _git(repo, "rev-parse", f"{args.base_sha}^{{commit}}", timeout=args.timeout).strip()
        current_prod_sha = _current_prod_sha(repo, args.prod_branch, args.timeout)
        head = _git(repo, "rev-parse", "HEAD", timeout=args.timeout).strip()
        branch = _git(repo, "branch", "--show-current", timeout=args.timeout).strip() or "DETACHED"
        current_dirty_paths = _status_paths(repo, args.timeout)
        dirty = bool(current_dirty_paths)
        worktrees = _parse_worktrees(
            _git(repo, "worktree", "list", "--porcelain", timeout=args.timeout)
        )
    except CheckError as exc:
        print("STATUS: STOP_AND_REVIEW")
        print(f"ERROR: {exc}")
        print("READ_ONLY: no changes were made")
        return 2

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
    declared_scope = tuple(
        normalized
        for value in args.scope
        if (normalized := _normalized_repo_path(value))
    )
    if declared_scope:
        task_paths = tuple(sorted(set(declared_scope)))
    else:
        try:
            committed_task_paths = _committed_paths(repo, current_prod_sha, head, args.timeout)
        except CheckError:
            committed_task_paths = _committed_paths(repo, base_sha, head, args.timeout)
        task_paths = tuple(sorted(set(committed_task_paths) | set(current_dirty_paths)))
    overlaps = _find_overlaps(
        repo,
        current_prod_sha,
        base_sha,
        branch,
        task_paths,
        other_worktrees,
        pull_requests,
        args.timeout,
    )

    prod_same = base_sha == current_prod_sha
    reasons: list[str] = []
    if not prod_same:
        reasons.append("PRODUCTION_MOVED")
    if dirty:
        reasons.append("CURRENT_WORKTREE_DIRTY")
    if not task_paths:
        reasons.append("SCOPE_NOT_DECLARED")
    if github_state == "UNAVAILABLE":
        reasons.append("GITHUB_UNAVAILABLE")
    if overlaps:
        reasons.append("FILE_OVERLAP")
    status = (
        "STOP_AND_REVIEW"
        if "PRODUCTION_MOVED" in reasons or "FILE_OVERLAP" in reasons
        else "WARNING"
        if reasons
        else "SAFE_TO_START"
    )
    print("PARALLEL_AI_CHECK")
    print(f"STATUS: {status}")
    print(f"BASE_SHA: {base_sha}")
    print(f"CURRENT_PROD_SHA: {current_prod_sha}")
    print(f"PROD_STATUS: {'SAME' if prod_same else 'PRODUCTION_MOVED'}")
    print("CURRENT_WORKTREE:")
    print(f"  PATH: {repo}")
    print(f"  BRANCH: {branch}")
    print(f"  HEAD: {head}")
    print(f"  CLEAN: {'NO' if dirty else 'YES'}")
    for reason in reasons:
        print(f"REASON: {reason}")
    print("TASK_SCOPE:")
    for scope in task_paths or ["(not declared)"]:
        print(f"  {scope}")
    print(f"OTHER_WORKTREES: {len(other_worktrees)}")
    for item in other_worktrees:
        print(f"  PATH: {item.path}")
        print(f"  BRANCH: {item.branch}")
        print(f"  HEAD: {item.head}")
        for annotation in item.annotations:
            print(f"  NOTE: {annotation}")
    print(f"FILE_OVERLAPS: {len(overlaps)}")
    for overlap in overlaps:
        print(f"  {overlap.path} <- {overlap.source}")
    print(f"GITHUB: {github_state}")
    if github_state == "AVAILABLE":
        print(f"OPEN_PULL_REQUESTS: {len(pull_requests)}")
        for pull_request in pull_requests:
            print(
                f"  #{pull_request.number} {pull_request.branch} "
                f"({len(pull_request.files)} files) {pull_request.url}"
            )
    if not prod_same:
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

"""Behavior tests for the read-only parallel AI worksite checker."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL = PROJECT_ROOT / "tools" / "parallel_ai_check.py"
PROD_BRANCH = "prod/closeout-20260818"


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _make_repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _run_git(repo, "init", "-b", PROD_BRANCH)
    _run_git(repo, "config", "user.name", "Parallel AI Test")
    _run_git(repo, "config", "user.email", "parallel-ai@example.invalid")
    (repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _run_git(repo, "add", "tracked.txt")
    _run_git(repo, "commit", "-m", "baseline")
    base_sha = _run_git(repo, "rev-parse", "HEAD")

    remote.mkdir()
    _run_git(remote, "init", "--bare")
    _run_git(repo, "remote", "add", "origin", str(remote))
    _run_git(repo, "push", "-u", "origin", PROD_BRANCH)
    _run_git(repo, "switch", "-c", "chore/test-task")
    return repo, base_sha


def _run_tool(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--repo-root",
            str(repo),
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def _advance_prod(repo: Path, tmp_path: Path) -> str:
    prod_worktree = tmp_path / "prod-update"
    _run_git(repo, "worktree", "add", str(prod_worktree), PROD_BRANCH)
    _run_git(prod_worktree, "config", "user.name", "Parallel AI Test")
    _run_git(prod_worktree, "config", "user.email", "parallel-ai@example.invalid")
    (prod_worktree / "prod-change.txt").write_text("new prod\n", encoding="utf-8")
    _run_git(prod_worktree, "add", "prod-change.txt")
    _run_git(prod_worktree, "commit", "-m", "advance prod")
    _run_git(prod_worktree, "push", "origin", PROD_BRANCH)
    return _run_git(prod_worktree, "rev-parse", "HEAD")


def test_clean_current_base_reports_safe_to_start(tmp_path: Path) -> None:
    repo, base_sha = _make_repository(tmp_path)

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "docs/development/new-guide.md",
        "--no-github",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "STATUS: SAFE_TO_START" in result.stdout
    assert "PROD_STATUS: SAME" in result.stdout
    assert "CLEAN: YES" in result.stdout


def test_prod_move_stops_and_names_new_commits(tmp_path: Path) -> None:
    repo, base_sha = _make_repository(tmp_path)
    current_prod_sha = _advance_prod(repo, tmp_path)

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "docs/development/new-guide.md",
        "--no-github",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "STATUS: STOP_AND_REVIEW" in result.stdout
    assert "PROD_STATUS: PRODUCTION_MOVED" in result.stdout
    assert f"CURRENT_PROD_SHA: {current_prod_sha}" in result.stdout
    assert "NEW_PROD_COMMITS:" in result.stdout
    assert "advance prod" in result.stdout


def test_dirty_current_worktree_reports_warning_reason(tmp_path: Path) -> None:
    repo, base_sha = _make_repository(tmp_path)
    (repo / "local-change.txt").write_text("dirty\n", encoding="utf-8")

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "docs/development/new-guide.md",
        "--no-github",
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "STATUS: WARNING" in result.stdout
    assert "REASON: CURRENT_WORKTREE_DIRTY" in result.stdout
    assert "CLEAN: NO" in result.stdout


def test_multiple_worktrees_are_listed_without_modification(tmp_path: Path) -> None:
    repo, base_sha = _make_repository(tmp_path)
    other = tmp_path / "other-worktree"
    _run_git(repo, "worktree", "add", "-b", "fix/other-task", str(other), base_sha)
    before = _run_git(repo, "worktree", "list", "--porcelain")

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "docs/development/new-guide.md",
        "--no-github",
    )

    after = _run_git(repo, "worktree", "list", "--porcelain")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OTHER_WORKTREES: 1" in result.stdout
    assert str(other.resolve()) in result.stdout
    assert "BRANCH: fix/other-task" in result.stdout
    assert after == before


def test_github_unavailable_keeps_local_git_results(tmp_path: Path, monkeypatch) -> None:
    repo, base_sha = _make_repository(tmp_path)
    git_path = shutil.which("git")
    assert git_path is not None
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(Path(git_path).parent), str(Path(sys.executable).parent))),
    )

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "docs/development/new-guide.md",
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "STATUS: WARNING" in result.stdout
    assert "GITHUB: UNAVAILABLE" in result.stdout
    assert "PROD_STATUS: SAME" in result.stdout
    assert "CURRENT_WORKTREE:" in result.stdout


def test_scope_overlap_with_other_worktree_stops_for_review(tmp_path: Path) -> None:
    repo, base_sha = _make_repository(tmp_path)
    other = tmp_path / "prewarm-worktree"
    _run_git(repo, "worktree", "add", "-b", "fix/prewarm-bug", str(other), base_sha)
    target = other / "app" / "prewarm.py"
    target.parent.mkdir(parents=True)
    target.write_text("# competing change\n", encoding="utf-8")
    _run_git(other, "add", "app/prewarm.py")
    _run_git(other, "commit", "-m", "change prewarm")

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "app/prewarm.py",
        "--no-github",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "STATUS: STOP_AND_REVIEW" in result.stdout
    assert "REASON: FILE_OVERLAP" in result.stdout
    assert "app/prewarm.py" in result.stdout
    assert "fix/prewarm-bug" in result.stdout


def test_uncommitted_scope_overlap_with_other_worktree_stops_for_review(
    tmp_path: Path,
) -> None:
    repo, base_sha = _make_repository(tmp_path)
    other = tmp_path / "dirty-overlap-worktree"
    _run_git(repo, "worktree", "add", "-b", "fix/dirty-overlap", str(other), base_sha)
    target = other / "app" / "prewarm.py"
    target.parent.mkdir(parents=True)
    target.write_text("# uncommitted competing change\n", encoding="utf-8")

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "app/prewarm.py",
        "--no-github",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "STATUS: STOP_AND_REVIEW" in result.stdout
    assert "REASON: FILE_OVERLAP_UNCOMMITTED" in result.stdout
    assert "app/prewarm.py" in result.stdout
    assert "fix/dirty-overlap" in result.stdout


def test_uncommitted_nonoverlap_in_other_worktree_does_not_block(
    tmp_path: Path,
) -> None:
    repo, base_sha = _make_repository(tmp_path)
    other = tmp_path / "dirty-nonoverlap-worktree"
    _run_git(repo, "worktree", "add", "-b", "fix/unrelated", str(other), base_sha)
    (other / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "app/prewarm.py",
        "--no-github",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "STATUS: SAFE_TO_START" in result.stdout
    assert "FILE_OVERLAP_UNCOMMITTED" not in result.stdout


def test_inaccessible_worktree_records_status_unavailable_and_continues(
    tmp_path: Path,
) -> None:
    repo, base_sha = _make_repository(tmp_path)
    other = tmp_path / "removed-worktree"
    _run_git(repo, "worktree", "add", "-b", "fix/removed", str(other), base_sha)
    shutil.rmtree(other)

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "app/prewarm.py",
        "--no-github",
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "STATUS: WARNING" in result.stdout
    assert "REASON: WORKTREE_STATUS_UNAVAILABLE" in result.stdout
    assert "WORKTREE_STATUS_UNAVAILABLE:" in result.stdout
    assert str(other.resolve()) in result.stdout


def test_missing_declared_scope_warns_even_when_task_paths_are_observed(
    tmp_path: Path,
) -> None:
    repo, base_sha = _make_repository(tmp_path)
    (repo / "observed.txt").write_text("observed\n", encoding="utf-8")
    _run_git(repo, "add", "observed.txt")
    _run_git(repo, "commit", "-m", "observed task change")

    result = _run_tool(repo, "--base-sha", base_sha, "--no-github")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "STATUS: WARNING" in result.stdout
    assert "REASON: SCOPE_NOT_DECLARED" in result.stdout
    assert "DECLARED_SCOPE:\n  (not declared)" in result.stdout
    assert "OBSERVED_TASK_PATHS:\n  observed.txt" in result.stdout


def test_remote_prod_unavailable_keeps_local_and_cached_facts(tmp_path: Path) -> None:
    repo, base_sha = _make_repository(tmp_path)
    missing_remote = tmp_path / "missing-remote.git"
    _run_git(repo, "remote", "set-url", "origin", str(missing_remote))

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "app/prewarm.py",
        "--no-github",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "STATUS: STOP_AND_REVIEW" in result.stdout
    assert "REMOTE_PROD_STATUS: UNAVAILABLE" in result.stdout
    assert f"CACHED_PROD_SHA: {base_sha}" in result.stdout
    assert "REASON: REMOTE_PROD_UNAVAILABLE" in result.stdout
    assert "CURRENT_WORKTREE:" in result.stdout
    assert f"PATH: {repo.resolve()}" in result.stdout
    assert "BRANCH: chore/test-task" in result.stdout
    assert "CLEAN: YES" in result.stdout
    assert "OTHER_WORKTREES: 0" in result.stdout
    assert "STATUS: SAFE_TO_START" not in result.stdout


def test_chinese_worktree_path_is_reported_without_replacement_characters(
    tmp_path: Path,
) -> None:
    chinese_root = tmp_path / "中文现场"
    chinese_root.mkdir()
    repo, base_sha = _make_repository(chinese_root)
    other = chinese_root / "并行工作区"
    _run_git(repo, "worktree", "add", "-b", "fix/chinese-path", str(other), base_sha)

    result = _run_tool(
        repo,
        "--base-sha",
        base_sha,
        "--scope",
        "docs/development/new-guide.md",
        "--no-github",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert str(repo.resolve()) in result.stdout
    assert str(other.resolve()) in result.stdout
    assert "�" not in result.stdout

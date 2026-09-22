"""Real miniature Git repositories reproduce guard false negatives without production I/O."""
from pathlib import Path

import pytest

from tests.test_parallel_ai_check import _make_repository, _run_git, _run_tool, _advance_prod


def other_worktree(repo: Path, tmp_path: Path, base: str) -> Path:
    other = tmp_path / 'other'
    _run_git(repo, 'worktree', 'add', '-b', 'fix/other', str(other), base)
    return other


def test_chinese_uncommitted_filename_overlap_cannot_be_lost_to_git_quoting(tmp_path):
    repo, base = _make_repository(tmp_path)
    _run_git(repo, 'config', 'core.quotepath', 'true')
    other = other_worktree(repo, tmp_path, base)
    path = other / 'app' / '商品 规则.py'
    path.parent.mkdir()
    path.write_text('# inert competing change\n', encoding='utf-8')
    result = _run_tool(repo, '--base-sha', base, '--scope', 'app/商品 规则.py', '--no-github')
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'FILE_OVERLAP_UNCOMMITTED' in result.stdout
    assert '商品 规则.py' in result.stdout


def test_committed_deletion_is_a_conflict(tmp_path):
    repo, base = _make_repository(tmp_path)
    other = other_worktree(repo, tmp_path, base)
    _run_git(other, 'rm', 'tracked.txt')
    _run_git(other, 'commit', '-m', 'delete tracked file')
    result = _run_tool(repo, '--base-sha', base, '--scope', 'tracked.txt', '--no-github')
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'FILE_OVERLAP' in result.stdout


def test_committed_rename_conflicts_with_the_old_path_too(tmp_path):
    repo, base = _make_repository(tmp_path)
    other = other_worktree(repo, tmp_path, base)
    _run_git(other, 'mv', 'tracked.txt', 'renamed.txt')
    _run_git(other, 'commit', '-m', 'rename tracked file')
    result = _run_tool(repo, '--base-sha', base, '--scope', 'tracked.txt', '--no-github')
    assert result.returncode == 2, result.stdout + result.stderr


def test_passing_latest_sha_cannot_hide_an_outdated_head(tmp_path):
    repo, _ = _make_repository(tmp_path)
    newest = _advance_prod(repo, tmp_path)
    result = _run_tool(repo, '--base-sha', newest, '--scope', 'docs/new.md', '--no-github')
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'BASE_NOT_IN_CURRENT_HEAD' in result.stdout


@pytest.mark.parametrize('scope', ['../outside.py', '/absolute/file.py', 'C:\\outside.py', 'app/../../outside.py', '', '   '])
def test_invalid_scope_never_reports_safe(tmp_path, scope):
    repo, base = _make_repository(tmp_path)
    result = _run_tool(repo, '--base-sha', base, '--scope', scope, '--no-github')
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'INVALID_SCOPE' in result.stdout + result.stderr


def test_checkout_of_production_branch_is_not_a_development_worktree(tmp_path):
    repo, base = _make_repository(tmp_path)
    _run_git(repo, 'switch', 'prod/closeout-20260818')
    result = _run_tool(repo, '--base-sha', base, '--scope', 'app/new.py', '--no-github')
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'PRODUCTION_WORKTREE' in result.stdout


def test_unrelated_history_is_reported_as_unknown_not_zero_changes(tmp_path):
    repo, base = _make_repository(tmp_path)
    other = other_worktree(repo, tmp_path, base)
    _run_git(other, 'switch', '--orphan', 'unrelated-history')
    (other / 'other.txt').write_text('unrelated', encoding='utf-8')
    _run_git(other, 'add', 'other.txt')
    _run_git(other, 'commit', '-m', 'unrelated root')
    result = _run_tool(repo, '--base-sha', base, '--scope', 'app/new.py', '--no-github')
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'COMPARISON_UNAVAILABLE' in result.stdout


def test_nul_status_parser_preserves_rename_and_special_filenames(monkeypatch, tmp_path):
    from tools import parallel_ai_check as guard
    raw = 'R  new name.py\x00old -> name.py\x00?? 中文\tline\nname.py\x00'
    monkeypatch.setattr(guard, '_git', lambda *args, **kwargs: raw)
    assert guard._status_paths(tmp_path, 1) == tuple(sorted({
        'new name.py', 'old -> name.py', '中文\tline\nname.py'}))


def test_git_failures_do_not_print_remote_credentials(monkeypatch, tmp_path):
    import subprocess
    from tools import parallel_ai_check as guard
    def failed(*args, **kwargs):
        assert kwargs['env']['GIT_OPTIONAL_LOCKS'] == '0'
        return subprocess.CompletedProcess(args[0], 128, '', 'https://user:secret-value@invalid.example/repo')
    monkeypatch.setattr(guard.subprocess, 'run', failed)
    with pytest.raises(guard.CheckError) as error:
        guard._run(['git', 'status'], cwd=tmp_path, timeout=1)
    assert 'secret-value' not in str(error.value)


def test_truncated_pull_request_file_list_is_not_complete(monkeypatch, tmp_path):
    import json
    from tools import parallel_ai_check as guard
    monkeypatch.setattr(guard.shutil, 'which', lambda _: 'gh')
    monkeypatch.setattr(guard, '_github_repo_slug', lambda *args: 'owner/repo')
    payload = [{'number': 1, 'title': 'test', 'url': 'https://github.com/owner/repo/pull/1',
                'headRefName': 'feature', 'changedFiles': 2,
                'files': [{'path': 'only-one.py'}], 'isCrossRepository': False}]
    monkeypatch.setattr(guard, '_run', lambda *args, **kwargs: json.dumps(payload))
    with pytest.raises(guard.CheckError):
        guard._open_pull_requests(tmp_path, 1)


@pytest.mark.parametrize('timeout', ['0', '-1', 'nan', 'inf'])
def test_invalid_timeout_is_rejected(tmp_path, timeout):
    repo, base = _make_repository(tmp_path)
    result = _run_tool(repo, '--base-sha', base, '--scope', 'app/new.py', '--timeout', timeout, '--no-github')
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'INVALID_TIMEOUT' in result.stdout + result.stderr

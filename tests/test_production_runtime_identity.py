"""Git agreement must not be presented as complete executable identity."""
import json

from app.services import production_identity as identity


def runner(argv, timeout=None):
    command = ' '.join(str(part) for part in argv)
    if 'rev-parse HEAD' in command:
        return identity.CommandResult(0, 'a' * 40 + '\n', '')
    if 'branch --show-current' in command:
        return identity.CommandResult(0, 'test/candidate\n', '')
    if 'status --porcelain' in command:
        return identity.CommandResult(0, '', '')
    if 'ls-remote' in command:
        return identity.CommandResult(0, 'a' * 40 + '\trefs/heads/prod/closeout-20260818\n', '')
    return identity.CommandResult(0, 'null', '')


def test_unconfigured_runtime_is_not_reported_as_full_match(tmp_path):
    service = identity.ProductionIdentityService(repo_root=tmp_path, command_runner=runner)
    snapshot = service.snapshot()
    assert snapshot['comparison']['version_status'] == 'MATCH'
    assert snapshot['comparison']['version_scope'] == 'repository_git_only'
    assert snapshot['runtime_identity']['status'] == 'UNCONFIGURED'


def test_runtime_mismatch_remains_visible_when_git_matches(tmp_path, monkeypatch):
    manifest = tmp_path / 'data' / 'production_manifest.json'
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({'schema_version': 1, 'approved': True}), encoding='utf-8')
    monkeypatch.setattr(identity, 'verify_runtime_manifest', lambda *args, **kwargs:
        {'status': 'MISMATCH', 'issues': ['EXTERNAL_FILE_CHANGED:zhaoshang_priority']})
    snapshot = identity.ProductionIdentityService(repo_root=tmp_path, command_runner=runner).snapshot()
    assert snapshot['comparison']['version_status'] == 'MATCH'
    assert snapshot['runtime_identity']['status'] == 'MISMATCH'


def test_bad_manifest_degrades_only_runtime_identity(tmp_path):
    manifest = tmp_path / 'data' / 'production_manifest.json'
    manifest.parent.mkdir()
    manifest.write_text('{broken', encoding='utf-8')
    snapshot = identity.ProductionIdentityService(repo_root=tmp_path, command_runner=runner).snapshot()
    assert snapshot['comparison']['version_status'] == 'MATCH'
    assert snapshot['runtime_identity']['status'] == 'UNKNOWN'
    assert snapshot['runtime_identity']['issues'] == ['RUNTIME_MANIFEST_READ_FAILED']


def test_runtime_verification_error_does_not_expose_exception_contents(tmp_path, monkeypatch):
    manifest = tmp_path / 'data' / 'production_manifest.json'
    manifest.parent.mkdir()
    manifest.write_text('{}', encoding='utf-8')

    def fail(*args, **kwargs):
        raise RuntimeError('sensitive-fixture-value-must-not-escape')

    monkeypatch.setattr(identity, 'verify_runtime_manifest', fail)
    snapshot = identity.ProductionIdentityService(repo_root=tmp_path, command_runner=runner).snapshot()
    assert snapshot['runtime_identity']['status'] == 'UNKNOWN'
    assert 'sensitive-fixture-value' not in repr(snapshot)


def test_scheduler_probe_provides_definition_hash_without_mutating_tasks():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'scripts/query_scheduled_task_json.ps1').read_text(encoding='utf-8-sig')
    assert 'Export-ScheduledTask' in source
    assert 'definition_sha256' in source
    assert 'SHA256' in source
    assert 'Register-ScheduledTask' not in source
    assert 'Unregister-ScheduledTask' not in source


def test_runtime_verification_receives_scheduler_hashes(tmp_path, monkeypatch):
    manifest = tmp_path / 'data' / 'production_manifest.json'
    manifest.parent.mkdir()
    manifest.write_text('{}', encoding='utf-8')
    captured = {}

    def verify(_root, _manifest, **kwargs):
        captured.update(kwargs['scheduler_definitions'])
        return {'status': 'UNKNOWN', 'issues': []}

    def scheduled_runner(argv, timeout=None):
        if 'query_scheduled_task_json' in ' '.join(str(p) for p in argv):
            return identity.CommandResult(0, json.dumps({'task_name': argv[argv.index('-TaskName') + 1], 'state': 'Ready', 'action': 'python.exe', 'last_task_result': 0, 'definition_sha256': 'c' * 64}), '')
        return runner(argv, timeout=timeout)

    monkeypatch.setattr(identity, 'verify_runtime_manifest', verify)
    identity.ProductionIdentityService(repo_root=tmp_path, command_runner=scheduled_runner).snapshot()
    assert captured['CNNC-PriceAudit-Incremental'] == 'c' * 64

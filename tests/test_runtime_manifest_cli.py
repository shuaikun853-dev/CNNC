"""Operator manifest capture/verify: no automatic approval or task writes."""
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools/runtime_manifest.py'


def load_tool():
    assert TOOL.is_file(), 'Runtime manifest CLI is not yet implemented'
    spec = importlib.util.spec_from_file_location('runtime_manifest_cli_test', TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_capture_is_unapproved_and_preserves_existing_output(tmp_path, monkeypatch):
    cli = load_tool()
    monkeypatch.setattr(cli, 'propose_manifest', lambda *a, **k: {'schema_version': 1, 'approved': False})
    monkeypatch.setattr(cli, '_scheduler_definitions', lambda *a: {})
    output = tmp_path / 'candidate-manifest.json'
    args = ['capture', '--repo', str(tmp_path), '--output', str(output),
            '--external-file', 'zhaoshang_create=E:/fixture/create.py',
            '--external-file', 'zhaoshang_priority=E:/fixture/priority.py',
            '--external-repository', 'mall_crawler=E:/fixture/crawler']
    assert cli.main(args) == 0
    assert json.loads(output.read_text(encoding='utf-8'))['approved'] is False
    original = output.read_bytes()
    assert cli.main(args) == 2
    assert output.read_bytes() == original


def test_verify_returns_failure_when_identity_not_proven(tmp_path, monkeypatch):
    cli = load_tool()
    manifest = tmp_path / 'manifest.json'
    manifest.write_text('{"approved":false}', encoding='utf-8')
    monkeypatch.setattr(cli, '_scheduler_definitions', lambda *a: {})
    monkeypatch.setattr(cli, 'verify_manifest', lambda *a, **k: {'status': 'UNAPPROVED', 'issues': []})
    assert cli.main(['verify', '--repo', str(tmp_path), '--manifest', str(manifest)]) == 2


def test_invalid_external_label_is_rejected_without_writing(tmp_path):
    cli = load_tool()
    output = tmp_path / 'manifest.json'
    assert cli.main(['capture', '--repo', str(tmp_path), '--output', str(output),
                     '--external-file', '../../outside=E:/fixture/file.py']) == 2
    assert not output.exists()


def test_failed_capture_does_not_print_source_or_secret(tmp_path, monkeypatch, capsys):
    cli = load_tool()

    def fail(*args, **kwargs):
        raise RuntimeError('credential-fixture-value-must-not-leak')

    monkeypatch.setattr(cli, 'propose_manifest', fail)
    monkeypatch.setattr(cli, '_scheduler_definitions', lambda *a: {})
    assert cli.main(['capture', '--repo', str(tmp_path), '--output', str(tmp_path / 'new.json'),
                     '--external-file', 'zhaoshang_create=E:/fixture/create.py',
                     '--external-file', 'zhaoshang_priority=E:/fixture/priority.py',
                     '--external-repository', 'mall_crawler=E:/fixture/crawler']) == 2
    output = capsys.readouterr()
    assert 'credential-fixture-value' not in output.out + output.err

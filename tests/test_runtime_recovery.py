"""Offline contract tests: backups must never overwrite live business state."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools' / 'runtime_recovery.py'
DAY = '2026-09-22'
DATABASES = (f'{DAY}/state.sqlite3', 'customer_prewarm.sqlite3', 'shared_crawl_cache.sqlite3')


def load_tool():
    assert TOOL.is_file(), 'Safe runtime backup/restore tool has not been implemented'
    spec = importlib.util.spec_from_file_location('cnnc_test_runtime_recovery', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_sources(tmp_path):
    root = tmp_path / 'source'
    for name in DATABASES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)')
            connection.execute('INSERT INTO evidence VALUES (1, ?)', (name,))
    return root


def test_backup_contains_committed_wal_rows_without_changing_source(tmp_path):
    tool = load_tool()
    source = make_sources(tmp_path)
    live = sqlite3.connect(source / DATABASES[0])
    try:
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('INSERT INTO evidence VALUES (2, ?)', ('committed WAL row',))
        live.commit()
        destination = tmp_path / 'backup'
        tool.create_backup(source, DAY, destination)
        manifest = tool.verify_backup(destination)
        assert manifest['consistency'] == 'per_database_snapshot_not_cross_database_atomic'
        assert len(manifest['databases']) == 3
        with closing(sqlite3.connect(destination / DATABASES[0])) as restored:
            assert restored.execute('SELECT value FROM evidence WHERE id=2').fetchone() == ('committed WAL row',)
        assert live.execute('SELECT count(*) FROM evidence').fetchone()[0] == 2
    finally:
        live.close()


def test_restore_verifies_backup_and_writes_only_new_directory(tmp_path):
    tool = load_tool()
    backup = tmp_path / 'backup'
    tool.create_backup(make_sources(tmp_path), DAY, backup)
    target = tmp_path / 'restored'
    tool.restore_backup(backup, target)
    for relative in DATABASES:
        with closing(sqlite3.connect(target / relative)) as connection:
            assert connection.execute('SELECT value FROM evidence').fetchone() == (relative,)
    assert tool.verify_backup(target)['business_date'] == DAY


@pytest.mark.parametrize('action', ['backup', 'restore'])
def test_existing_destination_is_never_overwritten(tmp_path, action):
    tool = load_tool()
    source = make_sources(tmp_path)
    backup = tmp_path / 'backup'
    tool.create_backup(source, DAY, backup)
    existing = tmp_path / 'existing'
    existing.mkdir()
    sentinel = existing / 'do-not-touch.txt'
    sentinel.write_text('keep', encoding='utf-8')
    with pytest.raises((FileExistsError, ValueError)):
        if action == 'backup':
            tool.create_backup(source, DAY, existing)
        else:
            tool.restore_backup(backup, existing)
    assert sentinel.read_text(encoding='utf-8') == 'keep'
    assert sorted(p.name for p in existing.iterdir()) == ['do-not-touch.txt']


def test_tampered_backup_is_rejected_before_restore(tmp_path):
    tool = load_tool()
    backup = tmp_path / 'backup'
    tool.create_backup(make_sources(tmp_path), DAY, backup)
    with (backup / DATABASES[0]).open('ab') as stream:
        stream.write(b'tampered')
    with pytest.raises(ValueError):
        tool.verify_backup(backup)
    target = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError):
        tool.restore_backup(backup, target)
    assert not target.exists()


def test_manifest_path_escape_is_rejected(tmp_path):
    tool = load_tool()
    backup = tmp_path / 'backup'
    tool.create_backup(make_sources(tmp_path), DAY, backup)
    manifest_path = backup / 'backup-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['databases'][0]['path'] = '../outside.sqlite3'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        tool.verify_backup(backup)


def test_restore_to_production_named_path_is_refused(tmp_path):
    tool = load_tool()
    backup = tmp_path / 'backup'
    tool.create_backup(make_sources(tmp_path), DAY, backup)
    target = tmp_path / 'cnnc-prod-runtime' / 'runtime' / 'new-day'
    with pytest.raises(ValueError):
        tool.restore_backup(backup, target)
    assert not target.exists()


def test_missing_required_database_does_not_publish_backup(tmp_path):
    tool = load_tool()
    source = make_sources(tmp_path)
    (source / DATABASES[2]).unlink()
    target = tmp_path / 'backup'
    with pytest.raises((FileNotFoundError, ValueError)):
        tool.create_backup(source, DAY, target)
    assert not (target / 'backup-manifest.json').exists()


@pytest.mark.parametrize('day', ['../../escape', '2026-02-30', '20260922'])
def test_business_date_is_validated_before_writing(tmp_path, day):
    tool = load_tool()
    target = tmp_path / 'backup'
    with pytest.raises(ValueError):
        tool.create_backup(make_sources(tmp_path), day, target)
    assert not target.exists()


def test_incomplete_backup_never_verifies(tmp_path):
    tool = load_tool()
    backup = tmp_path / 'incomplete'
    tool.create_backup(make_sources(tmp_path), DAY, backup)
    (backup / '.incomplete').write_text('unfinished', encoding='utf-8')
    with pytest.raises(ValueError):
        tool.verify_backup(backup)


def test_full_backup_restores_daily_evidence_but_not_locks_or_credentials(tmp_path):
    tool = load_tool()
    source = make_sources(tmp_path)
    day = source / DAY
    (day / 'BATCH_STATE.md').write_text('handoff evidence', encoding='utf-8')
    (day / 'daily_status.json').write_text('{"state":"OPEN"}', encoding='utf-8')
    (day / 'final.xlsx').write_bytes(b'workbook fixture bytes')
    (day / 'writer.lock').write_text('do not copy', encoding='utf-8')
    (day / 'auth.env').write_text('not part of runtime backups', encoding='utf-8')
    backup = tmp_path / 'full-backup'
    result = tool.create_backup(source, DAY, backup, include_artifacts=True)
    paths = {item['path'] for item in result['artifacts']}
    assert paths == {f'{DAY}/BATCH_STATE.md', f'{DAY}/daily_status.json', f'{DAY}/final.xlsx'}
    restored = tmp_path / 'restored'
    tool.restore_backup(backup, restored)
    assert (restored / DAY / 'BATCH_STATE.md').read_text(encoding='utf-8') == 'handoff evidence'
    assert not (restored / DAY / 'writer.lock').exists()
    assert not (restored / DAY / 'auth.env').exists()


def test_daily_evidence_checksum_is_verified_before_restore(tmp_path):
    tool = load_tool()
    source = make_sources(tmp_path)
    (source / DAY / 'BATCH_STATE.md').write_text('evidence', encoding='utf-8')
    backup = tmp_path / 'full-backup'
    tool.create_backup(source, DAY, backup, include_artifacts=True)
    (backup / DAY / 'BATCH_STATE.md').write_text('tampered', encoding='utf-8')
    restored = tmp_path / 'restored'
    with pytest.raises(ValueError):
        tool.restore_backup(backup, restored)
    assert not restored.exists()


@pytest.mark.parametrize('name', ['writer.lock', 'auth.env', 'auth_state.json'])
def test_manifest_cannot_smuggle_lock_or_auth_files(tmp_path, name):
    import hashlib
    tool = load_tool()
    backup = tmp_path / 'backup'
    tool.create_backup(make_sources(tmp_path), DAY, backup)
    member = backup / DAY / name
    member.write_bytes(b'not allowed')
    path = backup / 'backup-manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest['artifacts'] = [{'path': f'{DAY}/{name}', 'size': member.stat().st_size,
                              'sha256': hashlib.sha256(member.read_bytes()).hexdigest()}]
    path.write_text(json.dumps(manifest), encoding='utf-8')
    target = tmp_path / 'restored'
    with pytest.raises(ValueError):
        tool.restore_backup(backup, target)
    assert not target.exists()

"""Consistent per-database SQLite backups; verified restoration to NEW paths only.

This does not pause writers, promise cross-database atomicity, deploy code,
or undo an external task. A restored snapshot needs operational reconciliation.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import time
from typing import Any
import sys

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from tools.local_output_paths import new_local_output_path

MANIFEST = 'backup-manifest.json'
INCOMPLETE = '.incomplete'
CONSISTENCY = 'per_database_snapshot_not_cross_database_atomic'
KNOWN_ARTIFACT_NAMES = frozenset({'BATCH_STATE.md', 'daily_status.json', 'dashboard_actions.jsonl', 'manual_recrawl_actions.jsonl', 'final.xlsx', 'final.xlsx.manifest.json', 'review.xlsx', 'input.xlsx', 'result.xlsx', 'manifest.xlsx'})


def _day(value: str) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('business_date must be an ISO calendar date: YYYY-MM-DD')
    return value


def _database_names(business_date: str) -> tuple[str, ...]:
    return (f'{_day(business_date)}/state.sqlite3',
            'customer_prewarm.sqlite3', 'shared_crawl_cache.sqlite3')


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_destination(value: Path | str) -> Path:
    return new_local_output_path(value)


def _member(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or '\\' in relative or ':' in relative or '\x00' in relative:
        raise ValueError('Invalid snapshot member path')
    name = PurePosixPath(relative)
    if name.as_posix() != relative:
        raise ValueError('Backup members must use one canonical relative path')
    if any(part.endswith((' ', '.')) for part in name.parts):
        raise ValueError('Noncanonical Windows path components are forbidden')
    if name.is_absolute() or not name.parts or any(p in ('.', '..') for p in name.parts):
        raise ValueError('Snapshot paths must be relative and cannot escape their directory')
    target = root.joinpath(*name.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Snapshot member escapes its directory')
    current = root
    for part in name.parts:
        current = current / part
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('Snapshot members cannot be symbolic links or junctions')
    return target


def _check_sqlite(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            result = connection.execute('PRAGMA integrity_check').fetchall()
            foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
        if result != [('ok',)] or foreign_keys:
            raise ValueError(f'SQLite integrity verification failed: {path.name}')
    except sqlite3.Error as exc:
        raise ValueError(f'Invalid SQLite backup: {path.name}') from exc


def create_backup(source_root: Path | str, business_date: str,
                  destination: Path | str, *, timeout_seconds: float = 60.0,
                  include_artifacts: bool = False) -> dict[str, Any]:
    """Back up three committed SQLite states at separate consistent read points.

    timeout_seconds is a cooperative deadline checked between SQLite backup
    steps. A blocked native step can overrun it; no hard I/O or end-to-end
    deadline is promised. Hashing and file copies are not deadline-bounded. Ordinary local output paths only.
    Optional artifacts use registered business evidence names, never a broad
    extension allowlist. Their content still requires normal business redaction.
    """
    names = _database_names(business_date)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('Backup timeout must be finite and positive')
    source = Path(source_root).resolve(strict=True)
    target = _new_destination(destination)
    if target.is_relative_to(source):
        raise ValueError('Backup output must be outside the source runtime directory')
    for relative in names:
        if not _member(source, relative).is_file():
            raise FileNotFoundError(f'Required runtime database is missing: {relative}')
    target.mkdir(parents=True, exist_ok=False)
    (target / INCOMPLETE).write_text('Backup is not yet verified.\n', encoding='utf-8')
    records = []
    for relative in names:
        source_path = _member(source, relative)
        output_path = target / relative
        output_path.parent.mkdir(parents=True, exist_ok=True)
        started = _utc_now()
        deadline = time.monotonic() + timeout_seconds

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() >= deadline:
                raise TimeoutError(f'SQLite backup deadline exceeded: {relative}')
        uri = source_path.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=min(5.0, timeout_seconds))) as reader:
            with closing(sqlite3.connect(output_path)) as writer:
                reader.backup(writer, pages=128, progress=progress, sleep=0.025)
                writer.execute('PRAGMA journal_mode=DELETE').fetchone()
        _check_sqlite(output_path)
        records.append({'path': relative, 'size': output_path.stat().st_size,
                        'sha256': _hash(output_path), 'started_at': started,
                        'finished_at': _utc_now()})
    manifest = {'format': 'cnnc-runtime-backup', 'schema_version': 1,
                'business_date': business_date, 'created_at': _utc_now(),
                'consistency': CONSISTENCY, 'databases': records,
                'artifact_scope': 'daily_evidence' if include_artifacts else 'databases_only',
                'artifact_consistency': 'verified_individual_copies_not_cross_file_atomic',
                'artifact_policy': 'registered_business_evidence_names_only',
                'timeout_scope': 'cooperative_per_database_backup_steps_not_hard_io_deadline',
                'artifacts': _copy_daily_artifacts(source, business_date, target) if include_artifacts else []}
    _write_manifest(target, manifest)
    (target / INCOMPLETE).unlink()
    return verify_backup(target)


def _write_manifest(root: Path, manifest: dict[str, Any]) -> None:
    import os
    with (root / MANIFEST).open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def verify_backup(snapshot: Path | str) -> dict[str, Any]:
    """Check completeness, constrained paths, exact hashes and SQLite integrity."""
    root = Path(snapshot).resolve(strict=True)
    if (root / INCOMPLETE).exists():
        raise ValueError('Snapshot is incomplete and must not be restored')
    manifest_path = _member(root, MANIFEST)
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Snapshot manifest is missing or invalid') from exc
    if not isinstance(manifest, dict) or manifest.get('format') != 'cnnc-runtime-backup':
        raise ValueError('Unknown snapshot format')
    if type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        raise ValueError('Unsupported snapshot version')
    if manifest.get('consistency') != CONSISTENCY:
        raise ValueError('Snapshot must disclose its per-database consistency boundary')
    policies = {'artifact_policy': 'registered_business_evidence_names_only',
                'artifact_consistency': 'verified_individual_copies_not_cross_file_atomic',
                'timeout_scope': 'cooperative_per_database_backup_steps_not_hard_io_deadline'}
    if any(manifest.get(key) != value for key, value in policies.items()):
        raise ValueError('Unsupported or contradictory backup policy metadata')
    if manifest.get('artifact_scope') not in {'databases_only', 'daily_evidence'}:
        raise ValueError('Unsupported backup artifact scope')
    if manifest.get('artifact_scope') == 'databases_only' and manifest.get('artifacts'):
        raise ValueError('Database-only backup cannot declare extra artifacts')
    expected = set(_database_names(manifest.get('business_date')))
    records = manifest.get('databases')
    if not isinstance(records, list) or len(records) != len(expected):
        raise ValueError('Snapshot database set is incomplete')
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('path'), str):
            raise ValueError('Invalid database manifest entry')
        relative = record['path']
        if relative not in expected or relative in seen:
            raise ValueError('Unexpected or duplicate database member')
        seen.add(relative)
        path = _member(root, relative)
        expected_hash = record.get('sha256')
        if not isinstance(expected_hash, str) or not re.fullmatch('[0-9a-f]{64}', expected_hash):
            raise ValueError('Invalid database checksum')
        size = record.get('size')
        if type(size) is not int or size < 0 or not path.is_file():
            raise ValueError(f'Missing or invalid database: {relative}')
        if path.stat().st_size != size or _hash(path) != expected_hash:
            raise ValueError(f'Database checksum mismatch: {relative}')
        _check_sqlite(path)
    _verify_artifacts(root, manifest)
    return manifest


def restore_backup(snapshot: Path | str, destination: Path | str) -> dict[str, Any]:
    """Restore only into a new directory; never replace running or existing data."""
    source = Path(snapshot).resolve(strict=True)
    manifest = verify_backup(source)
    target = _new_destination(destination)
    if target.is_relative_to(source):
        raise ValueError('Restore output must be outside the backup directory')
    target.mkdir(parents=True, exist_ok=False)
    (target / INCOMPLETE).write_text('Restore is not yet verified.\n', encoding='utf-8')
    for record in manifest['databases'] + manifest.get('artifacts', []):
        source_path = _member(source, record['path'])
        output_path = target / record['path']
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open('rb') as reader, output_path.open('xb') as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            __import__('os').fsync(writer.fileno())
        if output_path.stat().st_size != record['size'] or _hash(output_path) != record['sha256']:
            raise ValueError('Backup changed while restoring; output remains incomplete')
        if record['path'] in {entry['path'] for entry in manifest['databases']}:
            _check_sqlite(output_path)
    _write_manifest(target, dict(manifest, restored_at=_utc_now()))
    (target / INCOMPLETE).unlink()
    return verify_backup(target)



def _known_daily_artifact(relative: str) -> bool:
    name = PurePosixPath(relative).name
    if name in KNOWN_ARTIFACT_NAMES:
        return True
    if re.fullmatch(r'BATCH_STATE_[A-Za-z0-9_-]+[.]md', name):
        return True
    return re.fullmatch(r'saas_audit_(?:final|review)_[0-9]{8}(?:_[A-Za-z0-9_-]+)?[.]xlsx(?:[.]manifest[.]json)?', name) is not None


def _copy_daily_artifacts(source: Path, day: str, target: Path) -> list[dict[str, Any]]:
    import os
    records = []
    seen_paths: set[str] = set()
    for source_path in sorted((source / day).rglob('*')):
        if not source_path.is_file() or not _known_daily_artifact(source_path.relative_to(source).as_posix()):
            continue
        relative = source_path.relative_to(source).as_posix()
        key = relative.casefold()
        if key in seen_paths:
            raise ValueError('Artifact paths collide under Windows case-insensitive semantics')
        seen_paths.add(key)
        source_path = _member(source, relative)
        output = _member(target, relative)
        output.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            before = _hash(source_path)
            with source_path.open('rb') as reader, output.open('wb') as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            copied = _hash(output)
            if before == copied == _hash(source_path):
                break
        else:
            raise ValueError(f'Artifact kept changing; retry during a quiet window: {relative}')
        records.append({'path': relative, 'size': output.stat().st_size, 'sha256': copied})
    return records


def _verify_artifacts(root: Path, manifest: dict[str, Any]) -> None:
    artifacts = manifest.get('artifacts', [])
    if not isinstance(artifacts, list):
        raise ValueError('Invalid artifact inventory')
    seen = {record['path'].casefold() for record in manifest['databases']}
    for record in artifacts:
        if not isinstance(record, dict) or not isinstance(record.get('path'), str):
            raise ValueError('Invalid artifact entry')
        relative = record['path']
        if relative.casefold() in seen or not relative.startswith(manifest['business_date'] + '/'):
            raise ValueError('Artifact path is duplicated or outside the selected business day')
        seen.add(relative.casefold())
        path = _member(root, relative)
        if not _known_daily_artifact(relative):
            raise ValueError('Snapshot cannot restore lock files, credentials or executable files')
        size = record.get('size')
        expected_hash = record.get('sha256')
        if type(size) is not int or size < 0 or not isinstance(expected_hash, str):
            raise ValueError('Invalid artifact checksum metadata')
        if not re.fullmatch('[0-9a-f]{64}', expected_hash) or not path.is_file():
            raise ValueError('Artifact is missing or checksum is invalid')
        if path.stat().st_size != size or _hash(path) != expected_hash:
            raise ValueError(f'Artifact checksum mismatch: {relative}')

def main(argv: list[str] | None = None) -> int:
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup', help='Back up the three runtime databases')
    backup.add_argument('--source-root', type=Path, required=True)
    backup.add_argument('--business-date', required=True)
    backup.add_argument('--destination', type=Path, required=True)
    backup.add_argument('--database-timeout', '--timeout', dest='timeout', type=float, default=60.0, help='Cooperative deadline between SQLite backup steps; not a hard I/O or total deadline')
    backup.add_argument('--include-artifacts', action='store_true', help='Include stable daily evidence; never auth files or locks')
    verify = commands.add_parser('verify', help='Verify checksums and SQLite integrity')
    verify.add_argument('--snapshot', type=Path, required=True)
    restore = commands.add_parser('restore', help='Restore to a NEW, non-production directory')
    restore.add_argument('--snapshot', type=Path, required=True)
    restore.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'backup':
            manifest = create_backup(args.source_root, args.business_date, args.destination,
                                     timeout_seconds=args.timeout, include_artifacts=args.include_artifacts)
        elif args.command == 'restore':
            manifest = restore_backup(args.snapshot, args.destination)
        else:
            manifest = verify_backup(args.snapshot)
    except (OSError, ValueError, sqlite3.Error, TimeoutError) as exc:
        print(f'RECOVERY_CHECK_FAILED: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'status': 'VERIFIED', 'operation': args.command,
                      'business_date': manifest['business_date'],
                      'database_count': len(manifest['databases']),
                      'artifact_count': len(manifest.get('artifacts', [])),
                      'artifact_scope': manifest.get('artifact_scope', 'databases_only'),
                      'consistency': CONSISTENCY,
                      'production_deployed': False}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

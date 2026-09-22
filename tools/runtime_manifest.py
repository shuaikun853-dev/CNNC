"""Capture an UNAPPROVED executable manifest or verify a reviewed one; no deployment."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.services.runtime_fingerprint import propose_manifest, verify_manifest
from tools.local_output_paths import new_local_output_path

DEFAULT_TASKS = ('CNNC-PriceAudit-Incremental', 'CNNC-Dashboard-ReadOnly')


def _pairs(values: list[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        label, separator, path = value.partition('=')
        if not separator or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', label) or not path:
            raise ValueError('Use label=absolute_path for each external dependency')
        if label in result or not Path(path).is_absolute():
            raise ValueError('External labels must be unique and paths absolute')
        result[label] = Path(path)
    return result


def _scheduler_definitions(repo: Path, tasks: list[str] | tuple[str, ...]) -> dict[str, str | None]:
    definitions = {}
    for name in tasks:
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_. -]{1,120}', name):
            raise ValueError('Invalid configured scheduler task name')
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                '-File', str(repo / 'scripts/query_scheduled_task_json.ps1'), '-TaskName', name],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20,
                check=True, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
            payload = json.loads(result.stdout)
            definitions[name] = payload.get('definition_sha256') if isinstance(payload, dict) else None
        except (OSError, ValueError, subprocess.SubprocessError):
            definitions[name] = None
    return definitions


def _save_new(path: Path, manifest: dict) -> None:
    destination = new_local_output_path(path)
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    capture = sub.add_parser('capture', help='Write a proposal; never auto-approve')
    capture.add_argument('--repo', type=Path, default=ROOT)
    capture.add_argument('--external-file', action='append', default=[])
    capture.add_argument('--external-repository', action='append', default=[])
    capture.add_argument('--scheduler-task', action='append')
    capture.add_argument('--output', type=Path, required=True)
    verify = sub.add_parser('verify', help='Read-only comparison with a reviewed manifest')
    verify.add_argument('--repo', type=Path, default=ROOT)
    verify.add_argument('--manifest', type=Path, required=True)
    verify.add_argument('--scheduler-task', action='append', help='Explicit configured task set; defaults to both production tasks')
    args = parser.parse_args(argv)
    try:
        if args.command == 'capture':
            files, repos = _pairs(args.external_file), _pairs(args.external_repository)
            if not {'zhaoshang_create', 'zhaoshang_priority'} <= files.keys() or 'mall_crawler' not in repos:
                raise ValueError('Create script, priority script and crawler repository are required')
            manifest = propose_manifest(args.repo, external_files=files,
                external_repositories=repos,
                scheduler_definitions=_scheduler_definitions(args.repo, args.scheduler_task or DEFAULT_TASKS))
            manifest['approved'] = False
            _save_new(args.output, manifest)
            print(json.dumps({'status': 'PROPOSED_NOT_APPROVED', 'production_deployed': False}))
            return 0
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        if not isinstance(manifest, dict):
            raise ValueError('Manifest must be a JSON object')
        tasks = args.scheduler_task or DEFAULT_TASKS
        result = verify_manifest(args.repo, manifest,
                                 scheduler_definitions=_scheduler_definitions(args.repo, tasks))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'MATCH' else 2
    except Exception as exc:
        # Paths, server responses and credentials may be in exception messages.
        print('RUNTIME_MANIFEST_FAILED:' + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

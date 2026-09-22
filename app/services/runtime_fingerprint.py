"""Read-only executable identity, separate from Git and business-state authority.

A captured observation is a proposal, not deployment approval. Verified output
contains labels and hashes, never external source text or credential values.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import os
from pathlib import Path, PurePosixPath
import re
import site
import subprocess
import sys
from typing import Any, Callable, Mapping

REQUIRED_FILES = frozenset({'zhaoshang_create', 'zhaoshang_priority'})
REQUIRED_REPOSITORIES = frozenset({'mall_crawler'})
_PIN = re.compile(r'([A-Za-z0-9._-]+)==([A-Za-z0-9.!+_-]+)')
_SHA = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
_HASH = re.compile(r'[0-9a-f]{64}')


def _hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _runtime_state() -> dict[str, Any]:
    return {'version': sys.version.split()[0], 'virtualenv': sys.prefix != sys.base_prefix,
            'user_site': site.ENABLE_USER_SITE is True,
            'pythonpath_configured': bool(os.environ.get('PYTHONPATH'))}


def _repository_state(path: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        result = subprocess.run(['git', '--no-optional-locks', '-C', str(path), *args],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=8, check=True, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
        return result.stdout
    sha = git('rev-parse', 'HEAD').strip()
    status = git('status', '--porcelain=v1', '-z', '--untracked-files=all')
    # Observable tracked bytecode is executable state too. Ignored generated
    # caches remain governed by Git ignore rules, not a blanket runtime exception.
    dirty = bool(status)
    return {'git_sha': sha, 'clean': not dirty}


def _pins(root: Path) -> dict[str, str]:
    pins = {}
    for line in (root / 'requirements.lock').read_text(encoding='utf-8').splitlines():
        text = line.strip()
        if not text or text.startswith('#'):
            continue
        match = _PIN.fullmatch(text)
        if match is None:
            raise ValueError('Dependency lock must contain exact distribution pins only')
        name, version = match.groups()
        normalized = re.sub(r'[-_.]+', '-', name).lower()
        if normalized in pins:
            raise ValueError('Duplicate normalized dependency name')
        pins[normalized] = version
    if not pins:
        raise ValueError('Dependency lock is empty')
    return pins




def _verified_package_version(name: str) -> str:
    """Verify distribution metadata and ownership of each resolved entrypoint.

    This is not a cryptographic package-file integrity or supply-chain proof.
    """
    distribution = importlib.metadata.distribution(name)
    records = tuple(distribution.files or ())
    owned_files = {Path(distribution.locate_file(file)).resolve() for file in records}
    names = set((distribution.read_text('top_level.txt') or '').split())
    if not names:
        for file in records:
            path = PurePosixPath(str(file))
            if not path.parts or path.parts[0].startswith('.'):
                continue
            if len(path.parts) == 1 and path.suffix == '.py':
                names.add(path.stem)
            elif len(path.parts) > 1 and path.parts[0].isidentifier() and path.suffix == '.py':
                names.add(path.parts[0])
    names = {value for value in names if value.isidentifier() and value != '__pycache__'}
    if not names or not owned_files:
        raise ValueError('Distribution has no verifiable owned import files')
    for module_name in sorted(names):
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            raise ValueError('Dependency import cannot be resolved')
        origin = spec.origin
        if origin and origin not in {'built-in', 'frozen'}:
            if Path(origin).resolve() not in owned_files:
                raise ValueError('Dependency entrypoint is not owned by its distribution')
        elif not spec.submodule_search_locations:
            raise ValueError('Dependency entrypoint has no verifiable file origin')
        for location in spec.submodule_search_locations or ():
            package_root = Path(location).resolve()
            if not any(file.is_relative_to(package_root) for file in owned_files):
                raise ValueError('Dependency namespace location is not owned by its distribution')
    return distribution.version


def propose_manifest(repo_root: Path | str, *, external_files: Mapping[str, Path],
                     external_repositories: Mapping[str, Path],
                     scheduler_definitions: Mapping[str, str],
                     runtime_reader: Callable = _runtime_state,
                     package_reader: Callable = _verified_package_version,
                     repository_reader: Callable = _repository_state) -> dict[str, Any]:
    """Capture facts for human review; this never authorizes a deployment."""
    root = Path(repo_root).resolve(strict=True)
    _pins(root)
    # Preserve the configured alias. Resolving it at capture time would hide
    # a later junction/symlink retarget while the actual executor follows it.
    files = {name: {'path': str(Path(path).absolute()), 'sha256': _hash(Path(path))}
             for name, path in external_files.items()}
    repos = {name: {'path': str(Path(path).absolute()),
                    'git_sha': repository_reader(Path(path))['git_sha']}
             for name, path in external_repositories.items()}
    return {'schema_version': 1, 'approved': False,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'source_git_sha': repository_reader(root)['git_sha'],
            'python': dict(runtime_reader()), 'dependency_lock_sha256': _hash(root / 'requirements.lock'),
            'external_files': files, 'external_repositories': repos,
            'scheduler_definitions': dict(scheduler_definitions)}


def verify_manifest(repo_root: Path | str, manifest: Mapping[str, Any] | None, *,
                    scheduler_definitions: Mapping[str, str | None],
                    runtime_reader: Callable = _runtime_state,
                    package_reader: Callable = _verified_package_version,
                    repository_reader: Callable = _repository_state) -> dict[str, Any]:
    """Verify the declared executable set, without reading credentials or taking actions."""
    result: dict[str, Any] = {'status': 'UNKNOWN', 'issues': [], 'external_files': {},
                             'scope': 'configured_runtime_not_remote_business_state'}
    if manifest is None:
        return dict(result, status='UNCONFIGURED')
    if not isinstance(manifest, Mapping) or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        return dict(result, issues=['INVALID_MANIFEST'])
    if manifest.get('approved') is not True:
        return dict(result, status='UNAPPROVED', issues=['MANIFEST_NOT_APPROVED'])
    mismatch = False

    def issue(code: str, *, unknown: bool = False) -> None:
        nonlocal mismatch
        result['issues'].append(code)
        mismatch = mismatch or not unknown

    root = Path(repo_root).resolve()
    try:
        state = repository_reader(root)
        expected_sha = manifest.get('source_git_sha')
        if not isinstance(expected_sha, str) or not _SHA.fullmatch(expected_sha):
            issue('SOURCE_IDENTITY_NOT_PINNED', unknown=True)
        elif state.get('git_sha') != expected_sha:
            issue('SOURCE_GIT_CHANGED')
        if state.get('clean') is not True:
            issue('SOURCE_WORKTREE_DIRTY' if state.get('clean') is False else 'SOURCE_WORKTREE_UNKNOWN',
                  unknown=state.get('clean') is not False)
    except Exception:
        issue('SOURCE_REPOSITORY_UNAVAILABLE', unknown=True)
    try:
        current_python = dict(runtime_reader())
        expected_python = manifest.get('python', {})
        if not isinstance(expected_python, Mapping) or not expected_python.get('version'):
            issue('PYTHON_NOT_PINNED', unknown=True)
        elif current_python.get('version') != expected_python['version']:
            issue('PYTHON_VERSION_CHANGED')
        if current_python.get('virtualenv') is not True or current_python.get('user_site') is not False or current_python.get('pythonpath_configured') is True:
            issue('PYTHON_NOT_ISOLATED')
        result['python'] = current_python
    except Exception:
        issue('PYTHON_IDENTITY_UNAVAILABLE', unknown=True)
    try:
        expected_hash = manifest.get('dependency_lock_sha256')
        if not isinstance(expected_hash, str) or not _HASH.fullmatch(expected_hash):
            issue('DEPENDENCY_LOCK_NOT_PINNED', unknown=True)
        elif _hash(root / 'requirements.lock') != expected_hash:
            issue('DEPENDENCY_LOCK_CHANGED')
        pins = _pins(root)
        result['dependency_count'] = len(pins)
        for name, expected_version in pins.items():
            try:
                actual = package_reader(name)
                if actual != expected_version:
                    issue('DEPENDENCY_VERSION_CHANGED:' + name)
            except Exception:
                issue('DEPENDENCY_UNAVAILABLE:' + name, unknown=True)
    except Exception:
        issue('DEPENDENCY_LOCK_UNAVAILABLE', unknown=True)
    files = manifest.get('external_files')
    if not isinstance(files, Mapping) or not REQUIRED_FILES <= files.keys():
        issue('REQUIRED_EXTERNAL_FILES_NOT_DECLARED', unknown=True)
        files = {} if not isinstance(files, Mapping) else files
    for name, record in files.items():
        if not isinstance(name, str) or not isinstance(record, Mapping):
            issue('INVALID_EXTERNAL_FILE_ENTRY', unknown=True)
            continue
        expected_hash = record.get('sha256')
        try:
            actual_hash = _hash(Path(record['path']))
            if not isinstance(expected_hash, str) or not _HASH.fullmatch(expected_hash):
                issue('EXTERNAL_FILE_NOT_PINNED:' + name, unknown=True)
                status = 'UNKNOWN'
            elif actual_hash != expected_hash:
                issue('EXTERNAL_FILE_CHANGED:' + name)
                status = 'MISMATCH'
            else:
                status = 'MATCH'
            result['external_files'][name] = {'status': status, 'sha256': actual_hash}
        except Exception:
            issue('EXTERNAL_FILE_UNAVAILABLE:' + name, unknown=True)
            result['external_files'][name] = {'status': 'UNKNOWN'}
    repositories = manifest.get('external_repositories')
    if not isinstance(repositories, Mapping) or not REQUIRED_REPOSITORIES <= repositories.keys():
        issue('REQUIRED_EXTERNAL_REPOSITORIES_NOT_DECLARED', unknown=True)
        repositories = {} if not isinstance(repositories, Mapping) else repositories
    result['external_repositories'] = {}
    for name, record in repositories.items():
        if not isinstance(name, str) or not isinstance(record, Mapping):
            issue('INVALID_EXTERNAL_REPOSITORY_ENTRY', unknown=True)
            continue
        try:
            state = repository_reader(Path(record['path']))
            expected_sha = record.get('git_sha')
            if not isinstance(expected_sha, str) or not _SHA.fullmatch(expected_sha):
                issue('EXTERNAL_REPOSITORY_NOT_PINNED:' + name, unknown=True)
            elif state.get('git_sha') != expected_sha:
                issue('EXTERNAL_REPOSITORY_CHANGED:' + name)
            if state.get('clean') is not True:
                issue('EXTERNAL_REPOSITORY_DIRTY:' + name,
                      unknown=state.get('clean') is not False)
            result['external_repositories'][name] = {'git_sha': state.get('git_sha'),
                                                     'clean': state.get('clean')}
        except Exception:
            issue('EXTERNAL_REPOSITORY_UNAVAILABLE:' + name, unknown=True)
            result['external_repositories'][name] = {'status': 'UNKNOWN'}
    definitions = manifest.get('scheduler_definitions')
    if not isinstance(definitions, Mapping) or not definitions:
        issue('SCHEDULER_DEFINITIONS_NOT_PINNED', unknown=True)
        definitions = {}
    if set(definitions) != set(scheduler_definitions):
        issue('SCHEDULER_DEFINITION_SCOPE_MISMATCH')
    for name, expected_hash in definitions.items():
        actual = scheduler_definitions.get(name)
        if not isinstance(expected_hash, str) or not _HASH.fullmatch(expected_hash):
            issue('SCHEDULER_DEFINITION_NOT_PINNED:' + str(name), unknown=True)
        elif not isinstance(actual, str) or not _HASH.fullmatch(actual):
            issue('SCHEDULER_DEFINITION_UNAVAILABLE:' + str(name), unknown=True)
        elif actual != expected_hash:
            issue('SCHEDULER_DEFINITION_CHANGED:' + str(name))
    result['status'] = 'MISMATCH' if mismatch else 'UNKNOWN' if result['issues'] else 'MATCH'
    return result

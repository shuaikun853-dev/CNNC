"""Runtime identity tests use fake scripts, fake repository readers and no network."""
import importlib
from pathlib import Path

import pytest

SHA = 'a' * 40
HASH = 'b' * 64


def api():
    return importlib.import_module('app.services.runtime_fingerprint')


def fixture_inputs(tmp_path):
    root = tmp_path / 'candidate'
    root.mkdir()
    (root / 'requirements.lock').write_text('demo-package==1.2.3\n', encoding='utf-8')
    files = {}
    for label in ('zhaoshang_create', 'zhaoshang_priority'):
        path = tmp_path / (label + '.py')
        path.write_text('# inert external tool fixture\n', encoding='utf-8')
        files[label] = path
    crawler = tmp_path / 'mall-crawler'
    crawler.mkdir()
    readers = {'runtime_reader': lambda: {'version': '3.14.5', 'virtualenv': True, 'user_site': False},
               'package_reader': lambda name: '1.2.3',
               'repository_reader': lambda path: {'git_sha': SHA, 'clean': True}}
    return root, files, {'mall_crawler': crawler}, {'CNNC-PriceAudit-Incremental': HASH}, readers


def proposal(tmp_path):
    root, files, repos, scheduler, readers = fixture_inputs(tmp_path)
    manifest = api().propose_manifest(root, external_files=files,
        external_repositories=repos, scheduler_definitions=scheduler, **readers)
    return root, files, repos, scheduler, readers, manifest


def verify(root, manifest, scheduler, readers):
    return api().verify_manifest(root, manifest, scheduler_definitions=scheduler, **readers)


def test_observation_is_not_automatic_production_approval(tmp_path):
    root, _, _, scheduler, readers, manifest = proposal(tmp_path)
    assert manifest['approved'] is False
    result = verify(root, manifest, scheduler, readers)
    assert result['status'] == 'UNAPPROVED'


def test_reviewed_full_identity_matches_unchanged_evidence(tmp_path):
    root, _, _, scheduler, readers, manifest = proposal(tmp_path)
    manifest['approved'] = True
    result = verify(root, manifest, scheduler, readers)
    assert result['status'] == 'MATCH'
    assert result['issues'] == []
    assert 'path' not in repr(result['external_files'])


def test_external_script_change_is_detected_even_when_cnnc_sha_is_same(tmp_path):
    root, files, _, scheduler, readers, manifest = proposal(tmp_path)
    manifest['approved'] = True
    files['zhaoshang_priority'].write_text('# changed after capture\n', encoding='utf-8')
    result = verify(root, manifest, scheduler, readers)
    assert result['status'] == 'MISMATCH'
    assert 'EXTERNAL_FILE_CHANGED:zhaoshang_priority' in result['issues']


def test_missing_external_script_is_unknown_not_match(tmp_path):
    root, files, _, scheduler, readers, manifest = proposal(tmp_path)
    manifest['approved'] = True
    files['zhaoshang_create'].unlink()
    result = verify(root, manifest, scheduler, readers)
    assert result['status'] != 'MATCH'
    assert 'EXTERNAL_FILE_UNAVAILABLE:zhaoshang_create' in result['issues']


def test_package_mismatch_is_detected(tmp_path):
    root, _, _, scheduler, readers, manifest = proposal(tmp_path)
    manifest['approved'] = True
    readers['package_reader'] = lambda name: '9.9.9'
    result = verify(root, manifest, scheduler, readers)
    assert result['status'] == 'MISMATCH'
    assert 'DEPENDENCY_VERSION_CHANGED:demo-package' in result['issues']


@pytest.mark.parametrize('runtime', [
    {'version': '3.14.5', 'virtualenv': False, 'user_site': True},
    {'version': '3.14.5', 'virtualenv': True, 'user_site': True},
    {'version': '3.14.6', 'virtualenv': True, 'user_site': False},
])
def test_unisolated_or_changed_python_cannot_match(tmp_path, runtime):
    root, _, _, scheduler, readers, manifest = proposal(tmp_path)
    manifest['approved'] = True
    readers['runtime_reader'] = lambda: runtime
    assert verify(root, manifest, scheduler, readers)['status'] == 'MISMATCH'


def test_changed_or_missing_scheduler_evidence_is_not_match(tmp_path):
    root, _, _, scheduler, readers, manifest = proposal(tmp_path)
    manifest['approved'] = True
    assert verify(root, manifest, {}, readers)['status'] != 'MATCH'
    changed = {name: 'c' * 64 for name in scheduler}
    result = verify(root, manifest, changed, readers)
    assert result['status'] == 'MISMATCH'


def test_missing_or_unapproved_manifest_is_never_assumed_safe(tmp_path):
    root, _, _, scheduler, readers = fixture_inputs(tmp_path)
    assert verify(root, None, scheduler, readers)['status'] == 'UNCONFIGURED'
    assert verify(root, {'schema_version': 99, 'approved': True}, scheduler, readers)['status'] != 'MATCH'


def test_changed_lock_is_detected_before_treating_new_pins_as_approved(tmp_path):
    root, _, _, scheduler, readers, manifest = proposal(tmp_path)
    manifest['approved'] = True
    (root / 'requirements.lock').write_text('demo-package==9.9.9\n', encoding='utf-8')
    readers['package_reader'] = lambda name: '9.9.9'
    result = verify(root, manifest, scheduler, readers)
    assert result['status'] == 'MISMATCH'
    assert 'DEPENDENCY_LOCK_CHANGED' in result['issues']


def test_dirty_repository_cannot_claim_complete_runtime_match(tmp_path):
    root, _, _, scheduler, readers, manifest = proposal(tmp_path)
    manifest['approved'] = True
    readers['repository_reader'] = lambda path: {'git_sha': SHA, 'clean': False}
    assert verify(root, manifest, scheduler, readers)['status'] == 'MISMATCH'


def test_missing_required_external_coverage_cannot_match(tmp_path):
    root, _, _, scheduler, readers, manifest = proposal(tmp_path)
    manifest['approved'] = True
    manifest['external_files'] = {}
    assert verify(root, manifest, scheduler, readers)['status'] != 'MATCH'

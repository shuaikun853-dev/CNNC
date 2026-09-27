import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from app.services import incremental_local_fallback as fallback
from scripts import cnnc_auth_browser as browser
from scripts.filesystem_safety import is_reparse_point


def test_browser_cleanup_works_without_python312_path_api(tmp_path, monkeypatch):
    monkeypatch.delattr(Path, 'is_junction', raising=False)
    monkeypatch.setattr(browser, 'BROWSER_TMP_ROOT', tmp_path)
    profile = browser._make_browser_profile_dir('cnnc_harvest_')
    metadata = json.loads((profile / browser.PROFILE_OWNER_FILE).read_text())
    monkeypatch.setattr(browser, '_process_inventory', lambda: [{
        'pid': metadata['pid'], 'created_at': metadata['owner_created_at'], 'cmdline': []}])
    assert browser._remove_browser_profile(profile)
    assert not profile.exists()


def test_fallback_cleanup_works_without_python312_path_api(tmp_path, monkeypatch):
    monkeypatch.delattr(Path, 'is_junction', raising=False)
    folder = tmp_path / 'local_fallback' / 'success'
    folder.mkdir(parents=True)
    (folder / 'manifest.xlsx').write_bytes(b'fixture')
    assert fallback._remove_known_artifacts(folder)
    assert not list(folder.iterdir())


def test_browser_marker_write_failure_removes_only_new_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, 'BROWSER_TMP_ROOT', tmp_path)
    preserved = tmp_path / 'operator-evidence.txt'
    preserved.write_text('keep')
    original = Path.write_text
    def fail_marker(path, *args, **kwargs):
        if path.name == browser.PROFILE_OWNER_FILE:
            raise OSError('fixture marker write failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', fail_marker)
    with pytest.raises(OSError, match='fixture marker write failure'):
        browser._make_browser_profile_dir('cnnc_harvest_')
    assert list(tmp_path.iterdir()) == [preserved]


@pytest.mark.parametrize('mode,attributes,unsafe', [
    (stat.S_IFDIR, 0, False), (stat.S_IFDIR, stat.FILE_ATTRIBUTE_REPARSE_POINT, True),
    (stat.S_IFLNK, 0, True),
])
def test_reparse_guard_uses_nonfollowing_attributes(monkeypatch, mode, attributes, unsafe):
    monkeypatch.setattr(Path, 'lstat', lambda path: SimpleNamespace(
        st_mode=mode, st_file_attributes=attributes))
    monkeypatch.setattr(Path, 'stat', lambda path: pytest.fail('must not follow target'))
    assert is_reparse_point(Path('fixture')) is unsafe


def test_reparse_guard_fails_closed_on_unreadable_entry(monkeypatch):
    def denied(path):
        raise PermissionError('fixture')
    monkeypatch.setattr(Path, 'lstat', denied)
    assert is_reparse_point(Path('fixture'))

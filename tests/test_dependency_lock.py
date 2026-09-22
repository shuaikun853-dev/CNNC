"""The same finite dependency set must drive CI and candidate runtime installs."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_dependency_lock_covers_real_external_and_browser_capabilities():
    lock = ROOT / 'requirements.lock'
    assert lock.is_file(), 'One reviewed runtime/CI lock is required'
    pins = {}
    for line in lock.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        assert re.fullmatch(r'[A-Za-z0-9._-]+==[A-Za-z0-9.!+_-]+', line)
        name, version = line.split('==', 1)
        key = name.lower().replace('_', '-')
        assert key not in pins
        pins[key] = version
    assert {'pandas', 'requests', 'playwright', 'psutil', 'tzdata', 'pytest-timeout'} <= pins.keys()


def test_runtime_and_ci_resolve_one_canonical_lock():
    assert '-r requirements.lock' in (ROOT / 'requirements.txt').read_text(encoding='utf-8')
    assert '-c ../requirements.lock' in (ROOT / '.github/constraints-ci.txt').read_text(encoding='utf-8')


def test_python_patch_version_is_explicit():
    assert re.fullmatch(r'3\.14\.\d+', (ROOT / '.python-version').read_text().strip())


def test_lock_excludes_versions_with_confirmed_20260922_advisories():
    from packaging.version import Version
    floors={'anyio':'4.14.2','click':'8.3.3','idna':'3.15',
            'pygments':'2.20.0','requests':'2.33.0','urllib3':'2.7.0'}
    pins={line.split('==',1)[0].lower():line.split('==',1)[1]
          for line in (ROOT/'requirements.lock').read_text(encoding='utf-8').splitlines()
          if line.strip() and not line.startswith('#')}
    assert all(Version(pins[name])>=Version(floor) for name,floor in floors.items())

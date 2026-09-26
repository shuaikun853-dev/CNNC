import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import cnnc_auth_core as core
from scripts import cnnc_auth_refresh as refresh


def provider_at(tmp_path, harvest, verify=lambda at, **kw: True):
    provider = core.CnncAuthProvider(
        state_path=tmp_path / 'state.json', ledger_path=tmp_path / 'ledger.jsonl',
        curl_files=(), harvest_func=harvest, verify_func=verify,
        exchange_func=lambda oc, **kw: 'AT-20260926-newtoken99')
    provider.state_path.write_text(json.dumps({'access_token': 'AT-20260926-oldtoken99'}))
    return provider


def test_real_daily_guard_valid_auth_ten_rounds_no_harvest(tmp_path):
    calls = []
    provider = provider_at(tmp_path, lambda **kw: calls.append(kw) or 'OC-fake')
    for _ in range(10):
        assert refresh.ensure_daily_curl_fresh(
            '2026-09-26', provider=provider, runner=refresh.run_curl_refresh, result_path=tmp_path / 'result.json',
            lock_path=tmp_path / 'maintenance.lock') == 'DAILY_CURL_REFRESH_OK'
    assert calls == []


@pytest.mark.parametrize('status', [core.CnncAuthStatus.SSO_LOGIN_REQUIRED,
    core.CnncAuthStatus.BROWSER_START_FAILED, core.CnncAuthStatus.BROWSER_CONNECT_FAILED,
    core.CnncAuthStatus.OC_NOT_CAPTURED, core.CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED])
def test_failed_automatic_guard_obeys_cooldown_manual_bypasses(tmp_path, status):
    calls = []
    def harvest(**kw):
        calls.append(kw)
        raise core.CnncAuthError(status, 'fixture failure')
    provider = provider_at(tmp_path, harvest, verify=lambda at, **kw: False)
    for _ in range(2):
        refresh.ensure_daily_curl_fresh('2026-09-26', provider=provider, runner=refresh.run_curl_refresh,
            result_path=tmp_path / 'result.json', lock_path=tmp_path / 'maintenance.lock')
    assert len(calls) == 1
    failure = json.loads(provider.state_path.read_text())
    assert failure['failure_type'] == status
    assert failure['last_attempt_at']
    refresh.run_curl_refresh(provider=provider, trigger='manual',
        result_path=tmp_path / 'result.json', lock_path=tmp_path / 'maintenance.lock')
    assert len(calls) == 2


@pytest.mark.parametrize('code', [412, 500, 502])
def test_verify_transport_failure_never_refreshes(tmp_path, code):
    calls = []
    provider = provider_at(tmp_path, lambda **kw: calls.append(kw) or 'OC-fake')
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('https://example.test', code, 'fixture', {}, None)
    provider.opener = Opener()
    provider._verify = provider._default_verify
    with pytest.raises(core.CnncAuthError):
        provider.ensure_valid()
    assert calls == []


def test_backup_retention_even_when_replace_fails(tmp_path, monkeypatch):
    provider = provider_at(tmp_path, lambda **kw: 'OC-fake')
    curl = tmp_path / 'query.curl'
    curl.write_text('curl "https://example.test" -H "access-token: AT-20260926-oldtoken99"')
    provider.curl_files = (curl,)
    for index in range(10):
        curl.with_name(curl.name + '.bak_20200101' + str(index)).write_text('old')
    monkeypatch.setattr(core.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('fixture')))
    with pytest.raises(core.CnncAuthError):
        provider.sync_curl_files('AT-20260926-newtoken99')
    assert len(list(tmp_path.glob('query.curl.bak_*'))) <= core.CURL_BACKUP_KEEP
    assert 'oldtoken99' in curl.read_text()
    assert not list(tmp_path.glob('*.tmp'))


def _concurrent_ensure_worker(root, barrier):
    root = Path(root)
    def verify(at, **kwargs):
        if at.endswith('oldtoken99'):
            barrier.wait(timeout=20)
            return False
        return True
    def harvest(**kwargs):
        with (root / 'harvest.txt').open('a') as handle:
            handle.write('harvest\n')
        return 'OC-fake'
    provider = core.CnncAuthProvider(
        state_path=root / 'state.json', ledger_path=root / 'ledger.jsonl', curl_files=(),
        harvest_func=harvest, verify_func=verify,
        exchange_func=lambda oc, **kw: 'AT-20260926-newtoken99')
    provider.ensure_valid()


def _concurrent_refresh_worker(root, barrier):
    root = Path(root)
    def harvest(**kwargs):
        with (root / 'harvest.txt').open('a') as handle:
            handle.write('harvest\n')
        return 'OC-fake'
    provider = core.CnncAuthProvider(
        state_path=root / 'state.json', ledger_path=root / 'ledger.jsonl', curl_files=(),
        harvest_func=harvest, verify_func=lambda at, **kw: True,
        exchange_func=lambda oc, **kw: 'AT-20260926-newtoken99')
    original_locked = provider._locked
    @contextmanager
    def synchronized_lock(**kwargs):
        barrier.wait(timeout=20)
        with original_locked(**kwargs):
            yield
    provider._locked = synchronized_lock
    provider.refresh()


@pytest.mark.parametrize('worker', [_concurrent_ensure_worker, _concurrent_refresh_worker])
def test_two_processes_single_flight(tmp_path, worker):
    provider_at(tmp_path, lambda **kw: 'OC-fake')
    ctx = multiprocessing.get_context('spawn')
    barrier = ctx.Barrier(2)
    processes = [ctx.Process(target=worker, args=(str(tmp_path), barrier))
                 for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(10)
    assert (tmp_path / 'harvest.txt').read_text().splitlines() == ['harvest']


def test_auth_data_override_shared_and_c_drive_rejected(tmp_path, monkeypatch):
    from scripts.cnnc_auth_paths import get_auth_data_dir
    monkeypatch.setenv('CNNC_AUTH_DATA_DIR', str(tmp_path))
    assert get_auth_data_dir(tmp_path / 'checkout-a') == get_auth_data_dir(tmp_path / 'checkout-b') == tmp_path
    monkeypatch.setenv('CNNC_AUTH_DATA_DIR', 'relative/data')
    with pytest.raises(ValueError):
        get_auth_data_dir(tmp_path)
    if os.name == 'nt':
        monkeypatch.setenv('CNNC_AUTH_DATA_DIR', 'C:\\cnnc-auth')
        with pytest.raises(ValueError):
            get_auth_data_dir(tmp_path)


def test_repeated_failure_keeps_original_cooldown_evidence(tmp_path):
    def harvest(**kw):
        raise core.CnncAuthError(core.CnncAuthStatus.OC_NOT_CAPTURED, 'fixture')
    provider = provider_at(tmp_path, harvest, verify=lambda at, **kw: False)
    with pytest.raises(core.CnncAuthError):
        provider.ensure_valid()
    state_bytes = provider.state_path.read_bytes()
    ledger_bytes = provider.ledger_path.read_bytes()
    for _ in range(10):
        with pytest.raises(core.CnncAuthError):
            provider.ensure_valid()
    assert provider.state_path.read_bytes() == state_bytes
    assert provider.ledger_path.read_bytes() == ledger_bytes


def test_automatic_failure_result_does_not_rewrite_during_cooldown(tmp_path):
    def harvest(**kw):
        raise core.CnncAuthError(core.CnncAuthStatus.OC_NOT_CAPTURED, 'fixture')
    provider = provider_at(tmp_path, harvest, verify=lambda at, **kw: False)
    kwargs = dict(provider=provider, trigger='daily-preflight', business_date='2026-09-26',
                  result_path=tmp_path / 'result.json', lock_path=tmp_path / 'lock')
    assert refresh.run_curl_refresh(**kwargs) == 1
    before = kwargs['result_path'].read_bytes()
    assert refresh.run_curl_refresh(**kwargs) == 1
    assert kwargs['result_path'].read_bytes() == before


def test_missing_credentials_is_not_proof_of_token_rejection(tmp_path):
    calls = []
    provider = provider_at(tmp_path, lambda **kw: calls.append(kw) or 'OC-fake')
    provider.state_path.unlink()
    with pytest.raises(core.CnncAuthError):
        provider.ensure_valid()
    assert calls == []


def test_browser_spawn_oserror_recorded_and_cooled_down(tmp_path, monkeypatch):
    calls = []
    provider = provider_at(tmp_path, lambda **kw: 'OC-fake', verify=lambda at, **kw: False)
    provider._harvest = provider._default_harvest
    def failed_spawn(*args, **kwargs):
        calls.append(1)
        raise OSError('fixture spawn failure')
    monkeypatch.setattr(core.subprocess, 'run', failed_spawn)
    for _ in range(2):
        with pytest.raises(core.CnncAuthError) as exc:
            provider.ensure_valid()
        assert exc.value.status == core.CnncAuthStatus.BROWSER_START_FAILED
    assert calls == [1]


def test_automatic_sync_failure_retains_partial_result_code(tmp_path, monkeypatch):
    provider = provider_at(tmp_path, lambda **kw: pytest.fail('must not harvest'))
    curl = tmp_path / 'query.curl'
    curl.write_text('curl "https://example.test" -H "access-token: AT-20260926-different99"')
    provider.curl_files = (curl,)
    def failed_sync(at):
        raise core.CnncAuthError(core.CnncAuthStatus.CURL_SYNC_FAILED, 'fixture')
    monkeypatch.setattr(provider, 'sync_curl_files', failed_sync)
    assert refresh.run_curl_refresh(provider=provider, trigger='daily-preflight',
        business_date='2026-09-26', result_path=tmp_path / 'result.json',
        lock_path=tmp_path / 'lock') == 4


def test_sync_matching_curl_does_not_rewrite_or_backup(tmp_path):
    provider = provider_at(tmp_path, lambda **kw: pytest.fail('must not harvest'))
    curl = tmp_path / 'query.curl'
    curl.write_text('curl "https://example.test" -H "access-token: AT-20260926-oldtoken99"')
    provider.curl_files = (curl,)
    before = curl.stat().st_mtime_ns
    provider.sync_curl_files('AT-20260926-oldtoken99')
    assert curl.stat().st_mtime_ns == before
    assert not list(tmp_path.glob('*.bak_*'))


def test_ensure_expired_deadline_has_zero_verify_or_harvest(tmp_path):
    calls = []
    provider = provider_at(tmp_path, lambda **kw: calls.append('harvest'),
                           verify=lambda at, **kw: calls.append('verify'))
    provider.monotonic = lambda: 100.0
    with pytest.raises(core.CnncAuthError) as exc:
        provider.ensure_valid(deadline_monotonic=99.0)
    assert exc.value.status == core.CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED
    assert calls == []


def test_ensure_verify_and_harvest_share_remaining_budget(tmp_path):
    now = [100.0]
    seen = []
    def verify(at, **kwargs):
        seen.append(('verify', kwargs['timeout']))
        if at.endswith('oldtoken99'):
            now[0] += 4.0
            return False
        return True
    def harvest(**kwargs):
        seen.append(('harvest', kwargs['timeout_seconds']))
        return 'OC-fake'
    provider = provider_at(tmp_path, harvest, verify=verify)
    provider.monotonic = lambda: now[0]
    provider.ensure_valid(deadline_monotonic=110.0)
    assert seen == [('verify', 10.0), ('harvest', 6.0), ('verify', 6.0)]


def test_ensure_default_refresh_receives_finite_total_deadline(tmp_path, monkeypatch):
    provider = provider_at(tmp_path, lambda **kw: 'OC-fake', verify=lambda at, **kw: False)
    provider.monotonic = lambda: 100.0
    calls = []
    monkeypatch.setattr(provider, 'refresh', lambda **kwargs: calls.append(kwargs))
    provider.ensure_valid()
    assert calls[0]['deadline_monotonic'] == 100.0 + (
        2 * core.VERIFY_TIMEOUT_SECONDS + provider.lock_wait_seconds
        + core.HARVEST_TIMEOUT_SECONDS + core.EXCHANGE_TIMEOUT_SECONDS)


@pytest.mark.parametrize('verified', [True, False])
def test_ensure_verify_exhausts_deadline_without_harvest(tmp_path, verified):
    now = [100.0]
    calls = []
    def verify(at, **kwargs):
        now[0] = 111.0
        return verified
    provider = provider_at(tmp_path, lambda **kw: calls.append('harvest'), verify=verify)
    provider.monotonic = lambda: now[0]
    with pytest.raises(core.CnncAuthError) as exc:
        provider.ensure_valid(deadline_monotonic=110.0)
    assert exc.value.status == core.CnncAuthStatus.AUTH_REFRESH_DEADLINE_EXCEEDED
    assert calls == []


def test_real_daily_guard_propagates_deadline(tmp_path):
    calls = []
    provider = provider_at(tmp_path, lambda **kw: calls.append('harvest'),
                           verify=lambda at, **kw: calls.append('verify'))
    provider.monotonic = lambda: 100.0
    status = refresh.ensure_daily_curl_fresh('2026-09-26', provider=provider,
        runner=refresh.run_curl_refresh, result_path=tmp_path / 'result.json',
        lock_path=tmp_path / 'lock', deadline_monotonic=99.0)
    assert status == 'AUTH_REFRESH_FAILED'
    assert calls == []


def test_dashboard_reads_all_results_from_canonical_override(tmp_path):
    env = dict(os.environ, CNNC_AUTH_DATA_DIR=str(tmp_path), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c',
        'import json; from app import dashboard_api as d; '
        'from scripts import cnnc_auth_refresh as r; '
        'print(json.dumps([str(d._CNNC_RECOVERY_RESULT_PATH), '
        'str(r.LOGIN_RECOVERY_RESULT_PATH), str(d._CURL_REFRESH_RESULT_PATH), '
        'str(r.CURL_REFRESH_RESULT_PATH)]))'], env=env, capture_output=True,
        text=True, check=True, timeout=30)
    paths = json.loads(result.stdout)
    assert paths[0] == paths[1]
    assert paths[2] == paths[3]
    assert all(Path(path).parent == tmp_path for path in paths)


@pytest.mark.parametrize('mode', ['refresh', 'verify_metadata', 'bootstrap'])
def test_automatic_deadline_bounds_thread_lock_wait(tmp_path, mode):
    provider = provider_at(tmp_path, lambda **kw: pytest.fail('must not harvest'),
                           verify=lambda at, **kw: mode == 'verify_metadata')
    if mode == 'bootstrap':
        provider.state_path.unlink()
    held = threading.Event()
    release = threading.Event()
    def holder():
        with core._thread_lock(provider._lock_path()):
            held.set()
            release.wait(0.8)
    worker = threading.Thread(target=holder)
    worker.start()
    assert held.wait(5)
    started = time.monotonic()
    try:
        with pytest.raises(core.CnncAuthError):
            provider.ensure_valid(deadline_monotonic=started + 0.05)
        assert time.monotonic() - started < 0.5
        assert provider.lock_wait_seconds == 120.0
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def _hold_auth_process_lock(path, held, release):
    with core._process_lock(Path(path), wait_seconds=5.0, monotonic=time.monotonic):
        held.set()
        release.wait(0.8)


def test_refresh_deadline_bounds_os_lock_wait(tmp_path):
    provider = provider_at(tmp_path, lambda **kw: pytest.fail('must not harvest'))
    ctx = multiprocessing.get_context('spawn')
    held, release = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=_hold_auth_process_lock,
                         args=(str(provider._lock_path()), held, release))
    worker.start()
    assert held.wait(10)
    started = time.monotonic()
    try:
        with pytest.raises(core.CnncAuthError):
            provider.refresh(deadline_monotonic=started + 0.05)
        assert time.monotonic() - started < 0.5
        assert provider.lock_wait_seconds == 120.0
    finally:
        release.set()
        worker.join(5)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
    assert worker.exitcode == 0

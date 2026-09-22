import importlib
import pytest


def modules():
    return (
        importlib.import_module('app.services.audit_state_kernel'),
        importlib.import_module('app.services.audit_run_store'),
        importlib.import_module('app.services.remote_execution_store'),
        importlib.import_module('app.services.daily_audit_store'),
    )


def test_legacy_store_preserves_canonical_methods_and_types():
    kernel, run, remote, legacy = modules()
    assert issubclass(legacy.DailyAuditStore, run.AuditRunStore)
    assert issubclass(legacy.DailyAuditStore, remote.RemoteExecutionStore)
    assert legacy.DailyAuditStore._transaction is kernel.AuditStateKernel._transaction
    assert legacy.DailyAuditStore.ensure_run is run.AuditRunStore.ensure_run
    assert legacy.DailyAuditStore.prepare_remote_intent is remote.RemoteExecutionStore.prepare_remote_intent
    assert legacy.LeaseToken is kernel.LeaseToken
    assert legacy.ProcessLock is kernel.ProcessLock
    assert legacy.StaleLease is kernel.StaleLease


def test_public_capabilities_do_not_expose_unrelated_operations():
    _, run, remote, _ = modules()
    assert not hasattr(run.AuditRunStore, 'prepare_remote_intent')
    assert not hasattr(remote.RemoteExecutionStore, 'freeze_snapshot')


def test_scoped_state_views_share_one_database_and_fencing(tmp_path):
    kernel, run, remote, legacy = modules()
    path = tmp_path / 'state.sqlite3'
    runs = run.AuditRunStore(path)
    remotes = remote.RemoteExecutionStore(path)
    all_state = legacy.DailyAuditStore(path)
    token = runs.acquire_lease('2026-09-22', 'governance-test', 60)
    job = remotes.prepare_remote_intent(token, 'one-job', 'one-task', 'immutable-hash')
    assert all_state.get_remote_job(job['id']) == job
    assert all_state.get_run('2026-09-22')['id'] == token.run_id
    runs.release_lease(token)
    with pytest.raises(kernel.StaleLease):
        remotes.claim_remote_submission(token, job['id'])
    assert all_state.get_remote_job(job['id'])['post_attempted'] == 0


def test_cross_table_rollback_still_uses_one_transaction(tmp_path):
    _, _, _, legacy = modules()
    store = legacy.DailyAuditStore(tmp_path / 'state.sqlite3')
    token = store.acquire_lease('2026-09-22', 'governance-test', 60)
    job = store.prepare_remote_intent(token, 'one-job', 'one-task', 'immutable-hash')
    before = store.get_run('2026-09-22')
    with pytest.raises(RuntimeError, match='rollback probe'):
        with store._transaction(immediate=True) as connection:
            connection.execute("UPDATE daily_runs SET state='FINALIZED' WHERE id=?", (token.run_id,))
            connection.execute("UPDATE remote_jobs SET post_attempted=1 WHERE id=?", (job['id'],))
            raise RuntimeError('rollback probe')
    assert store.get_run('2026-09-22') == before
    assert store.get_remote_job(job['id'])['post_attempted'] == 0

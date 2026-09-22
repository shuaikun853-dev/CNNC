from datetime import UTC, datetime
import ast
import importlib
from pathlib import Path
import subprocess
import sys


def test_runtime_context_loads_without_orchestration():
    code = "import sys; from app.services import runtime_context; assert 'app.services.incremental_audit_runtime' not in sys.modules; assert not any(k.startswith('app.workflows.') for k in sys.modules)"
    result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_runtime_context_paths_and_time_preserve_existing_contract(tmp_path):
    context = importlib.import_module('app.services.runtime_context')
    old = importlib.import_module('app.services.incremental_audit_runtime')
    assert old.RuntimeConfigurationError is context.RuntimeConfigurationError
    assert old.RuntimePaths is context.RuntimePaths
    assert old.runtime_paths is context.runtime_paths
    paths = context.runtime_paths('2026-09-22', tmp_path)
    assert paths.run_dir == tmp_path.absolute() / '2026-09-22'
    assert paths.detail_lock_path == tmp_path.absolute() / 'detail.lock'
    assert paths.final_output_path.name == 'saas_audit_final_20260922.xlsx'
    assert context.business_date_in_shanghai(datetime(2026,9,21,17,tzinfo=UTC)) == '2026-09-22'
    assert context.cutoff_for_business_date('2026-09-22') == datetime(2026,9,22,10,tzinfo=UTC)


def test_recovery_evidence_policy_preserves_compatibility():
    policy = importlib.import_module('app.services.remote_execution_policy')
    old = importlib.import_module('app.services.remote_running_recovery')
    assert old.decode_recovery_evidence is policy.decode_recovery_evidence
    assert old.encode_recovery_evidence is policy.encode_recovery_evidence
    assert old.RemoteExecutionState is policy.RemoteExecutionState
    evidence = policy.decode_recovery_evidence('not json')
    assert evidence == {'version':1,'observations':[],'restart':None,'probe_health':None,'submit_reconcile':None}
    evidence['observations'].append({'progress':1})
    assert policy.decode_recovery_evidence(None)['observations'] == []
    assert policy.decode_recovery_evidence(policy.encode_recovery_evidence(evidence)) == evidence


def test_policy_load_does_not_load_store_or_runtime():
    code = "import sys; from app.services import remote_execution_policy; assert 'app.services.daily_audit_store' not in sys.modules; assert 'app.services.incremental_audit_runtime' not in sys.modules"
    result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_store_does_not_import_recovery_orchestrator():
    from app.services import daily_audit_store
    tree = ast.parse(Path(daily_audit_store.__file__).read_text(encoding='utf-8-sig'))
    forbidden = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module in {'app.services.remote_running_recovery', 'app.services.incremental_audit_runtime'}]
    assert not forbidden


def test_prewarm_phases_have_no_dependency_cycle():
    from tools.architecture_graph import scan_repository, strongly_connected_components
    graph = scan_repository(Path(__file__).resolve().parents[1])
    cycles = strongly_connected_components(graph)
    assert not [group for group in cycles if any('customer_prewarm' in name for name in group)]


def test_prewarm_execution_has_one_canonical_implementation():
    execution = importlib.import_module('app.services.customer_prewarm_execution')
    dispatch = importlib.import_module('app.services.customer_prewarm_dispatch')
    lifecycle = importlib.import_module('app.services.customer_prewarm_lifecycle')
    assert dispatch._confirm_pending_priority_job is execution._confirm_pending_priority_job
    assert dispatch.CanaryLiveBudget is execution.CanaryLiveBudget
    assert lifecycle._mark_reconcile_manual is execution._mark_reconcile_manual
    assert lifecycle.harvest_existing_customer_prewarm_job is execution.harvest_existing_customer_prewarm_job

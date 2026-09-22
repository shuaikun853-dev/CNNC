"""Keep the delivered guard, navigation and reproducible test environment wired in."""
from pathlib import Path
import re
import yaml

ROOT=Path(__file__).resolve().parents[1]
WORKFLOW=ROOT/'.github/workflows/critical-safety-tests.yml'


def test_ci_triggers_on_development_tools_and_dependency_lock():
    document=yaml.load(WORKFLOW.read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    paths=set(document['on']['push']['paths'])
    assert {'tools/**','requirements.lock','requirements.in','.python-version'}<=paths


def test_ci_uses_same_python_patch_as_the_reviewed_environment():
    document=yaml.load(WORKFLOW.read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    steps=document['jobs']['critical-safety-tests']['steps']
    setup=next(step for step in steps if str(step.get('uses','')).startswith('actions/setup-python@'))
    assert setup['with'].get('python-version-file')=='.python-version'


def test_ci_executes_new_architecture_and_recovery_safety_contracts():
    text=WORKFLOW.read_text(encoding='utf-8')
    required=('test_architecture_graph.py','test_parallel_ai_check.py','test_parallel_ai_edge_cases.py',
              'test_runtime_recovery.py','test_runtime_fingerprint.py','test_runtime_manifest_cli.py',
              'test_second_review_regressions.py','test_governance_delivery_contract.py','test_tick_lease_window.py')
    assert all('tests/'+name in text for name in required)


def test_navigation_points_to_current_implementation_owners():
    architecture=(ROOT/'ARCHITECTURE.md').read_text(encoding='utf-8')
    routing=(ROOT/'docs/debug/BUG_ROUTING.md').read_text(encoding='utf-8')
    owners=('audit_run_store.py','remote_execution_store.py','audit_state_kernel.py',
            'customer_prewarm_execution.py','zhaoshang_read_operations.py',
            'zhaoshang_submission_operations.py','runtime_context.py')
    assert all(name in architecture and name in routing for name in owners)
    assert '历史债务：复用 scripts' not in architecture


def test_parallel_guard_has_a_short_agent_entrypoint():
    agents=(ROOT/'AGENTS.md').read_text(encoding='utf-8')
    assert 'tools/parallel_ai_check.py' in agents
    assert 'tests/test_architecture_contract.py' in agents


def test_recovery_doc_discloses_exact_backup_and_timeout_scope():
    text=(ROOT/'docs/development/SAFE_RELEASE_AND_RECOVERY.md').read_text(encoding='utf-8')
    assert '--database-timeout' in text
    assert '登记过的业务证据文件名' in text
    assert '不是端到端' in text
    assert 'PYTHONPATH' in text

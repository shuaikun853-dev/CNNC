"""Architecture safety contracts. Historical exemptions have been retired.

These checks constrain dependencies, not file sizes or subjective design quality.
A genuine architectural rule change requires an explicit reviewed decision.
"""
from pathlib import Path
import pytest
from tools.architecture_graph import scan_repository, strongly_connected_components

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def graph():
    return scan_repository(ROOT)


def _show(edges):
    return '\n'.join(f'  {source} -> {target}' for source, target in sorted(edges))


def test_no_new_app_to_scripts_dependencies(graph):
    bad = {(a,b) for a,b in graph.declared_imports if (a == 'app' or a.startswith('app.')) and (b == 'scripts' or b.startswith('scripts.'))}
    assert not bad, 'Application capabilities cannot depend on CLI scripts:\n' + _show(bad)


def test_no_new_app_reverse_dependencies_on_runtime(graph):
    bad = {(a,b) for a,b in graph.declared_imports if (a == 'app' or a.startswith('app.')) and b == 'app.services.incremental_audit_runtime'}
    assert not bad, 'A composition root is not a shared utility module:\n' + _show(bad)


def test_no_new_import_cycles_outside_existing_core_cycle(graph):
    cycles = strongly_connected_components(graph)
    assert not cycles, 'Dependency cycles are not allowed; members:\n' + '\n'.join(', '.join(sorted(group)) for group in cycles)


def test_persistence_never_imports_orchestration(graph):
    stores = {'app.services.daily_audit_store','app.services.audit_state_kernel','app.services.audit_run_store','app.services.remote_execution_store'}
    orchestration = ('app.workflows.', 'app.services.recovery.', 'app.services.incremental_audit_runtime', 'app.services.customer_prewarm_dispatch', 'app.services.customer_prewarm_lifecycle', 'app.services.remote_running_recovery')
    bad = {(a,b) for a,b in graph.declared_imports if a in stores and any(b == prefix.rstrip('.') or b.startswith(prefix.rstrip('.') + '.') for prefix in orchestration)}
    assert not bad, 'Persistence cannot call workflow coordinators:\n' + _show(bad)


def test_runtime_context_is_an_independent_leaf(graph):
    bad = {(a,b) for a,b in graph.declared_imports if a == 'app.services.runtime_context' and (b in {'app','scripts'} or b.startswith('app.') or b.startswith('scripts.'))}
    assert not bad, 'Paths and dates must not load the application:\n' + _show(bad)


def test_import_graph_has_no_unreviewed_unknowns(graph):
    assert not graph.warnings, "Unresolved imports require explicit architectural review:\n" + "\n".join(graph.warnings)

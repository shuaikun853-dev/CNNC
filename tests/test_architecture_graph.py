import importlib
import pytest


def graph_module():
    return importlib.import_module('tools.architecture_graph')


def test_package_alias_and_relative_imports_are_not_hidden():
    graph = graph_module().build_graph({
        'app/__init__.py': '', 'app/services/__init__.py': '',
        'app/services/a.py': 'from . import b\nfrom app.services import c\n',
        'app/services/b.py': '', 'app/services/c.py': '',
    })
    assert ('app.services.a', 'app.services.b') in graph.edges
    assert ('app.services.a', 'app.services.c') in graph.edges


def test_attribute_import_resolves_to_defining_module():
    graph = graph_module().build_graph({
        'app/a.py': 'from app.b import Thing\n', 'app/b.py': 'Thing = 1\n',
    })
    assert ('app.a', 'app.b') in graph.edges


def test_cycle_evidence_uses_real_edges_not_sorted_member_arrows():
    module = graph_module()
    graph = module.build_graph({'app/a.py':'import app.b\n', 'app/b.py':'import app.a\n'})
    assert module.cyclic_edges(graph) == frozenset({('app.a','app.b'),('app.b','app.a')})


def test_local_and_literal_dynamic_imports_are_visible():
    graph = graph_module().build_graph({
        'app/a.py': "def call():\n import app.b\n\nimport importlib\nimportlib.import_module('app.c')\n",
        'app/b.py': '', 'app/c.py': '',
    })
    assert ('app.a','app.b') in graph.edges
    assert ('app.a','app.c') in graph.edges


def test_syntax_errors_do_not_become_an_empty_safe_graph():
    with pytest.raises(ValueError, match='app/a.py'):
        graph_module().build_graph({'app/a.py': 'def broken(:\n'})


def test_graph_scan_does_not_execute_application_code():
    graph = graph_module().build_graph({'app/a.py': "raise AssertionError('never execute')\nimport app.b\n", 'app/b.py': ''})
    assert ('app.a','app.b') in graph.edges


def test_new_cycle_edge_is_detectable_inside_existing_members():
    module = graph_module()
    old = module.build_graph({'app/a.py':'import app.b\n','app/b.py':'import app.c\n','app/c.py':'import app.a\n'})
    new = module.build_graph({'app/a.py':'import app.b\nimport app.c\n','app/b.py':'import app.c\n','app/c.py':'import app.a\n'})
    assert module.cyclic_edges(new) - module.cyclic_edges(old) == {('app.a','app.c')}


def test_missing_script_module_is_not_hidden_from_policy_checks():
    graph = graph_module().build_graph({'app/a.py': 'def hidden():\n import scripts.future_helper\n'})
    assert ('app.a', 'scripts.future_helper') in graph.declared_imports


def test_import_above_package_root_is_reported():
    graph = graph_module().build_graph({'app/a.py': 'from .. import missing\n'})
    assert any('exceeds package' in warning for warning in graph.warnings)

"""Concrete third-review findings; no production data or external actions."""
import ast
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import pytest
from tests.test_runtime_recovery import load_tool, make_sources, DAY
from tests.test_production_identity import make_service, base_scripts, fail
from tests.test_parallel_ai_check import _make_repository, _run_git


def test_backup_detects_index_corruption_that_quick_check_misses(tmp_path):
    path=tmp_path/'index-mismatch.sqlite3'
    with closing(sqlite3.connect(path)) as con, con:
        con.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY,value TEXT)')
        con.execute("INSERT INTO evidence VALUES(1,'one')")
        con.execute('CREATE INDEX ix_evidence ON evidence(value)')
        con.execute('PRAGMA writable_schema=ON')
        con.execute("UPDATE sqlite_master SET sql='CREATE INDEX ix_evidence ON evidence(id)' WHERE name='ix_evidence'")
    with closing(sqlite3.connect(path)) as con:
        assert con.execute('PRAGMA quick_check').fetchall()==[('ok',)]
        assert con.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]
    with pytest.raises(ValueError):load_tool()._check_sqlite(path)


@pytest.mark.parametrize('field,value',[
    ('artifact_scope','invented'),('artifact_policy','copy_everything'),
    ('artifact_consistency','atomic'),('timeout_scope','hard_end_to_end')])
def test_backup_policy_metadata_is_validated(tmp_path,field,value):
    tool=load_tool();source=make_sources(tmp_path);target=tmp_path/'backup'
    manifest=tool.create_backup(source,DAY,target)
    manifest[field]=value
    (target/'backup-manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError):tool.verify_backup(target)


def test_changed_tracked_bytecode_is_not_full_runtime_match(tmp_path):
    from app.services.runtime_fingerprint import _repository_state
    repo,_=_make_repository(tmp_path)
    bytecode=repo/'cached.pyc';bytecode.write_bytes(b'initial-fixture')
    _run_git(repo,'add','cached.pyc');_run_git(repo,'commit','-m','fixture bytecode')
    bytecode.write_bytes(b'changed-fixture')
    assert _repository_state(repo)['clean'] is False


def test_unknown_git_status_has_unknown_dirty_classification():
    scripts=base_scripts();scripts['status --porcelain']=fail('unavailable')
    service,_=make_service(scripts)
    local=service.snapshot()['local']
    assert local['working_tree']=='UNKNOWN'
    assert local['dirty']['classification']=='UNKNOWN'


def test_returned_nested_snapshot_cannot_mutate_cached_identity():
    service,_=make_service(base_scripts(),local_ttl=60,remote_ttl=60,scheduler_ttl=60)
    first=service.snapshot();first['scheduler']['tasks'][0]['registered']=True
    first['startup']['python']['version']='forged-version'
    second=service.snapshot()
    assert second['scheduler']['tasks'][0]['registered'] is False
    assert second['startup']['python']['version']!='forged-version'


def test_dashboard_captures_identity_during_bootstrap_not_first_request():
    source=(Path(__file__).resolve().parents[1]/'app/dashboard_api.py').read_text(encoding='utf-8')
    tree=ast.parse(source)
    assert any(isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)
               and isinstance(node.value.func,ast.Name)
               and node.value.func.id=='get_production_identity_service' for node in tree.body)


def test_dynamic_importer_alias_cannot_hide_forbidden_dependency():
    from tools.architecture_graph import build_graph
    graph=build_graph({'app/a.py':"from importlib import import_module\nloader=import_module\nloader('scripts.tool')",'scripts/tool.py':''})
    assert ('app.a','scripts.tool') in graph.edges or graph.warnings


@pytest.mark.parametrize('target',['app.workflows','app.services.recovery'])
def test_persistence_contract_covers_orchestration_package_roots(target):
    from tools.architecture_graph import build_graph
    from tests.test_architecture_contract import test_persistence_never_imports_orchestration as check
    graph=build_graph({'app/services/audit_run_store.py':'import '+target})
    with pytest.raises(AssertionError):check(graph)


def test_import_origin_must_be_owned_by_distribution_not_shared_site_directory(tmp_path,monkeypatch):
    from app.services import runtime_fingerprint as module
    site_root=tmp_path/'site-packages';site_root.mkdir()
    distribution=SimpleNamespace(version='1.0',read_text=lambda name:'demo_pkg\n',
        locate_file=lambda path:site_root/str(path), files=[Path('demo_pkg/__init__.py')])
    monkeypatch.setattr(module.importlib.metadata,'distribution',lambda name:distribution)
    monkeypatch.setattr(module.importlib.util,'find_spec',lambda name:SimpleNamespace(
        origin=str(site_root/'demo_pkg.py'),submodule_search_locations=None))
    with pytest.raises(ValueError):module._verified_package_version('demo-pkg')


def test_backup_detects_foreign_key_inconsistency(tmp_path):
    path=tmp_path/'foreign-key.sqlite3'
    with closing(sqlite3.connect(path)) as con,con:
        con.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
        con.execute('CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id))')
        con.execute('INSERT INTO child VALUES(1,123)')
    with pytest.raises(ValueError):load_tool()._check_sqlite(path)

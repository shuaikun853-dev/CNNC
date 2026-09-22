"""Adversarial review cases, reproduced using only local fixtures and fakes."""
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from tests.test_parallel_ai_check import _make_repository, _run_git, _run_tool
from tests.test_runtime_recovery import load_tool, make_sources, DAY
from tests.test_runtime_fingerprint import proposal, verify, api
from tests.test_production_identity import make_service, base_scripts, fail, ok, SHA_A


@pytest.mark.parametrize('scope',['app//services','app/./services'])
def test_noncanonical_scope_does_not_hide_real_overlap(tmp_path, scope):
    repo, base = _make_repository(tmp_path)
    other = tmp_path/'other'
    _run_git(repo,'worktree','add','-b','other',str(other),base)
    p=other/'app/services/x.py';p.parent.mkdir(parents=True);p.write_text('# fixture',encoding='utf-8')
    result=_run_tool(repo,'--base-sha',base,'--scope',scope,'--no-github')
    assert result.returncode==2,result.stdout+result.stderr


def test_application_package_root_cannot_import_scripts():
    from tools.architecture_graph import build_graph
    from tests.test_architecture_contract import test_no_new_app_to_scripts_dependencies as contract
    graph=build_graph({'app/__init__.py':'import scripts.foo','scripts/foo.py':''})
    with pytest.raises(AssertionError):contract(graph)


def test_direct_self_import_is_visible_as_a_cycle():
    from tools.architecture_graph import build_graph,strongly_connected_components
    graph=build_graph({'app/a.py':'import app.a'})
    assert strongly_connected_components(graph)==(frozenset({'app.a'}),)


def test_unknown_dynamic_import_requires_explicit_review():
    from tools.architecture_graph import build_graph
    from tests import test_architecture_contract as contracts
    graph=build_graph({'app/a.py':'import importlib\nimportlib.import_module(module_name)'})
    assert graph.warnings
    check=getattr(contracts,'test_import_graph_has_no_unreviewed_unknowns',None)
    assert callable(check), 'CI must not silently ignore unresolved imports'
    with pytest.raises(AssertionError):check(graph)


def test_unknown_startup_is_not_reported_as_version_match():
    scripts=base_scripts(SHA_A,remote_sha=SHA_A)
    scripts['rev-parse HEAD']=[fail('unavailable'),ok(SHA_A+'\n')]
    service,_=make_service(scripts)
    snapshot=service.snapshot()
    assert snapshot['comparison']['process_vs_local']=='UNKNOWN'
    assert snapshot['comparison']['version_status']!='MATCH'


@pytest.mark.parametrize('payload',['', '[]','"unexpected"','{"foo":"bar"}'])
def test_invalid_scheduler_payload_is_unknown(payload):
    scripts=base_scripts();scripts['query_scheduled_task_json']=ok(payload)
    service,_=make_service(scripts)
    assert service.snapshot()['scheduler']['status']=='UNKNOWN'


@pytest.mark.parametrize('path',['src/__pycache__payload.py','data/component.bak.py','runtime/helper.py'])
def test_executable_source_is_not_misclassified_as_artifact(path):
    from app.services.production_identity import _is_artifact_path
    assert not _is_artifact_path(path)


def test_manifest_cannot_ignore_an_observed_production_task(tmp_path):
    root,_,_,scheduler,readers,manifest=proposal(tmp_path)
    manifest['approved']=True
    scheduler['CNNC-Dashboard-ReadOnly']='d'*64
    assert verify(root,manifest,scheduler,readers)['status']!='MATCH'


def test_uncontrolled_pythonpath_is_not_a_complete_match(tmp_path):
    root,_,_,scheduler,readers,manifest=proposal(tmp_path)
    manifest['approved']=True
    readers['runtime_reader']=lambda:{'version':'3.14.5','virtualenv':True,'user_site':False,'pythonpath_configured':True}
    assert verify(root,manifest,scheduler,readers)['status']!='MATCH'


def test_backup_rejects_case_insensitive_member_collisions(tmp_path):
    tool=load_tool();source=make_sources(tmp_path)
    file=source/DAY/'team/BATCH_STATE.md';file.parent.mkdir();file.write_text('evidence',encoding='utf-8')
    backup=tmp_path/'backup';manifest=tool.create_backup(source,DAY,backup,include_artifacts=True)
    duplicate=dict(manifest['artifacts'][0]);duplicate['path']=duplicate['path'].replace('/team/','/TEAM/')
    second=backup/duplicate['path'];second.parent.mkdir(exist_ok=True)
    if not second.exists():second.write_text('evidence',encoding='utf-8')
    manifest['artifacts'].append(duplicate)
    (backup/'backup-manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError):tool.verify_backup(backup)


@pytest.mark.skipif(os.name!='nt', reason='Windows volume aliases')
@pytest.mark.parametrize('drive_info',[(4, 'network'),(3,r'\Device\CVolume')])
def test_drive_alias_cannot_bypass_output_volume_policy(tmp_path,monkeypatch,drive_info):
    from tools import local_output_paths
    def fake_drive(drive):
        return (3,r'\Device\CVolume') if drive.casefold()=='c:' else drive_info
    monkeypatch.setattr(local_output_paths,'_windows_drive_info',fake_drive,raising=False)
    with pytest.raises(ValueError):local_output_paths.new_local_output_path(tmp_path/'new-output')


def test_dependency_version_reader_rejects_shadowed_import_origin(tmp_path,monkeypatch):
    module=api()
    method=getattr(module,'_verified_package_version',None)
    assert callable(method), 'Version metadata alone does not prove import origin'
    distribution=SimpleNamespace(version='1.0',read_text=lambda name:'demo_pkg\n',
                                 locate_file=lambda path:tmp_path/'venv/site-packages',files=[])
    monkeypatch.setattr(module.importlib.metadata,'distribution',lambda name:distribution)
    import importlib.util
    monkeypatch.setattr(importlib.util,'find_spec',lambda name:SimpleNamespace(
        origin=str(tmp_path/'shadow/demo_pkg.py'),submodule_search_locations=None))
    with pytest.raises((ValueError,RuntimeError)):method('demo-pkg')


def test_remote_git_error_does_not_publish_embedded_credentials():
    scripts=base_scripts()
    scripts['ls-remote']=fail('fatal: cannot access https://user:secret-fixture-value@invalid.example/repo')
    service,_=make_service(scripts)
    snapshot=service.snapshot()
    assert snapshot['remote']['status']=='UNKNOWN'
    assert 'secret-fixture-value' not in repr(snapshot)


@pytest.mark.parametrize('payload', ['not-a-sha\trefs/heads/prod/closeout-20260818\n',
    'a'*40+'\trefs/heads/unrelated\n', 'a'*40+'\n'])
def test_remote_version_requires_exact_commit_and_branch(payload):
    scripts=base_scripts(SHA_A, remote_sha=SHA_A)
    scripts['ls-remote']=ok(payload)
    service,_=make_service(scripts)
    snapshot=service.snapshot()
    assert snapshot['remote']['status']=='UNKNOWN'
    assert snapshot['comparison']['version_status']!='MATCH'


def test_package_child_reexport_is_not_a_direct_self_import():
    from tools.architecture_graph import build_graph, strongly_connected_components
    graph=build_graph({'app/__init__.py':'from . import feature','app/feature.py':''})
    assert strongly_connected_components(graph)==()
    assert ('app','app.feature') in graph.edges

"""Final independent-review reproductions; local temporary files and Git only."""
from pathlib import Path
from types import SimpleNamespace
import json
import shlex
import pytest
import yaml
from tests.test_parallel_ai_check import _make_repository

ROOT=Path(__file__).resolve().parents[1]


def test_new_destination_rejects_stream_of_existing_file(tmp_path):
    from tools.local_output_paths import new_local_output_path
    host=tmp_path/'existing.txt';host.write_text('preserve',encoding='utf-8')
    with pytest.raises(ValueError):new_local_output_path(str(host)+':manifest')
    assert host.read_text(encoding='utf-8')=='preserve'


@pytest.mark.parametrize('name',['new.txt:stream','NUL','CON.txt','nul .txt','LPT1','COM1','CONOUT$','tail.','tail '])
def test_nonordinary_windows_names_are_rejected_before_volume_lookup(monkeypatch,name):
    from tools import local_output_paths as module
    observed=[]
    monkeypatch.setattr(module,'_check_backing_volume',lambda drive:observed.append(drive))
    with pytest.raises(ValueError):module._ordinary_local_path('E:/audit/'+name)
    assert observed==[]


@pytest.mark.parametrize('statement',['import app.pkg.sub','from app.pkg.sub import VALUE'])
def test_package_initialization_cycle_is_visible(tmp_path,statement):
    import subprocess
    import sys
    from tools.architecture_graph import build_graph,strongly_connected_components
    sources={'app/__init__.py':'','app/pkg/__init__.py':'from app.x import VALUE',
             'app/pkg/sub.py':'VALUE=1','app/x.py':statement+'\nVALUE=2'}
    for name,text in sources.items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
    actual=subprocess.run([sys.executable,'-I','-B','-c','import sys; sys.path.insert(0,sys.argv[1]); import app.x',str(tmp_path)],
                          cwd=tmp_path,capture_output=True,text=True,timeout=15)
    assert actual.returncode!=0 and 'partially initialized' in actual.stderr
    graph=build_graph(sources)
    assert ('app.x','app.pkg') in graph.edges
    assert any({'app.x','app.pkg'}<=set(cycle) for cycle in strongly_connected_components(graph))


def test_package_reexports_do_not_create_implicit_ancestor_self_cycles():
    from tools.architecture_graph import build_graph,strongly_connected_components
    sources={'app/__init__.py':'from . import feature','app/feature.py':'import app.util','app/util.py':''}
    assert not strongly_connected_components(build_graph(sources))


@pytest.mark.parametrize('relation',['newer','missing','equal','fork'])
def test_same_branch_pr_identity_controls_self_exclusion(tmp_path,monkeypatch,capsys,relation):
    from tools import parallel_ai_check as tool
    repo,base=_make_repository(tmp_path)
    pr=SimpleNamespace(number=9,title='same name',url='https://example.invalid/pull/9',branch='chore/test-task',
        files=('tracked.txt',),from_fork=relation=='fork',head=None if relation=='missing' else (base if relation in {'equal','fork'} else 'f'*40))
    monkeypatch.setattr(tool,'_open_pull_requests',lambda *args:(pr,))
    result=tool.main(['--repo-root',str(repo),'--base-sha',base,'--scope','tracked.txt'])
    output=capsys.readouterr().out
    if relation=='equal':assert result==0 and 'STATUS: SAFE_TO_START' in output
    else:assert result==2 and 'STATUS: STOP_AND_REVIEW' in output


def test_github_pr_query_preserves_head_commit(monkeypatch,tmp_path):
    from tools import parallel_ai_check as tool
    commands=[];sha='b'*40
    payload=[{'number':7,'title':'fixture','url':'https://example.invalid/7','headRefName':'feature',
              'headRefOid':sha,'files':[{'path':'app/test.py'}],'changedFiles':1,'isCrossRepository':False}]
    monkeypatch.setattr(tool.shutil,'which',lambda name:'gh-fixture')
    monkeypatch.setattr(tool,'_github_repo_slug',lambda *args:'owner/repo')
    def run(command,**kwargs):commands.append(command);return json.dumps(payload)
    monkeypatch.setattr(tool,'_run',run)
    prs=tool._open_pull_requests(tmp_path,1)
    assert 'headRefOid' in commands[0][commands[0].index('--json')+1].split(',')
    assert getattr(prs[0],'head',None)==sha


def test_ci_no_longer_excludes_hermetic_recovery_wiring_test():
    document=yaml.load((ROOT/'.github/workflows/critical-safety-tests.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    step=next(s for s in document['jobs']['critical-safety-tests']['steps'] if s.get('name')=='Run critical safety tests')
    args=shlex.split(step['run']);target='tests/test_remote_running_recovery.py::test_default_dependencies_wire_probe_and_write_gated_restart'
    assert 'tests/test_remote_running_recovery.py' in args
    assert '--deselect='+target not in args
    assert not any(arg=='--deselect' and index+1<len(args) and args[index+1]==target for index,arg in enumerate(args))


def test_ci_includes_final_review_guards():
    document=yaml.load((ROOT/'.github/workflows/critical-safety-tests.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
    step=next(s for s in document['jobs']['critical-safety-tests']['steps'] if s.get('name')=='Run critical safety tests')
    assert 'tests/test_final_review_regressions.py' in shlex.split(step['run'])

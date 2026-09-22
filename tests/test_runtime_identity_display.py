"""Exercise the actual dashboard renderer, without starting pollers or HTTP calls."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]
JS=r'''
const elements=new Map();
global.document={addEventListener(){},querySelector(id){
  if(!elements.has(id))elements.set(id,{textContent:'',innerHTML:''});
  return elements.get(id);
}};
const ui=require(process.argv[1]);
const input=JSON.parse(process.argv[2]);
ui.renderProductionIdentity(input);
console.log(JSON.stringify(Object.fromEntries(elements)));
'''

def render(scheduler, runtime=None):
    node=shutil.which('node')
    assert node, 'JavaScript verification requires Node; do not silently skip'
    payload={'comparison':{'version_status':'MATCH','alerts':[]},'scheduler':scheduler}
    if runtime is not None:payload['runtime_identity']=runtime
    result=subprocess.run([node,'-e',JS,str(ROOT/'app/static/dashboard/app.js'),json.dumps(payload)],
        capture_output=True,text=True,encoding='utf-8',timeout=20,check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('registered,expected',[(False,'未注册'),(True,'已注册')])
def test_scheduler_summary_uses_registration_not_query_success(registered,expected):
    elements=render({'status':'OK','tasks':[{'name':'one','registered':registered}]})
    assert '调度器 '+expected in elements['#pi-chips']['textContent']


def test_partial_registration_remains_visible():
    elements=render({'status':'OK','tasks':[{'name':'one','registered':True},{'name':'two','registered':False}]})
    assert '部分注册' in elements['#pi-chips']['textContent']


def test_git_match_cannot_hide_runtime_mismatch_and_messages_are_escaped():
    elements=render({'status':'OK','tasks':[]},{'status':'MISMATCH','issues':['<img src=x onerror=alert(1)>']})
    assert '运行环境核验' in elements['#pi-kv']['innerHTML']
    assert '不一致' in elements['#pi-status']['innerHTML']
    assert '<img' not in elements['#pi-status']['innerHTML']


def test_old_api_without_runtime_evidence_does_not_show_full_match():
    elements=render({'status':'UNKNOWN','tasks':[]})
    assert '未提供' in elements['#pi-kv']['innerHTML']

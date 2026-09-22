"""Reproduce alias retargeting using isolated Windows junctions only."""
import os
from pathlib import Path
import subprocess
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.test_runtime_fingerprint import fixture_inputs, api, verify

@pytest.mark.skipif(os.name!='nt',reason='Windows junction semantics')
def test_external_configured_alias_retarget_cannot_hide_code_change(tmp_path):
    root,files,repos,scheduler,readers=fixture_inputs(tmp_path)
    old=tmp_path/'old-scripts';new=tmp_path/'new-scripts';old.mkdir();new.mkdir()
    (old/'create.py').write_text('# original executable\n',encoding='utf-8')
    (new/'create.py').write_text('# different executable\n',encoding='utf-8')
    alias=tmp_path/'external-alias'
    subprocess.run(['cmd','/c','mklink','/J',str(alias),str(old)],check=True,capture_output=True)
    try:
        files['zhaoshang_create']=alias/'create.py'
        manifest=api().propose_manifest(root,external_files=files,external_repositories=repos,
                                       scheduler_definitions=scheduler,**readers)
        manifest['approved']=True
        assert verify(root,manifest,scheduler,readers)['status']=='MATCH'
        os.rmdir(alias)
        subprocess.run(['cmd','/c','mklink','/J',str(alias),str(new)],check=True,capture_output=True)
        assert verify(root,manifest,scheduler,readers)['status']!='MATCH'
    finally:
        if alias.exists():os.rmdir(alias)

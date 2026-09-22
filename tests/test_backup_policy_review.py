"""Reproduce safety findings without touching C: or any network share."""
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.test_runtime_recovery import load_tool, make_sources, DAY

@pytest.mark.parametrize('raw',[r'\\localhost\C$\never-create',r'\\?\C:\never-create',r'\\.\C:\never-create'])
def test_backup_rejects_device_and_network_output_before_resolving(monkeypatch,raw):
    tool=load_tool()
    def forbidden_resolution(*args,**kwargs):
        raise AssertionError('Network/device output should be refused before filesystem access')
    monkeypatch.setattr(Path,'resolve',forbidden_resolution)
    with pytest.raises(ValueError):tool._new_destination(raw)

@pytest.mark.parametrize('name',['session.json','api_keys.txt','browser_state.json'])
def test_unknown_auth_artifact_names_are_not_copied(tmp_path,name):
    tool=load_tool();source=make_sources(tmp_path)
    (source/DAY/'BATCH_STATE.md').write_text('known evidence',encoding='utf-8')
    (source/DAY/name).write_text('synthetic authentication data',encoding='utf-8')
    target=tmp_path/'backup'
    manifest=tool.create_backup(source,DAY,target,include_artifacts=True)
    assert not (target/DAY/name).exists()
    assert [item['path'] for item in manifest['artifacts']]==[f'{DAY}/BATCH_STATE.md']


@pytest.mark.parametrize('relative',[f'{DAY}//final.xlsx',f'{DAY}/./final.xlsx'])
def test_manifest_members_must_use_one_canonical_path(tmp_path,relative):
    tool=load_tool()
    with pytest.raises(ValueError):
        tool._member(tmp_path,relative)

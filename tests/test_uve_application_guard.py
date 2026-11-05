import json
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
import pytest
from agent_training.manifest_loader import load_json_ref
from agent_training.uve.adapters import adb, application_health, ui_faults
from agent_training.uve import application_runtime, status_freshness
from agent_training.uve.bundle import load_manifest_bundle
from agent_training.uve.orchestrator import run_probe
from agent_training.uve.gate_evaluator import evaluate_gate

POLICY='config/uve/application_fault_policy.json'

def tree(text='Project ready', error=None, editable=False):
    root=ET.Element('hierarchy')
    ET.SubElement(root,'node',{'text':text,'class':'android.widget.TextView'})
    if error:
        ET.SubElement(root,'node',{'text':error,'class':'android.widget.EditText' if editable else 'android.widget.TextView'})
    return root

@pytest.mark.parametrize('error',['Render Error','Console Error','useTheme must be used within ThemeProvider',
                                'Unable to resolve module firebase/functions','Something went wrong'])
def test_visible_failure_beats_underlying_healthy_project(error):
    result=ui_faults.ui_fault_result(tree(error=error),POLICY)
    assert result['verdict']=='failed'
    assert result['failure_class']=='application_runtime_error'

def test_user_input_and_normal_validation_are_not_runtime_failures():
    assert ui_faults.ui_fault_result(tree(error='Render Error',editable=True),POLICY) is None
    assert ui_faults.ui_fault_result(tree(error='Password is required'),POLICY) is None

def test_focused_package_smoke_cannot_pass_a_redbox(tmp_path,monkeypatch):
    flow=tmp_path/'flow.json'
    flow.write_text(json.dumps({'steps':[{'kind':'assert_element','step_id':'project','selector':{'text':'Project ready'}}]}))
    monkeypatch.setattr(adb,'_read_ui_hierarchy',lambda *args:tree(error='Render Error'))
    result=adb._run_ui_flow({'timeout_seconds':1},str(flow),POLICY)
    assert result['verdict']=='failed'

def test_membership_probe_requires_its_own_flow(monkeypatch):
    monkeypatch.setattr(adb, 'load_json_ref', lambda _: {
        'adb_executable': 'fixture-adb', 'adb_serial': 'fixture-serial',
        'boot_property': 'sys.boot_completed', 'expected_value': '1', 'timeout_seconds': 1,
    })
    calls = []
    monkeypatch.setattr(adb, '_adb_run', lambda *args: calls.append(args))
    monkeypatch.setattr(adb, '_run_ui_flow', lambda *args: calls.append(args))
    result = adb.run_probe(probe_id='application_membership_ui_flow', ctx={
        'profile': {'adb_probe_ref': 'adb.json', 'ui_flow_ref': 'generic-smoke.json'},
    })
    assert result['verdict'] == 'not_verified'
    assert result['failure_class'] == 'binding_missing'
    assert not calls

@pytest.fixture
def health(tmp_path,monkeypatch):
    def document(name,doc):
        path=tmp_path/name; path.write_text(json.dumps(doc),encoding='utf-8'); return str(path)
    profile={'adb_probe_ref':document('adb.json',{'adb_executable':'selected-adb','adb_serial':'selected-device','timeout_seconds':1}),
      'guest_package_ref':document('package.json',{'package_substring':'org.example.app'}),
      'application_health_ref':document('health.json',{'fault_policy_ref':POLICY,'required_selectors':[{'text':'Project ready'}],
        'logcat_lookback_seconds':60,'logcat_max_lines':100})}
    state={'ui':tree(),'logs':'','commands':[]}
    def call(spec,args,timeout):
        assert spec['adb_serial']=='selected-device'
        state['commands'].append(args)
        if args[:3]==['shell','dumpsys','window']: output='mCurrentFocus=Window{ org.example.app/.Activity}'
        elif args[:2]==['shell','pidof']: output='42'
        elif args[:2]==['shell','date']: output='1000'
        elif args[0]=='logcat':
            assert '--pid=42' in args
            output=state['logs']
        else: raise AssertionError(args)
        return subprocess.CompletedProcess(args,0,output,'')
    monkeypatch.setattr(application_health,'_adb_run',call)
    monkeypatch.setattr(application_health,'_read_ui_hierarchy',lambda *args:state['ui'])
    return profile,state

def test_actual_project_and_pid_scoped_logs_required_for_health(health):
    profile,state=health
    result=application_health.run_probe(probe_id='application_health',ctx={'profile':profile})
    assert result['verdict']=='passed' and result['runtime_delivery_binding_sha256']
    assert any(args[0]=='logcat' for args in state['commands'])

def test_recent_fatal_log_fails_but_historical_log_does_not(health):
    profile,state=health
    state['logs']='999.000 42 42 E ReactNativeJS: ReferenceError: missing is not defined'
    result=application_health.run_probe(probe_id='application_health',ctx={'profile':profile})
    assert result['verdict']=='failed' and 'missing' not in json.dumps(result)
    state['logs']='100.000 42 42 E ReactNativeJS: ReferenceError: old failure'
    assert application_health.run_probe(probe_id='application_health',ctx={'profile':profile})['verdict']=='passed'

def test_wrong_expo_project_does_not_pass(health):
    profile,state=health; state['ui']=tree('Another project')
    assert application_health.run_probe(probe_id='application_health',ctx={'profile':profile})['verdict']=='not_verified'

def test_crash_appearing_during_observation_wins(health,monkeypatch):
    profile,_=health; snapshots=iter([tree(),tree(error='Render Error')])
    monkeypatch.setattr(application_health,'_read_ui_hierarchy',lambda *args:next(snapshots))
    assert application_health.run_probe(probe_id='application_health',ctx={'profile':profile})['verdict']=='failed'

@pytest.mark.parametrize('change',['stopped','dead_worker','stale_heartbeat','stale_code'])
def test_status_never_promotes_old_approval(change,monkeypatch):
    class Process:
        def __init__(self,pid):self.pid=pid
        def alive(self):return not(change=='dead_worker' and self.pid==2)
        def close(self):pass
    monkeypatch.setattr(status_freshness,'ParentProcess',Process)
    monkeypatch.setattr(status_freshness,'validate_stored_gate',lambda gate:change!='stale_code')
    status={'state':'stopped' if change=='stopped' else 'running','owner_pid':1,'worker_pid':2,
            'updated_at':0 if change=='stale_heartbeat' else time.time(),'tiers':[{'gate':{'decision':'approved'}}],
            'application':{'decision':'approved','gate':{'decision':'approved'}}}
    result=status_freshness.current_runtime_status(status,{'status_stale_after_seconds':10})
    assert result['application']['decision']=='not_verified'
    if change!='stale_code':assert not result['tiers']

def test_real_code_mutation_invalidates_cached_success_and_rejects_gate(tmp_path,monkeypatch):
    node=shutil.which('node')
    assert node, 'Node is required for the JavaScript fault-injection acceptance'
    source=tmp_path/'screen.js'; source.write_text('const value = 1;\n')
    root_doc=tmp_path/'root.json'; root_doc.write_text(json.dumps({'root_env':'UVE_GUARD_TEST_ROOT'}))
    monkeypatch.setenv('UVE_GUARD_TEST_ROOT',str(tmp_path))
    target=tmp_path/'code.json'
    target.write_text(json.dumps({'argv':[node,'--check',str(source)],'timeout_seconds':10,'expected_returncode':0}))
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps({'project_id':'code_fault_injection','environment':'test','project_root_ref':str(root_doc),
      'identity':{'watch_globs':['*.js'],'ignore_globs':[]},'execution_binding_profile_id':'application_acceptance',
      'command_probe_refs':{'application_code':str(target),'application_bundle':str(target)}}))
    from agent_training.uve import orchestrator
    assert run_probe('config/uve/profiles/emulator_workstation_smoke.json', 'engine_self_check')['verdict'] == 'passed'
    original=orchestrator._invoke_adapter
    def invoke(adapter,probe,ctx):
        if adapter=='application_health':return {'verdict':'passed','fault_origin':'none','runtime_delivery_binding_sha256':'fixture-transport-binding'}
        if probe=='application_membership_ui_flow':
            return {'verdict':'passed','ui_steps':[{'step_id':'fixture_feature_assertion','ok':True}]}
        return original(adapter,probe,ctx)
    monkeypatch.setattr(orchestrator,'_invoke_adapter',invoke)
    spec={'application':{'probe_ids':['application_code','application_bundle','application_health','application_membership_ui_flow'],
      'code_gate_id':'application_code_gate','gate_id':'application_acceptance'},'runner_role_id':'uve_runner','gate_evaluator_ref':'emulator_runtime_witness'}
    cache={}
    first=application_runtime.run_application_cycle(spec,str(profile),cache,['fixture_author'])
    assert first['decision']=='approved'
    first_code=cache['code']['probe_run_id']
    source.write_text('const broken = ;\n')
    broken=application_runtime.run_application_cycle(spec,str(profile),cache,['fixture_author'])
    assert broken['decision']=='rejected'
    assert broken['probes'][0]['verdict']=='failed' and broken['probes'][0]['probe_run_id']!=first_code
    source.write_text('const restored = 1;\n')
    assert application_runtime.run_application_cycle(spec,str(profile),cache,['fixture_author'])['decision']=='approved'

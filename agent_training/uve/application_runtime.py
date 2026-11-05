from __future__ import annotations
import json
import os
from pathlib import Path
from agent_training.manifest_loader import ManifestError, load_json_ref
from agent_training.uve.bundle import load_manifest_bundle, resolve_log_dir, write_json_atomic
from agent_training.uve.gate_evaluator import evaluate_gate
from agent_training.uve.orchestrator import run_probe


def materialize_application_profile(runtime_spec, context_path, parent_pid):
    context = json.loads(context_path.read_text(encoding='utf-8'))
    if str(context.get('owner_pid')) != str(parent_pid):
        raise ManifestError('application context belongs to another owner')
    app_spec = runtime_spec['application']
    raw_root = context.get('project_root')
    if not raw_root or not Path(raw_root).is_dir():
        return None
    root = Path(raw_root).resolve()
    contract_path = (root / app_spec['contract_relative']).resolve()
    contract_path.relative_to(root)
    if not contract_path.is_file():
        return None
    contract = load_json_ref(str(contract_path))
    if contract.get('schema_version') != 'uve_application_contract.v1' or not contract['health'].get('required_selectors'):
        raise ManifestError('application contract requires positive project assertions')
    directory = resolve_log_dir() / runtime_spec['generated_relative'] / str(parent_pid) / 'application'
    directory.mkdir(parents=True, exist_ok=True)
    root_doc = directory / 'root.json'
    write_changed(root_doc, {'context_ref': str(context_path), 'root_field': 'project_root', 'owner_pid': parent_pid})
    runtime_directory = directory.parent
    runtime_profile = load_json_ref(str(runtime_directory / runtime_spec['profile_filename']))
    code = dict(contract['code_probe'])
    bundle = dict(contract['bundle_probe'])
    bindings = directory / 'bindings.json'
    write_changed(bindings, {'runtime_port': int(context.get('dev_server_port') or 0)})
    bundle.update(argument_values_ref=str(bindings), required_argument_fields=['runtime_port'])
    for name, doc in [('code.json', code), ('bundle.json', bundle), ('health.json',
                     {**contract['health'], 'fault_policy_ref': app_spec['fault_policy_ref']})]:
        write_changed(directory / name, doc)
    membership_flow_ref = None
    membership_block = contract.get('membership_ui_flow') or {}
    generated_rel = membership_block.get('generatedFlowRelativePath')
    if generated_rel:
        generated_path = (root / generated_rel).resolve()
        generated_path.relative_to(root)
        if generated_path.is_file():
            flow_doc = load_json_ref(str(generated_path))
            membership_path = directory / 'membership_ui_flow.json'
            fault_ref = membership_block.get('fault_policy_ref') or app_spec['fault_policy_ref']
            write_changed(membership_path, {**flow_doc, 'fault_policy_ref': fault_ref})
            membership_flow_ref = str(membership_path)
    profile = {'schema_version': 'uve_project_profile.v1', 'project_id': contract['project_id'],
               'environment': contract['environment'], 'project_root_ref': str(root_doc),
               'identity': contract['identity'], 'execution_binding_profile_id': app_spec['execution_binding_profile_id'],
               'application_contract_ref': str(contract_path), 'runtime_context_ref': str(context_path),
               'adb_probe_ref': runtime_profile['adb_probe_ref'], 'guest_package_ref': runtime_profile['guest_package_ref'],
               'command_probe_refs': {'application_code': str(directory / 'code.json'), 'application_bundle': str(directory / 'bundle.json')},
               'application_health_ref': str(directory / 'health.json'),
               'lease_requirements': {'application_health': {'resource_kind': 'emulator_serial', 'serial_ref': runtime_profile['adb_probe_ref']}}}
    if membership_flow_ref:
        profile['membership_ui_flow_ref'] = membership_flow_ref
        profile['lease_requirements']['application_membership_ui_flow'] = {
            'resource_kind': 'emulator_serial',
            'serial_ref': runtime_profile['adb_probe_ref'],
        }
    profile_path = directory / 'profile.json'
    write_changed(profile_path, profile)
    return str(profile_path)


def write_changed(path, doc):
    if path.is_file() and json.loads(path.read_text(encoding='utf-8')) == doc:
        return
    write_json_atomic(path, doc)


def run_application_cycle(runtime_spec, profile_ref, cache, repair_actor_refs=None):
    app_spec = runtime_spec['application']
    bundle = load_manifest_bundle()['manifest_bundle_sha256']
    code = cache.get('code')
    if code:
        fresh = evaluate_gate(app_spec['code_gate_id'], [code], manifest_bundle_sha256=bundle,
                              gate_evaluator_ref=runtime_spec['gate_evaluator_ref'], repair_actor_refs=repair_actor_refs)
        if fresh['decision'] != 'approved':
            code = None
    if code is None:
        code = run_probe(profile_ref, 'application_code', role_id=runtime_spec['runner_role_id'], repair_actor_refs=repair_actor_refs)
        # Cache only proven success, never an unavailable or failed check.
        cache['code'] = code if code['verdict'] == 'passed' else None
    receipts = [code] + [run_probe(profile_ref, probe, role_id=runtime_spec['runner_role_id'], repair_actor_refs=repair_actor_refs)
                         for probe in app_spec['probe_ids'] if probe != 'application_code']
    gate = evaluate_gate(app_spec['gate_id'], receipts, manifest_bundle_sha256=bundle,
                         gate_evaluator_ref=runtime_spec['gate_evaluator_ref'], repair_actor_refs=repair_actor_refs)
    gate_path = resolve_log_dir() / 'gates' / (gate['trust_chain_ref'] + '.json')
    write_json_atomic(gate_path, gate)
    return {'decision': gate['decision'], 'profile_ref': profile_ref, 'gate': gate, 'gate_receipt_path': str(gate_path),
            'probes': [{'probe_id': receipt['probe']['probe_id'], 'probe_run_id': receipt['probe_run_id'],
                        'verdict': receipt['verdict'], 'failure_class': receipt.get('failure_class'),
                        'observation': receipt['observation'],
                        'receipt_path': str(resolve_log_dir() / 'probes' / (receipt['probe_run_id'] + '.json'))} for receipt in receipts]}

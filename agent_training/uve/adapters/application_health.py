from __future__ import annotations
import hashlib
import re
from agent_training.manifest_loader import load_json_ref
from agent_training.content_hash import content_hash
from agent_training.uve.adapters.adb import _adb_run, _read_ui_hierarchy
from agent_training.uve.adapters.ui_faults import ui_fault_result


def run_probe(*, probe_id, ctx):
    profile = ctx['profile']
    spec = load_json_ref(profile['adb_probe_ref'])
    health = load_json_ref(profile['application_health_ref'])
    package = load_json_ref(profile['guest_package_ref'])['package_substring']
    if not spec.get('adb_serial') or not spec.get('adb_executable') or not package:
        return {'verdict': 'not_verified', 'fault_origin': 'unknown', 'reason': 'missing selected application or device'}
    if not re.fullmatch(r'[A-Za-z0-9_.]+', package):
        raise ValueError('invalid selected Android package')
    timeout = spec['timeout_seconds']
    focused = _adb_run(spec, ['shell', 'dumpsys', 'window'], timeout)
    focus = '\n'.join(line for line in focused.stdout.splitlines() if 'mCurrentFocus' in line or 'mFocusedApp' in line)
    if focused.returncode or not re.search(r'(?<![\w.])' + re.escape(package) + r'/', focus):
        return {'verdict': 'not_verified', 'fault_origin': 'unknown', 'reason': 'selected application is not focused'}
    tree = _read_ui_hierarchy(spec, timeout)
    error = ui_fault_result(tree, health['fault_policy_ref'])
    if error:
        return error
    for selector in health.get('failure_selectors', []):
        if any(all(node.get(k) == v for k, v in selector.items()) for node in tree.iter('node')):
            return {'verdict': 'failed', 'fault_origin': 'subject_fault', 'failure_class': 'application_runtime_error', 'fault_rule': 'application_fallback'}
    for selector in health['required_selectors']:
        if not selector or not any(all(node.get(k) == v for k, v in selector.items()) for node in tree.iter('node')):
            return {'verdict': 'not_verified', 'fault_origin': 'unknown', 'reason': 'intended project is not rendered', 'missing_selector': selector}
    processes = _adb_run(spec, ['shell', 'pidof', package], timeout)
    pids = processes.stdout.split()
    if processes.returncode or not pids or not all(pid.isdecimal() for pid in pids):
        return {'verdict': 'not_verified', 'fault_origin': 'unknown', 'reason': 'selected application PID unavailable'}
    clock = _adb_run(spec, ['shell', 'date', '+%s'], timeout)
    if clock.returncode:
        raise ValueError('device clock unavailable')
    since = float(clock.stdout.strip()) - health['logcat_lookback_seconds']
    policy = load_json_ref(health['fault_policy_ref'])
    for pid in sorted(set(pids)):
        logs = _adb_run(spec, ['logcat', '-d', '-v', 'epoch', '--pid=' + pid, '-t', str(health['logcat_max_lines'])], timeout)
        if logs.returncode:
            raise ValueError('selected process logcat unavailable')
        for line in logs.stdout.splitlines():
            timestamp = re.match(r'^\s*(\d+\.\d+)\s', line)
            if not timestamp or float(timestamp[1]) < since:
                continue
            for rule in policy['log_rules']:
                if re.search(rule['pattern'], line, re.IGNORECASE):
                    return {'verdict': 'failed', 'fault_origin': 'subject_fault', 'failure_class': 'application_runtime_error',
                            'fault_rule': rule['id'], 'evidence_sha256': hashlib.sha256(line.encode()).hexdigest()}
    # A failure that appeared while reading logs must beat the earlier positive marker.
    error = ui_fault_result(_read_ui_hierarchy(spec, timeout), health['fault_policy_ref'])
    if error:
        return error
    return {'verdict': 'passed', 'fault_origin': 'none', 'outcome_token': 'application_runtime_healthy',
            'runtime_delivery_binding_sha256': content_hash({'package': package, 'pids': pids, 'selectors': health['required_selectors']})}

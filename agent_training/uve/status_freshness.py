from __future__ import annotations
import time
from agent_training.uve.runtime import ParentProcess
from agent_training.uve.gate_evaluator import validate_stored_gate


def current_runtime_status(status, policy):
    reason = None
    if status.get('state') != 'running':
        reason = 'verification worker is not running a completed cycle'
    elif time.time() - float(status.get('updated_at', 0)) > policy['status_stale_after_seconds']:
        reason = 'verification heartbeat is stale'
    else:
        for key in ('owner_pid', 'worker_pid'):
            process = ParentProcess(int(status.get(key, 0)))
            try:
                if not process.alive():
                    reason = key + ' is no longer alive'
                    break
            finally:
                process.close()
    if reason:
        return {**status, 'state': 'not_verified', 'reason': reason, 'tiers': [],
                'application': {'decision': 'not_verified', 'reason': reason}}
    app = status.get('application', {})
    if app.get('decision') == 'approved' and not validate_stored_gate(app.get('gate', {})):
        status = {**status, 'application': {'decision': 'not_verified', 'reason': 'application evidence no longer matches current code or context',
                                          'historical_gate_receipt_path': app.get('gate_receipt_path')}}
    return status

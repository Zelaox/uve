from __future__ import annotations
import re
from typing import Any
from agent_training.manifest_loader import load_json_ref


def detect_ui_fault(tree, policy: dict[str, Any]) -> str | None:
    for node in tree.iter('node'):
        if 'EditText' in node.get('class', ''):
            continue
        for rule in policy['ui_rules']:
            if any(re.search(rule['pattern'], node.get(attribute, ''), re.IGNORECASE)
                   for attribute in rule['attributes']):
                return rule['id']
    return None


def ui_fault_result(tree, policy_ref: str) -> dict | None:
    fault = detect_ui_fault(tree, load_json_ref(policy_ref))
    if fault:
        return {'verdict': 'failed', 'fault_origin': 'subject_fault',
                'failure_class': 'application_runtime_error', 'fault_rule': fault}
    return None

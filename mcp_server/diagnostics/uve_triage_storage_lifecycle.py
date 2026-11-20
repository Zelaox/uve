"""Lifecycle policy and audit for the manifest-bound Lab corpus."""
from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path

from .uve_triage_storage_lease import lease
from .uve_triage_storage_activity import approve_return, record_activity, refresh_state
from .uve_triage_storage_execute import compact, quarantine
from .uve_triage_storage_purge import plan as purge_plan, purge
from .uve_triage_storage_scan import audit_scan
from .uve_triage_store import json_text, load_config, open_store, sha256, transaction, utc_now


def policies(config):
    manifest = config.policy('storage_lifecycle_ref')
    bound = {key.removesuffix('_ref'): config.document(value)
             for key, value in manifest.items() if key.endswith('_ref')}
    bound['manifest'] = manifest
    bound['triage_retention'] = config.policy('retention_ref')
    bound['proof_policy'] = config.policy('proof_ref')
    return bound, sha256(json_text(bound).encode())


def assert_current_policy(config, expected_digest):
    _, actual = policies(load_config(config.root))
    if actual != expected_digest:
        raise ValueError('Lifecycle policy changed after the operation was prepared')


def main(config, argv):
    parser = argparse.ArgumentParser(prog='uve storage', description=__doc__)
    policy, digest = policies(config)
    operations = parser.add_subparsers(dest='operation', required=True)
    operations.add_parser('config')
    execute = operations.add_parser('execute')
    execute.add_argument('--mode', required=True, choices=['quarantine', 'purge', 'compact_sqlite'])
    execute.add_argument('--confirm-manifest-sha256', required=True)
    execute.add_argument('--inventory-run-id')
    execute.add_argument('--plan-sha256')
    execute.add_argument('--approval-id')
    execute.add_argument('--gate-receipt', type=Path)
    operations.add_parser('inactivity').add_argument('action', choices=['status'])
    operations.add_parser('activity').add_argument('action', choices=['ack'])
    operations.add_parser('return').add_argument('action', choices=['reconcile'])
    approval = operations.add_parser('purge')
    approval.add_argument('action', choices=['approve'])
    approval.add_argument('--inventory-run-id', required=True)
    approval.add_argument('--confirm-manifest-sha256', required=True)
    approval.add_argument('--approver-ref', required=True)
    for name in ('scan', 'plan'):
        scan = operations.add_parser(name)
        modes = [policy['manifest']['default_mode']]
        if name == 'plan':
            modes.append('purge')
            scan.add_argument('--inventory-run-id')
            scan.add_argument('--plan-sha256')
            scan.add_argument('--approval-id')
            scan.add_argument('--confirm-manifest-sha256')
        scan.add_argument('--mode', choices=modes, default=policy['manifest']['default_mode'])
        scan.add_argument('--actor-class', default=config.policy('ingest_ref')['default_actor_class'])
    health = operations.add_parser('health')
    health.add_argument('--write-snapshot', action='store_true')
    health.add_argument('--actor-class', default=config.policy('ingest_ref')['default_actor_class'])
    leases = operations.add_parser('lease').add_subparsers(dest='lease_operation', required=True)
    acquire = leases.add_parser('acquire')
    acquire.add_argument('--work-root', required=True)
    acquire.add_argument('--actor-ref', required=True)
    acquire.add_argument('--pid', type=int, default=os.getppid())
    acquire.add_argument('--linked-run-id')
    for name in ('heartbeat', 'release'):
        leases.add_parser(name).add_argument('--lease-id', required=True)
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(utc_now())
    actor = config.policy('ingest_ref')['default_actor_class']
    activity = policy['activity']
    try:
        if args.operation == 'config':
            result = {'manifest_sha256': digest, 'enabled': policy['manifest']['enabled'],
                      'default_mode': policy['manifest']['default_mode']}
        elif args.operation == 'lease':
            result = lease(config, policy, args.lease_operation, lease_id=getattr(args, 'lease_id', None),
                           work_root=getattr(args, 'work_root', None), actor=getattr(args, 'actor_ref', None),
                           pid=getattr(args, 'pid', None), linked_run=getattr(args, 'linked_run_id', None))
        elif args.operation == 'execute':
            if args.confirm_manifest_sha256 != digest:
                raise ValueError('Confirm the current lifecycle manifest hash')
            if args.mode == 'quarantine':
                result = quarantine(config, policy, digest, now, args.inventory_run_id, args.plan_sha256,
                                    args.approval_id, actor)
            elif args.mode == 'purge':
                result = purge(config, policy, digest, now, args.inventory_run_id, args.plan_sha256,
                               args.approval_id, args.gate_receipt, actor)
            else:
                result = compact(config, policy, digest, now, args.approval_id, actor)
        elif args.operation == 'plan' and args.mode == 'purge':
            if args.confirm_manifest_sha256 != digest:
                raise ValueError('Confirm the current lifecycle manifest hash')
            with open_store(config, readonly=True) as db:
                db.execute('BEGIN')
                result = purge_plan(db, config, policy, digest, now, args.inventory_run_id, args.plan_sha256, args.approval_id)
            directory = config.path(policy['verification']['request_output_relative'])
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (result['purge_plan_sha256'].split(':')[1] + '.json')
            path = config.path(path.relative_to(config.root))
            path.write_text(json_text(result), encoding='utf-8')
            result['request_path'] = str(path)
        elif args.operation in ('activity', 'inactivity', 'return', 'purge'):
            with open_store(config) as db, transaction(db):
                if args.operation == 'activity':
                    result = record_activity(db, policy, digest, now, activity['operator_ack_kind'], actor, 'cli')
                elif args.operation == 'inactivity':
                    result = refresh_state(db, policy, digest, now)
                elif args.operation == 'purge':
                    if args.confirm_manifest_sha256 != digest:
                        raise ValueError('Confirm the current lifecycle manifest hash')
                    result = approve_return(db, policy, digest, now, args.inventory_run_id, args.approver_ref)
                else:
                    state = refresh_state(db, policy, digest, now)
                    if state['paused'] or not state['post_return_reconcile_required']:
                        raise ValueError('Return reconciliation requires current Lab activity after a long absence')
                    result = audit_scan(db, config, policy, digest, actor, now,
                                        reconciliation_kind=policy['return_reconciliation']['reconciliation_kind'])
                    if not result['aborted']:
                        db.execute('UPDATE inactivity_protection_state SET last_post_return_inventory_run_id=? WHERE state_id=?',
                                   (result['cleanup_run_id'], state['state_id']))
        elif args.operation == 'health' and not args.write_snapshot:
            with open_store(config, readonly=True) as db:
                record = db.execute('SELECT * FROM storage_health_snapshots ORDER BY captured_at DESC LIMIT 1').fetchone()
                result = dict(record) if record else {'status': 'not_verified'}
        else:
            if args.actor_class not in config.policy('ingest_ref')['actor_classes']:
                raise ValueError('Unknown activity actor class')
            with open_store(config) as db, transaction(db):
                result = audit_scan(db, config, policy, digest, args.actor_class, now)
                if args.actor_class == activity['maintenance_actor_class']:
                    key = 'scheduled_health_kind' if args.operation == 'health' else 'scheduled_scan_kind'
                    record_activity(db, policy, digest, now, activity[key], args.actor_class, result['cleanup_run_id'])
                else:
                    refresh_state(db, policy, digest, now)
    except (OSError, ValueError) as error:
        parser.exit(2, f'{error}\n')
    print(json_text(result))
    return int(bool(result.get('aborted')))

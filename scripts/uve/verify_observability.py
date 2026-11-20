"""Run one implementation slice using the Lab-bound UVE engine."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


def unobserved_directories(root: Path, patterns: list[str]) -> list[str]:
    """Prune inventory traversal outside the fixed prefixes of the watch globs."""
    prefixes = []
    for pattern in patterns:
        parts = pattern.split('/')[:-1]
        prefix = []
        for part in parts:
            if any(mark in part for mark in '*?['):
                break
            prefix.append(part)
        prefixes.append(tuple(prefix))

    def walk(directory: Path, relative: tuple[str, ...]):
        if relative in prefixes and not any(item[:len(relative)] == relative and len(item) > len(relative) for item in prefixes):
            # A recursive watch below this prefix needs its descendants.
            if any(pattern.startswith('/'.join(relative) + '/**') for pattern in patterns):
                return
        for child in directory.iterdir():
            if not child.is_dir():
                continue
            target = (*relative, child.name)
            if any(prefix[:len(target)] == target for prefix in prefixes):
                yield from walk(child, target)
            elif not any(pattern.startswith('/'.join(relative) + '/**') for pattern in patterns):
                yield '/'.join(target) + '/**'

    return list(walk(root, ()))


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    config = json.loads((root / 'config/uve_triage_verification_manifest.json').read_text(encoding='utf-8'))
    binding = json.loads((root / config['binding_ref']).read_text(encoding='utf-8'))
    requested = sys.argv[1:]
    if not requested or any(slice_id not in config['slices'] for slice_id in requested):
        raise SystemExit('Supply a slice ID from the verification manifest')
    pipeline = root / binding['training_pipeline_relative_from_repo']
    out = root / config['ledger_relative']
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, binding['lab_root_env_key']: str(root), 'PYTHONUTF8': '1', 'PYTHONPATH': str(root)}
    watch = config['watch_globs'] + config.get('additional_watch_globs', [])
    native_identity = json.loads((pipeline / config['pipeline_identity_manifest_ref']).read_text(encoding='utf-8'))
    native_ignores = [(Path(binding['training_pipeline_relative_from_repo']) / pattern).as_posix()
                      for pattern in native_identity['identity_ignore_globs']]
    identity = {'watch_globs': watch,
                'ignore_globs': config['ignore_globs'] + native_ignores + unobserved_directories(root, watch)}
    for slice_id in requested:
        program = slice_id == config['program_acceptance_id']
        attempt = uuid.uuid4().hex
        target = out / f'{slice_id}-{attempt}-command.json'
        profile = out / f'{slice_id}-{attempt}-profile.json'
        fixture_root = root / config['program_fixture_root_relative'] if program else out
        tests = [config['test_relative'], *config['additional_test_paths']] if program else [config['test_relative']]
        target.write_text(json.dumps({
            'schema_version': 'uve_command_probe.v1',
            'argv': ['{python}', '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--basetemp',
                     str(fixture_root / f'.pytest-uve-{attempt}'), *tests, '-k', 'test_' if program else f'test_{slice_id.lower()}_'],
            'timeout_seconds': config['timeout_seconds'], 'expected_returncode': 0,
        }), encoding='utf-8')
        profile.write_text(json.dumps({
            'schema_version': 'uve_project_profile.v1', 'project_id': 'ai_lab_uve_observability',
            'environment': 'isolated_fixture', 'project_root_ref': config['project_root_ref'],
            'identity': identity,
            'command_probe_refs': {config['probe_id']: str(target)}, 'sync_issues_on_failure': False,
        }), encoding='utf-8')
        process = subprocess.run([sys.executable, '-B', '-m', 'agent_training.uve.cli', 'probe', 'run',
                                  '--profile', str(profile), '--probe', config['probe_id'],
                                  '--repair-actor-ref', config['actor_ref']], cwd=pipeline,
                                 env=env, capture_output=True, encoding='utf-8', check=False)
        if not process.stdout:
            print(process.stderr, file=sys.stderr)
            return process.returncode or 2
        receipt = json.loads(process.stdout)
        result = {'slice': slice_id, 'probe_run_id': receipt['probe_run_id'], 'verdict': receipt['verdict'],
                  'failure_class': receipt.get('failure_class'), 'profile': str(profile),
                  'subject_identity': receipt['subject_identity']['identity_sha256']}
        (out / f'{slice_id}-{attempt}.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result), flush=True)
        if receipt['verdict'] != 'passed':
            print(json.dumps(receipt['observation'], indent=2), file=sys.stderr)
            return process.returncode or 2
        if program:
            evaluated = subprocess.run(
                [sys.executable, '-B', '-m', 'agent_training.uve.cli', 'gate', 'evaluate',
                 '--gate', config['program_gate_id'], '--probe-run-id', receipt['probe_run_id'],
                 '--evaluator-ref', config['gate_evaluator_ref']], cwd=pipeline, env=env,
                capture_output=True, encoding='utf-8', check=False)
            gate = json.loads(evaluated.stdout)
            result['gate_ref'] = gate['trust_chain_ref']
            result['gate_decision'] = gate['decision']
            (out / f'{slice_id}-{attempt}.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps({'gate_ref': gate['trust_chain_ref'], 'decision': gate['decision']}), flush=True)
            if evaluated.returncode:
                return evaluated.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

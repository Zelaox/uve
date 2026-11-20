"""Export UVE and its standalone TRIAGE components without live project data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

PRIVATE_PARTS = {'protrainer', 'deep_protrainer_comments', 'deep_protrainer_video'}
EXCLUDED_DIRS = {'logs', 'artifacts', '__pycache__', '.git', '.pytest_cache'}
DROP = object()

def public_manifest(value, baseline=None):
    if isinstance(value, dict):
        if any('protrainer' in str(value.get(key, '')).casefold() for key in ('id', 'project_id')):
            return DROP
        for key, child in value.items():
            previous = baseline.get(key) if isinstance(baseline, dict) else None
            if key.endswith('_ref') and isinstance(child, str) and 'protrainer' in child.casefold() and not isinstance(previous, str):
                return DROP
        result = {}
        for key, child in value.items():
            if 'protrainer' in key.casefold():
                continue
            previous = baseline.get(key) if isinstance(baseline, dict) else None
            cleaned = public_manifest(child, previous)
            if cleaned is not DROP:
                result[key] = cleaned
        if isinstance(baseline, dict):
            for key, child in baseline.items():
                if 'android_example' in key and key not in result:
                    result[key] = child
        return result
    if isinstance(value, list):
        return [cleaned for child in value if (cleaned := public_manifest(child)) is not DROP]
    if isinstance(value, str) and 'protrainer' in value.casefold():
        return baseline if isinstance(baseline, str) and 'protrainer' not in baseline.casefold() else DROP
    return value

def excluded(path: Path) -> bool:
    return any(part.casefold() in PRIVATE_PARTS or part in EXCLUDED_DIRS for part in path.parts) or 'protrainer' in path.name.casefold() or path.name == 'uve_engine_state.json'

def export(candidate: Path, lab: Path, destination: Path) -> dict:
    copied = []
    # Published integrations are an allowlist. Host-only products require an
    # explicit public configuration change before a sync may include them.
    published = {
        path.relative_to(destination).as_posix(): json.loads(path.read_text(encoding='utf-8-sig'))
        for path in (destination / 'config').rglob('*.json')
        if path.name != 'uve_engine_state.json'
    }
    catalog_keys = {
        'config/lab_uve_classification_map.json': ('mappings', 'lab_finding_code'),
        'config/lab_uve_next_step_catalog.json': ('steps', 'step_id'),
        'config/lab_uve_reducer_catalog.json': ('reducers', 'reducer_id'),
        'config/uve_triage_adapter_manifest.json': ('uve_receipt_project_ids', None),
    }
    def copy(source: Path, relative: str | Path):
        relative = Path(relative)
        if excluded(relative):
            return
        ref = relative.as_posix()
        if ref.startswith('config/uve/profiles/'):
            return
        if (ref.startswith('config/uve/targets/') or (ref.startswith('config/lab_uve_') and relative.suffix == '.json')) and ref not in published:
            return
        target = (destination / relative).resolve()
        if not target.is_relative_to(destination):
            raise ValueError('Export target escapes destination')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if ref in published:
            baseline = published[ref]
            if ref in catalog_keys:
                field, identity = catalog_keys[ref]
                data = json.loads(target.read_text(encoding='utf-8-sig'))
                allowed = {row[identity] if identity else row for row in baseline[field]}
                data[field] = [row for row in data[field] if (row[identity] if identity else row) in allowed]
                target.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
            elif ref == 'config/lab_uve_uve_binding.json':
                data = json.loads(target.read_text(encoding='utf-8-sig'))
                data = {key: value for key, value in data.items() if not key.endswith('_binding_ref') or key in baseline}
                target.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
            elif ref == 'config/uve/uve_gate_manifest.json':
                data = json.loads(target.read_text(encoding='utf-8-sig'))
                data['gates'] = {key: value for key, value in data['gates'].items() if key in baseline['gates']}
                target.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        copied.append(relative.as_posix())
    for directory in ('agent_training/uve', 'agent_training/lab', 'config/uve'):
        for source in sorted((candidate / directory).rglob('*')):
            relative = source.relative_to(candidate)
            if source.is_file() and not excluded(relative):
                copy(source, relative)
    for name in ('issue_store.py', 'manifest_loader.py', 'paths.py', 'content_hash.py'):
        copy(candidate / 'agent_training' / name, 'agent_training/' + name)
    copy(candidate / 'config/manifests/triage_rules_manifest.json', 'config/manifests/triage_rules_manifest.json')
    # Keep the portable guide: the host's guide references private product trees.
    # Reconcile documentation separately when the exported implementation changes.
    for source in sorted((candidate / 'tests').glob('test_uve*.py')):
        # Keep public replacements for tests that require private application fixtures.
        if 'protrainer' not in source.read_text(encoding='utf-8').casefold():
            copy(source, 'tests/' + source.name)
    for name in ('build_probe.py', 'state_probe.cpp'):
        source = candidate / 'fixtures/workstation' / name
        if source.is_file():
            copy(source, 'fixtures/workstation/' + name)
    source = candidate / 'fixtures/prepare_reference_sources.py'
    if source.is_file():
        copy(source, 'fixtures/prepare_reference_sources.py')

    # Portable native-engine wrappers are maintained here, independently of host products.
    for pattern in ('uve_*.py', 'lab_uve_*.py', 'incident_triage.py'):
        for source in sorted((lab / 'mcp_server/diagnostics').glob(pattern)):
            copy(source, source.relative_to(lab))
    # Importing the full AI Lab server is not required by these standalone modules.
    for relative in ('mcp_server/__init__.py', 'mcp_server/diagnostics/__init__.py', 'scripts/debug/__init__.py'):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text('"""Standalone UVE/TRIAGE support namespace."""\n', encoding='utf-8')
    for name in ('triage_incident_bundle.py', 'triage_turn.py', 'triage_engine_session.py', 'validate_lab_uve_incident_bundle.py', 'validate_engine_uve.py', 'inventory_uve_triage_sources.py'):
        source = lab / 'scripts/debug' / name
        copy(source, source.relative_to(lab))
    for pattern in ('lab_uve_*.json', 'uve_triage*.json', 'uve_storage*.json'):
        for source in sorted((lab / 'config').glob(pattern)):
            copy(source, source.relative_to(lab))
    for source in sorted((lab / 'scripts/uve').glob('*')):
        if source.suffix in ('.py', '.ps1'):
            copy(source, source.relative_to(lab))
    for relative in ('config/engine_triage_policy.json', 'config/engine_triage_session_report.json', 'config/uve/uve_triage_storage_schema.sql'):
        source = lab / relative
        if source.is_file():
            copy(source, relative)
    for source in sorted((lab / 'config/uve/uve_triage_migrations').glob('*.sql')):
        copy(source, source.relative_to(lab))
    # Close manifest references, never by copying runtime data or the whole Lab config.
    pending = list((destination / 'config').glob('lab_uve_*.json')) + list((destination / 'config').glob('uve_triage*.json')) + list((destination / 'config').glob('uve_storage*.json'))
    visited = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        document = json.loads(path.read_text(encoding='utf-8-sig'))
        def walk(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if isinstance(child, str) and child.startswith('config/') and child.endswith(('.json','.sql','.txt')):
                        source = lab / child
                        if source.is_file() and not (destination / child).exists():
                            copy(source, child)
                            if child.endswith('.json') and (destination / child).exists():
                                pending.append(destination / child)
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(document)
    for name in ('lab_uve_uve_binding.json', 'lab_uve_engine_binding.json'):
        path = destination / 'config' / name
        data = json.loads(path.read_text(encoding='utf-8'))
        for key in ('training_pipeline_relative_from_repo', 'uve_training_pipeline_relative_from_lab'):
            if key in data:
                data[key] = '.'
        path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    for path in sorted((destination / 'config/uve').rglob('*.json')):
        relative = path.relative_to(destination).as_posix()
        baseline = published.get(relative)
        cleaned = public_manifest(json.loads(path.read_text(encoding='utf-8')), baseline)
        if cleaned is not DROP:
            path.write_text(json.dumps(cleaned, indent=2) + '\n', encoding='utf-8')
    for relative in ('tests/debug/test_lab_uve_incident_bundle.py', 'tests/debug/test_lab_uve_bridge_manifest.py', 'tests/debug/test_incident_triage_contract.py', 'tests/debug/test_uve_triage_observability.py'):
        if not (destination / relative).exists():
            copy(lab / relative, relative)
    return {'copied_files': sorted(set(copied)), 'copied_count': len(set(copied)), 'excluded': ['live logs, receipts and databases', 'application-specific ProTrainer trees', 'models, datasets and user files', 'dashboard-dependent AI Lab reducers']}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-root', type=Path, required=True)
    parser.add_argument('--lab-root', type=Path, required=True)
    parser.add_argument('--destination-root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = export(args.candidate_root.resolve(strict=True), args.lab_root.resolve(strict=True), args.destination_root.resolve(strict=True))
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()

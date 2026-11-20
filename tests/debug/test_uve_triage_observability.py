from pathlib import Path
import json
import os
import sqlite3
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def manifest(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))


def test_s0_closed_manifests_and_schema():
    spec = manifest('config/uve_triage_storage_manifest.json')
    for key, ref in spec.items():
        if key.endswith('_ref'):
            assert (ROOT / ref).exists(), ref
    catalog = manifest(spec['run_kind_catalog_ref'])
    ingest = manifest(spec['ingest_ref'])
    for route in ingest['routes']:
        assert route['run_kind'] in catalog['kinds']
    with sqlite3.connect(':memory:') as db:
        db.executescript((ROOT / spec['schema_sql_ref']).read_text(encoding='utf-8'))
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'runs', 'run_summaries', 'run_comparisons', 'probe_results', 'evidence_refs'} <= names


@pytest.fixture
def lab(tmp_path):
    from mcp_server.diagnostics.uve_triage_store import load_config

    (tmp_path / 'config/uve').mkdir(parents=True)
    for source in (ROOT / 'config').glob('uve_triage*'):
        shutil.copyfile(source, tmp_path / 'config' / source.name)
    for source in (ROOT / 'config').glob('uve_storage*'):
        shutil.copyfile(source, tmp_path / 'config' / source.name)
    for source in (ROOT / 'config').glob('lab_uve_*.json'):
        shutil.copyfile(source, tmp_path / 'config' / source.name)
    shutil.copyfile(ROOT / 'config/engine_triage_policy.json', tmp_path / 'config/engine_triage_policy.json')
    schema = ROOT / 'config/uve/uve_triage_storage_schema.sql'
    shutil.copyfile(schema, tmp_path / 'config/uve' / schema.name)
    migrations = ROOT / 'config/uve/uve_triage_migrations'
    shutil.copytree(migrations, tmp_path / 'config/uve' / migrations.name)
    config = load_config(tmp_path)
    active = config.document(config.policy('storage_lifecycle_ref')['active_work_ref'])
    reference = active['resource_lock_ref_in_pipeline']
    target = config.pipeline / reference
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / config.policy('lab_binding_ref')['training_pipeline_relative_from_repo'] / reference, target)
    return config


def test_s1_idempotent_migration_and_read_only_queries(lab):
    from mcp_server.diagnostics.uve_triage_store import open_store

    with open_store(lab) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        before = [tuple(row) for row in db.execute('SELECT * FROM schema_migrations')]
    with open_store(lab) as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM schema_migrations')] == before
    with open_store(lab, readonly=True) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute('INSERT INTO projects(project_id) VALUES (?)', ('unwanted',))
    schema = lab.path(lab.spec['schema_sql_ref'])
    schema.write_text(schema.read_text(encoding='utf-8') + '\n-- drift\n', encoding='utf-8')
    with pytest.raises(ValueError, match='modified'), open_store(lab):
        pass


def _reports(lab, *documents):
    path = lab.path(lab.document('config/engine_triage_policy.json')['report_dir'])
    path.mkdir(parents=True, exist_ok=True)
    for index, doc in enumerate(documents):
        (path / f'report-{index}.json').write_text(json.dumps(doc), encoding='utf-8')


def test_s2_summary_comparison_and_replay(lab):
    from mcp_server.diagnostics.uve_triage_ingest import ingest
    from mcp_server.diagnostics.uve_triage_store import open_store

    finding = {'diagnostic_code': 'physics.collider_stale_after_transform', 'severity': 'error', 'entity_id': 7}
    _reports(lab, {'generated_at': '2026-11-01T00:00:00Z', 'blocking': True, 'findings': [finding] * 65},
             {'generated_at': '2026-11-02T00:00:00Z', 'findings': []})
    first = ingest(lab, batch_label='fixture')
    assert first['integrity_check_ok'], first
    assert first['indexed'] == 2
    replay = ingest(lab, batch_label='replay')
    assert replay['unchanged'] == 2 and replay['indexed'] == 0
    with open_store(lab, readonly=True) as db:
        assert db.execute('SELECT linked_run_id FROM active_run_leases WHERE linked_run_id=?', (first['batch_id'],)).fetchone()
        assert db.execute('SELECT COUNT(*) FROM run_summaries').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM findings').fetchone()[0] == 1
        assert tuple(db.execute('SELECT category,occurrences_a,occurrences_b FROM run_comparison_items').fetchone()) == (
            'not_observed', 65, 0)


@pytest.fixture
def bundle_gate(tmp_path):
    from mcp_server.diagnostics.uve_triage_sources import discover
    from mcp_server.diagnostics.uve_triage_store import StoreConfig, load_config

    config = load_config(ROOT)
    spec = config.policy('ingest_ref')
    binding = config.policy('lab_binding_ref')
    proof = config.policy('proof_ref')
    from mcp_server.diagnostics.lab_uve_incident_bundle import build_incident_bundle, write_incident_bundle
    fixture_run = tmp_path / 'bundle-source.json'
    fixture_run.write_text(json.dumps({'plan_slug': 'isolated-observability-fixture', 'failure_reason': 'CODEGEN_STALL'}), encoding='utf-8')
    bundle_document = build_incident_bundle(repo_root=ROOT, run_path=fixture_run)
    bundle = write_incident_bundle(repo_root=ROOT, bundle=bundle_document)['bundle_path']
    config = StoreConfig(config.root, {**config.spec, 'database_relative_from_lab':
                         (tmp_path / 'proof.sqlite').relative_to(ROOT).as_posix()})
    env = {**os.environ, binding['lab_root_env_key']: str(ROOT), binding['bundle_validate_env_key']: str(bundle)}
    from scripts.uve.verify_observability import unobserved_directories

    original = config.pipeline / binding['validate_profile_relative_in_pipeline']
    prof = json.loads(original.read_text(encoding='utf-8'))
    prof['identity']['ignore_globs'].extend(unobserved_directories(ROOT, prof['identity']['watch_globs']))
    profile = str(tmp_path / 'bundle-profile.json')
    Path(profile).write_text(json.dumps(prof), encoding='utf-8')

    def uve(*args):
        process = subprocess.run([sys.executable, '-B', '-m', 'agent_training.uve.cli', *args],
                                 cwd=config.pipeline, env=env, capture_output=True, text=True, timeout=120)
        assert process.returncode == 0, process.stdout + process.stderr
        return json.loads(process.stdout)

    receipt = uve('probe', 'run', '--profile', profile, '--probe', 'command_check',
                  '--repair-actor-ref', binding['default_repair_actor_ref'])
    gate = uve('gate', 'evaluate', '--gate', binding['validate_gate_id'], '--probe-run-id', receipt['probe_run_id'],
               '--evaluator-ref', proof['allowed_evaluator_refs'][0])
    sources = ['lab_incident_bundle', 'uve_probe_receipt', 'uve_gate_receipt']
    paths = [source.path for source in discover(config, spec, sources)
             if source.path == bundle or source.path.stem in (receipt['probe_run_id'], gate['trust_chain_ref'])]
    assert len(paths) == 3
    return config, sources, paths, receipt, gate


def test_s3_real_bundle_gate_links_only_matching_bytes(bundle_gate):
    from mcp_server.diagnostics.uve_triage_ingest import ingest
    from mcp_server.diagnostics.uve_triage_query import read_query
    from mcp_server.diagnostics.uve_triage_store import open_store

    config, sources, paths, probe, gate = bundle_gate
    result = ingest(config, batch_label='bundle-gate-fixture', source_types=sources, selected_paths=paths)
    assert result['integrity_check_ok'], result
    with open_store(config, readonly=True) as db:
        assert db.execute('SELECT outcome FROM probe_results').fetchone()[0] == 'pass'
        assert db.execute('SELECT link_kind FROM run_links').fetchone()[0] == 'bundle_validation_probe'
        assert not db.execute('SELECT 1 FROM resolution_links').fetchone()
        runs = dict(db.execute("SELECT run_kind,run_id FROM runs WHERE run_kind IN ('uve_probe','uve_gate')"))
    for kind, receipt in (('uve_probe', probe), ('uve_gate', gate)):
        indexed = read_query(config, 'run-summary', runs[kind])
        assert indexed['receipt_explanations'][0]['status'] == 'recorded'
        assert indexed['receipt_explanations'][0]['explanation'] == receipt['explanation']


def test_s4_fail_warn_summaries_keep_evidence(lab):
    from mcp_server.diagnostics.uve_triage_ingest import ingest
    from mcp_server.diagnostics.uve_triage_store import open_store

    code = 'physics.collider_stale_after_transform'
    _reports(lab, {'generated_at': '2026-11-01T00:00:00Z', 'status': 'TRIAGE_FAIL',
                   'findings': [{'code': code, 'severity': 'error'}]},
             {'generated_at': '2026-11-02T00:00:00Z',
              'findings': [{'code': code, 'severity': 'warning'}]})
    assert ingest(lab, batch_label='summary-fixture')['integrity_check_ok']
    with open_store(lab, readonly=True) as db:
        summaries = {row['rollup_status']: row for row in db.execute('SELECT * FROM run_summaries')}
        assert set(summaries) == {'TRIAGE_FAIL', 'TRIAGE_WARN'}
        assert summaries['TRIAGE_FAIL']['error_count'] == summaries['TRIAGE_WARN']['warning_count'] == 1
        for row in summaries.values():
            assert row['summary_text'] and row['root_cause_status'] == 'hypothesis'
            assert db.execute('SELECT evidence_id FROM summary_evidence_refs WHERE run_id=? AND evidence_id IS NOT NULL',
                              (row['run_id'],)).fetchone()


def test_s4_summary_refresh_tracks_corrected_project(lab):
    from mcp_server.diagnostics.uve_triage_ingest import ingest
    from mcp_server.diagnostics.uve_triage_summarize import summarize_run
    from mcp_server.diagnostics.uve_triage_store import open_store, transaction

    _reports(lab, {'status': 'failed'})
    assert ingest(lab, batch_label='summary-project')['integrity_check_ok']
    with open_store(lab) as db, transaction(db):
        run_id = db.execute('SELECT run_id FROM runs').fetchone()[0]
        db.execute("INSERT INTO projects(project_id) VALUES('corrected-fixture')")
        db.execute("UPDATE runs SET project_id='corrected-fixture' WHERE run_id=?", (run_id,))
        summarize_run(db, lab, run_id)
        summary = db.execute('SELECT * FROM run_summaries WHERE run_id=?', (run_id,)).fetchone()
        assert summary['project_id'] == 'corrected-fixture'
        assert 'corrected-fixture' in summary['summary_text']


def test_s5_reads_persisted_comparisons_and_timelines(lab):
    from mcp_server.diagnostics.uve_triage_compare import compare_runs
    from mcp_server.diagnostics.uve_triage_ingest import ingest
    from mcp_server.diagnostics.uve_triage_query import read_query
    from mcp_server.diagnostics.uve_triage_store import open_store, transaction

    _reports(lab, {'generated_at': '2026-11-01T00:00:00Z',
                   'findings': [{'code': 'physics.collider_stale_after_transform', 'severity': 'warning'}]},
             {'generated_at': '2026-11-02T00:00:00Z'})
    assert ingest(lab, batch_label='timeline-fixture')['integrity_check_ok']
    with open_store(lab) as db, transaction(db):
        a, b = [row[0] for row in db.execute('SELECT run_id FROM runs ORDER BY started_at')]
        comparison = compare_runs(db, lab, a, b)
    stored = read_query(lab, 'comparison-show', comparison['comparison_id'])
    assert stored == comparison
    item = stored['items'][0]
    assert item['category'] == 'not_observed' and item['linked_gate_receipt_ref'] is None
    trace = read_query(lab, 'trace-finding', item['finding_fingerprint'])
    assert trace['occurrences'][0]['run_id'] == a
    assert trace['evidence'][0]['content_sha256']
    assert read_query(lab, 'run-summary', b)['summary']['summary_complete']


def test_s5_fixture_resolution_requires_actual_independent_gate(tmp_path, bundle_gate):
    from datetime import datetime, timedelta, timezone
    from mcp_server.diagnostics.uve_triage_ingest import ingest
    from mcp_server.diagnostics.uve_triage_query import read_query
    from mcp_server.diagnostics.uve_triage_store import StoreConfig, stable_id, open_store

    config, sources, paths, receipt, gate = bundle_gate
    relative = tmp_path.relative_to(ROOT).as_posix()
    proof = config.policy('proof_ref')
    project = receipt['subject_identity']['project_id']
    code = 'OBSERVABILITY_FIXTURE_BUNDLE_REJECTED'
    fingerprint = stable_id([project, code, None])
    proof['resolution_contracts'] = [{'project_id': project, 'diagnostic_code': code, 'gate_id': gate['gate_id'],
                                      'probe_ref': receipt['probe']['probe_id'], 'profile_ref': receipt['profile_ref'],
                                      'fingerprint': fingerprint,
                                      'required_stdout_lines': receipt['observation']['stdout'].splitlines()}]
    (tmp_path / 'fixture-proof.json').write_text(json.dumps(proof), encoding='utf-8')
    spec = config.policy('ingest_ref')
    route = spec['routes'][0]
    route['glob'] = relative + '/fixture-observation-*.json'
    (tmp_path / 'fixture-ingest.json').write_text(json.dumps(spec), encoding='utf-8')
    config = StoreConfig(config.root, {**config.spec, 'ingest_ref': relative + '/fixture-ingest.json',
                                      'proof_ref': relative + '/fixture-proof.json'})
    now = datetime.now(timezone.utc)

    def report(index, when, present):
        path = tmp_path / f'fixture-observation-{index}.json'
        findings = [{'code': code, 'severity': 'error'}] if present else []
        path.write_text(json.dumps({'project_id': project, 'generated_at': when.isoformat(), 'findings': findings}),
                        encoding='utf-8')
        return path

    a = report(1, now - timedelta(minutes=1), True)
    b = report(2, now + timedelta(minutes=1), False)
    result = ingest(config, batch_label='real-proof-fixture', selected_paths=[*paths, a, b],
                    source_types=[*sources, route['source_type']])
    assert result['integrity_check_ok'], result
    with open_store(config, readonly=True) as db:
        resolution = db.execute("SELECT * FROM run_comparison_items WHERE category='resolved'").fetchone()
        assert resolution['linked_gate_receipt_ref'] and resolution['finding_fingerprint'] == fingerprint
        stored = read_query(config, 'comparison-show', resolution['comparison_id'])
        assert stored['items'][0]['category'] == 'resolved'
    c = report(3, now + timedelta(minutes=2), True)
    assert ingest(config, batch_label='fixture-regression', selected_paths=[c], source_types=[route['source_type']])[
        'integrity_check_ok']
    timeline = read_query(config, 'fingerprint-timeline', fingerprint)
    assert [row['category'] for row in timeline['comparisons']] == ['resolved', 'regressed']


def test_s5_mcp_tool_registers_read_only_and_respects_manifest(lab, monkeypatch):
    from mcp_server.diagnostics import uve_triage_query, uve_triage_tool
    from mcp_server.diagnostics.uve_triage_ingest import ingest
    from mcp_server.diagnostics.uve_triage_store import open_store

    monkeypatch.setattr(uve_triage_tool, 'load_config', lambda: lab)
    monkeypatch.setattr(uve_triage_query, 'load_config', lambda: lab)

    class Registry:
        def register_tool(self, **kwargs):
            self.definition = kwargs
            return {'success': True}

    registry = Registry()
    assert uve_triage_tool.register_observability_tool(registry) == 1
    assert registry.definition['effect'] == 'evidence_read'
    _reports(lab, {'status': 'failed'})
    assert ingest(lab, batch_label='mcp-query')['integrity_check_ok']
    with open_store(lab, readonly=True) as db:
        run_id = db.execute('SELECT run_id FROM runs').fetchone()[0]
    result = registry.definition['function_obj']('run-summary', run_id)
    assert result['summary']['rollup_status'] == 'TRIAGE_FAIL'
    with pytest.raises(ValueError):
        registry.definition['function_obj']('diff-runs', run_id)
    ref = lab.path(lab.spec['adapter_ref'])
    adapter = json.loads(ref.read_text(encoding='utf-8'))
    adapter['mcp_query_enabled'] = False
    ref.write_text(json.dumps(adapter), encoding='utf-8')
    assert uve_triage_tool.register_observability_tool(Registry()) == 0
    with pytest.raises(ValueError):
        registry.definition['function_obj']('run-summary', run_id)


def test_s6_engine_entry_indexes_and_keeps_exit_contract(tmp_path):
    from mcp_server.diagnostics.uve_triage_store import load_config, open_store

    config = load_config(ROOT)
    ref = tmp_path / 'storage.json'
    ref.write_text(json.dumps({**config.spec, 'database_relative_from_lab':
                               (tmp_path / 'engine.sqlite').relative_to(ROOT).as_posix()}), encoding='utf-8')
    env = {**os.environ, config.spec['db_manifest_override_env']: str(ref)}
    cmd = [sys.executable, '-B', str(ROOT / 'scripts/debug/triage_engine_session.py'), '--no-expansion-enrichment', '--index']
    for _ in range(2):
        process = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, encoding='utf-8', timeout=120)
        assert process.returncode == 0, process.stdout + process.stderr
        assert 'ENGINE_UVE_VALID' not in process.stdout
        assert json.loads(process.stdout)['observability_index']['status'] == 'indexed'
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(config.spec['db_manifest_override_env'], str(ref))
        with open_store(load_config(ROOT), readonly=True) as db:
            assert db.execute('SELECT COUNT(*) FROM run_summaries').fetchone()[0] == 2
            assert db.execute('SELECT COUNT(*) FROM run_comparisons').fetchone()[0] == 1


def test_s6_uve_gate_entry_can_mirror_saved_receipts(bundle_gate, tmp_path):
    from mcp_server.diagnostics.uve_triage_store import load_config, open_store

    config, _, _, receipt, gate = bundle_gate
    storage = tmp_path / 'storage-sidecar.json'
    storage.write_text(json.dumps(config.spec), encoding='utf-8')
    binding = config.policy('lab_binding_ref')
    env = {**os.environ, binding['lab_root_env_key']: str(ROOT), config.spec['db_manifest_override_env']: str(storage)}
    process = subprocess.run([sys.executable, '-B', '-m', 'agent_training.uve.cli', 'gate', 'evaluate',
                              '--gate', gate['gate_id'], '--probe-run-id', receipt['probe_run_id'],
                              '--evaluator-ref', gate['gate_evaluator_ref'], '--index'],
                             cwd=config.pipeline, env=env, capture_output=True, encoding='utf-8', timeout=60)
    assert process.returncode == 0, process.stdout + process.stderr
    assert json.loads(process.stdout)['decision'] == 'approved'
    assert 'not_verified' not in process.stderr
    with open_store(config, readonly=True) as db:
        assert db.execute('SELECT COUNT(*) FROM run_summaries').fetchone()[0] == 2
        assert db.execute('SELECT checked FROM gate_links').fetchone()[0] == 1


def test_s7_documented_observability_commands():
    docs = (ROOT / 'docs/triage-observability.md').read_text(encoding='utf-8')
    for module in ('uve_triage_ingest', 'uve_triage_query'):
        assert f'mcp_server.diagnostics.{module}' in docs
        command = [sys.executable, '-B', '-m', f'mcp_server.diagnostics.{module}', '--help']
        assert subprocess.run(command, cwd=ROOT, capture_output=True, timeout=20).returncode == 0
    assert 'seven-day' in docs


def test_s8_unmapped_diagnostic_fails_and_fake_resolution_is_rejected(lab):
    from mcp_server.diagnostics.uve_triage_ingest import ingest
    from mcp_server.diagnostics.uve_triage_store import open_store

    code = 'physics.collider_stale_after_transform'
    _reports(lab, {'generated_at': '2026-11-01T00:00:00Z', 'findings': [{'diagnostic_code': code}]},
             {'generated_at': '2026-11-02T00:00:00Z'},
             {'generated_at': '2026-11-03T00:00:00Z', 'findings': [{'diagnostic_code': 'unregistered.fixture'}]})
    result = ingest(lab, batch_label='diagnostic-parity')
    assert not result['integrity_check_ok'] and result['indexed'] == 2 and len(result['errors']) == 1
    with open_store(lab) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT COUNT(*) FROM run_summaries').fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE run_comparison_items SET lifecycle_status_after='resolved'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO resolution_links VALUES('invented','invented','invented','2026-11-11')")


def test_s8_cutover_requires_recorded_parallel_window(lab):
    from datetime import datetime, timedelta, timezone
    from mcp_server.diagnostics.uve_triage_migration import cutover_status, record_reconciliation
    from mcp_server.diagnostics.uve_triage_store import open_store, transaction

    now = datetime.now(timezone.utc)
    checked = {'unmatched_sources': [], 'missing_proposal_ids': [], 'integrity_check': 'ok', 'run_count': 1, 'summary_count': 1}
    report = {'errors': [], 'reconciliation': checked}
    with open_store(lab) as db, transaction(db):
        assert cutover_status(db, lab)['status'] == 'not_verified'
        record_reconciliation(db, {**report, 'observed_at': (now - timedelta(days=8)).isoformat()})
        assert cutover_status(db, lab)['status'] == 'not_verified'
        record_reconciliation(db, {**report, 'observed_at': now.isoformat()})
        assert cutover_status(db, lab)['status'] == 'eligible'
        record_reconciliation(db, {**report, 'observed_at': (now + timedelta(seconds=1)).isoformat(),
                                  'errors': [{'error_class': 'fixture-mismatch'}]})
        assert cutover_status(db, lab)['status'] == 'not_verified'


def test_s9_lifecycle_manifest_is_closed_and_default_disabled(lab):
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import policies
    from mcp_server.diagnostics.uve_triage_store import load_config, open_store

    policy, digest = policies(lab)
    assert not policy['manifest']['enabled'] and policy['manifest']['default_mode'] == 'dry_run'
    assert digest.startswith('sha256:')
    with open_store(lab) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT COUNT(*) FROM cleanup_runs').fetchone()[0] == 0
    binding = load_config(ROOT).policy('lab_binding_ref')
    env = {**os.environ, binding['lab_root_env_key']: str(ROOT)}
    process = subprocess.run([sys.executable, '-B', '-m', 'agent_training.uve.cli', 'storage', 'config'],
                             cwd=ROOT / binding['training_pipeline_relative_from_repo'], env=env,
                             capture_output=True, encoding='utf-8', timeout=30)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)['enabled'] is False


def _scratch(lab, policy):
    from datetime import timedelta, datetime, timezone

    rel = policy['scope']['roots'][0]['relative']
    folder = lab.path(rel)
    folder.mkdir(parents=True, exist_ok=True)
    artifact = folder / 'generated.scratch'
    artifact.write_bytes(b'isolated ephemeral fixture')
    old = (datetime.now(timezone.utc) - timedelta(days=120)).timestamp()
    os.utime(artifact, (old, old))
    return rel, artifact


def _lock_manifest(lab, policy):
    relative = policy['active_work']['resource_lock_ref_in_pipeline']
    real_pipeline = ROOT / lab.policy('lab_binding_ref')['training_pipeline_relative_from_repo']
    target = lab.pipeline / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(real_pipeline / relative, target)


def test_s10_scan_protects_lease_during_grace_and_denies_excluded_paths(lab):
    from datetime import datetime, timedelta, timezone
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import policies
    from mcp_server.diagnostics.uve_triage_storage_lease import lease
    from mcp_server.diagnostics.uve_triage_storage_scan import audit_scan
    from mcp_server.diagnostics.uve_triage_store import open_store, transaction

    policy, digest = policies(lab)
    rel, artifact = _scratch(lab, policy)
    _lock_manifest(lab, policy)
    (artifact.parent / 'model.safetensors').write_bytes(b'protected fixture')
    (artifact.parent / 'ProTrainer').mkdir()
    (artifact.parent / 'ProTrainer/ignored.tmp').write_bytes(b'excluded fixture')
    acquired = lease(lab, policy, 'acquire', work_root=rel, actor='codex', pid=os.getpid())
    now = datetime.now(timezone.utc)
    with open_store(lab) as db, transaction(db):
        db.execute('UPDATE active_run_leases SET expires_at=? WHERE lease_id=?',
                   ((now - timedelta(seconds=1)).isoformat(), acquired['lease_id']))
        result = audit_scan(db, lab, policy, digest, 'operator', now)
        assert not result['aborted'] and result['bytes_reclaimable'] == 0
        assert db.execute("SELECT COUNT(*) FROM cleanup_candidates WHERE decision_reason_ref='active_work_lease'").fetchone()[0] == 2
        assert not db.execute("SELECT 1 FROM cleanup_candidates WHERE uri_relative LIKE '%ProTrainer%'").fetchone()
    lease(lab, policy, 'release', lease_id=acquired['lease_id'])
    with open_store(lab) as db, transaction(db):
        result = audit_scan(db, lab, policy, digest, 'operator', now)
        assert result['bytes_reclaimable'] == artifact.stat().st_size
        assert result['protected_count'] == 1


def test_s10_active_manifest_reference_and_unknown_class_fail_closed(lab):
    from datetime import datetime, timezone
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import policies
    from mcp_server.diagnostics.uve_triage_storage_scan import audit_scan
    from mcp_server.diagnostics.uve_triage_store import open_store, transaction

    policy, digest = policies(lab)
    _, artifact = _scratch(lab, policy)
    reference = lab.path('config/fixture_artifact.json')
    reference.write_text(json.dumps({'artifact_ref': artifact.relative_to(lab.root).as_posix()}), encoding='utf-8')
    with open_store(lab) as db, transaction(db):
        inventory = audit_scan(db, lab, policy, digest, 'operator', datetime.now(timezone.utc))
        assert not inventory['aborted'] and inventory['bytes_reclaimable'] == 0
        row = db.execute('SELECT * FROM cleanup_candidates WHERE cleanup_run_id=?', (inventory['cleanup_run_id'],)).fetchone()
        assert row['reference_count'] == 1 and row['decision'] == 'protect'
    reference.unlink()
    (artifact.parent / 'unknown.payload').write_bytes(b'unknown fixture class')
    with open_store(lab) as db, transaction(db):
        assert audit_scan(db, lab, policy, digest, 'operator', datetime.now(timezone.utc))['aborted']


def _enable_fixture(lab):
    ref = lab.path(lab.spec['storage_lifecycle_ref'])
    config = json.loads(ref.read_text(encoding='utf-8'))
    config['enabled'] = True
    ref.write_text(json.dumps(config), encoding='utf-8')


def test_s11_return_inventory_and_explicit_approval_required(lab):
    from datetime import datetime, timedelta, timezone
    from mcp_server.diagnostics.uve_triage_storage_activity import approve_return, record_activity, refresh_state, require_mode
    from mcp_server.diagnostics.uve_triage_storage_execute import quarantine
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import policies
    from mcp_server.diagnostics.uve_triage_storage_scan import audit_scan
    from mcp_server.diagnostics.uve_triage_store import open_store, transaction

    _enable_fixture(lab)
    policy, digest = policies(lab)
    _, artifact = _scratch(lab, policy)
    now = datetime.now(timezone.utc)
    activity = policy['activity']
    with open_store(lab) as db, transaction(db):
        old = now - timedelta(days=100)
        record_activity(db, policy, digest, old, activity['operator_ack_kind'], 'operator', 'old')
        mid = refresh_state(db, policy, digest, old + timedelta(days=40))
        assert mid['allowed_modes'] == ['dry_run']
        assert refresh_state(db, policy, digest, now)['paused']
        state = record_activity(db, policy, digest, now, activity['operator_ack_kind'], 'operator', 'return')
        assert not state['paused'] and state['post_return_reconcile_required']
        with pytest.raises(ValueError, match='requires current'):
            require_mode(db, policy, digest, now, 'purge')
        scan = audit_scan(db, lab, policy, digest, 'operator', now,
                          reconciliation_kind=policy['return_reconciliation']['reconciliation_kind'])
        db.execute('UPDATE inactivity_protection_state SET last_post_return_inventory_run_id=?', (scan['cleanup_run_id'],))
        approval = approve_return(db, policy, digest, now, scan['cleanup_run_id'], 'fixture-operator')
        with pytest.raises(ValueError, match='requires current'):
            require_mode(db, policy, digest, now, 'purge', approval['approval_id'], 'stale')
    result = quarantine(lab, policy, digest, now, scan['cleanup_run_id'], scan['plan_sha256'], None, 'operator')
    assert result['aborted'] and artifact.exists()
    result = quarantine(lab, policy, digest, now, scan['cleanup_run_id'], scan['plan_sha256'], approval['approval_id'], 'operator')
    assert not result['aborted'] and result['bytes_quarantined'] > 0 and not artifact.exists()


def test_s11_partial_quarantine_keeps_completed_action_receipt(lab, monkeypatch):
    from datetime import datetime, timezone
    from mcp_server.diagnostics.uve_triage_storage_activity import record_activity
    from mcp_server.diagnostics.uve_triage_storage_execute import quarantine
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import policies
    from mcp_server.diagnostics.uve_triage_storage_scan import audit_scan
    from mcp_server.diagnostics.uve_triage_store import open_store, transaction

    _enable_fixture(lab)
    policy, digest = policies(lab)
    _, first = _scratch(lab, policy)
    second = first.parent / 'z-last.scratch'
    shutil.copy2(first, second)
    now = datetime.now(timezone.utc)
    with open_store(lab) as db, transaction(db):
        record_activity(db, policy, digest, now, policy['activity']['operator_ack_kind'], 'operator', 'fixture')
        inventory = audit_scan(db, lab, policy, digest, 'operator', now)
    rename = Path.rename

    def fail_second(path, destination):
        if path == second:
            raise OSError('Injected second move failure')
        return rename(path, destination)

    monkeypatch.setattr(Path, 'rename', fail_second)
    result = quarantine(lab, policy, digest, now, inventory['cleanup_run_id'], inventory['plan_sha256'], None, 'operator')
    assert result['aborted'] and result['bytes_quarantined'] > 0
    assert not first.exists() and second.exists()
    with open_store(lab, readonly=True) as db:
        states = {row[0] for row in db.execute('SELECT action_kind FROM cleanup_actions')}
        assert states == {'quarantined', 'quarantine_prepared'}


def test_s11_compaction_honors_holds_and_preserves_comparisons(lab):
    from datetime import datetime, timezone
    from mcp_server.diagnostics.uve_triage_ingest import ingest
    from mcp_server.diagnostics.uve_triage_storage_execute import compact
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import policies
    from mcp_server.diagnostics.uve_triage_store import open_store, transaction

    _enable_fixture(lab)
    _reports(lab, {'generated_at': '2026-11-01T00:00:00Z', 'status': 'failed'},
             {'generated_at': '2026-11-02T00:00:00Z'})
    assert ingest(lab, batch_label='compaction')['integrity_check_ok']
    policy, digest = policies(lab)
    now = datetime.now(timezone.utc)
    assert compact(lab, policy, digest, now, None, 'operator')['aborted']
    with open_store(lab) as db, transaction(db):
        before = [tuple(row) for row in db.execute('SELECT * FROM run_comparisons')]
        assert db.execute('SELECT 1 FROM evidence_holds WHERE released_at IS NULL').fetchone()
        db.execute('UPDATE evidence_holds SET released_at=?', (now.isoformat(),))
    result = compact(lab, policy, digest, now, None, 'operator')
    assert not result['aborted'], result
    with open_store(lab, readonly=True) as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM run_comparisons')] == before
        assert db.execute('SELECT COUNT(*) FROM compaction_runs').fetchone()[0] == 2


def test_s12_scheduler_inventory_does_not_move_clock(lab):
    from datetime import datetime, timedelta, timezone
    from mcp_server.diagnostics.uve_triage_storage_activity import record_activity, refresh_state
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import main, policies
    from mcp_server.diagnostics.uve_triage_store import open_store, transaction

    policy, digest = policies(lab)
    activity = policy['activity']
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=120)
    with open_store(lab) as db, transaction(db):
        record_activity(db, policy, digest, old, activity['operator_ack_kind'], 'operator', 'fixture')
    assert main(lab, ['scan', '--mode', policy['manifest']['default_mode'],
                      '--actor-class', activity['maintenance_actor_class']]) == 0
    with open_store(lab) as db, transaction(db):
        assert refresh_state(db, policy, digest, now)['last_activity_at'] == old.isoformat()
        assert db.execute('SELECT inactivity_clock_eligible FROM lab_activity_markers WHERE activity_kind_ref=?',
                          (activity['scheduled_scan_kind'],)).fetchone()[0] == 0
        assert db.execute('SELECT paused FROM inactivity_protection_state').fetchone()[0] == 1
    if os.name == 'nt':
        process = subprocess.run(['powershell.exe', '-NoProfile', '-File',
                                  str(ROOT / 'scripts/uve/install_storage_lifecycle_task.ps1')],
                                 cwd=ROOT, capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, process.stderr
        assert 'CalendarTrigger' in process.stdout and 'run_storage_lifecycle_task.ps1' in process.stdout


def test_s12_real_independent_gate_can_purge_only_the_reviewed_fixture(tmp_path):
    from datetime import datetime, timedelta, timezone
    from mcp_server.diagnostics.uve_triage_storage_activity import approve_return, record_activity
    from mcp_server.diagnostics.uve_triage_storage_execute import quarantine
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import policies
    from mcp_server.diagnostics.uve_triage_storage_purge import plan, purge
    from mcp_server.diagnostics.uve_triage_storage_scan import audit_scan
    from mcp_server.diagnostics.uve_triage_store import load_config, open_store, transaction

    def write(name, value):
        path = tmp_path / (name + '.json')
        path.write_text(json.dumps(value), encoding='utf-8')
        return path.relative_to(ROOT).as_posix()

    config = load_config(ROOT)
    policy, _ = policies(config)
    fixture = tmp_path.relative_to(ROOT).as_posix()
    scope = {**policy['scope'], 'roots': [{**policy['scope']['roots'][0], 'relative': fixture + '/scratch'}],
             'permanent_root_prefixes': ['engine', 'config']}
    retention = {**policy['policy'], 'durations_days': {**policy['policy']['durations_days'], 'quarantine_hold': 0}}
    verification = {**policy['verification'], 'request_output_relative': fixture}
    lifecycle = write('lifecycle', {**policy['manifest'], 'enabled': True, 'scope_ref': write('scope', scope),
                                    'policy_ref': write('retention', retention),
                                    'verification_ref': write('verification', verification),
                                    'quarantine_relative': fixture + '/quarantine'})
    override = write('storage', {**config.spec, 'storage_lifecycle_ref': lifecycle,
                                   'database_relative_from_lab': fixture + '/storage.sqlite'})
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(config.spec['db_manifest_override_env'], str(ROOT / override))
        config = load_config(ROOT)
        policy, digest = policies(config)
        _, artifact = _scratch(config, policy)
        now = datetime.now(timezone.utc)
        activity = policy['activity']
        with open_store(config) as db, transaction(db):
            record_activity(db, policy, digest, now - timedelta(days=120), activity['operator_ack_kind'], 'operator', 'fixture')
            record_activity(db, policy, digest, now, activity['operator_ack_kind'], 'operator', 'return')
            inventory = audit_scan(db, config, policy, digest, 'operator', now,
                                   reconciliation_kind=policy['return_reconciliation']['reconciliation_kind'])
            inventory_id, inventory_hash = inventory['cleanup_run_id'], inventory['plan_sha256']
            db.execute('UPDATE inactivity_protection_state SET last_post_return_inventory_run_id=?', (inventory_id,))
            approval = approve_return(db, policy, digest, now, inventory_id, 'fixture-operator')['approval_id']
        result = quarantine(config, policy, digest, now, inventory_id, inventory_hash, approval, 'operator')
        assert not result['aborted'] and not artifact.exists(), result
        assert purge(config, policy, digest, now, inventory_id, inventory_hash, approval, None, 'operator')['aborted']
        now = datetime.now(timezone.utc)
        with open_store(config, readonly=True) as db:
            request = plan(db, config, policy, digest, now, inventory_id, inventory_hash, approval)
        assert len(request['candidates']) == 1
        request_path = ROOT / write('purge-request', request)
        binding = config.policy('lab_binding_ref')
        env = {**os.environ, binding['lab_root_env_key']: str(ROOT), verification['request_env']: str(request_path)}

        def uve(*args):
            p = subprocess.run([sys.executable, '-B', '-m', 'agent_training.uve.cli', *args],
                               cwd=config.pipeline, env=env, capture_output=True, encoding='utf-8', timeout=60)
            assert p.returncode == 0, p.stdout + p.stderr
            return json.loads(p.stdout)

        receipt = uve('probe', 'run', '--profile', verification['profile_ref_in_pipeline'], '--probe', verification['probe_id'],
                      '--repair-actor-ref', binding['default_repair_actor_ref'])
        gate = uve('gate', 'evaluate', '--gate', verification['gate_id'], '--probe-run-id', receipt['probe_run_id'],
                   '--evaluator-ref', config.policy('proof_ref')['allowed_evaluator_refs'][0])
        log = json.loads((config.pipeline / config.policy('ingest_ref')['uve_log_root_ref_in_pipeline']).read_text(encoding='utf-8'))
        gate_path = config.pipeline / log['log_dir'] / 'gates' / (gate['trust_chain_ref'] + '.json')
        final = purge(config, policy, digest, now, inventory_id, inventory_hash, approval, gate_path, 'operator')
        assert not final['aborted'] and final['bytes_purged'] > 0, final
        assert not config.path(request['candidates'][0]['dest_uri']).exists()


def test_s13_codegen_burst_keeps_all_scratch_protected(lab, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timedelta, timezone
    from threading import Event
    from types import SimpleNamespace
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import policies
    from mcp_server.diagnostics.uve_triage_storage_scan import audit_scan
    from mcp_server.diagnostics.uve_triage_storage_writer import lease_codegen_writes
    from mcp_server.diagnostics.uve_triage_store import open_store, transaction

    from mcp_server.diagnostics import uve_triage_storage_writer

    monkeypatch.setattr(uve_triage_storage_writer, 'load_config', lambda: lab)
    policy, digest = policies(lab)
    _, artifact = _scratch(lab, policy)
    _lock_manifest(lab, policy)
    ready, done = Event(), Event()

    @lease_codegen_writes
    def write(*, project_root, state):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=120)).timestamp()
        for index in range(64):
            path = project_root / f'generated-{index}.scratch'
            path.write_bytes(b'writer burst')
            os.utime(path, (old, old))
        ready.set()
        assert done.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write, project_root=artifact.parent, state=SimpleNamespace(run_id='fixture-codegen'))
        try:
            assert ready.wait(10)
            with open_store(lab) as db, transaction(db):
                scan = audit_scan(db, lab, policy, digest, 'operator', datetime.now(timezone.utc))
                assert not scan['aborted'] and scan['bytes_reclaimable'] == 0 and scan['protected_count'] == 65
                active = db.execute('SELECT * FROM active_run_leases').fetchone()
                assert active['linked_run_id'] == 'fixture-codegen' and active['released_at'] is None
        finally:
            done.set()
        writer.result(timeout=10)
    with open_store(lab, readonly=True) as db:
        assert db.execute('SELECT released_at FROM active_run_leases').fetchone()[0]


def test_s13_heartbeat_retries_busy_database(lab, monkeypatch):
    from threading import Event
    from mcp_server.diagnostics import uve_triage_storage_writer
    from mcp_server.diagnostics.uve_triage_storage_lifecycle import policies

    ref = lab.path(lab.policy('storage_lifecycle_ref')['active_work_ref'])
    active = json.loads(ref.read_text(encoding='utf-8'))
    active['heartbeat_interval_seconds'] = 0.01
    ref.write_text(json.dumps(active), encoding='utf-8')
    policy, _ = policies(lab)
    _, artifact = _scratch(lab, policy)
    original = uve_triage_storage_writer.lease
    succeeded = Event()
    attempts = []

    def busy_first(*args, **kwargs):
        if args[2] == 'heartbeat':
            attempts.append(True)
            if len(attempts) == 1:
                error = sqlite3.OperationalError('database is locked')
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            succeeded.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(uve_triage_storage_writer, 'lease', busy_first)
    with uve_triage_storage_writer.storage_work(lab, artifact.parent, 'codegen'):
        assert succeeded.wait(2)
    assert len(attempts) >= 2

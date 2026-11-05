import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const root = fileURLToPath(new URL('../', import.meta.url));
const readJson = path => JSON.parse(readFileSync(resolve(root, path), 'utf8'));

function main() {
  const { values } = parseArgs({
    options: {
      'from-receipt': { type: 'string' },
      'failure-class': { type: 'string' },
      'fault-rule': { type: 'string' },
      'probe-id': { type: 'string' },
      registry: { type: 'string', default: 'examples/project/error-registry.json' },
      help: { type: 'boolean' },
    },
  });
  if (values.help) {
    console.log('Usage: node scripts/uve-error-registry-lookup.mjs --from-receipt ID [--registry PATH]');
    console.log('You may also query --failure-class, --fault-rule, and --probe-id directly.');
    return 0;
  }

  let receipt;
  const id = values['from-receipt'];
  if (id !== undefined) {
    if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new Error('Expected a probe run ID');
    const engine = readJson('config/uve/uve_engine_manifest.json');
    const { log_dir: logDir } = readJson(engine.log_root_ref);
    receipt = readJson(resolve(root, logDir, 'probes', id + '.json'));
    if (receipt.probe_run_id !== id) throw new Error('Probe run ID differs from the receipt');
  }
  const query = {
    probeId: values['probe-id'] ?? receipt?.probe?.probe_id ?? null,
    failureClass: values['failure-class'] ?? receipt?.failure_class ?? receipt?.observation?.failure_class ?? null,
    faultRule: values['fault-rule'] ?? receipt?.observation?.fault_rule ?? null,
  };
  if (!Object.values(query).some(Boolean)) throw new Error('Supply a probe run ID or observed failure coordinates');

  const { entries } = readJson(values.registry);
  if (!Array.isArray(entries)) throw new Error('Registry must contain an entries array');
  const matches = entries.filter(entry => {
    const fields = Object.keys(query).filter(field => entry[field]);
    return fields.length > 0 && fields.every(field => entry[field] === query[field]);
  });
  console.log(JSON.stringify({ query, matches }, null, 2));
  return matches.length ? 0 : 2;
}

try {
  process.exitCode = main();
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}

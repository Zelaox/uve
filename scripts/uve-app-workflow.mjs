import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const root = fileURLToPath(new URL('../', import.meta.url));

function main() {
  const { values } = parseArgs({
    options: {
      profile: { type: 'string' },
      'repair-actor-ref': { type: 'string' },
      probe: { type: 'string', multiple: true },
      help: { type: 'boolean' },
    },
  });
  if (values.help) {
    console.log('Usage: node scripts/uve-app-workflow.mjs --profile PATH --repair-actor-ref ACTOR [--probe ID ...]');
    return 0;
  }
  if (!values.profile?.trim() || !values['repair-actor-ref']?.trim()) {
    throw new Error('--profile and --repair-actor-ref are required');
  }

  const profile = resolve(root, values.profile);
  const config = JSON.parse(readFileSync(profile, 'utf8'));
  const probes = values.probe ?? config.verification_probe_ids;
  if (!Array.isArray(probes) || !probes.length || probes.some(id => typeof id !== 'string' || !id.trim())) {
    throw new Error('Declare verification_probe_ids in the profile or supply --probe for each required probe');
  }

  for (const probe of probes) {
    const result = spawnSync(process.env.UVE_PYTHON_EXECUTABLE || 'python', [
      '-B', '-m', 'agent_training.uve.cli', 'probe', 'run',
      '--profile', profile, '--probe', probe, '--repair-actor-ref', values['repair-actor-ref'],
    ], { cwd: root, stdio: 'inherit', windowsHide: true });
    if (result.error) throw result.error;
    if (result.status === null) throw new Error('Python stopped before returning a probe result for ' + probe);
    if (result.status !== 0) return result.status;
  }
  return 0;
}

try {
  process.exitCode = main();
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}

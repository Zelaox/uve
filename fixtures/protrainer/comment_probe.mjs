import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [directory, operationId, mode, recipePath] = process.argv.slice(2);
const recipe = JSON.parse(await fs.readFile(recipePath, 'utf8'));
const fixturePath = path.join(directory, recipe.fixture_file);
const report = fields => console.log(JSON.stringify({ operation_id: operationId, ...fields }));

if (mode === 'seed') {
  const postId = `${operationId}-post`;
  const commentDocs = recipe.rows.map((row, index) => ({
    id: `${operationId}-${index}`,
    data: { ...row, postId, userId: `${operationId}-user-${index}` },
  }));
  const fixture = { operation_id: operationId, postId, commentDocs, raw_count: commentDocs.length,
    postRaw: { commentCount: recipe.expected_visible } };
  await fs.writeFile(fixturePath, JSON.stringify(fixture), { flag: 'wx' });
  report({ seeded: true });
} else if (mode === 'diagnose') {
  const fixture = JSON.parse(await fs.readFile(fixturePath, 'utf8'));
  if (fixture.operation_id !== operationId) throw new Error('Fixture belongs to another operation');
  const appRoot = path.resolve(recipe.project_relative);
  const source = path.resolve(appRoot, recipe.diagnostic_module_relative);
  if (!source.startsWith(appRoot + path.sep)) throw new Error('Unbound diagnostic source');
  const { runCommentVisibilityDiagnosticPipeline } = await import(pathToFileURL(source));
  const result = runCommentVisibilityDiagnosticPipeline({ ...fixture, stageIds: recipe.stage_ids });
  report({ raw_count: result.rawDocs, normalized_count: result.normalizedCount,
    visible_count: result.backendDisplayCount, stage_counts: result.stages });
} else {
  throw new Error('Unsupported fixture action');
}

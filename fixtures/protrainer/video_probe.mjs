// Real application orchestration with isolated file-backed ports for native and Firebase.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';

const [directory, operationId, mode, recipePath] = process.argv.slice(2);
const recipe = JSON.parse(fs.readFileSync(recipePath, 'utf8'));
const sourceRoot = path.resolve(recipe.source_relative);
const sourcePath = relative => {
  const target = path.resolve(sourceRoot, relative);
  if (!target.startsWith(sourceRoot + path.sep)) throw new Error('Fixture source escapes its bound snapshot');
  return target;
};
const jobsManifest = JSON.parse(fs.readFileSync(sourcePath(recipe.jobs_manifest_relative), 'utf8'));
const { COMMUNITY_FEED_WRITE_COLLECTION: postsCollection } = await import(pathToFileURL(sourcePath(recipe.data_module_relative)));
const statePath = path.join(directory, recipe.state_file);
const original = `${operationId}-original`;
const visible = `${operationId}-visible`;
const draft = { id: operationId, postId: operationId, userId: original, fields: { userId: original, content: recipe.caption } };
const report = value => JSON.stringify({ operation_id: operationId, ...value }) + '\n';

function save(state) {
  const temporary = statePath + '.tmp';
  fs.writeFileSync(temporary, JSON.stringify(state));
  fs.renameSync(temporary, statePath);
}

const context = vm.createContext({ console });
async function appModule(relative, dependencies = {}) {
  const file = sourcePath(relative);
  const module = new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), { context, identifier: relative });
  await module.link(async name => {
    const members = dependencies[name];
    if (!members) throw new Error(`Fixture dependency is undeclared: ${name}`);
    return new vm.SyntheticModule(Object.keys(members), function expose() {
      for (const [key, value] of Object.entries(members)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  return module.namespace;
}

if (mode === 'seed') {
  const state = { operation_id: operationId, account_switched: false, owner_preserved: false, checkpoint: {},
    converted: 0, uploads: 0, posts: {}, notifications: 0, status: 'draft', commits: 0 };
  const { createPersistentVideoClient } = await appModule(recipe.client_relative);
  const auth = { currentUser: { uid: original }, app: { options: {} } };
  const client = createPersistentVideoClient({
    auth, messages: () => ({}), readLegacy: async () => null, writeLegacy: async () => {}, warn: () => {},
    requestNoticePermission: async () => {}, clearCredentials: async () => {},
    authorize: async (_id, author) => { state.owner_preserved = author.uid === original; auth.currentUser = { uid: visible }; },
    savePayload: async (_id, payload) => { state.payload = payload; },
    native: {
      listJobs: async () => JSON.stringify(state.job ? [state.job] : []),
      sourceUri: async () => recipe.source_uri,
      enqueue: async metadata => { state.job = JSON.parse(metadata); state.status = 'queued'; },
    },
  });
  await client.enqueue({ ...draft, asset: { uri: recipe.source_uri, type: 'video' } });
  state.account_switched = auth.currentUser.uid === visible;
  state.owner_preserved &&= state.job.userId === original && state.payload.userId === original;
  save(state);
  process.stdout.write(report({ seeded: true }));
} else {
  const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
  if (state.operation_id !== operationId) throw new Error('Worker fixture binding mismatch');
  const runtime = { auth: { currentUser: { uid: original }, authStateReady: async () => {} }, storage: {}, db: {} };
  const increment = value => ({ increment: value });
  const firestore = {
    doc: (_database, ...parts) => parts.join('/'), increment, serverTimestamp: () => null,
    runTransaction: async (_database, body) => {
      const writes = [];
      await body({
        get: async ref => ({ exists: () => Object.hasOwn(state.posts, ref), data: () => state.posts[ref] }),
        set: (ref, value) => writes.push([ref, value]), update: (ref, value) => writes.push([ref, value]),
      });
      for (const [ref, fields] of writes) {
        const previous = state.posts[ref] ?? {};
        state.posts[ref] = { ...previous, ...Object.fromEntries(Object.entries(fields).map(([key, value]) =>
          [key, value?.increment === undefined ? value : (previous[key] ?? 0) + value.increment])) };
      }
      if (writes.length) state.commits += 1;
      state.owner_preserved &&= state.posts[`${postsCollection}/${operationId}`]?.userId === original;
      save(state);
    },
  };
  const publisher = await appModule(recipe.publisher_relative, {
    'firebase/firestore': firestore,
    '@protrainer/core': { COMMUNITY_FEED_WRITE_COLLECTION: postsCollection,
      buildCommunityPostMediaFirestoreFields: media => ({ mediaType: media.mediaType }) },
    './UserStatsService': { DEFAULT_STATS: { postsCount: 0 } },
  });
  const native = {
    execution: async () => JSON.stringify({ checkpoint: state.checkpoint }),
    checkpoint: async (_job, _nonce, _phase, data) => {
      state.checkpoint = JSON.parse(data);
      save(state);
      if (mode === 'interrupt' && state.checkpoint.committed) {
        await new Promise(() => {
          process.stdout.write(report({ saved: true }), () => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0));
        });
      }
    },
    retainPrepared: async (_job, _nonce, data) => data,
    finish: async (_job, _nonce, status) => {
      state.status = status;
      if (status === 'posted' && state.checkpoint.committed) state.notifications += 1;
      save(state);
    },
  };
  const headless = await appModule(recipe.worker_relative, {
    'react-native': { NativeModules: { [jobsManifest.moduleName]: native } },
    'expo-file-system/legacy': { getInfoAsync: async () => ({ exists: true }) },
    './communityVideoPreparation': { prepareCommunityVideo: async () => {
      state.converted += 1;
      return { uri: recipe.prepared_uri, thumbnailUri: recipe.thumbnail_uri, preparedVideo: true };
    } },
    './communityPostMediaUpload': { uploadCommunityPostMedia: async (owner, _asset, _collection, _ref, ports) => {
      if (owner !== original || ports.storage !== runtime.storage || !state.converted) throw new Error('Upload not bound to conversion and owner');
      state.uploads += 1;
      return { mediaType: 'video' };
    } },
    './UploadMetadataService': { recordUploadMetadata: async () => {} },
    './communityVideoJobPublisher': { publishCommunityVideoJob: publisher.publishCommunityVideoJob },
    './communityVideoJobStorage': { readVideoJobPayload: async () => state.payload,
      videoJobContext: () => runtime, clearVideoJobContext: async () => { delete state.payload; save(state); } },
    '../hooks/useCommunityMediaCleanup': { discardPreparedVideo: async () => {} },
    '../../config/community-video-jobs.manifest.json': { default: jobsManifest },
  });
  await headless.runCommunityVideoHeadlessTask({ jobId: operationId, nonce: mode });
  process.stdout.write(report({ completed: state.status === 'posted' }));
}

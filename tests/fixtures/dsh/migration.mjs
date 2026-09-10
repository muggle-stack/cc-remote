/** Zero-provider checks against the exact installed upstream physical codecs. */
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createSessionFormatCatalog } from '@deepseek-ai/dsh-session-format';
import { releasedV0SessionFormatCodec, releasedV1SessionFormatCodec, sessionFormatV0ToV1 } from '@deepseek-ai/dsh-session-format-v0-to-v1';
import { sessionFormatV1ToV2 } from '@deepseek-ai/dsh-session-format-v1-to-v2';
import { assertReleasedV3Header, releasedV2SessionFormatCodec, releasedV3SessionFormatCodec, restoreReleasedV3Artifact, sessionFormatV2ToV3 } from '@deepseek-ai/dsh-session-format-v2-to-v3';

const catalog = createSessionFormatCatalog({
  currentVersion: 3,
  codecs: [releasedV0SessionFormatCodec, releasedV1SessionFormatCodec, releasedV2SessionFormatCodec, releasedV3SessionFormatCodec],
  currentEncoder: releasedV3SessionFormatCodec,
  migrations: [sessionFormatV0ToV1, sessionFormatV1ToV2, sessionFormatV2ToV3],
  restoreCurrent: artifact => restoreReleasedV3Artifact(artifact, new Set()),
  restoreTransformedCurrent: artifact => restoreReleasedV3Artifact(artifact, new Set()),
  restoreCurrentHeader(value) { assertReleasedV3Header(value); return value; },
});
const event = (type, data, surfaceOp) => ({ type, data, time: 10, ...(surfaceOp ? { surfaceOp } : {}) });
const records = [
  event('turn/start', { turn: 1 }),
  event('step/start', { turn: 1, step: 1 }),
  event('user/message', { id: 'human', role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: 'migration fixture' }] }, 'append'),
  event('request/header', { header: { config: { provider: 'mock', model: 'mock' }, system: 'fixture system' }, reason: 'initial' }),
  event('step/end', { turn: 1, step: 1 }),
  event('turn/end', { turn: 1, reason: { kind: 'completed' } }),
].map((record, seq) => ({ ...record, seq }));
const restore = (header, rows) => {
  const operation = catalog.createRestore(header, { recovery: 'strict', validation: 'current' });
  for (const row of rows) operation.decodeRow(row);
  return operation.finish();
};
for (const version of [0, 1, 2]) {
  const header = { type: 'session', version, id: `legacy-${version}`, createdAt: 1, delegationDepth: 0,
    agentPreset: 'code', ...(version === 2 ? { isSeeded: false } : { seedLength: 0 }) };
  const source = [header, ...records].map(row => JSON.stringify(row)).join('\n') + '\n';
  const path = new URL(`legacy-${version}.jsonl`, import.meta.url);
  await writeFile(path, source, { mode: 0o600 });
  const original = await readFile(path);
  const migrated = restore(header, JSON.parse(JSON.stringify(records)));
  assert.equal(migrated.header.version, 3);
  assert.equal(migrated.header.agentPreset, 'ptc');
  assert.equal(migrated.events.filter(row => row.type === 'user/message')[0].data.id, 'human');
  assert.ok(migrated.events.some(row => row.type === 'system/message'));
  assert.deepEqual(await readFile(path), original, 'source remains untouched');
  assert.deepEqual(restore(header, records), migrated, 'migration is deterministic');
  assert.throws(() => restore(header, [...records, { ...event('external/opaque', { seq: 1 }), seq: records.length, ignorable: true }]), /unclassified|unsupported/i);
  assert.deepEqual(await readFile(path), original, 'failed migration preserves source');
}
assert.throws(() => catalog.createRestore({ type: 'session', version: 4, id: 'future' },
  { recovery: 'strict', validation: 'current' }), /version|unsupported|newer/i);
console.log('PASS native V0/V1/V2 physical migration, preset mapping, source preservation and unsupported records');

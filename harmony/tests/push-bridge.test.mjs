import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import ts from '../../web/node_modules/typescript/lib/typescript.js';

const source = readFileSync(new URL('../entry/src/main/ets/model/PushBridge.ets', import.meta.url), 'utf8');
const js = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText.replace(/^import .*;$/gm, '');
const factory = new Function('util', 'notificationManager', 'pushService', 'vibrator', 'sameOrigin',
  js.replace('export class PushBridge', 'class PushBridge') + ';return PushBridge;');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

function setup(pushService, vibrator = {}) {
  const prefs = new Map();
  const replies = [];
  let handler;
  const controller = {
    getUrl: () => 'https://remote.example/', runJavaScript: async () => {},
    createWebMessagePorts: () => [{}, {
      onMessageEvent: fn => { handler = fn; }, close: () => {},
      postMessageEvent: value => replies.push(JSON.parse(value)),
    }], postMessage: () => {},
  };
  const PushBridge = factory({ generateRandomUUID: () => 'nonce' }, {
    isNotificationEnabled: async () => true, requestEnableNotification: async () => {},
  }, pushService, vibrator, (origin, current) => new URL(current).origin === origin);
  const instance = new PushBridge({}, {
    getSync: (key, fallback) => prefs.get(key) ?? fallback,
    put: async (key, value) => { prefs.set(key, value); }, flush: async () => {},
  });
  return { instance, prefs, replies, controller,
    send: (id, method) => handler(JSON.stringify({ id, method })),
    sendMessage: value => handler(value) };
}

test('recording haptics are a short touch pulse limited to the current origin and port', async () => {
  const pulses = [];
  const state = setup({}, { startVibration: async (effect, attribute) => pulses.push([effect, attribute]) });
  await state.instance.attach(state.controller, 'https://remote.example', '');
  state.controller.getUrl = () => 'https://outside.example/';
  state.sendMessage('recording-haptic');
  assert.deepEqual(pulses, []);
  state.controller.getUrl = () => 'https://remote.example/';
  state.sendMessage('recording-haptic');
  state.sendMessage('recording-haptic');
  assert.deepEqual(pulses, [[{ type: 'time', duration: 25 }, { id: 0, usage: 'touch' }]]);
  state.instance.detach();
  state.sendMessage('recording-haptic');
  assert.equal(pulses.length, 1);
  assert.deepEqual(state.replies, []);
});

test('unconfigured Push Kit leaves server switching usable without a phantom token', async () => {
  let deletes = 0;
  const state = setup({ getToken: async () => { throw { code: 1000900012 }; },
    deleteToken: async () => { deletes++; } });
  await state.instance.attach(state.controller, 'https://remote.example', '');
  state.send(1, 'token');
  await tick();
  assert.equal(state.replies[0].error, 'push_not_configured');
  assert.notEqual(state.prefs.get('pushIssued'), true);
  await state.instance.resetServer();
  assert.equal(deletes, 0);
});

test('failed logout invalidation is retried before any new token is exposed', async () => {
  let fail = true;
  let requests = 0;
  let deletes = 0;
  const state = setup({ getToken: async () => { requests++; return 'token'; },
    deleteToken: async () => { deletes++; if (fail) throw new Error('offline'); } });
  await state.instance.attach(state.controller, 'https://remote.example', '');
  state.send(1, 'token');
  await tick();
  assert.equal(state.prefs.get('pushIssued'), true);
  state.send(2, 'delete');
  await tick();
  assert.equal(state.prefs.get('pushReset'), true);
  state.send(3, 'token');
  await tick();
  assert.equal(requests, 1, 'old provider token must not be reused after failed invalidation');
  fail = false;
  state.send(4, 'token');
  await tick();
  assert.equal(requests, 2);
  assert.equal(deletes, 3);
  assert.equal(state.prefs.get('pushReset'), false);
});

test('server switch drains an in-flight getToken before invalidation and suppresses its stale reply', async () => {
  let resolveToken;
  let deletes = 0;
  const state = setup({ getToken: () => new Promise(resolve => { resolveToken = resolve; }),
    deleteToken: async () => { deletes++; } });
  await state.instance.attach(state.controller, 'https://remote.example', '');
  state.send(1, 'token');
  await tick();
  const switching = state.instance.resetServer();
  resolveToken('old-server-token');
  await switching;
  assert.equal(deletes, 1);
  assert.deepEqual(state.replies, []);
  assert.equal(state.prefs.get('pushIssued'), false);
});

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import ts from '../../web/node_modules/typescript/lib/typescript.js';

// Execute the production policy with the platform URL implementation substituted
// by Node's WHATWG URL. Device acceptance still checks ArkTS URL normalization.
const source = readFileSync(new URL('../entry/src/main/ets/model/UrlPolicy.ts', import.meta.url), 'utf8');
const bundle = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const js = bundle.outputText.replace("import { url } from '@kit.ArkTS';", 'const url = { URL: globalThis.URL };');
const policy = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
const server = 'https://remote.example';

test('server accepts only an HTTPS origin and canonicalizes ports and IPv6', () => {
  assert.equal(policy.serverOrigin(' HTTPS://REMOTE.EXAMPLE:443/ '), server);
  assert.equal(policy.serverOrigin('https://remote.example:8443'), server + ':8443');
  assert.equal(policy.serverOrigin('https://[::1]:8443/'), 'https://[::1]:8443');
  for (const value of ['http://remote.example', 'remote.example', server + '/path', server + '?',
    server + '#', 'https://user:pass@remote.example', server + ':0', server + ':65536',
    'https://remote.example\\@evil.example', 'https:///remote.example', 'https://remote.\nexample']) {
    assert.equal(policy.serverOrigin(value), '', value);
  }
});

test('exact origin matching cannot be widened by redirects, credentials or hostname suffixes', () => {
  assert.equal(policy.sameOrigin(server, server + '/api/login'), true);
  for (const value of [server + ':8443/', server + '.evil/path', 'https://evil/?' + server,
    'https://user@remote.example', 'blob:' + server + '/id', 'file:///tmp/page']) {
    assert.equal(policy.sameOrigin(server, value), false, value);
  }
});

test('microphone access requires the configured HTTPS origin and an audio-only request', () => {
  const audio = 'TYPE_AUDIO_CAPTURE';
  const video = 'TYPE_VIDEO_CAPTURE';
  assert.equal(policy.microphoneRequest(server, server + '/chat', server, [audio]), true);
  for (const origin of ['https://evil.example', 'http://remote.example', server + ':8443',
    server + '.evil', 'https://user@remote.example', 'null']) {
    assert.equal(policy.microphoneRequest(server, server, origin, [audio]), false, origin);
  }
  assert.equal(policy.microphoneRequest(server, 'https://evil.example', server, [audio]), false);
  assert.equal(policy.microphoneRequest('', server, server, [audio]), false);
  for (const resources of [[], [video], [audio, video]]) {
    assert.equal(policy.microphoneRequest(server, server, server, resources), false);
  }
});

test('same-origin navigation stays mounted and external clicks go to the system browser', () => {
  assert.equal(policy.navigationAction(server + '/login', server, server, true, true, false), 'allow');
  assert.equal(policy.navigationAction('https://docs.example/a', server, server, true, true, true), 'external');
  assert.equal(policy.navigationAction('https://docs.example/a', server, server, false, true, true), 'external');
  assert.equal(policy.navigationAction(server + '/preview', server, server, false, true, true), 'allow');
  assert.equal(policy.navigationAction('about:blank', server, '', false, true, false), 'allow');
  assert.equal(policy.navigationAction('about:blank', server, server, true, true, false), 'block');
});

test('target-blank links dispatch directly, while authenticated and delayed popups stay isolated', () => {
  assert.equal(policy.newWindowAction('https://github.com/org/repo/pull/73?tab=files#diff', server, true), 'external');
  assert.equal(policy.newWindowAction(server + '/preview', server, true), 'browser');
  for (const value of ['', 'about:blank']) {
    assert.equal(policy.newWindowAction(value, server, true), 'browser');
  }
  for (const value of ['', 'about:blank', server, 'https://github.com']) {
    assert.equal(policy.newWindowAction(value, server, false), 'block');
  }
  for (const value of [policy.SETTINGS_URL, 'file:///private/data', 'javascript:alert(1)',
    'intent://open', 'http://example.com', 'https://user:password@example.com',
    'https://example.com\\evil', 'https://example.com\n']) {
    assert.equal(policy.newWindowAction(value, server, true), 'block', value);
  }
});

test('automatic navigation cannot launch the browser, except an authorized popup first destination', () => {
  const target = 'https://github.com';
  assert.equal(policy.navigationAction(target, server, server, true, true, false), 'block');
  assert.equal(policy.navigationAction(target, server, server, false, true, false), 'block');
  assert.equal(policy.navigationAction(target, server, 'about:blank', false, true, false, true), 'external');
  assert.equal(policy.navigationAction(target, server, server, true, true, false, true), 'block');
  // Neither frame loads nor pending windows widen native settings access.
  assert.equal(policy.navigationAction(target, server, server, false, false, false, true), 'allow');
  assert.equal(policy.navigationAction(policy.SETTINGS_URL, server, server, false, true, false, true), 'block');
});

test('settings requires an exact command, a top-level user gesture and the chat document', () => {
  assert.equal(policy.navigationAction(policy.SETTINGS_URL, server, server, true, true, true), 'settings');
  for (const [value, current, chat, main, gesture] of [
    [policy.SETTINGS_URL, server, true, true, false],
    [policy.SETTINGS_URL, server, true, false, true],
    [policy.SETTINGS_URL, server, false, true, true],
    [policy.SETTINGS_URL, 'https://evil.example', true, true, true],
    [policy.SETTINGS_URL + '?action=delete', server, true, true, true],
  ]) assert.equal(policy.navigationAction(value, server, current, chat, main, gesture), 'block');
});

test('preserves opaque preview frames without allowing privileged command/file navigation', () => {
  for (const value of ['about:blank', 'blob:' + server + '/preview', 'https://preview.example/page']) {
    assert.equal(policy.navigationAction(value, server, server, true, false, false), 'allow');
  }
  for (const value of ['file:///private/data', 'intent://open', 'javascript:alert(1)', 'http://insecure.example']) {
    assert.equal(policy.navigationAction(value, server, server, true, true, true), 'block');
  }
});

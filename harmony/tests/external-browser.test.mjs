import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import ts from '../../web/node_modules/typescript/lib/typescript.js';

const transpile = (file) => ts.transpileModule(readFileSync(new URL(file, import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const policyJs = transpile('../entry/src/main/ets/model/UrlPolicy.ts')
  .replace("import { url } from '@kit.ArkTS';", 'const url = { URL: globalThis.URL };');
const policy = await import('data:text/javascript;base64,' + Buffer.from(policyJs).toString('base64'));
const openerJs = transpile('../entry/src/main/ets/model/ExternalBrowser.ets')
  .replace(/^import .*;$/gm, '').replace('export class ExternalBrowser', 'class ExternalBrowser');

function setup(startAbility) {
  let now = 10000;
  const Opener = new Function('httpsOrigin', 'Date', openerJs + ';return ExternalBrowser;')(
    policy.httpsOrigin, { now: () => now });
  return { opener: new Opener(), context: { startAbility }, advance: (ms) => { now += ms; } };
}

test('browser dispatch keeps the complete destination and uses OS resolution without credentials', async () => {
  const calls = [];
  const state = setup(async want => { calls.push(want); });
  const target = 'https://github.com/org/repo/pull/73?tab=files#diff';
  await state.opener.open(target, state.context);
  assert.deepEqual(calls, [{ uri: target, action: 'ohos.want.action.viewData', entities: ['entity.system.browsable'] }]);
  for (const value of ['file:///private/data', 'javascript:alert(1)', 'intent://open',
    'http://example.com', 'https://user:pass@example.com', 'https://example.com\n']) {
    await assert.rejects(state.opener.open(value, state.context), /Unsupported browser URL/);
  }
  assert.equal(calls.length, 1);
});

test('interception and popup callbacks cannot open duplicate browser windows', async () => {
  const calls = [];
  let finish;
  const state = setup(want => { calls.push(want); return new Promise(resolve => { finish = resolve; }); });
  const first = state.opener.open('https://github.com', state.context);
  state.advance(2000);
  await state.opener.open('https://github.com', state.context);
  await state.opener.open('https://docs.example', state.context);
  assert.equal(calls.length, 1);
  finish();
  await first;
  const retry = state.opener.open('https://github.com', state.context);
  finish();
  await retry;
  await state.opener.open('https://github.com', state.context);
  assert.equal(calls.length, 2);
  state.advance(1001);
  const laterClick = state.opener.open('https://github.com', state.context);
  finish();
  await laterClick;
  assert.equal(calls.length, 3);
});

test('OS launch failures are surfaced and do not permanently disable retry', async () => {
  for (const synchronous of [false, true]) {
    let attempts = 0;
    const state = setup(() => {
      attempts++;
      if (attempts !== 1) return Promise.resolve();
      if (synchronous) throw new Error('no browser');
      return Promise.reject(new Error('no browser'));
    });
    await assert.rejects(state.opener.open('https://github.com', state.context), /no browser/);
    state.advance(1001);
    await state.opener.open('https://github.com', state.context);
    assert.equal(attempts, 2);
  }
});

// Exercise the native page's routing callbacks with platform boundaries stubbed.
// ArkUI layout is compiled separately by DevEco; no browser mock can prove the
// operating system's actual handoff on a phone.
function shell(startAbility = async () => {}) {
  const source = readFileSync(new URL('../entry/src/main/ets/pages/Index.ets', import.meta.url), 'utf8');
  const callbacks = source.split('  @Builder')[0]
    .replace(/^import .*;$/gm, '')
    .replace(/@\w+(?:\([^)]*\))?\s*/g, '')
    .replace('struct Index {', 'class Index {') + '\n}';
  const js = ts.transpileModule(callbacks, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const timers = new Map();
  const toasts = [];
  const controllers = [];
  class Controller {
    url = 'about:blank';
    loaded = [];
    constructor() { controllers.push(this); }
    getUrl() { return this.url; }
    loadUrl(url) { this.loaded.push(url); this.url = url; }
    setCustomUserAgent() {}
    static getDefaultUserAgent() { return 'ArkWeb'; }
  }
  const state = setup(startAbility);
  const Index = new Function('webview', 'ExternalBrowser', ...Object.keys(policy), 'setTimeout', 'clearTimeout',
    js + ';return Index;')({ WebviewController: Controller }, state.opener.constructor, ...Object.values(policy),
    (fn) => { const id = timers.size + 1; timers.set(id, fn); return id; }, id => timers.delete(id));
  const page = new Index();
  page.getUIContext = () => ({ getHostContext: () => state.context,
    getPromptAction: () => ({ showToast: toast => toasts.push(toast.message) }) });
  page.server = 'https://remote.example';
  page.chat.url = page.server + '/';
  page.pushAttached = true;
  const mainRequest = (url, gesture = true) => ({ getRequestUrl: () => url,
    isMainFrame: () => true, isRequestGesture: () => gesture });
  function popup(url, gesture = true) {
    const settled = [];
    page.newWindow({ targetUrl: url, isUserTrigger: gesture, handler: {
      setWebController: controller => settled.push(controller),
    } });
    return settled;
  }
  return { page, popup, mainRequest, controllers, timers, toasts };
}

test('external anchor and target-blank routes leave the mounted chat and its push attachment untouched', async () => {
  const calls = [];
  const state = shell(async want => { calls.push(want.uri); });
  const chat = state.page.chat;
  assert.equal(state.page.intercept(state.mainRequest('https://github.com'), true), true);
  await Promise.resolve();
  assert.deepEqual(state.popup('https://docs.example'), [null]);
  await Promise.resolve();
  assert.deepEqual(calls, ['https://github.com', 'https://docs.example']);
  assert.equal(state.page.chat, chat);
  assert.equal(state.page.chat.getUrl(), 'https://remote.example/');
  assert.deepEqual(chat.loaded, []);
  assert.equal(state.page.browserOpen, false);
  assert.equal(state.page.pushAttached, true);
  assert.equal(state.controllers.length, 2); // Only the original page controllers.
});

test('user-created blank popup hands off its resolved URL and returns to chat', async () => {
  const calls = [];
  const state = shell(async want => { calls.push(want.uri); });
  const settled = state.popup('about:blank');
  state.page.attached(false);
  assert.deepEqual(settled, [state.page.browser]);
  assert.equal(state.page.intercept(state.mainRequest('https://github.com', false), false), true);
  await Promise.resolve();
  assert.deepEqual(calls, ['https://github.com']);
  assert.equal(state.page.browserOpen, false);
  assert.equal(state.page.popupNavigationPending, false);
  assert.deepEqual(state.page.chat.loaded, []);
  assert.equal(state.page.pushAttached, true);
});

test('both attached WebViews register downloads before resolving a popup', () => {
  const state = shell();
  const attached = [];
  state.page.downloads = { attach: controller => attached.push(controller) };
  state.page.attached(true);
  const settled = state.popup(state.page.server + '/preview');
  state.page.attached(false);
  assert.deepEqual(attached, [state.page.chat, state.page.browser]);
  assert.deepEqual(settled, [state.page.browser]);
  assert.deepEqual(state.page.chat.loaded, []);
});

test('same-origin popup stays internal and a cancelled attachment always settles its handler', () => {
  const state = shell(() => { throw new Error('must not launch'); });
  assert.deepEqual(state.popup('https://github.com', false), [null]);
  const settled = state.popup(state.page.server + '/preview');
  assert.equal(state.page.intercept(state.mainRequest(state.page.server + '/preview'), false), false);
  assert.equal(state.page.browserOpen, true);
  state.page.closeBrowser();
  assert.deepEqual(settled, [null]);
  assert.equal(state.timers.size, 0);
  assert.equal(state.page.popupNavigationPending, false);
});

test('a failed OS handoff reports an error without a blank internal page or a chat reload', async () => {
  const state = shell(async () => { throw new Error('browser unavailable'); });
  await state.page.openExternal('https://github.com');
  assert.equal(state.toasts.length, 1);
  assert.match(state.toasts[0], /未能打开系统浏览器/);
  assert.equal(state.page.browserOpen, false);
  assert.deepEqual(state.page.chat.loaded, []);
});

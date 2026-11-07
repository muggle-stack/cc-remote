import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import ts from '../../web/node_modules/typescript/lib/typescript.js';

function compile(path) {
  return ts.transpileModule(readFileSync(new URL(path, import.meta.url), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText.replace(/^import .*;$/gm, '').replace(/^export /gm, '');
}
const policy = new Function('url', compile('../entry/src/main/ets/model/UrlPolicy.ts') + ';return sameOrigin;')({ URL });
const factory = new Function('webview', 'fileIo', 'picker', 'util', 'sameOrigin',
  compile('../entry/src/main/ets/model/Downloads.ets') + ';return { Downloads, downloadName, downloadAllowed };');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

function setup() {
  const calls = [];
  const notices = [];
  const files = new Map();
  const state = { origin: 'https://remote.example', document: 'https://remote.example/',
    pick: async () => ['file://selected/report.png'], copyError: false };
  let sequence = 0;
  let delegate;
  class Delegate {
    onBeforeDownload(fn) { this.before = fn; }
    onDownloadFinish(fn) { this.finish = fn; }
    onDownloadFailed(fn) { this.fail = fn; }
  }
  const fs = {
    OpenMode: { WRITE_ONLY: 2 }, accessSync: () => true, mkdirSync() {}, listFileSync: () => [],
    unlinkSync: path => { files.delete(path); calls.push(['unlink', path]); },
    open: async uri => { calls.push(['open', uri]); return { fd: 42 }; },
    close: async file => { calls.push(['close', file.fd]); },
    copyFile: async (path, fd) => {
      calls.push(['copy', path, fd]);
      if (state.copyError) throw Error('disk full');
      files.set('selected', files.get(path));
    },
  };
  const api = factory({ WebDownloadDelegate: Delegate }, fs, {
    DocumentSaveOptions: class {}, DocumentViewPicker: class {
      async save(options) { calls.push(['picker', options.newFileNames]); return state.pick(); }
    },
  }, { generateRandomUUID: () => `uuid-${++sequence}` }, policy);
  const controller = { getUrl: () => state.document, setDownloadDelegate: value => { delegate = value; } };
  const downloads = new api.Downloads({ cacheDir: '/cache' }, () => state.origin, message => notices.push(message));
  downloads.attach(controller);
  function item(source = 'blob:https://remote.example/uuid', name = '报告.png') {
    const guid = `download-${++sequence}`;
    return { path: '', cancelled: false, getGuid: () => guid, getUrl: () => source,
      getSuggestedFileName: () => name,
      start(path) { this.path = path; files.set(path, new Uint8Array([0, 255, 42, 0])); },
      cancel() { this.cancelled = true; delegate.fail(this); } };
  }
  return { ...api, state, calls, notices, files, downloads, item, get delegate() { return delegate; } };
}

test('native downloads accept only configured-origin documents and HTTP/blob sources', () => {
  const { downloadAllowed } = setup();
  for (const source of ['https://remote.example/api/media/id?download=1', 'blob:https://remote.example/uuid']) {
    assert.equal(downloadAllowed('https://remote.example', 'https://remote.example/', source), true);
  }
  for (const source of ['blob:null/uuid', 'blob:https://remote.example.evil/uuid', 'https://evil.example/file',
    'file:///etc/passwd', 'data:text/html,hi', 'https://user@remote.example/file', 'http://remote.example/file']) {
    assert.equal(downloadAllowed('https://remote.example', 'https://remote.example/', source), false, source);
  }
  assert.equal(downloadAllowed('https://remote.example', 'about:blank', 'https://remote.example/file'), false);
});

test('untrusted downloads cancel without opening a picker or creating a file', () => {
  const s = setup();
  const item = s.item('blob:null/uuid');
  s.delegate.before(item);
  assert.equal(item.cancelled, true);
  assert.equal(item.path, '');
  assert.equal(s.calls.length, 0);
});

test('blob bytes reach the picker-selected FD unchanged; completion is deduplicated and temporary file removed', async () => {
  const s = setup();
  const item = s.item();
  s.delegate.before(item);
  s.delegate.finish(item);
  s.delegate.finish(item);
  await tick();
  assert.deepEqual(s.files.get('selected'), new Uint8Array([0, 255, 42, 0]));
  assert.equal(s.files.has(item.path), false);
  assert.deepEqual(s.calls.filter(c => c[0] === 'picker'), [['picker', ['报告.png']]]);
  assert.deepEqual(s.calls.filter(c => c[0] === 'close'), [['close', 42]]);
  assert.equal(s.notices.at(-1), '文件已保存。');
});

test('HTTP media stays with ArkWeb and concurrent downloads do not stack save dialogs', async () => {
  const s = setup();
  const one = s.item('https://remote.example/api/media/machine/id?download=1', 'video.mp4');
  const two = s.item();
  s.delegate.before(one);
  s.delegate.before(two);
  assert.equal(two.cancelled, true);
  assert.equal(one.cancelled, false);
  s.delegate.finish(one);
  await tick();
  const three = s.item();
  s.delegate.before(three);
  assert.notEqual(three.path, '');
  s.downloads.cancel();
});

test('picker cancellation cleans cache, reports cancellation, and allows retry', async () => {
  const s = setup();
  s.state.pick = async () => [];
  const item = s.item();
  s.delegate.before(item);
  s.delegate.finish(item);
  await tick();
  assert.equal(s.calls.some(c => c[0] === 'open'), false);
  assert.equal(s.files.size, 0);
  assert.equal(s.notices.at(-1), '已取消保存。');
  const retry = s.item();
  s.delegate.before(retry);
  assert.notEqual(retry.path, '');
  s.downloads.cancel();
});

test('server change during a picker prevents stale export, even after a new server is selected', async () => {
  const s = setup();
  let resolve;
  s.state.pick = () => new Promise(r => { resolve = r; });
  const item = s.item();
  s.delegate.before(item);
  s.delegate.finish(item);
  s.downloads.cancel();
  s.state.origin = 'https://other.example';
  resolve(['file://selected/stale']);
  await tick();
  assert.equal(s.calls.some(c => c[0] === 'open'), false);
  assert.equal(s.files.size, 0);
  assert.equal(s.notices.includes('文件已保存。'), false);
});

test('copy failure closes the granted FD, cleans cache and never reports success', async () => {
  const s = setup();
  s.state.copyError = true;
  const item = s.item();
  s.delegate.before(item);
  s.delegate.finish(item);
  await tick();
  assert.deepEqual(s.calls.filter(c => c[0] === 'close'), [['close', 42]]);
  assert.equal(s.files.size, 0);
  assert.equal(s.notices.includes('文件已保存。'), false);
});

test('native failure cleans partial files and ignores late callbacks', async () => {
  const s = setup();
  const item = s.item();
  s.delegate.before(item);
  s.delegate.fail(item);
  s.delegate.finish(item);
  await tick();
  assert.equal(s.files.size, 0);
  assert.equal(s.calls.some(c => c[0] === 'picker'), false);
  assert.equal(s.notices.filter(n => n.includes('下载失败')).length, 1);
});

test('native start failure cancels once without recursive failure notifications', () => {
  const s = setup();
  const item = s.item();
  item.start = () => { throw Error('cannot move temporary file'); };
  s.delegate.before(item);
  assert.equal(item.cancelled, true);
  assert.deepEqual(s.notices, ['无法开始下载，请重试。']);
  const retry = s.item();
  s.delegate.before(retry);
  assert.notEqual(retry.path, '');
  s.downloads.cancel();
});

test('suggested filenames cannot traverse paths or hide their extension', () => {
  const { downloadName } = setup();
  assert.equal(downloadName('../../私密/报告.pdf'), '_.._私密_报告.pdf');
  assert.equal(downloadName('..'), 'download');
  assert.equal(downloadName('name\u202eexe.png'), 'name_exe.png');
  const name = downloadName('文'.repeat(200) + '.pdf');
  assert.ok(Buffer.byteLength(name) < 255);
  assert.ok(name.endsWith('.pdf'));
});

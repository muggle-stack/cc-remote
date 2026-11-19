import { expect, test } from '../../web/node_modules/@playwright/test/index.mjs';
import { readFileSync } from 'node:fs';

const adapter = readFileSync(new URL('../entry/src/main/resources/rawfile/shell.js', import.meta.url), 'utf8');
async function install(page, origin = 'http://127.0.0.1:4177') {
  await page.evaluate(`${adapter}(${JSON.stringify(origin)});`);
}

test('native menu integration preserves the original sidebar rendering', async ({ page }) => {
  await page.goto('/tests/history-browser.html?mobile-sidebar=1');
  await expect(page.locator('.thread')).toBeVisible();
  const appearance = () => page.locator('.pane').evaluate(node => {
    const style = getComputedStyle(node);
    return [style.filter, style.transform, style.borderRadius, style.boxShadow, style.transition];
  });
  const before = await appearance();
  await install(page);
  expect(await appearance()).toEqual(before);
  await expect(page.locator('.pane')).toHaveCSS('filter', 'blur(0px)');
});

test('chat menu stays unchanged after native adapter installation and menu switches', async ({ page }) => {
  await page.goto('/tests/history-browser.html?header-menu=1&engine=codex');
  await expect(page.getByRole('button', { name: '更多设置' })).toBeVisible();
  await page.getByRole('button', { name: '更多设置' }).click();
  const originalItems = await page.locator('.header-menu-items').innerText();
  await page.keyboard.press('Escape');
  await install(page);
  await install(page);
  await expect(page.locator('#cc-remote-harmony-settings-fallback')).toBeHidden();
  for (let cycle = 0; cycle < 2; cycle++) {
    await page.getByRole('button', { name: '更多设置' }).click();
    await expect(page.locator('.header-menu-items')).toHaveText(originalItems, { useInnerText: true });
    await page.getByRole('button', { name: /^通知/ }).click();
    await page.getByRole('button', { name: '返回设置' }).click();
    await expect(page.locator('.header-menu-items')).toHaveText(originalItems, { useInnerText: true });
    await page.keyboard.press('Escape');
  }
});

test('client settings becomes visible after leaving chat without duplicating the adapter', async ({ page }) => {
  await page.goto('/tests/history-browser.html?header-menu=1');
  await expect(page.getByRole('button', { name: '更多设置' })).toBeVisible();
  await install(page);
  await expect(page.locator('#cc-remote-harmony-settings-fallback')).toBeHidden();
  await page.evaluate(() => document.querySelector('#root').replaceChildren());
  await install(page);
  await expect(page.getByRole('link', { name: '客户端设置', exact: true })).toHaveCount(1);
  await expect(page.getByRole('link', { name: '客户端设置', exact: true })).toBeVisible();
});

test('client settings remains reachable at login', async ({ page }) => {
  await page.goto('/tests/history-browser.html?header-menu=1');
  await expect(page.getByRole('button', { name: '更多设置' })).toBeVisible();
  await page.evaluate(() => document.querySelector('#root').replaceChildren());
  await install(page);
  await expect(page.getByRole('link', { name: '客户端设置', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: '客户端设置', exact: true })).toHaveAttribute('href', 'cc-remote-app://settings');
});

test('other origins and iframe documents receive no native settings link', async ({ page }) => {
  await page.goto('/tests/history-browser.html?header-menu=1');
  await install(page, 'https://other.example');
  await expect(page.locator('#cc-remote-harmony-style')).toHaveCount(0);
  await page.evaluate(() => {
    const frame = document.createElement('iframe');
    frame.src = '/tests/history-browser.html?header-menu=1';
    document.body.append(frame);
  });
  await expect.poll(() => page.frames().length).toBe(2);
  const frame = page.frames()[1];
  await frame.waitForURL(/history-browser\.html/);
  await frame.waitForLoadState();
  await install(frame);
  await expect(frame.locator('#cc-remote-harmony-style')).toHaveCount(0);
});

const pushAdapter = readFileSync(new URL('../entry/src/main/resources/rawfile/push-bridge.js', import.meta.url), 'utf8');
async function attachPush(page, options = {}) {
  const origin = 'http://127.0.0.1:4177';
  await page.evaluate(`${pushAdapter}(${JSON.stringify(origin)}, 'test-handshake');`);
  await page.evaluate((options) => {
    const channel = new MessageChannel();
    window.pushCalls = [];
    window.pushEnabled = options.enabled ?? false;
    window.pushToken = 'private-native-token-'.repeat(8);
    window.invalidatedPushTokens = [];
    window.failPushDelete = false;
    channel.port1.onmessage = (event) => {
      if (event.data === 'recording-haptic') {
        window.pushCalls.push(event.data);
        return;
      }
      const request = JSON.parse(event.data);
      window.pushCalls.push(request.method);
      const result = { id: request.id };
      if (request.method === 'status') result.enabled = window.pushEnabled;
      if (request.method === 'enable') {
        window.pushEnabled = !options.deny;
        result.enabled = window.pushEnabled;
      }
      if (request.method === 'token') {
        if (options.unconfigured) result.error = 'push_not_configured';
        else result.token = window.pushToken;
      }
      if (request.method === 'delete') {
        if (window.failPushDelete) result.error = 'native_failed';
        else {
          window.invalidatedPushTokens.push(window.pushToken);
          window.pushToken += 'next';
        }
      }
      channel.port1.postMessage(JSON.stringify(result));
    };
    window.postMessage('test-handshake', location.origin, [channel.port2]);
  }, options);
  await expect.poll(() => page.evaluate(() => !!window.ccRemoteHarmonyPush)).toBe(true);
}

test('native push bridge rejects wrong origin, iframe and forged port handshake', async ({ page }) => {
  await page.goto('/tests/history-browser.html?header-menu=1');
  await page.evaluate(`${pushAdapter}('https://wrong.example', 'known');`);
  expect(await page.evaluate(() => !!window.ccRemoteHarmonyPush)).toBe(false);
  expect(await page.evaluate(() => !!window.ccRemoteHarmonyRecordingHaptic)).toBe(false);
  await page.evaluate(`${pushAdapter}(location.origin, 'unpredictable');`);
  await page.evaluate(() => {
    const fake = new MessageChannel();
    window.postMessage('wrong-nonce', location.origin, [fake.port1]);
  });
  expect(await page.evaluate(() => !!window.ccRemoteHarmonyPush)).toBe(false);
  await page.evaluate(() => {
    const frame = document.createElement('iframe');
    frame.src = '/tests/history-browser.html';
    document.body.append(frame);
  });
  await expect.poll(() => page.frames().length).toBe(2);
  const frame = page.frames()[1];
  await frame.waitForLoadState();
  await frame.evaluate(`${pushAdapter}(location.origin, 'frame');`);
  expect(await frame.evaluate(() => !!window.ccRemoteHarmonyPush)).toBe(false);
  expect(await frame.evaluate(() => !!window.ccRemoteHarmonyRecordingHaptic)).toBe(false);
  await attachPush(page);
  await page.evaluate(() => window.ccRemoteHarmonyRecordingHaptic());
  await expect.poll(() => page.evaluate(() => window.pushCalls)).toContain('recording-haptic');
  expect(await page.evaluate(async () => {
    try { await window.ccRemoteHarmonyPush.request('readFiles'); return false; } catch { return true; }
  })).toBe(true);
});

test('native notification permission, binding and off use only Harmony endpoints', async ({ page }) => {
  const calls = [];
  await page.route('**/api/push/harmony/**', route => {
    calls.push([route.request().url(), route.request().postDataJSON()]);
    return route.fulfill({ json: route.request().url().endsWith('/config') ? { enabled: true } : { ok: true } });
  });
  await page.route('**/api/push-config', () => { throw new Error('native must not call Web Push'); });
  await page.goto('/tests/history-browser.html?header-menu=1');
  await page.evaluate(() => import('/src/harmony-push.ts'));
  await attachPush(page);
  expect(await page.evaluate(async () => {
    const native = await import('/src/harmony-push.ts');
    const push = await import('/src/push.ts');
    if (!await native.requestHarmonyPermission()) return false;
    if (!await push.enableRemotePush('device-a', 'session')) return false;
    return push.disableRemotePush();
  })).toBe(true);
  const subscribed = calls.find(([path]) => path.endsWith('/subscribe'))[1];
  expect(subscribed.machine_id).toBe('device-a');
  expect(subscribed.notification_mode).toBe('session');
  expect(calls.some(([path]) => path.endsWith('/unsubscribe'))).toBe(true);
  expect(await page.evaluate(() => window.pushCalls)).toContain('delete');
});

test('disabled Relay and missing signed capability do not report push enabled', async ({ page }) => {
  let enabled = false;
  let subscriptions = 0;
  await page.route('**/api/push/harmony/**', route => {
    const config = route.request().url().endsWith('/config');
    if (!config) subscriptions++;
    return route.fulfill({ json: config ? { enabled } : { ok: true } });
  });
  await page.goto('/tests/history-browser.html?header-menu=1');
  await page.evaluate(() => import('/src/harmony-push.ts'));
  await attachPush(page, { unconfigured: true });
  expect(await page.evaluate(async () => (await import('/src/harmony-push.ts')).requestHarmonyPermission())).toBe(false);
  expect(await page.evaluate(() => window.pushCalls)).not.toContain('enable');
  enabled = true;
  expect(await page.evaluate(async () => (await import('/src/push.ts')).enableRemotePush('device-a', 'generic'))).toBe(false);
  expect(await page.evaluate(async () => (await import('/src/harmony-push.ts')).harmonyPushSnapshot().error)).toContain('开通 Push Kit');
  expect(subscriptions).toBe(0);
});

test('ordinary Web keeps VAPID registration and does not call Harmony endpoints', async ({ page }) => {
  const calls = [];
  await page.route('**/api/push-config', route => route.fulfill({ json: { enabled: true, public_key: 'YQ' } }));
  await page.route('**/api/push/subscribe', route => {
    calls.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true } });
  });
  await page.route('**/api/push/harmony/**', () => { throw new Error('Web must not call native API'); });
  await page.goto('/tests/history-browser.html?header-menu=1');
  expect(await page.evaluate(async () => {
    const subscription = { toJSON: () => ({ endpoint: 'https://push.example/sub', keys: { p256dh: 'public', auth: 'private' } }) };
    Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: { ready: Promise.resolve({
      pushManager: { getSubscription: async () => subscription },
    }) } });
    window.PushManager ??= class {};
    return (await import('/src/push.ts')).enableRemotePush('device-a', 'generic');
  })).toBe(true);
  expect(calls).toEqual([{ machine_id: 'device-a', notification_mode: 'generic',
    endpoint: 'https://push.example/sub', keys: { p256dh: 'public', auth: 'private' } }]);
});

test('denied native permission never requests a provider token', async ({ page }) => {
  await page.route('**/api/push/harmony/config', route => route.fulfill({ json: { enabled: true } }));
  await page.goto('/tests/history-browser.html?header-menu=1');
  await page.evaluate(() => import('/src/harmony-push.ts'));
  await attachPush(page, { deny: true });
  expect(await page.evaluate(async () => (await import('/src/harmony-push.ts')).requestHarmonyPermission())).toBe(false);
  expect(await page.evaluate(() => window.pushCalls)).not.toContain('token');
});

test('foreground reconciliation and changed target serialize without stale final binding', async ({ page }) => {
  await page.goto('/tests/history-browser.html?header-menu=1');
  expect(await page.evaluate(async () => {
    const { PushBindingCoordinator } = await import('/src/push.ts');
    const calls = [];
    let release;
    const coordinator = new PushBindingCoordinator(async target => {
      calls.push(target);
      if (calls.length === 1) await new Promise(resolve => { release = resolve; });
      return true;
    });
    const first = coordinator.setTarget({ machineId: 'device-a', mode: 'session' });
    const second = coordinator.setTarget({ machineId: 'device-b', mode: 'generic' }, true);
    release();
    await Promise.all([first, second]);
    await coordinator.setTarget({ machineId: 'device-b', mode: 'generic' }, true);
    await coordinator.setTarget(null, true);
    return { calls, snapshot: coordinator.snapshot() };
  })).toEqual({ calls: [
    { machineId: 'device-a', mode: 'session' },
    { machineId: 'device-b', mode: 'generic' },
    { machineId: 'device-b', mode: 'generic' }, null,
  ], snapshot: { state: 'off', target: null, bound: null } });
});

test('native Off invalidates provider token even when Relay is unreachable', async ({ page }) => {
  await page.route('**/api/push/harmony/config', route => route.fulfill({ json: { enabled: true } }));
  await page.route('**/api/push/harmony/subscribe', route => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/push/harmony/unsubscribe', route => route.abort());
  await page.goto('/tests/history-browser.html?header-menu=1');
  await attachPush(page, { enabled: true });
  expect(await page.evaluate(async () => {
    const push = await import('/src/push.ts');
    if (!await push.enableRemotePush('device-a', 'generic')) return false;
    return push.disableRemotePush();
  })).toBe(true);
  expect(await page.evaluate(() => window.pushCalls)).toContain('delete');
});

for (const failure of ['lost response', 'timeout', 'HTTP failure']) {
  test(`native subscribe rolls back an accepted binding after ${failure}`, async ({ page }) => {
    const bindings = new Map();
    let failSubscribe = true;
    await page.route('**/api/push/harmony/**', async route => {
      const path = new URL(route.request().url()).pathname.split('/').pop();
      if (path === 'config') return route.fulfill({ json: { enabled: true } });
      const body = route.request().postDataJSON();
      if (path === 'unsubscribe') return route.abort(); // Provider revocation must suffice.
      bindings.set(body.token, body);
      if (!failSubscribe) return route.fulfill({ json: { ok: true } });
      if (failure === 'timeout') return; // The real fetch must honor AbortSignal.timeout.
      if (failure === 'HTTP failure') return route.fulfill({ status: 503 });
      return route.abort();
    });
    await page.goto('/tests/history-browser.html?header-menu=1');
    await attachPush(page, { enabled: true });
    await page.evaluate(() => {
      // Accelerate only the request deadline; native messages use their real timer.
      const timeout = AbortSignal.timeout.bind(AbortSignal);
      AbortSignal.timeout = ms => timeout(ms === 12000 ? 100 : ms);
    });
    expect(await page.evaluate(async () =>
      !!await (await import('/src/push.ts')).enableRemotePush('device-a', 'generic'))).toBe(false);
    const revoked = await page.evaluate(() => window.invalidatedPushTokens);
    expect(bindings.size).toBe(1); // Relay really committed before the response failed.
    expect([...bindings.keys()].every(token => revoked.includes(token))).toBe(true);
    expect(await page.evaluate(async () =>
      (await import('/src/harmony-push.ts')).harmonyPushSnapshot().error)).not.toBe('');
    failSubscribe = false;
    expect(await page.evaluate(async () =>
      (await import('/src/push.ts')).enableRemotePush('device-a', 'generic'))).toBe(true);
    expect([...bindings.keys()].filter(token => !revoked.includes(token))).toHaveLength(1);
  });
}

test('failed native mode update clears the revoked binding and can recover', async ({ page }) => {
  let failSubscribe = false;
  await page.route('**/api/push/harmony/**', route => {
    const path = new URL(route.request().url()).pathname.split('/').pop();
    if (path === 'subscribe' && failSubscribe) return route.abort();
    return route.fulfill({ json: path === 'config' ? { enabled: true } : { ok: true } });
  });
  await page.goto('/tests/history-browser.html?header-menu=1');
  await attachPush(page, { enabled: true });
  await page.evaluate(async () => {
    const push = await import('/src/push.ts');
    window.coordinator = new push.PushBindingCoordinator(target => target
      ? push.enableRemotePush(target.machineId, target.mode) : push.disableRemotePush());
  });
  const generic = { machineId: 'device-a', mode: 'generic' };
  const session = { ...generic, mode: 'session' };
  expect(await page.evaluate(target => window.coordinator.setTarget(target), generic))
    .toEqual({ state: 'remote', target: generic, bound: generic });
  failSubscribe = true;
  expect(await page.evaluate(target => window.coordinator.setTarget(target), session))
    .toEqual({ state: 'local', target: session, bound: null });
  expect(await page.evaluate(() => window.coordinator.isRemoteActive('device-a'))).toBe(false);
  failSubscribe = false;
  expect(await page.evaluate(target => window.coordinator.setTarget(target), generic))
    .toEqual({ state: 'remote', target: generic, bound: generic });
});

test('failed native rollback reports uncertainty and retries cleanup before subscribing', async ({ page }) => {
  let subscriptions = 0;
  await page.route('**/api/push/harmony/**', route => {
    const path = new URL(route.request().url()).pathname.split('/').pop();
    if (path === 'config') return route.fulfill({ json: { enabled: true } });
    if (path === 'subscribe') subscriptions++;
    return route.abort();
  });
  await page.goto('/tests/history-browser.html?header-menu=1');
  await attachPush(page, { enabled: true });
  await page.evaluate(() => { window.failPushDelete = true; });
  for (let i = 0; i < 2; i++) {
    expect(await page.evaluate(async () =>
      !!await (await import('/src/push.ts')).enableRemotePush('device-a', 'generic'))).toBe(false);
    expect(await page.evaluate(async () =>
      (await import('/src/harmony-push.ts')).harmonyPushSnapshot().error)).toContain('清理尚未确认');
  }
  expect(subscriptions).toBe(1);
  await page.evaluate(() => { window.failPushDelete = false; });
  expect(await page.evaluate(async () => (await import('/src/push.ts')).disableRemotePush())).toBe(true);
  expect(await page.evaluate(() => window.invalidatedPushTokens)).toHaveLength(1);
});

for (const native of [false, true]) {
  for (const failure of ['load', 'delegate']) {
    test(`${native ? 'Harmony' : 'Web'} push handles lazy ${failure} failure without leaving binding pending`, async ({ page }) => {
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let chunkRequests = 0;
      const chunk = native ? 'harmony-push-client' : 'web-push';
      await page.route(`**/src/${chunk}.ts*`, route => {
        chunkRequests++;
        if (failure === 'load') return route.abort();
        const methods = native
          ? ['requestHarmonyPermission', 'enableHarmonyPush', 'disableHarmonyPush']
          : ['enableRemotePush', 'disableRemotePush'];
        return route.fulfill({ contentType: 'application/javascript', body: methods.map(method =>
          `export async function ${method}() { throw new Error('private provider detail'); }`,
        ).join('\n') });
      });
      await page.goto('/tests/history-browser.html?header-menu=1');
      if (native) await attachPush(page, { enabled: true });
      const result = await page.evaluate(async (native) => {
        const push = await import('/src/push.ts');
        const harmony = await import('/src/harmony-push.ts');
        const coordinator = new push.PushBindingCoordinator(target => target
          ? push.enableRemotePush(target.machineId, target.mode)
          : push.disableRemotePush());
        const target = { machineId: 'device-a', mode: 'generic' };
        const states = [];
        coordinator.subscribe(snapshot => states.push(snapshot.state));
        const enabled = await push.enableRemotePush(target.machineId, target.mode);
        const first = await coordinator.setTarget(target);
        const retry = await coordinator.setTarget(target, true);
        const disabled = await push.disableRemotePush();
        const off = await coordinator.setTarget(null, true);
        const permission = native ? await harmony.requestHarmonyPermission() : false;
        return { enabled, disabled, permission, states, first, retry, off,
          error: harmony.harmonyPushSnapshot().error };
      }, native);
      expect(chunkRequests).toBeGreaterThan(0);
      expect(result.enabled).toBe(false);
      expect(result.disabled).toBe(false);
      expect(result.permission).toBe(false);
      expect(result.first).toEqual({ state: 'local', target: { machineId: 'device-a', mode: 'generic' }, bound: null });
      expect(result.retry).toEqual(result.first);
      expect(result.off).toEqual({ state: 'off', target: null, bound: null });
      expect(result.states).toEqual(['off', 'binding', 'local', 'binding', 'local', 'binding', 'off']);
      if (native) expect(result.error).toContain('刷新后重试');
      expect(result.error).not.toContain('private provider detail');
      expect(errors).toEqual([]);
    });
  }
}

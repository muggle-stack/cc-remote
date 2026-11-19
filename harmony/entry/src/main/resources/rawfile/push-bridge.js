(function installPushBridge(origin, nonce) {
  if (window.top !== window || location.origin !== origin) return;
  const allowed = new Set(['status', 'enable', 'token', 'delete']);
  function attach(event) {
    if (event.data !== nonce || event.ports.length !== 1) return;
    window.removeEventListener('message', attach);
    const port = event.ports[0];
    const pending = new Map();
    let sequence = 0;
    let closed = false;
    port.onmessage = (message) => {
      let result;
      try { result = JSON.parse(message.data); } catch { return; }
      const waiter = pending.get(result.id);
      if (!waiter) return;
      pending.delete(result.id);
      clearTimeout(waiter.timer);
      if (result.error) waiter.reject(new Error(result.error));
      else waiter.resolve({ enabled: result.enabled, token: result.token });
    };
    port.start();
    const bridge = Object.freeze({
      request(method) {
        if (closed || !allowed.has(method)) return Promise.reject(new Error('unavailable'));
        if (pending.size >= 8) return Promise.reject(new Error('busy'));
        return new Promise((resolve, reject) => {
          const id = ++sequence;
          const timer = setTimeout(() => {
            pending.delete(id);
            reject(new Error('native_timeout'));
          }, 30000);
          pending.set(id, { resolve, reject, timer });
          port.postMessage(JSON.stringify({ id, method }));
        });
      }
    });
    Object.defineProperty(window, 'ccRemoteHarmonyPush', { value: bridge, configurable: true });
    Object.defineProperty(window, 'ccRemoteHarmonyRecordingHaptic', {
      value: () => { if (!closed) port.postMessage('recording-haptic'); },
      configurable: true
    });
    window.addEventListener('pagehide', () => {
      closed = true;
      port.close();
      for (const waiter of pending.values()) {
        clearTimeout(waiter.timer);
        waiter.reject(new Error('unavailable'));
      }
      pending.clear();
      delete window.ccRemoteHarmonyPush;
      delete window.ccRemoteHarmonyRecordingHaptic;
    }, { once: true });
    window.dispatchEvent(new Event('cc-remote-harmony-ready'));
  }
  // A fresh unpredictable nonce is only installed in the trusted top document;
  // a preview iframe cannot substitute a forged port via window.postMessage.
  window.addEventListener('message', attach);
  setTimeout(() => window.removeEventListener('message', attach), 10000);
})

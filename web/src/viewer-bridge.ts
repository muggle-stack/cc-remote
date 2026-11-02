import { PROTOCOL_VERSION } from "./protocol.ts";

export interface BridgeGrant {
  id: string; runner: string; entry: string; script_origins: string[];
}
interface Pending {
  id: number; wire: string; message: Record<string, unknown>;
  expected: number | null; received: number; credit: number;
  timer: number; started: number;
}
const MAX_ACTIVE = 4;
const MAX_QUEUE = 64;
const MAX_FILE = 256 * 1024 * 1024;
const CHUNK = 64 * 1024;

function identity(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (n) => n.toString(16).padStart(2, "0")).join("");
}

/** Only this trusted parent owns cookies and the resource socket. Each frame
 * gets a dedicated port, bound to one grant, never an arbitrary fetch proxy. */
export class ViewerBridge {
  private ws: WebSocket | null = null;
  private port: MessagePort | null = null;
  private pending = new Map<number, Pending>();
  private active = new Map<string, Pending>();
  private queue: Pending[] = [];
  private seen = new Set<number>();
  private ready = false;
  private bound = false;
  private connected = false;
  private pageLoaded = false;
  private created = Date.now();
  private disposed = false;
  private loads = 0;
  private timer: number;
  private nonce = identity();
  private requestTotal = 0;
  private listener: (event: MessageEvent) => void;
  private onLoad: () => void;
  private frame: HTMLIFrameElement;
  private grant: BridgeGrant;
  private onReady: () => void;
  private onError: (message: string, clear: boolean) => void;

  constructor(frame: HTMLIFrameElement, grant: BridgeGrant,
    onReady: () => void, onError: (message: string, clear: boolean) => void) {
    this.frame = frame; this.grant = grant; this.onReady = onReady; this.onError = onError;
    this.listener = (event) => {
      if (event.source !== frame.contentWindow || event.origin !== "null"
          || event.data?.type !== "cc-viewer-bridge-ready"
          || event.data?.id !== grant.id || this.connected || this.disposed) return;
      if (event.data.v !== PROTOCOL_VERSION) {
        this.fail("预览组件版本不一致，请刷新 cc-remote 页面后重试。", false);
        return;
      }
      this.ready = true;
      this.connect();
    };
    this.onLoad = () => {
      if (++this.loads > 1) this.fail("预览页面发生跳转，请重新打开。", true);
    };
    window.addEventListener("message", this.listener);
    frame.addEventListener("load", this.onLoad);
    this.timer = window.setInterval(() => {
      if (this.disposed) return;
      if (!this.connected && ++this.requestTotal > 6) this.fail("预览连接超时，请重新连接。", false);
      if (this.connected && !this.pageLoaded && Date.now() - this.created > 120000)
        this.fail("页面启动超时，请检查代码、CDN 连接和设备状态。", false);
      if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "ping" }));
    }, 5000);
    // Avoid opening a second socket in StrictMode's synchronous probe mount.
    queueMicrotask(() => {
      if (this.disposed) return;
      const url = new URL("/ws/viewer-client", location.href);
      url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(url);
      this.ws = ws;
      ws.binaryType = "arraybuffer";
      ws.onopen = () => {
        if (!this.disposed) ws.send(JSON.stringify({ type: "bind", v: PROTOCOL_VERSION, id: grant.id }));
      };
      ws.onmessage = (event) => this.receive(event.data);
      ws.onerror = () => this.fail("预览资源连接中断，请重新连接。", false);
      ws.onclose = () => this.fail("预览资源连接已断开，请重新连接。", false);
      frame.src = grant.runner;
    });
  }

  private connect() {
    if (!this.ready || !this.bound || this.connected || this.disposed) return;
    this.connected = true;
    window.removeEventListener("message", this.listener);
    const channel = new MessageChannel();
    this.port = channel.port1;
    this.port.onmessage = (event) => this.command(event.data);
    this.frame.contentWindow?.postMessage({ type: "cc-viewer-bridge-init", v: PROTOCOL_VERSION,
      id: this.grant.id, nonce: this.nonce, entry: this.grant.entry, script_origins: this.grant.script_origins,
    }, "*", [channel.port2]); // The exact target is an opaque frame; no secret is sent.
  }

  private command(data: unknown) {
    if (this.disposed || !data || typeof data !== "object") return;
    const msg = data as Record<string, unknown>;
    if (msg.nonce !== this.nonce) return;
    if (msg.type === "loaded") { this.pageLoaded = true; this.onReady(); return; }
    // A codec/media failure need not disconnect the page or other resources.
    if (msg.type === "media-error") {
      this.onError(typeof msg.message === "string" ? msg.message.slice(0, 400) : "预览音视频加载失败。", false);
      return;
    }
    if (msg.type === "error") {
      this.fail(typeof msg.message === "string" ? msg.message.slice(0, 400) : "页面加载失败。", false);
      return;
    }
    const id = msg.id;
    if (typeof id !== "number" || !Number.isSafeInteger(id) || id <= 0) { this.fail("预览资源请求无效。", true); return; }
    if (msg.type === "read") {
      if (this.seen.has(id) || this.seen.size >= 4096) { this.fail("预览资源请求超限。", true); return; }
      this.seen.add(id);
      if (this.queue.length >= MAX_QUEUE || typeof msg.path !== "string" || msg.path.length > 4096
          || !["GET", "HEAD"].includes(String(msg.method)) || !msg.headers || typeof msg.headers !== "object"
          || Object.entries(msg.headers).some(([key, value]) => !["range", "if-none-match", "if-range"].includes(key)
            || typeof value !== "string" || value.length > 256 || Array.from(value).some((char) => char.charCodeAt(0) < 32))) {
        this.port?.postMessage({ type: "failed", id, message: "资源请求过多或无效。" }); return;
      }
      const read: Pending = { id, wire: identity(), message: { type: "read", path: msg.path,
        method: msg.method, headers: msg.headers }, expected: null, received: 0, credit: 0,
        timer: 0, started: Date.now() };
      this.pending.set(id, read);
      this.queue.push(read);
      this.arm(read, 60000);
      this.drain();
    } else if (msg.type === "pull") {
      const read = this.pending.get(id);
      if (!read || !this.active.has(read.wire)) return;
      if (read.credit >= 8) { this.fail("预览读取窗口超限。", true); return; }
      read.credit++;
      this.send({ type: "pull", request_id: read.wire, credits: 1 });
    } else if (msg.type === "cancel") {
      const read = this.pending.get(id);
      if (read) this.finish(read, true);
    } else this.fail("预览资源请求无效。", true);
  }

  private arm(read: Pending, ms = 35000) {
    clearTimeout(read.timer);
    read.timer = window.setTimeout(() => {
      this.port?.postMessage({ type: "failed", id: read.id, message: "资源读取超时，请重新连接。" });
      this.finish(read, true);
    }, Math.min(ms, Math.max(1, 600000 - (Date.now() - read.started))));
  }

  private send(message: object) {
    if (this.ws?.readyState === WebSocket.OPEN && this.ws.bufferedAmount < 1024 * 1024) this.ws.send(JSON.stringify(message));
    else this.fail("预览连接不可用。", false);
  }

  private drain() {
    while (!this.disposed && this.bound && this.active.size < MAX_ACTIVE && this.queue.length) {
      const read = this.queue.shift()!;
      this.active.set(read.wire, read);
      this.arm(read);
      this.send({ ...read.message, request_id: read.wire });
    }
  }

  private receive(data: unknown) {
    if (this.disposed) return;
    try {
      if (data instanceof ArrayBuffer) {
        if (data.byteLength < 17 || data.byteLength > CHUNK + 16) throw new Error();
        const wire = Array.from(new Uint8Array(data, 0, 16), (n) => n.toString(16).padStart(2, "0")).join("");
        const read = this.active.get(wire);
        if (!read) return; // A cancelled request may have already-sent chunks.
        const size = data.byteLength - 16;
        if (read.expected === null || read.credit <= 0 || read.received + size > read.expected) throw new Error();
        read.credit--;
        read.received += size;
        this.arm(read);
        const bytes = data.slice(16);
        this.port?.postMessage({ type: "chunk", id: read.id, bytes }, [bytes]);
        return;
      }
      if (typeof data !== "string" || data.length > 8192) throw new Error();
      const msg = JSON.parse(data);
      if (msg.type === "bound") {
        if (this.bound || msg.v !== PROTOCOL_VERSION || msg.id !== this.grant.id) throw new Error();
        this.bound = true; this.connect(); this.drain(); return;
      }
      if (msg.type === "pong") return;
      if (msg.type === "error") {
        const clear = [401, 403, 410].includes(msg.status) && msg.error !== "device_offline";
        this.fail(clear ? "预览权限已失效，请重新打开。" : "设备或预览连接已断开，请重新连接。", clear); return;
      }
      const read = this.active.get(msg.request_id);
      if (!read) return;
      if (msg.type === "response") {
        if (read.expected !== null || ![200, 206, 304, 416].includes(msg.status)
            || !msg.headers || typeof msg.headers !== "object") throw new Error();
        const expected = Number(msg.headers["content-length"] ?? 0);
        if (!Number.isSafeInteger(expected) || expected < 0 || expected > MAX_FILE) throw new Error();
        read.expected = read.message.method === "HEAD" || ![200, 206].includes(msg.status) ? 0 : expected;
        this.port?.postMessage({ type: "response", id: read.id, status: msg.status, headers: msg.headers });
      } else if (msg.type === "end") {
        if (read.expected === null || read.received !== read.expected) throw new Error();
        this.port?.postMessage({ type: "end", id: read.id }); this.finish(read);
      } else if (msg.type === "failed") {
        this.port?.postMessage({ type: "failed", id: read.id, status: msg.status,
          message: msg.status === 403 ? "资源未登记或无权读取。" : "资源不存在、已更改或读取失败。" });
        this.finish(read);
      } else throw new Error();
    } catch { this.fail("预览资源协议不匹配，请刷新页面。", true); }
  }

  private finish(read: Pending, cancel = false) {
    clearTimeout(read.timer);
    if (cancel && this.active.has(read.wire) && this.ws?.readyState === WebSocket.OPEN)
      this.ws.send(JSON.stringify({ type: "cancel", request_id: read.wire }));
    this.active.delete(read.wire);
    this.pending.delete(read.id);
    this.queue = this.queue.filter((item) => item !== read);
    this.drain();
  }

  private fail(message: string, clear: boolean) {
    if (this.disposed) return;
    this.onError(message, clear);
    this.dispose();
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    window.clearInterval(this.timer);
    window.removeEventListener("message", this.listener);
    this.frame.removeEventListener("load", this.onLoad);
    this.port?.postMessage({ type: "disconnected" });
    this.port?.close();
    for (const read of this.pending.values()) clearTimeout(read.timer);
    this.pending.clear(); this.active.clear(); this.queue = [];
    this.ws?.close();
  }
}

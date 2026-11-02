export const VIRTUAL_ORIGIN = "https://cc-remote-viewer.invalid";
const MAX_FILE = 256 * 1024 * 1024;
const MAX_CODE = 2 * 1024 * 1024;
const MAX_DOCUMENT = 16 * 1024 * 1024;
const MAX_CODE_TEXT = 16 * 1024 * 1024;
// One decoded HTML document (including inert model data), plus code text.
const MAX_TOTAL_TEXT = MAX_DOCUMENT * 2 + MAX_CODE_TEXT;
const MAX_BLOBS = 64 * 1024 * 1024;

function resourcePath(url: string): string {
  const path = new URL(url, VIRTUAL_ORIGIN).pathname;
  try { return decodeURIComponent(path).slice(0, 180); } catch { return path.slice(0, 180); }
}

function oversizedText(url: string, size: number, limit: number, label: string): Error {
  return new Error(`${label}过大：${resourcePath(url)}（${(size / 1024 / 1024).toFixed(2)} MiB，${size} 字节），`
    + `上限 ${limit / 1024 / 1024} MiB。请将内嵌数据拆为独立资源后按需加载。`);
}
interface Read {
  resolve: (response: Response) => void;
  reject: (error: Error) => void;
  controller?: ReadableStreamDefaultController<Uint8Array>;
  signal: AbortSignal;
  abort: () => void;
  remaining: number;
  outstanding: number;
  head: boolean;
}

/** Lives only in the opaque frame. The port cannot address another publication. */
export class Resources {
  private next = 0;
  private reads = new Map<number, Read>();
  private closed = false;
  private blobs = new Map<string, Promise<string>>();
  private urls: string[] = [];
  private blobBytes = 0;
  private textBytes = 0;
  private textReserved = 0;
  private codeBytes = 0;
  private blobReserved = 0;
  private versions = new Map<string, string>();
  private cleanups = new Set<() => void>();
  private port: MessagePort;
  private nonce: string;
  private base: string;

  constructor(port: MessagePort, nonce: string, base: string) {
    this.port = port; this.nonce = nonce; this.base = base;
    port.onmessage = (event) => this.receive(event.data);
  }

  send(value: object) { this.port.postMessage({ ...value, nonce: this.nonce }); }

  onClose(cleanup: () => void) {
    if (this.closed) cleanup();
    else this.cleanups.add(cleanup);
  }

  url(value: string, base = this.base): URL {
    const url = new URL(value, base);
    if (url.origin !== VIRTUAL_ORIGIN || url.username || url.password)
      throw new Error("此预览仅支持已登记目录内的资源；外部数据请求未开放。");
    return url;
  }

  fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    if (this.closed) throw new Error("预览连接已断开，请重新连接。");
    const request = new Request(input instanceof Request ? input : new URL(String(input), this.base), init);
    const url = this.url(request.url);
    if (!["GET", "HEAD"].includes(request.method) || request.body)
      throw new Error("远程预览只支持静态资源 GET / HEAD，不支持提交数据。");
    if (request.signal.aborted) throw new DOMException("Aborted", "AbortError");
    if (this.reads.size >= 68 || this.next >= 4096) throw new Error("预览资源请求超限。");
    const headers: Record<string, string> = {};
    for (const key of ["range", "if-none-match", "if-range"]) {
      const value = request.headers.get(key);
      if (value !== null) headers[key] = value;
    }
    const id = ++this.next;
    return new Promise<Response>((resolve, reject) => {
      const read: Read = { resolve, reject, signal: request.signal, remaining: 0, outstanding: 0, head: request.method === "HEAD",
        abort: () => this.fail(id, new DOMException("Aborted", "AbortError"), true) };
      this.reads.set(id, read);
      request.signal.addEventListener("abort", read.abort, { once: true });
      this.send({ type: "read", id, path: url.pathname, method: request.method, headers });
    });
  };

  private pull(id: number, read: Read) {
    // Eight 64 KiB credits, including queued chunks, cap each stream at 512 KiB.
    const room = Math.max(0, Math.floor(read.controller?.desiredSize ?? 0));
    while (read.remaining > read.outstanding * 65536 && read.outstanding < Math.min(8, room)) {
      read.outstanding++;
      this.send({ type: "pull", id });
    }
  }

  private receive(msg: Record<string, unknown>) {
    if (msg.type === "disconnected") { this.close(); return; }
    const id = Number(msg.id);
    const read = this.reads.get(id);
    if (!read) return;
    try {
      if (msg.type === "response") {
        const status = Number(msg.status);
        const headers = new Headers(msg.headers as Record<string, string>);
        const length = Number(headers.get("content-length") ?? 0);
        if (!Number.isSafeInteger(length) || length < 0 || length > MAX_FILE || read.controller)
          throw new Error("预览资源大小无效。");
        read.remaining = !read.head && [200, 206].includes(status) ? length : 0;
        const body = read.head || status === 304 ? null : new ReadableStream<Uint8Array>({
          start: (controller) => { read.controller = controller; },
          pull: () => this.pull(id, read),
          cancel: () => { this.send({ type: "cancel", id }); this.remove(id); },
        }, { highWaterMark: 8 });
        read.resolve(new Response(body, { status, headers }));
      } else if (msg.type === "chunk") {
        if (!(msg.bytes instanceof ArrayBuffer) || !read.controller || read.outstanding <= 0
            || msg.bytes.byteLength > read.remaining) throw new Error("预览资源传输无效。");
        read.outstanding--;
        read.remaining -= msg.bytes.byteLength;
        read.controller.enqueue(new Uint8Array(msg.bytes));
        this.pull(id, read);
      } else if (msg.type === "end") {
        read.controller?.close(); this.remove(id);
      } else if (msg.type === "failed") {
        this.fail(id, new Error(String(msg.message ?? "资源读取失败。")));
      }
    } catch (error) { this.fail(id, error instanceof Error ? error : new Error("资源读取失败。"), true); }
  }

  private remove(id: number) {
    const read = this.reads.get(id);
    if (read) read.signal.removeEventListener("abort", read.abort);
    this.reads.delete(id);
  }

  private fail(id: number, error: Error, cancel = false) {
    const read = this.reads.get(id);
    if (!read) return;
    if (cancel && !this.closed) this.send({ type: "cancel", id });
    read.reject(error);
    read.controller?.error(error);
    this.remove(id);
  }

  async text(url: string, kind: "code" | "document" = "code"): Promise<string> {
    const response = await this.fetch(url);
    const limit = kind === "document" ? MAX_DOCUMENT : MAX_CODE;
    const size = Number(response.headers.get("content-length"));
    let problem: Error | undefined;
    if (!response.ok) problem = new Error(`预览资源读取失败（HTTP ${response.status}）：${resourcePath(url)}。`);
    else if (!response.headers.has("content-length") || !Number.isSafeInteger(size) || size < 0)
      problem = new Error(`预览资源大小无效：${resourcePath(url)}。`);
    else if (size > limit) problem = oversizedText(url, size, limit, kind === "document" ? "预览 HTML" : "预览代码资源");
    else if (this.textBytes + this.textReserved + size * 2 > MAX_TOTAL_TEXT)
      problem = new Error(`预览文本超过 ${MAX_TOTAL_TEXT / 1024 / 1024} MiB 总预算：${resourcePath(url)}。`);
    if (problem) {
      await response.body?.cancel();
      throw problem;
    }
    // Reserve before decoding: parallel stylesheets must share the same bound.
    // The binary channel already enforces each response's declared byte length.
    const reservation = size * 2;
    this.textReserved += reservation;
    try {
      const text = await response.text();
      if (text.length > limit || this.textBytes + this.textReserved - reservation + text.length * 2 > MAX_TOTAL_TEXT)
        throw new Error(`预览文本超过加载预算：${resourcePath(url)}。`);
      this.remember(url, response);
      this.textBytes += text.length * 2;
      return text;
    } finally { this.textReserved -= reservation; }
  }

  checkCode(source: string, url: string) {
    // A larger HTML allowance must not turn large inline scripts/styles into
    // an unbounded parser workload. Inert JSON/mesh data is never parsed as JS.
    const size = new Blob([source]).size;
    if (size > MAX_CODE) throw oversizedText(url, size, MAX_CODE, "预览脚本/样式");
    if (this.codeBytes + source.length * 2 > MAX_CODE_TEXT)
      throw new Error(`预览脚本/样式超过 ${MAX_CODE_TEXT / 1024 / 1024} MiB 文本预算：${resourcePath(url)}。`);
    this.codeBytes += source.length * 2;
  }

  blob(data: BlobPart, type: string): string {
    const blob = new Blob([data], { type });
    if (this.blobBytes + blob.size > MAX_BLOBS) throw new Error("预览资源超过内存缓存预算。");
    this.blobBytes += blob.size;
    const url = URL.createObjectURL(blob);
    this.urls.push(url);
    return url;
  }

  asset(value: string, base: string): Promise<string> {
    if (value.startsWith("data:")) return Promise.resolve(value);
    const url = this.url(value, base).href;
    let promise = this.blobs.get(url);
    if (!promise) {
      promise = this.fetch(url).then(async (response) => {
        const size = Number(response.headers.get("content-length"));
        if (!response.ok || size > MAX_BLOBS - this.blobBytes - this.blobReserved) {
          await response.body?.cancel(); throw new Error("预览资源无法读取或超过缓存预算。");
        }
        this.blobReserved += size;
        try {
          const body = await response.blob();
          this.remember(url, response);
          return this.blob(body, response.headers.get("content-type") ?? "application/octet-stream");
        } finally { this.blobReserved -= size; }
      });
      this.blobs.set(url, promise);
    }
    return promise;
  }

  private remember(url: string, response: Response) {
    const key = this.url(url).pathname;
    const etag = response.headers.get("etag");
    if (!etag || (this.versions.has(key) && this.versions.get(key) !== etag))
      throw new Error("预览文件在加载期间发生变化，请刷新。");
    this.versions.set(key, etag);
  }

  async verify() {
    // Revalidate the small code/asset graph, not all model files. A directory
    // isn't an atomic snapshot; detect observed edits rather than mixing them.
    const versions = [...this.versions];
    for (let i = 0; i < versions.length; i += 4) {
      await Promise.all(versions.slice(i, i + 4).map(async ([path, etag]) => {
        const response = await this.fetch(new URL(path, VIRTUAL_ORIGIN), { method: "HEAD" });
        if (!response.ok || response.headers.get("etag") !== etag)
          throw new Error("预览文件在加载期间发生变化，请刷新。");
      }));
    }
  }

  close() {
    this.closed = true;
    for (const cleanup of this.cleanups) cleanup();
    this.cleanups.clear();
    for (const id of this.reads.keys()) this.fail(id, new Error("预览连接已断开，请重新连接。"));
  }

  dispose() {
    this.close();
    for (const url of this.urls) URL.revokeObjectURL(url);
    this.urls = []; this.blobs.clear();
    this.port.close();
  }
}

import { createFile, type Movie, type MP4BoxBuffer } from "mp4box";
import type { Resources } from "./resources";

const CHUNK = 256 * 1024;
const MAX_METADATA = 8 * 1024 * 1024;
const MAX_PENDING = 16 * 1024 * 1024;
const MAX_FILE = 256 * 1024 * 1024;
export interface MediaAsset { element: HTMLMediaElement; url: string }
type MediaSourceConstructor = typeof MediaSource;
class UnsupportedMedia extends Error {}

/** No native network access or credentials enter the opaque frame. MP4/M4A
 * ranges use the existing publication port; MP4Box remuxes (never transcodes)
 * them into MSE fragments. Other browser codecs retain bounded blob fallback.
 */
export function installMedia(resources: Resources, assets: MediaAsset[], report: (message: string) => void): () => void {
  const MAX_RESIDENT = 8;
  const loadingAttribute = "data-cc-remote-media-loading";
  const loadingStyle = document.createElement("style");
  // Chromium's disabled native controls swallow even capturing pointer events.
  // Until a source is ready, let the media element receive the first play tap.
  loadingStyle.textContent = `[${loadingAttribute}]::-webkit-media-controls{pointer-events:none!important}`;
  document.head.append(loadingStyle);
  interface Entry {
    element: HTMLMediaElement; visible: boolean; pendingPlay: boolean; used: number;
    load?: Promise<void>; activate: (explicit: boolean) => Promise<void> | undefined;
    retire: () => void; dispose: () => void;
  }
  const entries = new Map<HTMLMediaElement, Entry>();
  const resident = new Set<Entry>();
  let closed = false, clock = 0;
  const abort = () => new DOMException("Media load cancelled", "AbortError");
  const prefetch = (entry: Entry) => { void entry.activate(false)?.catch(() => {}); };
  const observer = new IntersectionObserver(changes => {
    if (closed) return;
    // Apply the whole visibility batch before selecting an eviction victim.
    for (const change of changes) entries.get(change.target as HTMLMediaElement)!.visible = change.isIntersecting;
    for (const entry of entries.values()) if (entry.visible) prefetch(entry);
  });
  for (const { element, url } of assets) {
    element.setAttribute(loadingAttribute, "");
    const play = element.play.bind(element), pause = element.pause.bind(element);
    const ownPlay = Object.getOwnPropertyDescriptor(element, "play");
    const ownPause = Object.getOwnPropertyDescriptor(element, "pause");
    const preload = element.getAttribute("preload");
    let position = 0, intent = 0, pendingPlays = 0;
    let unload = () => {};
    const entry: Entry = {
      element, visible: false, pendingPlay: false, used: 0,
      activate(explicit) {
        if (closed) return Promise.reject(abort());
        entry.used = ++clock;
        if (entry.load) return entry.load;
        if (resident.size >= MAX_RESIDENT) {
          const victim = [...resident].filter(item => !item.pendingPlay && item.element.paused
            && (explicit || !item.visible)).sort((a, b) => a.used - b.used)[0];
          if (!victim) return explicit ? Promise.reject(new Error("请先暂停一个播放器后再播放。")) : undefined;
          victim.retire();
        }
        resident.add(entry);
        element.preload = "auto";
        const request = new AbortController();
        let player: RangeMedia | undefined;
        let release = () => {};
        entry.load = new Promise<void>((resolve, reject) => {
          let ready = false;
          const fail = (error: Error) => {
            if (request.signal.aborted) return;
            reject(error);
            if (ready) { entry.retire(); report(error.message); }
          };
          const loaded = () => {
            if (position > 0) element.currentTime = position;
            element.removeAttribute(loadingAttribute);
            ready = true;
            resolve();
          };
          const cancelled = () => reject(abort());
          const failed = () => fail(new Error("浏览器无法解码此预览媒体。"));
          element.addEventListener("loadedmetadata", loaded, { once: true });
          request.signal.addEventListener("abort", cancelled, { once: true });
          unload = () => {
            request.abort(); player?.dispose();
            element.removeEventListener("loadedmetadata", loaded);
            element.removeEventListener("error", failed);
            pause(); element.srcObject = null; element.removeAttribute("src"); element.load();
            release();
          };
          const blobFallback = () => {
            void resources.mediaAsset(url, request.signal).then(asset => {
              if (request.signal.aborted) { asset.release(); return; }
              release = asset.release;
              element.addEventListener("error", failed, { once: true });
              element.src = asset.url; element.load();
            }).catch(fail);
          };
          const Source = window.MediaSource ?? (window as unknown as { ManagedMediaSource?: MediaSourceConstructor }).ManagedMediaSource;
          if (Source && /\.(?:mp4|m4v|m4a)$/i.test(new URL(url).pathname)) {
            player = new RangeMedia(resources, element, url, Source, message => fail(new Error(message)), blobFallback);
            player.open();
          } else blobFallback();
        });
        void entry.load.catch(error => {
          if (!request.signal.aborted && !closed) {
            entry.retire(); report(error instanceof Error ? error.message : "预览音视频加载失败。");
          }
        });
        return entry.load;
      },
      retire() {
        if (!resident.delete(entry)) return;
        if (element.readyState > 0) position = element.currentTime;
        intent++; pendingPlays = 0; entry.pendingPlay = false;
        unload(); unload = () => {}; entry.load = undefined;
        element.setAttribute(loadingAttribute, "");
      },
      dispose() {
        entry.retire();
        element.removeAttribute(loadingAttribute);
        if (preload === null) element.removeAttribute("preload"); else element.setAttribute("preload", preload);
        element.removeEventListener("click", clicked, true);
        element.removeEventListener("play", touched);
        if (ownPlay) Object.defineProperty(element, "play", ownPlay); else delete (element as Partial<HTMLMediaElement>).play;
        if (ownPause) Object.defineProperty(element, "pause", ownPause); else delete (element as Partial<HTMLMediaElement>).pause;
      },
    };
    const touched = () => { entry.used = ++clock; };
    element.play = async () => {
      const token = intent;
      pendingPlays++;
      entry.pendingPlay = true;
      try {
        await entry.activate(true);
        if (closed || token !== intent) throw abort();
        return await play();
      } finally {
        if (token === intent) entry.pendingPlay = --pendingPlays > 0;
      }
    };
    element.pause = () => { intent++; pendingPlays = 0; entry.pendingPlay = false; pause(); };
    const clicked = (event: MouseEvent) => {
      // Native controls cannot start a source-less lazy element. Honour the
      // first tap even if it arrives before the visibility prefetch completes.
      if (element.controls && !element.readyState && !event.defaultPrevented) {
        event.preventDefault();
        void element.play().catch(error => {
          if (!closed && error?.name !== "AbortError" && error?.name !== "NotAllowedError") report(String(error));
        });
      }
    };
    element.addEventListener("click", clicked, true);
    element.addEventListener("play", touched);
    entries.set(element, entry);
    observer.observe(element);
  }
  // Keep small existing pages' eager preload behaviour. preload=none galleries
  // warm only visible controls so native buttons are usable without loading
  // every sample. Explicit play() also loads off-screen/custom players.
  for (const entry of entries.values()) {
    if (entry.element.autoplay || entry.element.preload !== "none") {
      if (resident.size >= MAX_RESIDENT) break;
      prefetch(entry);
    }
  }
  return () => {
    closed = true; observer.disconnect();
    for (const entry of entries.values()) entry.dispose();
    loadingStyle.remove();
  };
}

class RangeMedia {
  private file = createFile(true);
  private source: MediaSource;
  private objectUrl?: string;
  private buffers = new Map<number, SourceBuffer>();
  private queued: { buffer: SourceBuffer; data: ArrayBuffer }[] = [];
  private length = 0;
  private etag = "";
  private offset = 0;
  private info?: Movie;
  private ready = false;
  private closed = false;
  private busy = false;
  private ended = false;
  private seeking = false;
  private generation = 0;
  private request?: AbortController;
  private timer = 0;
  private opening = 0;
  private resources: Resources;
  private element: HTMLMediaElement;
  private url: string;
  private Source: MediaSourceConstructor;
  private report: (message: string) => void;
  private fallback: () => void;

  constructor(resources: Resources, element: HTMLMediaElement, url: string, Source: MediaSourceConstructor,
    report: (message: string) => void, fallback: () => void) {
    this.resources = resources; this.element = element; this.url = url; this.Source = Source; this.report = report;
    this.fallback = fallback;
    this.source = new Source();
    this.file.onReady = info => { this.info = info; };
    this.file.onError = () => { throw new Error("无法解析预览音视频文件。"); };
    this.file.onSegment = (id, _user, data, nextSample) => {
      const buffer = this.buffers.get(id);
      if (buffer) this.queued.push({ buffer, data });
      this.file.releaseUsedSamples(id, nextSample);
      if (this.queued.reduce((sum, item) => sum + item.data.byteLength, 0) > MAX_PENDING)
        throw new Error("预览媒体分段超过内存预算。");
    };
  }

  open() {
    const media = this.element;
    // ManagedMediaSource on iPhone requires opting out of remote playback.
    media.disableRemotePlayback = true;
    this.source.addEventListener("sourceopen", this.start, { once: true });
    media.addEventListener("seeking", this.seek);
    media.addEventListener("play", this.wake);
    media.addEventListener("error", this.mediaError);
    this.opening = window.setTimeout(() => {
      if (this.source.readyState === "closed") this.fail(new UnsupportedMedia());
    }, 3000);
    // WebKit blocks blob:null MediaSource URLs in opaque frames, but supports
    // attaching that same MediaSource directly. Chromium uses the blob path.
    try { media.srcObject = this.source; }
    catch {
      this.objectUrl = URL.createObjectURL(this.source);
      media.src = this.objectUrl;
    }
    media.load();
  }

  private start = () => {
    window.clearTimeout(this.opening);
    void this.initialize().catch(this.fail);
  };

  private mediaError = () => this.fail(this.ready ? new Error("浏览器无法解码此预览媒体。") : new UnsupportedMedia());

  private async initialize() {
    this.request = new AbortController();
    const head = await this.resources.fetch(this.url, { method: "HEAD", signal: this.request.signal });
    this.length = Number(head.headers.get("content-length"));
    this.etag = head.headers.get("etag") ?? "";
    if (!head.ok || !this.etag || !Number.isSafeInteger(this.length) || this.length <= 0 || this.length > MAX_FILE)
      throw new Error("无法读取预览媒体信息。");
    let metadata = 0;
    while (!this.info && !this.closed) {
      const data = await this.read(this.offset);
      metadata += data.byteLength;
      if (metadata > MAX_METADATA) throw new Error("预览媒体索引超过 8 MiB 上限。");
      const next = this.file.appendBuffer(data);
      if (!this.info && (!Number.isSafeInteger(next) || next <= this.offset || next >= this.length))
        throw new Error("预览媒体缺少可读取的索引。");
      this.offset = next;
    }
    if (this.closed || !this.info) return;
    const info = this.info;
    const tracks = info.tracks.filter(track => track.video || track.audio);
    const duration = info.duration / info.timescale;
    if (!tracks.length || tracks.length > 4 || tracks.some(track => track.nb_samples > 1_000_000)
        || !Number.isFinite(duration) || duration <= 0 || info.isFragmented)
      throw new UnsupportedMedia();
    for (const track of tracks) {
      const mime = `${track.video ? "video" : "audio"}/mp4; codecs="${track.codec}"`;
      if (!this.Source.isTypeSupported(mime)) throw new UnsupportedMedia();
      const buffer = this.source.addSourceBuffer(mime);
      this.buffers.set(track.id, buffer);
      this.file.setSegmentOptions(track.id, null, { nbSamples: 32, rapAlignement: false });
    }
    for (const init of this.file.initializeSegmentation("per-track"))
      this.queued.push({ buffer: this.buffers.get(init.id)!, data: init.buffer });
    await this.append();
    if (this.closed) return;
    this.source.duration = duration;
    this.offset = this.file.seek(0, true).offset;
    this.ready = true;
    this.file.start();
    this.timer = window.setInterval(this.wake, 500);
    this.wake();
  }

  private async read(offset: number): Promise<MP4BoxBuffer> {
    if (!Number.isSafeInteger(offset) || offset < 0 || offset >= this.length) throw new Error("媒体读取位置无效。");
    const end = Math.min(offset + CHUNK, this.length) - 1;
    this.request = new AbortController();
    const response = await this.resources.fetch(this.url, { signal: this.request.signal,
      // The publication uses weak stat ETags, which HTTP forbids in If-Range.
      // Validate every returned range against the initial identity instead.
      headers: { Range: `bytes=${offset}-${end}` } });
    if (response.status !== 206 || response.headers.get("etag") !== this.etag
        || response.headers.get("content-range") !== `bytes ${offset}-${end}/${this.length}`
        || Number(response.headers.get("content-length")) !== end - offset + 1) {
      await response.body?.cancel();
      throw new Error("预览媒体已变化或无法分段读取，请刷新。");
    }
    const data = await response.arrayBuffer() as MP4BoxBuffer;
    data.fileStart = offset;
    return data;
  }

  private ahead() {
    const { buffered, currentTime } = this.element;
    for (let i = 0; i < buffered.length; i++)
      if (buffered.start(i) <= currentTime + 0.1 && buffered.end(i) > currentTime)
        return buffered.end(i) - currentTime;
    return 0;
  }

  private seek = () => {
    if (!this.ready || this.closed || this.ahead() > 1) return;
    this.seeking = true;
    this.generation++;
    this.request?.abort();
    this.wake();
  };

  private wake = () => {
    if (this.closed || !this.ready || this.busy || (!this.seeking && (this.ended || this.ahead() >= 15))) return;
    this.busy = true;
    void this.pump().catch(this.fail).finally(() => {
      this.busy = false;
      if (this.seeking && !this.closed) this.wake();
    });
  };

  private async pump() {
    if (this.seeking) {
      this.seeking = false; this.ended = false;
      this.file.stop(); this.queued = [];
      // seek() resets segmentation; resetting the release cursor also permits
      // previously evicted samples to be reclaimed after a backward seek.
      for (const id of this.buffers.keys()) this.file.releaseUsedSamples(id, 0);
      this.offset = this.file.seek(this.element.currentTime, true).offset;
      this.file.start();
    }
    const generation = this.generation;
    await this.append();
    while (!this.closed && generation === this.generation && this.ahead() < 15 && !this.ended) {
      const data = await this.read(this.offset);
      if (this.closed || generation !== this.generation) return;
      const end = data.fileStart + data.byteLength;
      const next = this.file.appendBuffer(data, end === this.length);
      const retained = this.file.stream.buffers.reduce((sum, item) => sum + item.byteLength, 0);
      if (retained + this.file.getAllocatedSampleDataSize() > MAX_PENDING)
        throw new Error("预览媒体缓冲超过 16 MiB 上限。");
      this.offset = next;
      await this.append();
      if (generation !== this.generation || this.closed) return;
      if (this.offset >= this.length) {
        this.file.flush(); await this.append();
        if (generation !== this.generation || this.closed) return;
        this.ended = true;
        if (this.source.readyState === "open") this.source.endOfStream();
      }
    }
  }

  private waitUpdate(buffer: SourceBuffer, action: () => void): Promise<void> {
    return new Promise((resolve, reject) => {
      const done = () => { cleanup(); resolve(); };
      const failed = () => { cleanup(); reject(new Error("浏览器无法解码此预览媒体。")); };
      const cleanup = () => {
        buffer.removeEventListener("updateend", done); buffer.removeEventListener("error", failed);
        this.source.removeEventListener("sourceclose", done);
      };
      buffer.addEventListener("updateend", done, { once: true });
      buffer.addEventListener("error", failed, { once: true });
      this.source.addEventListener("sourceclose", done, { once: true });
      try { action(); } catch (error) { cleanup(); reject(error); }
    });
  }

  private async append() {
    while (this.queued.length && !this.closed) {
      const { buffer, data } = this.queued.shift()!;
      // Bound native decoder history as well as JS buffers. A backward seek
      // simply reacquires the old range; it doesn't retain a full movie blob.
      const before = this.element.currentTime - 10;
      if (before > 0 && buffer.buffered.length && buffer.buffered.start(0) < before)
        await this.waitUpdate(buffer, () => buffer.remove(0, before));
      if (this.closed) return;
      await this.waitUpdate(buffer, () => buffer.appendBuffer(data));
    }
  }

  private fail = (error: unknown) => {
    if (this.closed || error instanceof DOMException && error.name === "AbortError") return;
    this.dispose();
    if (error instanceof UnsupportedMedia) this.fallback();
    else this.report(error instanceof Error ? error.message : "预览音视频加载失败。");
  };

  dispose() {
    if (this.closed) return;
    this.closed = true;
    this.request?.abort(); this.file.stop(); this.queued = [];
    window.clearInterval(this.timer); window.clearTimeout(this.opening);
    this.source.removeEventListener("sourceopen", this.start);
    this.element.removeEventListener("seeking", this.seek);
    this.element.removeEventListener("play", this.wake);
    this.element.removeEventListener("error", this.mediaError);
    HTMLMediaElement.prototype.pause.call(this.element); this.element.srcObject = null; this.element.removeAttribute("src"); this.element.load();
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
  }
}

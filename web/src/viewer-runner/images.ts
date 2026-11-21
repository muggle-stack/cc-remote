import type { Resources } from "./resources";

interface ImageLoad {
  url: string;
  previous: string | null;
  ready: Promise<string>;
}

/** Route runtime image assignments before the browser starts a native request. */
export function installImages(resources: Resources, report: (message: string) => void) {
  const images = HTMLImageElement.prototype;
  const src = Object.getOwnPropertyDescriptor(images, "src")!;
  const complete = Object.getOwnPropertyDescriptor(images, "complete")!;
  const { get: getSource, set: setSource } = src;
  const { setAttribute, removeAttribute, decode } = images;
  const pending = new WeakMap<HTMLImageElement, ImageLoad>();
  const changed = () => new DOMException("Image source changed.", "EncodingError");

  function current(image: HTMLImageElement) {
    const load = pending.get(image);
    // Native attribute changes can also supersede an in-flight assignment.
    if (load && image.getAttribute("src") === load.previous) return load;
  }

  function localUrl(value: string, base: string) {
    if (!value.trim()) return;
    try {
      const url = resources.url(value, base);
      if (url.protocol === "https:") return url;
    } catch {
      // Native data/blob handling and rejection of other URLs stay under CSP.
    }
  }

  function assign(image: HTMLImageElement, value: string) {
    const raw = String(value);
    const base = image.baseURI;
    const url = localUrl(raw, base);
    pending.delete(image);
    if (!url) {
      setSource!.call(image, raw);
      return;
    }

    const ready = resources.asset(url.href, base).then(blob => {
      if (current(image)?.ready !== ready) throw changed();
      pending.delete(image);
      setSource!.call(image, blob);
      return blob;
    }, (error: unknown) => {
      if (current(image)?.ready === ready) {
        pending.delete(image);
        removeAttribute.call(image, "src");
        const detail = error instanceof Error ? error.message : "资源读取失败。";
        report(`预览图片加载失败（${url.pathname.slice(0, 180)}）。${detail}`);
        image.dispatchEvent(new Event("error"));
      }
      throw new DOMException("Image could not be loaded.", "EncodingError");
    });
    pending.set(image, { url: url.href, previous: image.getAttribute("src"), ready });
    // Pages using load/error events do not consume the decode promise.
    void ready.catch(() => {});
  }

  Object.defineProperty(images, "src", {
    ...src,
    get(this: HTMLImageElement) { return current(this)?.url ?? getSource!.call(this); },
    set(this: HTMLImageElement, value: string) { assign(this, value); },
  });
  Object.defineProperty(images, "complete", {
    ...complete,
    get(this: HTMLImageElement) { return current(this) ? false : complete.get!.call(this); },
  });
  images.setAttribute = function (name, value) {
    const key = String(name);
    if (key.toLowerCase() === "src") assign(this, value);
    else setAttribute.call(this, key, value);
  };
  images.removeAttribute = function (name) {
    const key = String(name);
    if (key.toLowerCase() === "src") pending.delete(this);
    removeAttribute.call(this, key);
  };
  if (typeof decode === "function") {
    images.decode = async function () {
      const load = current(this);
      if (load) {
        const blob = await load.ready;
        if (current(this) || this.getAttribute("src") !== blob) throw changed();
      }
      return decode.call(this);
    };
  }
}

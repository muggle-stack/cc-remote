import { PROTOCOL_VERSION } from "../protocol";
import { Compiler } from "./compiler";
import { Resources, VIRTUAL_ORIGIN } from "./resources";
import { installPreviewEscape } from "../preview-keyboard";
import { installMedia } from "./media";
import { installViewerZoom } from "../viewer-zoom";

const id = /^\/__cc_viewer\/bridge\/([a-f0-9]{32})$/.exec(location.pathname)?.[1];
let opaque = false;
try { opaque = parent !== window && !parent.document; } catch { opaque = true; }
// Never execute source when opened directly or when the HTTP sandbox is absent.
if (id && parent !== window && opaque) {
  installPreviewEscape();
  let initialized = false;
  const ready = () => parent.postMessage({ type: "cc-viewer-bridge-ready", v: PROTOCOL_VERSION, id }, "*");
  const interval = window.setInterval(ready, 500);
  ready();
  const initialize = (event: MessageEvent) => {
    const value = event.data;
    if (initialized || event.source !== parent || value?.type !== "cc-viewer-bridge-init"
        || value.v !== PROTOCOL_VERSION || value.id !== id || event.ports.length !== 1
        || typeof value.nonce !== "string" || !/^[a-f0-9]{32}$/.test(value.nonce)
        || typeof value.entry !== "string" || !value.entry.startsWith("/") || value.entry.startsWith("//")
        || !Array.isArray(value.script_origins)) return;
    initialized = true;
    window.clearInterval(interval);
    window.removeEventListener("message", initialize);
    const base = new URL(value.entry, VIRTUAL_ORIGIN).href;
    const resources = new Resources(event.ports[0], value.nonce, base);
    const report = (message: string) => resources.send({ type: "error", message: message.slice(0, 400) });
    window.addEventListener("error", (error) => report(error.message || "预览脚本加载失败。"));
    window.addEventListener("unhandledrejection", (error) => report(error.reason instanceof Error ? error.reason.message : "预览脚本运行失败。"));
    document.addEventListener("securitypolicyviolation", (event) => {
      report(`页面使用了尚未支持的资源加载方式（${event.effectiveDirective}），请使用已验证的静态页面或 Isolated 模式。`);
    });
    window.addEventListener("pagehide", () => resources.dispose(), { once: true });
    // Request(relativeURL), used by Three.js FileLoader, resolves against this
    // logical base. Native network access remains blocked by response CSP.
    const logicalBase = document.createElement("base"); logicalBase.href = base;
    document.head.prepend(logicalBase);
    window.fetch = resources.fetch;
    void new Compiler(resources, new Set<string>(value.script_origins), base).document().then(async ({ doc, scripts, imports, media }) => {
      document.documentElement.lang = doc.documentElement.lang;
      document.documentElement.className = doc.documentElement.className;
      // Keep the base in place: removing/reinserting it momentarily restores
      // the real document URL and triggers base-uri violations in Chromium.
      for (const child of Array.from(document.head.childNodes)) if (child !== logicalBase) child.remove();
      document.head.append(...Array.from(doc.head.childNodes));
      document.body.replaceWith(doc.body);
      // WebKit may keep the empty resource selection made in DOMParser's inert
      // document, especially for nested <source>. Start selection only after
      // the rewritten sources are attached to the real sandbox document.
      document.querySelectorAll<HTMLMediaElement>("audio, video").forEach((media) => media.load());
      resources.onClose(installMedia(resources, media, message => resources.send({ type: "media-error", message: message.slice(0, 400) })));
      const map = document.createElement("script"); map.type = "importmap"; map.textContent = JSON.stringify({ imports });
      document.head.append(map);
      // Navigation/form submission is outside the static-preview contract.
      document.addEventListener("submit", (event) => { event.preventDefault(); report("远程预览不支持表单提交。"); }, true);
      document.addEventListener("click", (event) => {
        const link = (event.target as Element | null)?.closest?.("a[href]");
        if (link && !link.getAttribute("href")?.startsWith("#")) {
          event.preventDefault(); report("远程预览暂不支持页面跳转，请从预览列表打开页面。");
        }
      }, true);
      for (const item of scripts) {
        await new Promise<void>((resolve, reject) => {
          const script = document.createElement("script");
          if (item.type === "module") {
            script.type = "module";
            script.src = resources.blob(`import ${JSON.stringify(item.source)};`, "text/javascript");
          } else script.src = item.source;
          script.onload = () => resolve();
          script.onerror = () => reject(new Error("预览脚本加载失败，请检查已登记的 CDN 来源。"));
          document.body.append(script);
        });
      }
      document.dispatchEvent(new Event("DOMContentLoaded", { bubbles: true }));
      window.dispatchEvent(new Event("load"));
      installViewerZoom();
      resources.send({ type: "loaded" });
    }).catch((error: unknown) => report(error instanceof Error ? error.message : "预览加载失败。"));
  };
  window.addEventListener("message", initialize);
}

import type { PptxViewer, SlideHandle } from "@aiden0z/pptx-renderer";

export interface PresentationState {
  type: "state";
  pageNumber: number;
  pageCount: number;
  zoom: number;
  busy: boolean;
  partial: boolean;
  error: string | null;
}

// Runs in a separate, opaque-origin document under the HTML preview policy.
export function installPresentationPreview(
  renderer: typeof import("@aiden0z/pptx-renderer"),
) {
  let cleanup = () => {};
  window.addEventListener("pagehide", () => cleanup());
  window.addEventListener("message", async event => {
    if (event.source !== parent || event.data?.type !== "cc-remote-presentation"
        || !event.ports[0]
        || !(event.data.bytes instanceof ArrayBuffer)) return;
    cleanup();
    const port = event.ports[0];
    const inspectPage = Number.isInteger(event.data.inspectPage) ? Number(event.data.inspectPage) : null;
    const container = document.getElementById("slide")!;
    const rail = document.getElementById("pages")!;
    const scroll = document.getElementById("scroll")!;
    if (typeof event.data.accent === "string") {
      document.documentElement.style.setProperty("--accent", event.data.accent);
    }
    const state: PresentationState = { type: "state", pageNumber: 1, pageCount: 0,
      zoom: 100, busy: true, partial: false, error: null };
    let alive = true;
    let viewer: PptxViewer | null = null;
    let observer: IntersectionObserver | null = null;
    let continuous = event.data.mobile === true;
    let navigation = 0;
    let jumping = false;
    let settledTop: number | null = null;
    let scrollFrame = 0;
    const abort = new AbortController();
    const thumbnails = new Map<Element, SlideHandle>();
    const send = () => { if (alive) port.postMessage(state); };
    const finishInspection = () => {
      if (alive) port.postMessage({ ...state, type: "inspection" });
    };
    const selectPage = (index: number) => {
      rail.querySelector('[aria-current="page"]')?.removeAttribute("aria-current");
      rail.querySelector(`button[value="${index}"]`)?.setAttribute("aria-current", "page");
      state.pageNumber = index + 1;
      send();
    };
    const trackScroll = () => {
      cancelAnimationFrame(scrollFrame);
      scrollFrame = requestAnimationFrame(() => {
        if (!continuous || jumping
            || (settledTop !== null && Math.abs(scroll.scrollTop - settledTop) < 1)) return;
        settledTop = null;
        const top = scroll.getBoundingClientRect().top + 13;
        const slides = container.querySelectorAll<HTMLElement>("[data-slide-index]");
        const atEnd = scroll.scrollTop > 0 && scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 1;
        const visible = atEnd ? slides[slides.length - 1]
          : Array.from(slides).find(slide => slide.getBoundingClientRect().bottom > top);
        if (visible) selectPage(Number(visible.dataset.slideIndex));
      });
    };
    const fitTableInsets = (element: HTMLElement) => {
      // Some generators emit inch-sized margins. Use the OOXML default
      // horizontal inset only when the margins consume the entire cell.
      for (const cell of element.querySelectorAll<HTMLTableCellElement>("td")) {
        const inset = parseFloat(cell.style.paddingLeft) + parseFloat(cell.style.paddingRight);
        const tableWidth = cell.closest("table")?.parentElement?.clientWidth ?? cell.offsetWidth;
        const width = Math.min(cell.offsetWidth, tableWidth);
        if (width > 0 && inset >= width) {
          cell.style.paddingLeft = cell.style.paddingRight = `${Math.min(9.6, width / 4)}px`;
        }
      }
    };
    const updatePage = async (index: number) => {
      if (!viewer || !state.pageCount) return;
      const next = Math.max(0, Math.min(state.pageCount - 1, index));
      const request = ++navigation;
      jumping = true;
      state.error = null;
      selectPage(next);
      try {
        if (continuous) await viewer.goToSlide(next, { behavior: "instant", block: "start", inline: "nearest" });
        else await viewer.renderSlide(next);
      } catch {
        state.error = "幻灯片渲染失败";
        state.busy = false;
        send();
      } finally {
        if (request === navigation) {
          settledTop = scroll.scrollTop;
          jumping = false;
        }
      }
    };
    const keyboard = (key: KeyboardEvent) => {
      if (key.ctrlKey || key.metaKey || key.altKey || key.shiftKey || key.isComposing) return;
      if (key.key === "Escape") port.postMessage({ type: "escape" });
      else if (!state.pageCount || document.getSelection()?.type === "Range") return;
      else if (key.key === "ArrowRight" || key.key === "PageDown") void updatePage(state.pageNumber);
      else if (key.key === "ArrowLeft" || key.key === "PageUp") void updatePage(state.pageNumber - 2);
      else return;
      key.preventDefault();
    };
    const blockLinks = (click: MouseEvent) => {
      if (rail.contains(click.target as Node)) return;
      click.preventDefault();
      click.stopPropagation();
    };
    document.addEventListener("keydown", keyboard);
    document.addEventListener("click", blockLinks, true);
    scroll.addEventListener("scroll", trackScroll, { passive: true });
    cleanup = () => {
      if (!alive) return;
      alive = false;
      abort.abort();
      observer?.disconnect();
      for (const handle of thumbnails.values()) handle.dispose();
      thumbnails.clear();
      viewer?.destroy();
      document.removeEventListener("keydown", keyboard);
      document.removeEventListener("click", blockLinks, true);
      scroll.removeEventListener("scroll", trackScroll);
      cancelAnimationFrame(scrollFrame);
      container.replaceChildren();
      rail.replaceChildren();
      port.close();
    };
    port.onmessage = async message => {
      if (!alive) return;
      const command = message.data;
      if (command?.type === "dispose") { cleanup(); return; }
      if (!viewer) return;
      try {
        if (command?.type === "page" && Number.isInteger(command.index)) {
          await updatePage(command.index);
        } else if (command?.type === "zoom" && [100, 125, 150, 200, 300].includes(command.value)) {
          state.zoom = command.value;
          await viewer.setZoom(state.zoom);
          if (continuous) await updatePage(state.pageNumber - 1);
        } else if (command?.type === "thumbnails") {
          rail.hidden = command.visible !== true;
        } else if (command?.type === "mobile" && typeof command.value === "boolean"
            && continuous !== command.value) {
          const page = state.pageNumber - 1;
          continuous = command.value;
          if (continuous) {
            await viewer.renderList({ windowed: true, initialSlides: 2, batchSize: 2 });
          }
          await updatePage(page);
        }
      } catch {
        state.error = "幻灯片渲染失败";
        state.busy = false;
        send();
      }
    };
    send();
    try {
      viewer = new renderer.PptxViewer(container, {
        externalMedia: "block", pdfjs: false, lazyMedia: true, lazySlides: true,
        scrollContainer: scroll,
        zipLimits: { ...renderer.RECOMMENDED_ZIP_LIMITS,
          maxEntryUncompressedBytes: 16 * 1024 * 1024,
          maxTotalUncompressedBytes: 64 * 1024 * 1024,
          maxMediaBytes: 64 * 1024 * 1024 },
        onRenderStart: () => { state.busy = true; state.partial = false; send(); },
        onRenderComplete: () => {
          if (alive) fitTableInsets(container);
          state.busy = false;
          send();
        },
        onSlideError: () => { state.error = "该页无法完整渲染，可下载原文件查看"; send(); },
        onNodeError: () => { state.partial = true; send(); },
        onSlideChange: (index: number) => { if (!continuous && !jumping) selectPage(index); },
      });
      await viewer!.open(event.data.bytes, {
        renderMode: continuous ? "list" : "slide", signal: abort.signal,
        listOptions: { windowed: true, initialSlides: 2, batchSize: 2 },
      });
      if (!alive) return;
      state.pageCount = viewer!.slideCount;
      if (!state.pageCount) throw new Error("Empty presentation");
      if (inspectPage !== null) {
        if (inspectPage < 1 || inspectPage > state.pageCount) {
          state.error = `This presentation has ${state.pageCount} slides. Select a valid page.`;
        } else if (inspectPage !== 1) {
          await updatePage(inspectPage - 1);
        }
        finishInspection();
        return;
      }
      observer = new IntersectionObserver(entries => {
        if (!alive) return;
        for (const entry of entries) {
          if (entry.isIntersecting && !thumbnails.has(entry.target)) {
            const button = entry.target as HTMLButtonElement;
            const preview = button.firstElementChild as HTMLElement;
            const handle = viewer!.renderThumbnailToContainer(Number(button.value), preview, { width: 108 });
            if (handle) {
              thumbnails.set(button, handle);
              void handle.ready.then(() => { if (alive) fitTableInsets(preview); }).catch(() => {});
            }
          } else if (!entry.isIntersecting) {
            thumbnails.get(entry.target)?.dispose();
            thumbnails.delete(entry.target);
          }
        }
      }, { root: rail, rootMargin: "0px 112px" });
      for (let index = 0; index < state.pageCount; index++) {
        const button = document.createElement("button");
        button.type = "button";
        button.value = String(index);
        button.setAttribute("aria-label", `第 ${index + 1} 页`);
        if (!index) button.setAttribute("aria-current", "page");
        button.onclick = () => { void updatePage(index); };
        const preview = document.createElement("div");
        preview.className = "thumbnail";
        const label = document.createElement("span");
        label.className = "page-number";
        label.textContent = String(index + 1);
        button.append(preview, label);
        rail.append(button);
        observer.observe(button);
      }
      state.busy = false;
      send();
    } catch {
      if (!alive) return;
      viewer?.destroy();
      state.error = "PPTX 无法解析，文件可能已损坏或超出预览上限";
      state.busy = false;
      if (inspectPage !== null) finishInspection();
      else send();
    }
  });
}

export function presentationDocument(script: string): string {
  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; img-src data: blob:; style-src 'unsafe-inline'; font-src data: blob:; media-src data: blob:; form-action 'none'; base-uri 'none'">
<style>
html,body{margin:0;height:100%;overflow:hidden;color-scheme:light}
body{display:flex;flex-direction:column;font:12px system-ui,sans-serif}
#pages{display:flex;flex:none;gap:8px;overflow-x:auto;overscroll-behavior:contain;padding:10px;min-height:86px}
#pages[hidden]{display:none}
#pages button{flex:none;position:relative;width:112px;border:2px solid transparent;border-radius:12px;padding:0;overflow:hidden;background:#fff;cursor:pointer}
#pages button[aria-current="page"]{border-color:var(--accent,#8886ff)}
.thumbnail{width:108px;height:61px;border-radius:10px;overflow:hidden;pointer-events:none}
.page-number{position:absolute;bottom:0;right:0;padding:1px 4px;border-top-left-radius:6px;background:#fff;color:#333;font:10px system-ui}
#scroll{flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;padding:12px}
#slide{width:100%;min-height:100%}
#slide>div:not([data-slide-index]),#slide>[data-slide-index]>div:first-child{border-radius:12px;overflow:hidden}
</style></head><body><div id="scroll"><div id="slide"></div></div><nav id="pages" aria-label="幻灯片缩略图" hidden></nav>
<script>${script.replace(/<\/script/gi, "<\\/script")}</script></body></html>`;
}

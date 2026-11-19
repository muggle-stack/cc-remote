import { renderToStaticMarkup } from "react-dom/server";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import "./artifact-inspection.css";
import { isMermaidFenceClass, mermaidPreviewSvg, renderMermaidSvg } from "./mermaid";
import { rehypePreviewHtml } from "./markdown-preview-html";

const capture = document.querySelector<HTMLElement>("#capture")!;
const bundleUrl = new URL(".", window.location.href);
const decode = (bytes: ArrayBuffer) => new TextDecoder("utf-8", { fatal: true }).decode(bytes);

let diagramId = 0;

async function diagramImage(source: string) {
  const svg = mermaidPreviewSvg(await renderMermaidSvg(source, "inspect-diagram-" + ++diagramId, "light"));
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
  const image = new Image();
  image.src = url;
  try {
    await image.decode();
  } finally {
    URL.revokeObjectURL(url);
  }
  return image;
}

async function prepareMarkdown(source: string) {
  capture.innerHTML = renderToStaticMarkup(<article className="markdown">
    <Markdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypePreviewHtml, rehypeKatex]}>
      {source}
    </Markdown>
  </article>);
  const blocks = [...capture.querySelectorAll("pre > code")]
    .filter((block) => isMermaidFenceClass(block.className));
  let partial = blocks.length > 24;
  for (const block of blocks.slice(0, 24)) {
    try {
      block.parentElement!.replaceWith(await diagramImage(block.textContent ?? ""));
    } catch {
      // Keep the original fenced source if the diagram cannot render.
      partial = true;
    }
  }
  return { partial };
}

async function loadPdf(bytes: ArrayBuffer, pageNumber: number) {
  const [{ getDocument, GlobalWorkerOptions }, { default: workerUrl }] = await Promise.all([
    import("pdfjs-dist"),
    import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
  ]);
  // PDF.js can run its worker module in the existing sandboxed renderer.
  // Load it explicitly so an opaque frame needs no worker network channel.
  GlobalWorkerOptions.workerSrc = workerUrl;
  await import(/* @vite-ignore */ workerUrl);
  const task = getDocument({
    data: new Uint8Array(bytes),
    cMapUrl: new URL("cmaps/", bundleUrl).href,
    cMapPacked: true,
    standardFontDataUrl: new URL("standard_fonts/", bundleUrl).href,
    enableXfa: false,
    isEvalSupported: false,
    maxImageSize: 16_000_000,
    useWasm: false,
    useWorkerFetch: false,
  });
  let passwordRequired = false;
  task.onPassword = () => {
    passwordRequired = true;
    void task.destroy();
  };
  try {
    const pdf = await task.promise;
    checkPage(pageNumber, pdf.numPages);
    const page = await pdf.getPage(pageNumber);
    const initial = page.getViewport({ scale: 1 });
    if (!Number.isFinite(initial.width) || !Number.isFinite(initial.height)
        || initial.width <= 0 || initial.height <= 0) throw new Error("Invalid PDF page dimensions.");
    const viewport = page.getViewport({ scale: Math.min(
      window.innerWidth / initial.width, window.innerHeight / initial.height,
    ) });
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.ceil(viewport.width));
    canvas.height = Math.max(1, Math.ceil(viewport.height));
    capture.style.width = "fit-content";
    capture.append(canvas);
    await page.render({ canvas, viewport }).promise;
    return { pageCount: pdf.numPages };
  } catch (error) {
    if (passwordRequired) throw new Error("Password-protected PDFs cannot be inspected.");
    throw error;
  } finally {
    await task.destroy();
  }
}

function checkPage(pageNumber: number, count: number) {
  if (!Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber > count) {
    throw new Error("Page " + pageNumber + " is out of range. This preview has " + count + " page(s).");
  }
}

async function readyForCapture(element: HTMLElement) {
  await document.fonts.ready;
  await Promise.allSettled([...element.querySelectorAll("img")].map((image) => image.decode()));
}

async function loadWord(bytes: ArrayBuffer, pageNumber: number) {
  const { renderAsync } = await import("docx-preview");
  const styles = document.createElement("div");
  document.head.append(styles);
  await renderAsync(bytes, capture, styles, {
    className: "word",
    inWrapper: false,
    ignoreLastRenderedPageBreak: false,
    renderAltChunks: false,
    renderComments: false,
    useBase64URL: true,
  });
  await readyForCapture(capture);
  const sections = [...capture.querySelectorAll<HTMLElement>(":scope > section.word")];
  const pages = sections.map((section) => {
    const frame = section.getBoundingClientRect();
    const height = Math.max(frame.height, section.scrollHeight);
    const sliceHeight = Number.parseFloat(getComputedStyle(section).minHeight)
      || Math.min(frame.height || window.innerHeight, window.innerHeight);
    return { section, height, sliceHeight, count: Math.max(1, Math.ceil(height / sliceHeight)) };
  });
  const pageCount = pages.reduce((total, page) => total + page.count, 0);
  checkPage(pageNumber, pageCount);
  let index = pageNumber - 1;
  for (const page of pages) {
    if (index >= page.count) {
      index -= page.count;
      continue;
    }
    const width = page.section.getBoundingClientRect().width || window.innerWidth;
    const offset = page.sliceHeight * index;
    const height = Math.min(page.sliceHeight, page.height - offset);
    const scale = Math.min(1, window.innerWidth / width, window.innerHeight / height);
    capture.replaceChildren(page.section);
    Object.assign(capture.style, {
      width: Math.ceil(width * scale) + "px",
      height: Math.ceil(height * scale) + "px",
      overflow: "hidden",
    });
    Object.assign(page.section.style, {
      margin: "0",
      width: width + "px",
      transformOrigin: "top left",
      transform: "scale(" + scale + ") translateY(-" + offset + "px)",
    });
    break;
  }
  return { pageCount };
}

function cropMarkdownPage(pageNumber: number) {
  const documentBody = capture.querySelector<HTMLElement>(":scope > article.markdown")!;
  const height = Math.max(documentBody.offsetHeight, documentBody.scrollHeight);
  const pageCount = Math.max(1, Math.ceil(height / window.innerHeight));
  checkPage(pageNumber, pageCount);
  const offset = (pageNumber - 1) * window.innerHeight;
  capture.style.height = Math.min(window.innerHeight, height - offset) + "px";
  capture.style.overflow = "hidden";
  documentBody.style.transform = "translateY(-" + offset + "px)";
  return { pageCount };
}

window.addEventListener("message", async (event) => {
  const port = event.ports[0];
  if (event.source !== parent || event.data?.type !== "cc-remote-inspect-document"
      || !port || !(event.data.bytes instanceof ArrayBuffer)) return;
  const { bytes, kind, inspectPage: pageNumber, resourceUrl } = event.data;
  try {
    let result;
    if (kind === "pdf") {
      result = await loadPdf(bytes, pageNumber);
    } else if (kind === "docx") {
      result = await loadWord(bytes, pageNumber);
    } else if (kind === "mermaid") {
      const image = await diagramImage(decode(bytes));
      image.style.cssText = "max-width:100vw;max-height:100vh;object-fit:contain";
      capture.style.width = "fit-content";
      capture.replaceChildren(image);
    } else if (kind === "markdown") {
      if (resourceUrl) {
        const base = document.createElement("base");
        base.href = resourceUrl;
        document.head.append(base);
      }
      const markdown = await prepareMarkdown(decode(bytes));
      await readyForCapture(capture);
      result = { ...markdown, ...cropMarkdownPage(pageNumber) };
    } else {
      throw new Error("Unsupported document renderer.");
    }
    port.postMessage({ type: "inspection", pageNumber, ...result });
  } catch (error) {
    port.postMessage({
      type: "inspection",
      error: error instanceof Error ? error.message : "Unable to render the document.",
    });
  } finally {
    port.close();
  }
}, { once: true });
parent.postMessage({ type: "cc-remote-artifact-ready" }, "*");

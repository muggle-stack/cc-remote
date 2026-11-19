import { useEffect, useRef, useState } from "react";
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFPageProxy,
  RenderTask,
} from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url";
import { Icon } from "../icons";

const PDF_CANVAS_MAX_EDGE = 4096;
const PDF_CANVAS_MAX_PIXELS = 8_000_000;
const PDF_IMAGE_MAX_PIXELS = 16_000_000;
const PDF_PAGE_MAX_CSS_EDGE = 16_384;

function decodeBase64(data: string): Uint8Array<ArrayBuffer> {
  const binary = window.atob(data);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function pdfErrorMessage(error: unknown): string {
  const name = error instanceof Error ? error.name : "";
  if (name === "PasswordException") return "PDF 受密码保护，暂不支持预览";
  if (name === "InvalidPDFException" || name === "FormatError") {
    return "PDF 无法解析或已损坏";
  }
  return "PDF 预览失败";
}

export function PdfArtifactPreview({ data, title }: {
  data?: string;
  title: string;
}) {
  const stageRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [stageWidth, setStageWidth] = useState(0);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const updateWidth = () => {
      if (stage.clientWidth > 24) setStageWidth(stage.clientWidth - 24);
    };
    updateWidth();
    const observer = new ResizeObserver(updateWidth);
    observer.observe(stage);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    let loadingTask: PDFDocumentLoadingTask | null = null;
    let passwordRequired = false;

    setDocument(null);
    setPageNumber(1);
    setRendering(false);
    setError(null);

    const load = async () => {
      if (!data) {
        setError("预览数据不完整");
        return;
      }
      try {
        const bytes = decodeBase64(data);
        const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
        if (cancelled) return;
        pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
        loadingTask = pdfjs.getDocument({
          data: bytes,
          enableXfa: false,
          isEvalSupported: false,
          maxImageSize: PDF_IMAGE_MAX_PIXELS,
          useWasm: false,
          useWorkerFetch: false,
        });
        loadingTask.onPassword = () => {
          passwordRequired = true;
          if (!cancelled) {
            setError("PDF 受密码保护，暂不支持预览");
          }
          void loadingTask?.destroy();
        };
        const loaded = await loadingTask.promise;
        if (cancelled) {
          await loadingTask.destroy();
          return;
        }
        setDocument(loaded);
      } catch (loadError) {
        if (cancelled || passwordRequired) return;
        setError(pdfErrorMessage(loadError));
      }
    };
    void load();

    return () => {
      cancelled = true;
      if (loadingTask && !loadingTask.destroyed) void loadingTask.destroy();
    };
  }, [data]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!document || !canvas || stageWidth <= 0) return;
    let cancelled = false;
    let page: PDFPageProxy | null = null;
    let renderTask: RenderTask | null = null;

    setRendering(true);
    setError(null);
    const render = async () => {
      try {
        page = await document.getPage(pageNumber);
        if (cancelled) return;
        const baseViewport = page.getViewport({ scale: 1 });
        if (!Number.isFinite(baseViewport.width)
            || !Number.isFinite(baseViewport.height)
            || baseViewport.width <= 0 || baseViewport.height <= 0) {
          throw new Error("Invalid PDF page dimensions");
        }
        const cssScale = Math.min(
          2,
          stageWidth / baseViewport.width,
          PDF_PAGE_MAX_CSS_EDGE / Math.max(
            baseViewport.width,
            baseViewport.height,
          ),
        );
        if (!Number.isFinite(cssScale) || cssScale <= 0) {
          throw new Error("Invalid PDF page scale");
        }
        const pixelRatio = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
        let renderScale = cssScale * pixelRatio;
        let viewport = page.getViewport({ scale: renderScale });
        if (!Number.isFinite(viewport.width) || !Number.isFinite(viewport.height)
            || viewport.width <= 0 || viewport.height <= 0) {
          throw new Error("Invalid PDF render dimensions");
        }
        const resourceScale = Math.min(
          1,
          PDF_CANVAS_MAX_EDGE / viewport.width,
          PDF_CANVAS_MAX_EDGE / viewport.height,
          Math.sqrt(PDF_CANVAS_MAX_PIXELS / (viewport.width * viewport.height)),
        );
        if (resourceScale < 1) {
          renderScale *= resourceScale;
          viewport = page.getViewport({ scale: renderScale });
        }
        canvas.width = Math.max(1, Math.floor(viewport.width));
        canvas.height = Math.max(1, Math.floor(viewport.height));
        canvas.style.width = `${Math.max(1, baseViewport.width * cssScale)}px`;
        canvas.style.height = `${Math.max(1, baseViewport.height * cssScale)}px`;
        const context = canvas.getContext("2d", { alpha: false });
        if (!context) throw new Error("Canvas is unavailable");
        renderTask = page.render({
          canvas,
          canvasContext: context,
          viewport,
          background: "#fff",
        });
        await renderTask.promise;
        if (!cancelled) setRendering(false);
      } catch (renderError) {
        if (cancelled) return;
        setRendering(false);
        setError(pdfErrorMessage(renderError));
      } finally {
        page?.cleanup();
      }
    };
    void render();

    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [document, pageNumber, stageWidth]);

  const pageCount = document?.numPages ?? 0;
  return <div className="artifact-pdf-stage" ref={stageRef}>
    <nav className="artifact-pdf-controls" aria-label={`${title} PDF 页面`}>
      <button type="button" disabled={!document || pageNumber <= 1 || rendering}
        onClick={() => setPageNumber((page) => Math.max(1, page - 1))}>
        上一页
      </button>
      <span>{document ? `${pageNumber} / ${pageCount} 页` : "正在读取 PDF…"}</span>
      <button type="button"
        disabled={!document || pageNumber >= pageCount || rendering}
        onClick={() => setPageNumber((page) => Math.min(pageCount, page + 1))}>
        下一页
      </button>
    </nav>
    <div className="artifact-pdf-page">
      {!document
        ? error
          ? <div className="preview-error"><Icon name="read" size={18} />{error}</div>
          : <div className="diff-empty"><span className="thinking"><span/><span/><span/></span> 正在准备预览…</div>
        : <>
          <canvas ref={canvasRef} aria-label={`${title} 第 ${pageNumber} 页`} />
          {error
            ? <div className="artifact-pdf-overlay preview-error">
              <Icon name="read" size={18} />{error}
            </div>
            : rendering && <div className="artifact-pdf-loading" aria-live="polite">
            <span className="thinking"><span/><span/><span/></span> 正在渲染第 {pageNumber} 页…
          </div>}
        </>}
    </div>
  </div>;
}

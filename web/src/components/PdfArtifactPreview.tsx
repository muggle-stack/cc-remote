import { useEffect, useRef, useState } from "react";
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFPageProxy,
  RenderTask,
  TextLayer,
} from "pdfjs-dist";
import type { TextContent } from "pdfjs-dist/types/src/display/api";
import pdfWorkerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url";
import { Icon } from "../icons";

const PDF_CANVAS_MAX_EDGE = 4096;
const PDF_CANVAS_MAX_PIXELS = 8_000_000;
const PDF_IMAGE_MAX_PIXELS = 16_000_000;
const PDF_PAGE_MAX_CSS_EDGE = 16_384;
const MAX_SEARCH_CHARACTERS = 8 * 1024 * 1024;
const MAX_SEARCH_HITS = 10_000;

type Hit = { page: number; start: number; end: number };
type SearchResult = { hits: Hit[]; limited: boolean };
const EMPTY_RESULTS: SearchResult = { hits: [], limited: false };

function pdfText(content: TextContent) {
  return content.items.flatMap(item => "str" in item ? [item.str + (item.hasEOL ? " " : "")] : []);
}

async function searchPdf(pdf: PDFDocumentProxy, term: string, cancelled: () => boolean) {
  const pattern = new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu");
  const hits: Hit[] = [];
  let budget = MAX_SEARCH_CHARACTERS;
  for (let page = 1; page <= pdf.numPages && !cancelled(); page++) {
    const source = await (await pdf.getPage(page)).getTextContent();
    if (cancelled()) break;
    const text = pdfText(source).join("");
    const withinBudget = text.slice(0, budget);
    for (const match of withinBudget.matchAll(pattern)) {
      hits.push({ page, start: match.index, end: match.index + match[0].length });
      if (hits.length >= MAX_SEARCH_HITS) return { hits, limited: true };
    }
    budget -= text.length;
    if (budget <= 0) return { hits, limited: budget < 0 || page < pdf.numPages };
  }
  return { hits, limited: false };
}

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
  const paperRef = useRef<HTMLDivElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [stageWidth, setStageWidth] = useState(0);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(0);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [term, setTerm] = useState("");
  const [finding, setFinding] = useState(false);
  const [searchError, setSearchError] = useState(false);
  const [results, setResults] = useState<SearchResult>(EMPTY_RESULTS);
  const [hitIndex, setHitIndex] = useState(0);
  const [textLayout, setTextLayout] = useState<{ page: number; layer: TextLayer; parts: string[] } | null>(null);

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
    setTextLayout(null);

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
        const resources = import.meta.env.DEV ? "/node_modules/pdfjs-dist"
          : `/assets/pdfjs-${pdfjs.version}`;
        loadingTask = pdfjs.getDocument({
          data: bytes,
          cMapUrl: `${resources}/cmaps/`,
          cMapPacked: true,
          standardFontDataUrl: `${resources}/standard_fonts/`,
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
    const text = textRef.current;
    const paper = paperRef.current;
    if (!document || !canvas || !text || !paper || stageWidth <= 0) return;
    let cancelled = false;
    let page: PDFPageProxy | null = null;
    let renderTask: RenderTask | null = null;
    let textLayer: TextLayer | null = null;

    setRendering(true);
    setError(null);
    setTextLayout(null);
    text.replaceChildren();
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
          zoom ? zoom / 100 : Math.min(2, stageWidth / baseViewport.width),
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
        const cssViewport = page.getViewport({ scale: cssScale });
        paper.style.width = `${Math.max(1, cssViewport.width)}px`;
        paper.style.height = `${Math.max(1, cssViewport.height)}px`;
        paper.style.setProperty("--total-scale-factor", String(cssScale * cssViewport.userUnit));
        const context = canvas.getContext("2d", { alpha: false });
        if (!context) throw new Error("Canvas is unavailable");
        renderTask = page.render({
          canvas,
          canvasContext: context,
          viewport,
          background: "#fff",
        });
        await renderTask.promise;
        if (cancelled) return;
        setRendering(false);
        try {
          const [{ TextLayer }, textContent] = await Promise.all([
            import("pdfjs-dist/legacy/build/pdf.mjs"), page.getTextContent(),
          ]);
          if (cancelled) return;
          textLayer = new TextLayer({ textContentSource: textContent, container: text, viewport: cssViewport });
          await textLayer.render();
          if (!cancelled) setTextLayout({ page: pageNumber, layer: textLayer, parts: pdfText(textContent) });
        } catch {
          // A malformed text layer must not hide the rendered page.
          if (!cancelled) text.replaceChildren();
        }
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
      textLayer?.cancel();
    };
  }, [document, pageNumber, stageWidth, zoom]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [document, pageNumber]);

  useEffect(() => {
    let cancelled = false;
    setResults(EMPTY_RESULTS);
    setHitIndex(0);
    setSearchError(false);
    setFinding(!!document && !!term);
    if (!document || !term) return;
    void searchPdf(document, term, () => cancelled).then(result => {
      if (cancelled) return;
      setResults(result);
      setFinding(false);
      if (result.hits.length) setPageNumber(result.hits[0].page);
    }).catch(() => {
      if (cancelled) return;
      setFinding(false);
      setSearchError(true);
    });
    return () => { cancelled = true; };
  }, [document, term]);

  const activeHit = results.hits[hitIndex];
  useEffect(() => {
    if (!textLayout || !activeHit || activeHit.page !== textLayout.page) return;
    const highlighted: HTMLElement[] = [];
    let offset = 0;
    for (const [index, part] of textLayout.parts.entries()) {
      const end = offset + part.length;
      if (offset >= activeHit.end) break;
      const span = textLayout.layer.textDivs[index];
      if (span?.isConnected && offset < activeHit.end && end > activeHit.start) {
        span.classList.add("pdf-text-hit");
        highlighted.push(span);
      }
      offset = end;
    }
    highlighted[0]?.scrollIntoView({ block: "nearest", inline: "nearest" });
    return () => highlighted.forEach(span => span.classList.remove("pdf-text-hit"));
  }, [textLayout, activeHit]);

  const jumpHit = (direction: number) => {
    if (!results.hits.length) return;
    const next = (hitIndex + direction + results.hits.length) % results.hits.length;
    setHitIndex(next);
    setPageNumber(results.hits[next].page);
  };

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
      <select disabled={!document} value={zoom} onChange={event => setZoom(Number(event.target.value))} aria-label="PDF 缩放">
        <option value={0}>适应宽度</option>
        {[75, 100, 125, 150, 200, 300].map(value => <option key={value} value={value}>{value}%</option>)}
      </select>
      <button type="button" disabled={!document} aria-expanded={searchOpen} onClick={() => {
        if (searchOpen) setTerm("");
        setSearchOpen(!searchOpen);
      }}>查找</button>
    </nav>
    {searchOpen && <form className="artifact-pdf-controls pdf-search" role="search" onSubmit={event => {
      event.preventDefault();
      const query = searchInput.trim();
      if (query === term) jumpHit(1);
      else setTerm(query);
    }}>
      <input type="search" maxLength={160} autoFocus placeholder="搜索文字" aria-label="搜索 PDF 文字"
        value={searchInput} onChange={event => {
          setSearchInput(event.target.value);
          if (!event.target.value) setTerm("");
        }} />
      <button type="submit">搜索</button>
      <button type="button" aria-label="上一个匹配" disabled={!activeHit || finding} onClick={() => jumpHit(-1)}>‹</button>
      <button type="button" aria-label="下一个匹配" disabled={!activeHit || finding} onClick={() => jumpHit(1)}>›</button>
      {term && <span role="status">{finding ? "正在查找…" : searchError ? "文字读取失败"
        : activeHit ? `${hitIndex + 1} / ${results.hits.length}${results.limited ? "+" : ""}`
          : results.limited ? "搜索范围内无匹配" : "无匹配文字"}</span>}
    </form>}
    <div className="artifact-pdf-page" ref={scrollRef}>
      {!document
        ? error
          ? <div className="preview-error"><Icon name="read" size={18} />{error}</div>
          : <div className="diff-empty"><span className="thinking"><span/><span/><span/></span> 正在准备预览…</div>
        : <>
          <div className="artifact-pdf-paper" ref={paperRef}>
            <canvas ref={canvasRef} aria-label={`${title} 第 ${pageNumber} 页`} />
            <div className="artifact-pdf-text" ref={textRef} />
          </div>
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

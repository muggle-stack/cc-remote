import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { Icon } from "../icons";
import {
  mermaidPreviewSvg,
  mermaidSourceProblem,
  renderMermaidSvg,
  type MermaidTheme,
} from "../mermaid";
import { ImageLightbox } from "./ImageLightbox";

interface RenderedMermaid {
  source: string;
  theme: MermaidTheme;
  state: "idle" | "loading" | "ready" | "error";
  svg?: string;
  error?: string;
}

interface MermaidPreview {
  svg: string;
  source: string;
  theme: MermaidTheme;
}

function activeTheme(): MermaidTheme {
  if (typeof document === "undefined") return "light";
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function safeRenderId(id: string): string {
  const safe = id.replace(/[^a-zA-Z0-9_-]/g, "");
  return `cc-remote-mermaid-${safe || "diagram"}`;
}

export function MermaidBlock({ source }: { source: string }) {
  const element = useRef<HTMLDivElement | null>(null);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const renderGeneration = useRef(0);
  const renderId = safeRenderId(useId());
  const [nearViewport, setNearViewport] = useState(false);
  const [theme, setTheme] = useState<MermaidTheme>(activeTheme);
  const [copied, setCopied] = useState(false);
  const [preview, setPreview] = useState<MermaidPreview | null>(null);
  const [rendered, setRendered] = useState<RenderedMermaid>({
    source: "",
    theme: "light",
    state: "idle",
  });

  useEffect(() => {
    const node = element.current;
    if (!node) return;
    if (typeof IntersectionObserver === "undefined") {
      setNearViewport(true);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      setNearViewport(true);
      observer.disconnect();
    }, { rootMargin: "600px 0px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    const observer = new MutationObserver(() => setTheme(activeTheme()));
    observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    setTheme(activeTheme());
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!nearViewport) return;
    const generation = ++renderGeneration.current;
    const problem = mermaidSourceProblem(source);
    if (problem) {
      setRendered({ source, theme, state: "error", error: problem });
      return;
    }
    setRendered({ source, theme, state: "loading" });
    void renderMermaidSvg(source, `${renderId}-${generation}`, theme).then(
      (svg) => {
        if (renderGeneration.current !== generation) return;
        setRendered({ source, theme, state: "ready", svg });
      },
      () => {
        if (renderGeneration.current !== generation) return;
        setRendered({
          source,
          theme,
          state: "error",
          error: "Mermaid 语法无效，已显示源码",
        });
      },
    );
    return () => {
      if (renderGeneration.current === generation) renderGeneration.current += 1;
    };
  }, [nearViewport, renderId, source, theme]);

  useEffect(() => () => {
    if (copyTimer.current) clearTimeout(copyTimer.current);
  }, []);

  const closePreview = useCallback(() => {
    setPreview(null);
  }, []);

  useEffect(() => {
    if (preview
        && (preview.source !== source || preview.theme !== theme)) {
      closePreview();
    }
  }, [closePreview, preview, source, theme]);

  const copy = () => {
    void navigator.clipboard?.writeText(source).then(() => {
      setCopied(true);
      if (copyTimer.current) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(false), 1500);
    }).catch(() => {});
  };
  const current = rendered.source === source && rendered.theme === theme
    ? rendered
    : { source, theme, state: "idle" as const };
  const openPreview = () => {
    if (current.state !== "ready" || !current.svg) return;
    setPreview({
      svg: mermaidPreviewSvg(current.svg),
      source,
      theme,
    });
  };

  return (
    <>
    <div ref={element} className={`mermaid-block ${current.state}`}
      data-mermaid-state={current.state}>
      <div className="mermaid-head">
        <span>Mermaid</span>
        <div className="mermaid-actions">
        {current.state === "ready" && current.svg
          ? <button type="button" className="mermaid-action mermaid-zoom"
              onClick={openPreview} aria-label="放大 Mermaid 图表"
              title="放大图表">
              <Icon name="expand" size={13} />
            </button>
          : null}
        <button type="button"
          className={"mermaid-action mermaid-copy" + (copied ? " copied" : "")}
          onClick={copy} aria-label="复制 Mermaid 源码"
          title={copied ? "已复制" : "复制源码"}>
          <Icon name={copied ? "check" : "copy"} size={13} />
        </button>
        </div>
      </div>
      {current.state === "ready" && current.svg
        ? <button type="button" className="mermaid-svg"
            aria-label="放大 Mermaid 图表" title="点击放大"
            onClick={openPreview}
            dangerouslySetInnerHTML={{ __html: current.svg }} />
        : <pre className="mermaid-source"><code>{source}</code></pre>}
      {current.state === "error" && <div className="mermaid-error" role="status">
        {current.error}
      </div>}
    </div>
    {preview && typeof document !== "undefined"
      ? <ImageLightbox sanitizedSvg={preview.svg} alt="Mermaid 图表预览"
          dialogLabel="Mermaid 图表预览" closeLabel="关闭 Mermaid 图表预览"
          onClose={closePreview} />
      : null}
    </>
  );
}

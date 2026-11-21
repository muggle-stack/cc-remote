import type { useViewerZoom } from "../use-viewer-zoom";

export function PagePreviewZoom({ zoom, label }: { zoom: ReturnType<typeof useViewerZoom>; label: string }) {
  return <nav className="artifact-pdf-controls" aria-label={label}>
    <button type="button" onClick={() => zoom.command("out")} disabled={!zoom.ready || zoom.scale <= 0.1} aria-label="缩小页面">−</button>
    <button type="button" onClick={() => zoom.command("fit")} disabled={!zoom.ready} aria-pressed={zoom.fit}>适应宽度</button>
    <span>{Math.round(zoom.scale * 100)}%</span>
    <button type="button" onClick={() => zoom.command("in")} disabled={!zoom.ready || zoom.scale >= 4} aria-label="放大页面">+</button>
    <button type="button" onClick={() => zoom.command("reset")} disabled={!zoom.ready}>100%</button>
  </nav>;
}

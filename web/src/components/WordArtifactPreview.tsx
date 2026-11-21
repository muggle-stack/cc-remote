import { useEffect, useRef, useState } from "react";
import { useViewerZoom } from "../use-viewer-zoom";
import { PagePreviewZoom } from "./PagePreviewZoom";

export default function WordArtifactPreview({ data, title }: { data?: string; title: string }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const channel = useRef<MessageChannel | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">(data ? "loading" : "error");
  const zoom = useViewerZoom(frame, data ? "docx" : undefined, title);
  useEffect(() => () => channel.current?.port1.close(), []);
  useEffect(() => {
    if (status !== "loading") return;
    const timer = window.setTimeout(() => setStatus("error"), 30_000);
    return () => window.clearTimeout(timer);
  }, [status]);
  const open = () => {
    if (!data || !frame.current?.contentWindow || channel.current) return;
    try {
      const bytes = Uint8Array.from(atob(data), value => value.charCodeAt(0));
      const connection = new MessageChannel();
      channel.current = connection;
      connection.port1.onmessage = event => {
        setStatus(event.data?.type === "ready" ? "ready" : "error");
        connection.port1.close();
      };
      frame.current.contentWindow.postMessage({ type: "cc-remote-word", bytes: bytes.buffer },
        "*", [connection.port2, bytes.buffer]);
    } catch { setStatus("error"); }
  };
  return <div className="artifact-html-stage">
    <PagePreviewZoom zoom={zoom} label="文档缩放" />
    {status === "loading" && <p className="preview-truncated" role="status">正在渲染文档…</p>}
    {status === "error" ? <p className="preview-error" role="alert">DOCX 预览失败，可下载原文件查看。</p>
      : <iframe ref={frame} className="artifact-html-preview" src="/word-preview.html" title={`${title} 文档`}
        sandbox="allow-scripts" referrerPolicy="no-referrer" onLoad={open} />}
  </div>;
}

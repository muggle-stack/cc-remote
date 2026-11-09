import { useEffect, useState } from "react";

export default function ArtifactDownload({ id, file, data, content, converted, unavailable }: {
  id: string; file: string; data?: string; content?: string; converted?: string; unavailable?: string;
}) {
  const [download, setDownload] = useState<{
    id: string; url: string; data?: string; content?: string;
  } | null>(null);
  const ready = !unavailable && (data != null || content != null);
  useEffect(() => {
    if (!ready) { setDownload(null); return; }
    let url: string | undefined;
    try {
      const bytes = data ? Uint8Array.from(atob(data), value => value.charCodeAt(0)) : content ?? "";
      url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
      setDownload({ id, url, data, content });
    } catch { setDownload(null); }
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [id, data, content, ready]);
  const filename = file.split("/").at(-1) || "download";
  const label = converted ? "下载预览 PDF" : "下载原文件";
  const icon = <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4" /></svg>;
  if (!ready || !download || download.id !== id || download.data !== data || download.content !== content) {
    return <button type="button" className="iconbtn" disabled aria-label={label}
      title={unavailable || "正在准备下载…"}>{icon}</button>;
  }
  return <a className="iconbtn" href={download.url} download={converted ? filename.replace(/\.[^.]+$/, ".pdf") : filename}
    aria-label={label} title={label}>{icon}</a>;
}

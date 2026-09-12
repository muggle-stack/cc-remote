import { useEffect, useState } from "react";

export default function ArtifactDownload({ id, file, data, content, converted }: {
  id: string; file: string; data?: string; content?: string; converted?: string;
}) {
  const [download, setDownload] = useState<{ id: string; url: string } | null>(null);
  useEffect(() => {
    let url: string | undefined;
    try {
      const bytes = data ? Uint8Array.from(atob(data), value => value.charCodeAt(0)) : content ?? "";
      url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
      setDownload({ id, url });
    } catch { setDownload(null); }
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [id, data, content]);
  if (!download || download.id !== id) return null;
  const filename = file.split("/").at(-1) || "download";
  const label = converted ? "下载预览 PDF" : "下载原文件";
  return <a className="iconbtn" href={download.url} download={converted ? filename.replace(/\.[^.]+$/, ".pdf") : filename}
    aria-label={label} title={label}><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4" /></svg></a>;
}

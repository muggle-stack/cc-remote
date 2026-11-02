import { useEffect, useRef, useState } from "react";

export function VideoArtifactPreview({ machineId, mediaId, title, size }: {
  machineId?: string | null;
  mediaId?: string;
  title: string;
  size?: number;
}) {
  if (!machineId || !mediaId || !/^[a-f0-9]{32}$/.test(mediaId)) {
    return <div className="preview-error">视频地址已失效，请刷新文件重试。</div>;
  }
  const src = `/api/media/${encodeURIComponent(machineId)}/${mediaId}`;
  return <VideoPlayer key={src} src={src} title={title} size={size} />;
}

function VideoPlayer({ src, title, size }: { src: string; title: string; size?: number }) {
  const ref = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string>();
  useEffect(() => {
    const video = ref.current;
    if (!video) return;
    video.src = src;
    return () => {
      video.pause();
      video.removeAttribute("src");
      video.load(); // Cancel outstanding ranges, including Safari's metadata read.
    };
  }, [src]);
  return <div className="artifact-video-stage">
    <video ref={ref} controls playsInline preload="metadata" aria-label={title}
      onError={() => {
        const code = ref.current?.error?.code;
        setError(code === 3 || code === 4
          ? "无法播放这段视频，可能是当前浏览器不支持其编码。可以下载后播放，或刷新文件重试。"
          : "视频读取中断，请确认设备在线后刷新文件重试。");
      }} />
    <div className="artifact-video-footer">
      <span>{size != null ? `${(size / 1024 / 1024).toFixed(1)} MiB` : "MP4"}</span>
      <a href={`${src}?download=1`} download={title}>下载视频</a>
    </div>
    {error && <p className="artifact-video-error" role="alert">{error}</p>}
  </div>;
}

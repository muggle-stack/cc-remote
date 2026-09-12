import { useEffect, useRef, useState } from "react";

const AUDIO_TYPES: Record<string, string> = {
  "audio/wav": "WAV", "audio/mpeg": "MP3", "audio/mp4": "M4A",
  "audio/aac": "AAC", "audio/flac": "FLAC", "audio/ogg": "Ogg",
  "audio/webm": "WebM",
};
// Same bounded, requester-only envelope as image/PDF artifact previews.
const MAX_AUDIO_BYTES = 8 * 1024 * 1024;

export function AudioArtifactPreview({ data, mediaType, title, size }: {
  data?: string;
  mediaType?: string;
  title: string;
  size?: number;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [url, setUrl] = useState<string>();
  const [error, setError] = useState<string>();

  useEffect(() => {
    const audio = audioRef.current;
    let objectUrl: string | undefined;
    setUrl(undefined);
    setError(undefined);
    try {
      if (!data || !mediaType || !Object.hasOwn(AUDIO_TYPES, mediaType)) {
        throw new Error("音频数据不可用，请刷新文件重试。");
      }
      if (data.length > Math.ceil(MAX_AUDIO_BYTES / 3) * 4) {
        throw new Error("音频超过 8 MiB 预览上限。");
      }
      const decoded = atob(data);
      if (decoded.length > MAX_AUDIO_BYTES) {
        throw new Error("音频超过 8 MiB 预览上限。");
      }
      const bytes = new Uint8Array(decoded.length);
      for (let index = 0; index < decoded.length; index++) {
        bytes[index] = decoded.charCodeAt(index);
      }
      objectUrl = URL.createObjectURL(new Blob([bytes], { type: mediaType }));
      setUrl(objectUrl);
    } catch (cause) {
      setError(cause instanceof Error && !(cause instanceof DOMException)
        ? cause.message : "音频数据读取失败，请刷新文件重试。");
    }
    return () => {
      // Removing an element alone can leave media playing in the background.
      audio?.pause();
      audio?.removeAttribute("src");
      audio?.load();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [data, mediaType]);

  const fileSize = size == null ? "" : size >= 1024 * 1024
    ? `${(size / (1024 * 1024)).toFixed(1)} MiB`
    : `${Math.ceil(size / 1024)} KiB`;

  return <div className="artifact-audio-stage">
    <section className="artifact-audio-card" aria-label="音频预览">
      <div className="artifact-audio-heading">
        <span className="artifact-audio-icon" aria-hidden="true">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 18V5l12-2v13M9 9l12-2" />
            <ellipse cx="6" cy="18" rx="3" ry="3" />
            <ellipse cx="18" cy="16" rx="3" ry="3" />
          </svg>
        </span>
        <div><strong title={title}>{title}</strong>
          <span>{[AUDIO_TYPES[mediaType ?? ""], fileSize].filter(Boolean).join(" · ")}</span>
        </div>
      </div>
      <audio ref={audioRef} controls preload="metadata" src={url}
        aria-label={`${title} 播放器`}
        onError={(event) => {
          if (url && event.currentTarget.currentSrc === url) {
            setError("此浏览器无法播放该音频编码，可下载原文件试听。");
          }
        }} />
      {error && <p className="artifact-audio-error" role="alert">{error}</p>}
      <div className="artifact-audio-actions">
        <label>速度 <select aria-label="播放速度" defaultValue="1" disabled={!url || !!error}
          onChange={(event) => {
            if (audioRef.current) audioRef.current.playbackRate = Number(event.currentTarget.value);
          }}>
          {[0.75, 1, 1.25, 1.5, 2].map((rate) => <option key={rate} value={rate}>{rate}×</option>)}
        </select></label>
        {url && <a href={url} download={title}>下载音频</a>}
      </div>
    </section>
  </div>;
}

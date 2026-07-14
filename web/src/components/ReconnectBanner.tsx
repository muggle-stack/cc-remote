interface Props {
  banner?: string;
  replaying: boolean;
  truncated: boolean;
}

export function ReconnectBanner({ banner, replaying, truncated }: Props) {
  const parts: string[] = [];
  if (replaying) parts.push("正在补发历史…");
  if (banner) parts.push(banner);
  if (truncated && !replaying) parts.push("（部分历史可能缺失）");
  const text = parts.join(" · ");
  if (!text) return null;
  return (
    <div className="banner show" role="status" aria-live="polite">
      <span className="sp" aria-hidden="true" />
      <span>{text}</span>
    </div>
  );
}

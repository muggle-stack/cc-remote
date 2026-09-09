import type { CSSProperties, ReactNode, Ref } from "react";

/** Shared geometry for optimistic, cached and canonical user attachments. */
export function UserImageButton({
  width, height, maxHeight = 180, src, label = "用户发送的图片", ariaLabel, className = "",
  buttonRef, title, disabled, onClick, children,
}: {
  width: number;
  height: number;
  maxHeight?: number;
  src?: string;
  label?: string;
  ariaLabel?: string;
  className?: string;
  buttonRef?: Ref<HTMLButtonElement>;
  title?: string;
  disabled?: boolean;
  onClick: () => void;
  children?: ReactNode;
}) {
  const valid = Number.isFinite(width) && Number.isFinite(height)
    && width > 0 && height > 0;
  const w = valid ? width : 180;
  const h = valid ? height : 180;
  const scale = Math.min(240 / w, maxHeight / h);
  return <button ref={buttonRef} type="button"
    className={`ubub-image-trigger ${className}`.trim()}
    style={{ "--user-image-width": `${w * scale}px`,
      aspectRatio: `${w} / ${h}` } as CSSProperties}
    title={title} aria-label={ariaLabel ?? `预览${label}`}
    disabled={disabled} onClick={onClick}>
    {src ? <img src={src} className="ubub-img" alt={label} /> : children}
  </button>;
}

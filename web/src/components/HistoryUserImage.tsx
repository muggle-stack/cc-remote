import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import type { Turn } from "../domain/conversation";
import { UserImageButton } from "./UserImageButton";
import {
  historyImageDisplaySource,
} from "../turn-image-previews";
import type {
  HistoryImageAsset,
  HistoryImageVariant,
} from "../history-image-assets";
import {
  historyImageAssetCacheSnapshot,
  HISTORY_IMAGE_REQUEST_TIMEOUT_MS,
  shouldAutoloadHistoryImage,
  subscribeHistoryImageAssetCacheChanges,
} from "../history-image-assets";

export function HistoryUserImage({
  turnId,
  imageId,
  width,
  height,
  maxHeight,
  asset,
  fallback,
  onLoad,
  onPreview,
  label = "用户发送的图片",
  variant = "thumbnail",
}: {
  turnId: string;
  imageId: string;
  width: number;
  height: number;
  maxHeight?: number;
  asset?: HistoryImageAsset;
  fallback?: NonNullable<Turn["images"]>[number];
  onLoad?: (
    turnId: string,
    imageId: string,
    variant: HistoryImageVariant,
  ) => boolean;
  onPreview: () => void;
  label?: string;
  variant?: HistoryImageVariant;
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const residencyKey = `${turnId}\u0000${imageId}\u0000${variant}`;
  const residencyRef = useRef({
    key: residencyKey,
    observedAsset: false,
  });
  // This ref records render-visible residency, not merely a request attempt.
  // Keep it across an LRU eviction so the image cannot auto-compete forever.
  if (residencyRef.current.key !== residencyKey) {
    residencyRef.current = { key: residencyKey, observedAsset: false };
  }
  if (asset) residencyRef.current.observedAsset = true;
  const shouldAutoload = shouldAutoloadHistoryImage(
    asset,
    residencyRef.current.observedAsset,
  );
  const evicted = !asset && residencyRef.current.observedAsset;
  const intersectingRef = useRef(false);
  const waitingForCapacityRef = useRef(false);
  const attemptedCacheSnapshotRef = useRef<number | null>(null);
  const cacheSnapshot = useSyncExternalStore(
    subscribeHistoryImageAssetCacheChanges,
    historyImageAssetCacheSnapshot,
    historyImageAssetCacheSnapshot,
  );
  const [stalled, setStalled] = useState(false);
  useEffect(() => {
    setStalled(false);
    if (asset?.status !== "loading") return;
    const elapsed = typeof asset.startedAt === "number"
      ? Math.max(0, Date.now() - asset.startedAt)
      : 0;
    const remaining = Math.max(
      0, HISTORY_IMAGE_REQUEST_TIMEOUT_MS - elapsed);
    if (remaining === 0) {
      setStalled(true);
      return;
    }
    const timer = window.setTimeout(
      () => setStalled(true),
      remaining,
    );
    return () => window.clearTimeout(timer);
  }, [
    asset?.requestGeneration,
    asset?.startedAt,
    asset?.status,
    imageId,
    turnId,
    variant,
  ]);
  useEffect(() => {
    if (!shouldAutoload || !onLoad) {
      intersectingRef.current = false;
      waitingForCapacityRef.current = false;
      return;
    }
    const node = triggerRef.current;
    const requestImage = (): boolean => {
      const accepted = onLoad(turnId, imageId, variant);
      // Consume every synchronous begin/cancel mutation caused by this attempt.
      // A failed transport send must not wake the same component into a loop.
      attemptedCacheSnapshotRef.current = historyImageAssetCacheSnapshot();
      waitingForCapacityRef.current = !accepted;
      return accepted;
    };
    if (!node || typeof IntersectionObserver === "undefined") {
      intersectingRef.current = true;
      requestImage();
      return () => {
        intersectingRef.current = false;
        waitingForCapacityRef.current = false;
      };
    }
    const observer = new IntersectionObserver((entries) => {
      intersectingRef.current = entries.some((entry) => entry.isIntersecting);
      if (!intersectingRef.current) return;
      if (requestImage()) observer.disconnect();
    }, { rootMargin: "500px 0px" });
    observer.observe(node);
    return () => {
      intersectingRef.current = false;
      waitingForCapacityRef.current = false;
      observer.disconnect();
    };
  }, [asset, imageId, onLoad, shouldAutoload, turnId, variant]);

  // A full cache can reject an otherwise-visible image. Retry at most once for
  // each cache admission wake; failed begin() calls do not publish, so this
  // cannot turn into a render or network loop.
  useEffect(() => {
    if (!shouldAutoload || !onLoad || !intersectingRef.current
        || !waitingForCapacityRef.current
        || attemptedCacheSnapshotRef.current === cacheSnapshot) return;
    const accepted = onLoad(turnId, imageId, variant);
    attemptedCacheSnapshotRef.current = historyImageAssetCacheSnapshot();
    waitingForCapacityRef.current = !accepted;
  }, [
    asset,
    cacheSnapshot,
    imageId,
    onLoad,
    shouldAutoload,
    turnId,
    variant,
  ]);

  const src = historyImageDisplaySource(asset, fallback);
  const retryable = asset?.status === "error" || stalled || evicted;
  const canRetry = retryable && !!onLoad;
  const retryCanonical = () => {
    const accepted = !!onLoad?.(turnId, imageId, variant);
    attemptedCacheSnapshotRef.current = historyImageAssetCacheSnapshot();
    waitingForCapacityRef.current = !accepted;
    if (accepted) setStalled(false);
  };
  const imageButton = (
    <UserImageButton buttonRef={triggerRef}
      className="history-image-trigger"
      width={width} height={height} maxHeight={maxHeight}
      src={src ?? undefined} label={label}
      title={asset?.status === "error" ? asset.error : undefined}
      ariaLabel={src
        ? `预览${label}`
        : canRetry
        ? `重试加载${label}`
        : `预览${label}`}
      disabled={!src && !canRetry}
      onClick={() => {
        if (src) onPreview();
        else if (canRetry) retryCanonical();
      }}>
      <span className={`history-image-placeholder${
          canRetry ? " retryable" : ""
        }`} aria-hidden="true">
          {canRetry ? <><span>{asset?.error ?? "图片加载未完成"}</span><span>点击重试</span></> : ""}
        </span>
    </UserImageButton>
  );
  if (!src || !canRetry) return imageButton;
  return (
    <span className="history-image-control">
      {imageButton}
      <button type="button" className="history-image-retry"
        onClick={retryCanonical}>
        点击重试
      </button>
    </span>
  );
}

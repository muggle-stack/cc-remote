import { useContext, useLayoutEffect, useRef, type ImgHTMLAttributes } from "react";
import { PanelVisible } from "../right-panel-presentation";
import {
  constrainImageTransform, panImageTransform, pinchImageTransform, zoomImageTransform,
  IMAGE_WHEEL_ZOOM_RATE, MAX_IMAGE_SCALE,
  type ImagePoint, type ImageTransform,
} from "../image-gesture";

type SafariGesture = Event & { scale: number; clientX: number; clientY: number };

/** File previews share this viewport in both docked and centered presentations. */
export function ZoomableImage({ src, alt, onLoad, onError }: Pick<
  ImgHTMLAttributes<HTMLImageElement>, "src" | "alt" | "onLoad" | "onError"
>) {
  const stageRef = useRef<HTMLDivElement>(null);
  const visible = useContext(PanelVisible);
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const imageRef = useRef<HTMLImageElement>(null);

  useLayoutEffect(() => {
    const stage = stageRef.current;
    const image = imageRef.current;
    if (!stage || !image) return;
    let transform: ImageTransform = { scale: 1, x: 0, y: 0 };
    let ready = false;
    let interacted = false;
    let frame: number | null = null;
    const pointers = new Map<number, ImagePoint>();
    const sizes = () => {
      // CSS keeps a contained base image so shrinking can still reveal the
      // whole picture. Open tall files at readable width, with room to zoom
      // further even when that width is more than 4x the contained size.
      // Preserve fractional CSS pixels: rounding the narrow contained width
      // would magnify that error when a very long image fills the panel.
      const style = getComputedStyle(image);
      const width = parseFloat(style.width) || 0, height = parseFloat(style.height) || 0;
      const widthScale = width > 0
        ? Math.max(1, Math.min(image.naturalWidth, stage.clientWidth) / width) : 1;
      return {
        image: { width, height },
        viewport: { width: stage.clientWidth, height: stage.clientHeight },
        widthScale,
        maxScale: widthScale * MAX_IMAGE_SCALE,
      };
    };
    const localPoint = (event: { clientX: number; clientY: number }) => {
      const rect = stage.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    };
    const paint = () => {
      frame = null;
      image.style.transform = `translate(${transform.x}px,${transform.y}px) scale(${transform.scale})`;
      image.style.visibility = ready ? "visible" : "hidden";
      stage.dataset.zoomed = String(transform.scale > 1);
    };
    const update = (next: ImageTransform) => {
      transform = next;
      if (frame === null) frame = requestAnimationFrame(paint);
    };
    const baseline = () => ({ transform, points: [...pointers.values()], sizes: sizes() });
    let start = baseline();
    let nativeGesture: { transform: ImageTransform; scale: number } | null = null;
    paint();

    const down = (event: PointerEvent) => {
      if (!ready || (event.pointerType === "mouse" && event.button !== 0)) return;
      event.preventDefault();
      nativeGesture = null;
      pointers.set(event.pointerId, localPoint(event));
      stage.setPointerCapture(event.pointerId);
      start = baseline();
    };
    const move = (event: PointerEvent) => {
      if (!pointers.has(event.pointerId)) return;
      interacted = true;
      pointers.set(event.pointerId, localPoint(event));
      const points = [...pointers.values()];
      const { image, viewport, maxScale } = start.sizes;
      if (points.length >= 2 && start.points.length >= 2) {
        update(pinchImageTransform(start.transform, start.points[0], start.points[1],
          points[0], points[1], image, viewport, maxScale));
      } else if (points.length === 1 && start.points.length === 1) {
        update(panImageTransform(start.transform, points[0].x - start.points[0].x,
          points[0].y - start.points[0].y, image, viewport, maxScale));
      }
    };
    const finish = (event: PointerEvent) => {
      if (!pointers.delete(event.pointerId)) return;
      if (stage.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
      start = baseline();
    };
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      // Safari can also emit wheel events during a native gesture. One input
      // stream owns the transform, so the same pinch is never applied twice.
      if (!ready || pointers.size || nativeGesture) return;
      interacted = true;
      const { image, viewport, maxScale } = sizes();
      const delta = (value: number, page: number) => value * (
        event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16
          : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? page : 1);
      update(event.ctrlKey
        ? zoomImageTransform(transform, Math.exp(-delta(event.deltaY, viewport.height) * IMAGE_WHEEL_ZOOM_RATE),
          localPoint(event), image, viewport, maxScale)
        : panImageTransform(transform, -delta(event.deltaX, viewport.width),
          -delta(event.deltaY, viewport.height), image, viewport, maxScale));
    };
    const gestureStart = (raw: Event) => {
      raw.preventDefault();
      const event = raw as SafariGesture;
      if (ready && !pointers.size && Number.isFinite(event.scale) && event.scale > 0) {
        nativeGesture = { transform, scale: event.scale };
      }
    };
    const gestureChange = (raw: Event) => {
      raw.preventDefault();
      const event = raw as SafariGesture;
      if (!nativeGesture || pointers.size || !Number.isFinite(event.scale) || event.scale <= 0) return;
      interacted = true;
      const { image, viewport, maxScale } = sizes();
      // Trackpad gestures need more travel gain; touch pointers retain 1:1
      // finger tracking and continue to own overlapping iOS gesture events.
      update(zoomImageTransform(nativeGesture.transform, (event.scale / nativeGesture.scale) ** 2,
        localPoint(event), image, viewport, maxScale));
    };
    const gestureEnd = (event: Event) => {
      event.preventDefault();
      nativeGesture = null;
    };
    const fit = () => {
      if (!visibleRef.current || stage.clientWidth === 0 || stage.clientHeight === 0) return;
      if (!image.complete || !image.naturalWidth || !image.naturalHeight) return;
      const { image: imageSize, viewport, widthScale, maxScale } = sizes();
      if (!imageSize.width || !imageSize.height || !viewport.width || !viewport.height) return;
      transform = interacted ? constrainImageTransform(transform, imageSize, viewport, maxScale) : {
        scale: widthScale, x: 0,
        y: Math.max(0, (imageSize.height * widthScale - viewport.height) / 2),
      };
      // Apply load/resize geometry before the browser paints. Queuing this in
      // RAF exposes the contained base image (or a previous file's transform)
      // for a frame before it expands to readable width.
      ready = true;
      if (frame !== null) cancelAnimationFrame(frame);
      paint();
      start = baseline();
      nativeGesture = null;
    };
    const resize = new ResizeObserver(fit);
    resize.observe(stage);
    resize.observe(image);
    image.addEventListener("load", fit);
    fit();
    stage.addEventListener("pointerdown", down);
    stage.addEventListener("pointermove", move);
    stage.addEventListener("pointerup", finish);
    stage.addEventListener("pointercancel", finish);
    stage.addEventListener("lostpointercapture", finish);
    stage.addEventListener("wheel", wheel, { passive: false });
    stage.addEventListener("gesturestart", gestureStart, { passive: false });
    stage.addEventListener("gesturechange", gestureChange, { passive: false });
    stage.addEventListener("gestureend", gestureEnd, { passive: false });
    return () => {
      if (frame !== null) cancelAnimationFrame(frame);
      resize.disconnect();
      image.removeEventListener("load", fit);
      stage.removeEventListener("pointerdown", down);
      stage.removeEventListener("pointermove", move);
      stage.removeEventListener("pointerup", finish);
      stage.removeEventListener("pointercancel", finish);
      stage.removeEventListener("lostpointercapture", finish);
      for (const id of pointers.keys()) {
        if (stage.hasPointerCapture(id)) stage.releasePointerCapture(id);
      }
      stage.removeEventListener("wheel", wheel);
      stage.removeEventListener("gesturestart", gestureStart);
      stage.removeEventListener("gesturechange", gestureChange);
      stage.removeEventListener("gestureend", gestureEnd);
    };
  }, [src]);

  return <div ref={stageRef} className="artifact-image-stage" data-lock-horizontal-swipe>
    <img ref={imageRef} src={src} alt={alt} draggable={false}
      onDragStart={event => event.preventDefault()} onLoad={onLoad} onError={onError} />
  </div>;
}

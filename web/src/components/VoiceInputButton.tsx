import { useRef, useSyncExternalStore } from "react";
import { Icon } from "../icons";
import type { VoiceInputGesture } from "../use-voice-input";

const COARSE_POINTER = "(pointer: coarse)";

function subscribeToPointer(callback: () => void) {
  const query = window.matchMedia(COARSE_POINTER);
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}

function useHoldGesture() {
  return useSyncExternalStore(
    subscribeToPointer,
    () => navigator.userAgent.includes("cc-remote-harmony/") || window.matchMedia(COARSE_POINTER).matches,
    () => false,
  );
}

export function VoiceInputButton({ recording, finishing, start, release, cancel }: {
  recording: boolean;
  finishing: boolean;
  start: (gesture: VoiceInputGesture) => void;
  release: () => void;
  cancel: () => void;
}) {
  const pointer = useRef<number | null>(null);
  const hold = useHoldGesture();
  const label = finishing ? "正在完成语音转写"
    : hold ? "按住说话"
      : recording ? "停止语音转写" : "开始语音转写";
  return <button
    type="button"
    className={"sendbtn voice-input" + (recording ? " voice-input-held" : "")}
    aria-label={label}
    title={label}
    aria-busy={finishing || undefined}
    onPointerDown={(event) => {
      if (!hold || event.button !== 0 || pointer.current !== null || finishing) return;
      event.preventDefault();
      pointer.current = event.pointerId;
      event.currentTarget.setPointerCapture(event.pointerId);
      start("hold");
    }}
    onPointerUp={(event) => {
      if (pointer.current !== event.pointerId) return;
      event.preventDefault();
      pointer.current = null;
      release();
    }}
    onLostPointerCapture={(event) => {
      if (pointer.current !== event.pointerId) return;
      pointer.current = null;
      cancel();
    }}
    onPointerCancel={(event) => {
      if (pointer.current !== event.pointerId) return;
      pointer.current = null;
      cancel();
    }}
    onKeyDown={(event) => {
      if (event.key !== " " && event.key !== "Enter") return;
      if (hold || event.repeat) event.preventDefault();
      if (hold && !event.repeat && !finishing) start("hold");
    }}
    onKeyUp={(event) => {
      if (!hold || (event.key !== " " && event.key !== "Enter")) return;
      event.preventDefault();
      release();
    }}
    onBlur={() => { if (hold && pointer.current === null && !finishing) cancel(); }}
    onContextMenu={(event) => { if (hold) event.preventDefault(); }}
    onClick={() => {
      if (hold || finishing) return;
      if (recording) release();
      else start("toggle");
    }}
  >
    <Icon name={recording && !hold ? "stop" : "waveform"} size={19} />
  </button>;
}

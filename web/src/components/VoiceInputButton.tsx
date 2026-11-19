import { useRef, useSyncExternalStore } from "react";
import { Icon } from "../icons";
import type { VoiceInputGesture, VoiceInputStage } from "../use-voice-input";

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

export function VoiceInputStatus({ stage }: { stage: VoiceInputStage }) {
  if (stage === "idle") return null;
  const text = stage === "starting" ? "正在打开麦克风"
    : stage === "recording" ? "正在录音" : "正在完成语音转写";
  return <div className="composer-notice voice-input-status" role="status" data-stage={stage}>{text}</div>;
}

export function VoiceInputButton({ stage, start, release, cancel }: {
  stage: VoiceInputStage;
  start: (gesture: VoiceInputGesture) => void;
  release: () => void;
  cancel: () => void;
}) {
  const pointer = useRef<number | null>(null);
  const hold = useHoldGesture();
  const active = stage === "starting" || stage === "recording";
  const finishing = stage === "finishing";
  const label = finishing ? "正在完成语音转写"
    : hold ? "按住说话"
      : active ? "停止语音转写" : "开始语音转写";
  return <button
    type="button"
    className={"sendbtn voice-input" + (stage === "recording" ? " voice-input-held" : "")}
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
      if (active) release();
      else start("toggle");
    }}
  >
    <Icon name={active && !hold ? "stop" : "waveform"} size={19} />
  </button>;
}

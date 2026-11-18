import { useRef } from "react";
import { Icon } from "../icons";

export function VoiceInputButton({ recording, finishing, start, release, cancel }: {
  recording: boolean;
  finishing: boolean;
  start: () => void;
  release: () => void;
  cancel: () => void;
}) {
  const pointer = useRef<number | null>(null);
  return <button
    type="button"
    className={"sendbtn voice-input" + (recording ? " voice-input-held" : "")}
    aria-label="按住说话"
    title="按住说话"
    aria-busy={finishing || undefined}
    onPointerDown={(event) => {
      if (event.button !== 0 || pointer.current !== null || finishing) return;
      event.preventDefault();
      pointer.current = event.pointerId;
      event.currentTarget.setPointerCapture(event.pointerId);
      start();
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
      event.preventDefault();
      if (!event.repeat && !finishing) start();
    }}
    onKeyUp={(event) => {
      if (event.key !== " " && event.key !== "Enter") return;
      event.preventDefault();
      release();
    }}
    onBlur={() => { if (pointer.current === null && !finishing) cancel(); }}
    onContextMenu={(event) => event.preventDefault()}
    onClick={(event) => event.preventDefault()}
  >
    <Icon name="waveform" size={19} />
  </button>;
}

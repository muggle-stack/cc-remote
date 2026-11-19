import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import captureWorklet from "./voice-capture.worklet.js?url&no-inline";

const HOLD_MS = 220;
const MAX_BUFFERED_AUDIO = 240000;
const TRANSCRIPT_EVENT = "conversation.item.input_audio_transcription";
const UPDATE_SESSION = JSON.stringify({
  type: "session.update",
  session: {
    type: "transcription",
    audio: { input: {
      format: { type: "audio/pcm", rate: 24000 },
      transcription: { model: "gpt-live-transcribe" },
      turn_detection: null,
    } },
  },
});

type Stage = "idle" | "starting" | "recording" | "finishing";
type Recording = {
  scope: string;
  context: AudioContext;
  timer: number;
  stream?: MediaStream;
  node?: AudioWorkletNode;
  socket?: WebSocket;
  ready: boolean;
  pending: ArrayBuffer[];
  bytes: number;
  sent: number;
  released: boolean;
  committed: boolean;
  transcript: string;
};

export function voiceInputAvailable() {
  return window.isSecureContext && typeof AudioContext !== "undefined"
    && typeof AudioWorkletNode !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

function candidateError(error: unknown) {
  if (error instanceof DOMException && error.name === "NotAllowedError") return "请允许麦克风访问后再试。";
  if (error instanceof DOMException && error.name === "NotFoundError") return "找不到可用的麦克风。";
  return "实时转写暂不可用。";
}

export function useVoiceInput(
  enabled: boolean, scope: string, onDraft: (text: string) => void, onError: (message: string) => void,
) {
  const recordingRef = useRef<Recording | null>(null);
  const [stage, setStage] = useState<Stage>("idle");
  const [transcript, setTranscript] = useState("");

  const cancel = useCallback(() => {
    const recording = recordingRef.current;
    if (!recording) return;
    recordingRef.current = null;
    window.clearTimeout(recording.timer);
    recording.stream?.getTracks().forEach((track) => track.stop());
    if (recording.node) {
      recording.node.port.onmessage = null;
      recording.node.port.close();
      recording.node.disconnect();
    }
    if (recording.socket) {
      recording.socket.onopen = null;
      recording.socket.onmessage = null;
      recording.socket.onerror = null;
      recording.socket.onclose = null;
      recording.socket.close();
    }
    void recording.context.close().catch(() => {});
    setStage("idle");
    setTranscript("");
  }, []);

  useLayoutEffect(() => {
    if (!enabled || recordingRef.current?.scope !== scope) cancel();
  }, [cancel, enabled, scope]);
  useEffect(() => {
    const visibility = () => { if (document.hidden) cancel(); };
    document.addEventListener("visibilitychange", visibility);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      cancel();
    };
  }, [cancel]);

  const finish = (recording: Recording, text: string, error?: string) => {
    if (recordingRef.current !== recording) return;
    cancel();
    if (text.trim()) onDraft(text.trim());
    if (error) onError(error);
  };

  const start = () => {
    if (!enabled || recordingRef.current) return;
    let context: AudioContext;
    try {
      // Use the press gesture to unlock Web Audio, then request the mic only
      // after the hold threshold.
      context = new AudioContext();
      void context.resume().catch(() => {});
    } catch (error) {
      onError(candidateError(error));
      return;
    }
    const recording: Recording = {
      scope, context, timer: 0, ready: false, pending: [], bytes: 0, sent: 0,
      released: false, committed: false, transcript: "",
    };
    recordingRef.current = recording;
    const unavailable = () => finish(recording, recording.transcript, "实时转写暂不可用。");

    const connect = async () => {
      setStage("starting");
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true }, video: false,
        });
        if (recordingRef.current !== recording) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        recording.stream = stream;
        recording.timer = window.setTimeout(unavailable, 15000);
        await Promise.all([context.audioWorklet.addModule(captureWorklet), context.resume()]);
        if (recordingRef.current !== recording) return;
        const url = new URL("/v1/realtime?intent=transcription", location.href);
        url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
        const socket = new WebSocket(url);
        recording.socket = socket;

        const sendAudio = (audio: ArrayBuffer) => {
          const encoded = btoa(String.fromCharCode(...new Uint8Array(audio)));
          socket.send(JSON.stringify({ type: "input_audio_buffer.append", audio: encoded }));
          recording.sent += audio.byteLength;
        };
        const commit = () => {
          if (!recording.sent) {
            finish(recording, "");
            return;
          }
          socket.send(JSON.stringify({ type: "input_audio_buffer.commit" }));
        };
        socket.onopen = () => socket.send(UPDATE_SESSION);
        socket.onmessage = (event) => {
          let message;
          try { message = JSON.parse(event.data); } catch { unavailable(); return; }
          if (message?.type === "session.updated" && !recording.ready) {
            recording.ready = true;
            if (!recording.released) window.clearTimeout(recording.timer);
            for (const audio of recording.pending) sendAudio(audio);
            recording.pending = [];
            recording.bytes = 0;
            if (recording.committed) commit();
          } else if (message?.type === `${TRANSCRIPT_EVENT}.delta`) {
            if (typeof message.transcript === "string") {
              recording.transcript = message.transcript;
            } else if (typeof message.delta === "string") {
              recording.transcript += message.delta;
            }
            setTranscript(recording.transcript);
          } else if (message?.type === `${TRANSCRIPT_EVENT}.completed`) {
            finish(recording, typeof message.transcript === "string" ? message.transcript : "");
          } else if (message?.type === "error") {
            unavailable();
          }
        };
        socket.onerror = unavailable;
        socket.onclose = unavailable;

        const node = new AudioWorkletNode(context, "cc-remote-voice-capture", {
          channelCount: 1, channelCountMode: "explicit",
        });
        recording.node = node;
        node.port.onmessage = (event) => {
          if (recordingRef.current !== recording) return;
          if (event.data.type === "audio") {
            const audio: ArrayBuffer = event.data.audio;
            if (recording.ready && socket.readyState === WebSocket.OPEN) {
              if (socket.bufferedAmount > MAX_BUFFERED_AUDIO) { unavailable(); return; }
              sendAudio(audio);
            } else {
              recording.pending.push(audio);
              recording.bytes += audio.byteLength;
              if (recording.bytes > MAX_BUFFERED_AUDIO) unavailable();
            }
          } else if (event.data.type === "flushed") {
            recording.committed = true;
            if (recording.ready && socket.readyState === WebSocket.OPEN) commit();
          }
        };
        context.createMediaStreamSource(stream).connect(node);
        // The worklet's output is silence. A connected output keeps the graph
        // scheduled on browsers that suspend unconnected capture nodes.
        node.connect(context.destination);
        setStage("recording");
      } catch (error) {
        finish(recording, recording.transcript, candidateError(error));
      }
    };
    recording.timer = window.setTimeout(() => void connect(), HOLD_MS);
  };

  const release = () => {
    const recording = recordingRef.current;
    if (!recording) return;
    if (!recording.node || !recording.stream) { cancel(); return; }
    if (recording.released) return;
    recording.released = true;
    setStage("finishing");
    recording.stream.getTracks().forEach((track) => track.stop());
    window.clearTimeout(recording.timer);
    recording.timer = window.setTimeout(() => finish(
      recording, recording.transcript, "未收到实时转写的最终结果。",
    ), 16000);
    recording.node.port.postMessage("flush");
  };

  return { stage, transcript, start, release, cancel };
}

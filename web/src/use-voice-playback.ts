import { useCallback, useEffect, useRef, useState } from "react";

const RATE = 24000;
const SPEECH_LIMIT = 4096;

function* speechParts(text: string) {
  const characters = Array.from(text);
  let start = 0;
  while (start < characters.length) {
    let end = Math.min(start + SPEECH_LIMIT, characters.length);
    if (end < characters.length) {
      for (let i = end - 1; i > start + SPEECH_LIMIT / 2; i--) {
        if (/[。！？.!?\n]/.test(characters[i])) { end = i + 1; break; }
      }
    }
    const part = characters.slice(start, end).join("");
    if (part.trim()) yield part;
    start = end;
  }
}

function pause(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted();
    const abort = () => {
      window.clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, 80);
    signal.addEventListener("abort", abort, { once: true });
  });
}

async function play(context: AudioContext, signal: AbortSignal, text: string) {
  let scheduledUntil = context.currentTime;
  for (const part of speechParts(text)) {
    const response = await fetch("/v1/audio/speech", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: "tts-1", voice: "alloy", input: part, response_format: "pcm" }),
      signal,
    });
    if (!response.ok || !response.body) throw new Error("speech request failed");
    const reader = response.body.getReader();
    let carry: number | null = null;
    let received = false;
    try {
      while (true) {
        // Backpressure limits live decoded buffers even when synthesis is
        // much faster than playback.
        while (scheduledUntil - context.currentTime > 2) await pause(signal);
        signal.throwIfAborted();
        const { done, value } = await reader.read();
        if (done) break;
        let data = value;
        if (carry !== null) {
          data = new Uint8Array(value.byteLength + 1);
          data[0] = carry;
          data.set(value, 1);
        }
        const samples = Math.floor(data.byteLength / 2);
        carry = data.byteLength % 2 ? data[data.byteLength - 1] : null;
        if (!samples) continue;
        received = true;
        const pcm = new DataView(data.buffer, data.byteOffset, samples * 2);
        const buffer = context.createBuffer(1, samples, RATE);
        const channel = buffer.getChannelData(0);
        for (let i = 0; i < samples; i++) channel[i] = pcm.getInt16(i * 2, true) / 32768;
        const source = context.createBufferSource();
        source.buffer = buffer;
        source.connect(context.destination);
        source.onended = () => source.disconnect();
        const start = Math.max(scheduledUntil, context.currentTime + 0.01);
        source.start(start);
        scheduledUntil = start + buffer.duration;
      }
      if (!received || carry !== null) throw new Error("invalid PCM stream");
    } finally {
      void reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
  while (scheduledUntil > context.currentTime) await pause(signal);
}

type Playback = { turn: string; context: AudioContext; controller: AbortController };

export function useVoicePlayback(enabled: boolean) {
  const current = useRef<Playback | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const stop = useCallback(() => {
    const active = current.current;
    if (!active) return;
    current.current = null;
    active.controller.abort();
    void active.context.close().catch(() => {});
    setPlaying(null);
  }, []);
  useEffect(() => { if (!enabled) stop(); }, [enabled, stop]);
  useEffect(() => {
    const visibility = () => { if (document.hidden) stop(); };
    document.addEventListener("visibilitychange", visibility);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      stop();
    };
  }, [stop]);

  const toggle = (turn: string, text: string) => {
    const sameTurn = current.current?.turn === turn;
    stop();
    setFailed(null);
    if (sameTurn || !enabled || !text.trim()) return;
    try {
      const context = new AudioContext();
      const controller = new AbortController();
      const active = { turn, context, controller };
      current.current = active;
      setPlaying(turn);
      // Resume synchronously in the click, before awaiting the network.
      void context.resume().then(() => play(context, controller.signal, text)).catch(() => {
        if (current.current === active) setFailed(turn);
      }).finally(() => {
        if (current.current === active) stop();
      });
    } catch {
      setFailed(turn);
    }
  };
  return { playing, failed, toggle };
}

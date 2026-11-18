const outputRate = 24000;
const chunkSamples = outputRate / 10;

class VoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new DataView(new ArrayBuffer(chunkSamples * 2));
    this.samples = 0;
    this.weight = 0;
    this.sum = 0;
    this.stopped = false;
    this.port.onmessage = (event) => {
      if (event.data !== "flush" || this.stopped) return;
      this.stopped = true;
      if (this.weight) this.push(this.sum / this.weight);
      this.flush();
      this.port.postMessage({ type: "flushed" });
    };
  }

  push(sample) {
    const value = Math.max(-1, Math.min(1, sample));
    this.chunk.setInt16(this.samples++ * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true);
    if (this.samples === chunkSamples) this.flush();
  }

  flush() {
    if (!this.samples) return;
    const audio = this.chunk.buffer.slice(0, this.samples * 2);
    this.port.postMessage({ type: "audio", audio }, [audio]);
    this.samples = 0;
  }

  process(inputs) {
    if (this.stopped) return false;
    for (const sample of inputs[0]?.[0] ?? []) {
      let remaining = outputRate;
      while (remaining) {
        const weight = Math.min(remaining, sampleRate - this.weight);
        this.weight += weight;
        this.sum += weight * sample;
        remaining -= weight;
        if (this.weight === sampleRate) {
          this.push(this.sum / sampleRate);
          this.sum = 0;
          this.weight = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("cc-remote-voice-capture", VoiceCaptureProcessor);

/* Audio-only PCM16 transport. Area resampling carries phase across render quanta. */
class DictationCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.phase = 0;
    this.area = 0;
    this.packet = new Int16Array(1280);
    this.count = 0;
    this.energy = 0;
    this.port.onmessage = (event) => {
      if (event.data === "flush") {
        this.send();
        this.port.postMessage({ flushed: true });
      }
    };
  }
  send() {
    if (!this.count) return;
    const pcm = this.packet.slice(0, this.count);
    this.port.postMessage({ pcm: pcm.buffer, level: Math.sqrt(this.energy / this.count) }, [pcm.buffer]);
    this.count = 0;
    this.energy = 0;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const value of input) {
      let remaining = 1;
      while (remaining > 0.0000001) {
        const take = Math.min(remaining, this.ratio - this.phase);
        this.area += value * take;
        this.phase += take;
        remaining -= take;
        if (this.phase >= this.ratio - 0.0000001) {
          const sample = Math.max(-1, Math.min(1, this.area / this.ratio));
          this.packet[this.count++] = Math.round(sample * (sample < 0 ? 32768 : 32767));
          this.energy += sample * sample;
          this.phase = 0;
          this.area = 0;
          if (this.count === 1280) this.send();
        }
      }
    }
    return true;
  }
}
registerProcessor("dictation-capture", DictationCapture);

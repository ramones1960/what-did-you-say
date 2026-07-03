// マイク入力を 16kHz mono int16 PCM に変換して main スレッドへ渡す AudioWorklet。
// ブラウザの AudioContext は 44.1k/48kHz で動くため、ここで線形補間で
// ダウンサンプルする。

const TARGET_RATE = 16000;
const CHUNK_SAMPLES = 2048; // 16kHz で 128ms ごとに送信

class PcmWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.readPos = 0;
    this.input = new Float32Array(0);
    this.out = new Int16Array(CHUNK_SAMPLES);
    this.outPos = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;

    // 前回の残りと連結
    const merged = new Float32Array(this.input.length + channel.length);
    merged.set(this.input);
    merged.set(channel, this.input.length);

    let pos = this.readPos;
    while (pos + this.ratio < merged.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const sample = merged[i] * (1 - frac) + merged[i + 1] * frac;
      const s = Math.max(-1, Math.min(1, sample));
      this.out[this.outPos++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.outPos === CHUNK_SAMPLES) {
        this.port.postMessage(this.out.buffer.slice(0));
        this.outPos = 0;
      }
      pos += this.ratio;
    }

    // 未消費サンプルを持ち越す
    const consumed = Math.floor(pos);
    this.input = merged.slice(consumed);
    this.readPos = pos - consumed;
    return true;
  }
}

registerProcessor("pcm-worklet", PcmWorklet);

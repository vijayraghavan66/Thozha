/* Runs only during an explicitly active call. No network or persistent storage. */
class ThozhaCapture extends AudioWorkletProcessor {
  constructor() { super(); this.frames = []; this.length = 0; }
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) {
      this.frames.push(channel.slice()); this.length += channel.length;
      if (this.length >= sampleRate * 0.02) {
        const block = new Float32Array(this.length); let offset = 0;
        for (const frame of this.frames) { block.set(frame, offset); offset += frame.length; }
        this.port.postMessage(block, [block.buffer]); this.frames = []; this.length = 0;
      }
    }
    return true;
  }
}
registerProcessor('thozha-capture', ThozhaCapture);

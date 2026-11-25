export type CallState =
  | 'connecting'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'interrupted'
  | 'muted'
  | 'disconnected'
  | 'ended';

/** Hysteresis, onset debounce and bounded utterances. Frames arrive every ~20ms. */
export class TurnDetector {
  speaking = false;
  private onset = 0;
  private silence = 0;
  private duration = 0;
  private voicedMs = 0;
  private noise = 0.003;
  reset() {
    this.speaking = false;
    this.onset = this.silence = this.duration = this.voicedMs = 0;
  }
  feed(rms: number, ms: number, playback: boolean): 'start' | 'end' | undefined {
    const threshold = Math.max(playback ? 0.035 : 0.012, this.noise * (playback ? 4 : 3));
    const voiced = rms > threshold;
    if (!this.speaking && !voiced) this.noise = Math.min(0.012, this.noise * 0.98 + rms * 0.02);
    if (!this.speaking) {
      this.onset = voiced ? this.onset + ms : 0;
      if (this.onset >= (playback ? 180 : 120)) {
        this.speaking = true;
        this.voicedMs = this.onset;
        this.duration = this.onset;
        this.silence = 0;
        return 'start';
      }
    } else {
      this.duration += ms;
      if (voiced) this.voicedMs += ms;
      this.silence = voiced ? 0 : this.silence + ms;
      if (this.silence >= 620 || this.duration >= 29000) {
        const valid = this.voicedMs >= 260;
        this.reset();
        return valid ? 'end' : undefined;
      }
    }
  }
}
export function splitSpeech(text: string, final = false): { sentences: string[]; rest: string } {
  // Never read code fences. Complete sentences can be synthesized before the full reply.
  const cleaned = text.replace(/```[\s\S]*?(?:```|$)/g, ' Code omitted. ').replace(/[#*_`]/g, '');
  const sentences: string[] = [];
  let rest = cleaned;
  for (;;) {
    const match = /[.!?](?:\s|$)/.exec(rest);
    let end = match ? match.index + 1 : -1;
    if ((end < 0 || end > 300) && rest.length > 300) {
      const space = rest.lastIndexOf(' ', 300);
      end = space > 0 ? space : 300;
    }
    if (end <= 0) break;
    const sentence = rest.slice(0, end).trim();
    if (sentence) sentences.push(sentence);
    rest = rest.slice(end).trimStart();
  }
  if (final && rest.trim()) {
    sentences.push(rest.trim());
    rest = '';
  }
  return { sentences, rest };
}
export function wav16(samples: Float32Array, rate: number): string {
  const size = Math.floor((samples.length * 16000) / rate);
  const bytes = new ArrayBuffer(44 + size * 2);
  const view = new DataView(bytes);
  const str = (at: number, value: string) =>
    [...value].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
  str(0, 'RIFF');
  view.setUint32(4, 36 + size * 2, true);
  str(8, 'WAVE');
  str(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true);
  view.setUint32(28, 32000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  str(36, 'data');
  view.setUint32(40, size * 2, true);
  for (let i = 0; i < size; i++) {
    // Average each source interval to reduce aliasing when downsampling.
    const a = Math.floor((i * rate) / 16000),
      b = Math.max(a + 1, Math.floor(((i + 1) * rate) / 16000));
    let sum = 0;
    for (let j = a; j < b && j < samples.length; j++) sum += samples[j];
    view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, sum / (b - a))) * 32767, true);
  }
  const data = new Uint8Array(bytes);
  let binary = '';
  for (let i = 0; i < data.length; i += 8192)
    binary += String.fromCharCode(...data.subarray(i, i + 8192));
  return btoa(binary);
}

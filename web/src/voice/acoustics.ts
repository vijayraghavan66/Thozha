/** Acoustic policy consumes Silero probabilities, never energy alone. */
export class SpeechGate {
  speaking = false;
  silenceMs = 0;
  private onset = 0;
  private duration = 0;
  private voicedMs = 0;
  private noise = 0.002;
  endpointMs = 800;
  reset() {
    this.speaking = false;
    this.silenceMs = this.onset = this.duration = this.voicedMs = 0;
  }
  /** Carry confirmed near-end speech through the playback → listening transition. */
  resumeConfirmedSpeech() {
    this.reset();
    this.speaking = true;
    this.duration = this.voicedMs = 256;
  }
  feed(probability: number, rms: number, ms: number, playback: boolean) {
    if (!Number.isFinite(probability) || !Number.isFinite(rms)) return;
    if (!this.speaking && probability < 0.2 && !playback)
      this.noise = Math.max(0.0001, Math.min(0.02, this.noise * 0.98 + rms * 0.02));
    // Quiet listening must not be vetoed by a second amplitude classifier.
    const positive = playback
      ? probability >= 0.75 && rms > Math.max(0.001, this.noise * 1.5)
      : probability >= 0.5;
    const continuing = probability >= 0.3;
    if (!this.speaking) {
      this.onset = positive ? this.onset + ms : Math.max(0, this.onset - ms * 2);
      if (this.onset >= (playback ? 288 : 160)) {
        this.speaking = true;
        this.duration = this.voicedMs = this.onset;
        this.silenceMs = 0;
        return 'start';
      }
    } else {
      this.duration += ms;
      if (continuing) this.voicedMs += ms;
      this.silenceMs = continuing ? 0 : this.silenceMs + ms;
      // Retain normal pauses; avoid the previous 960 ms fixed endpoint wait.
      if (this.silenceMs >= this.endpointMs || this.duration >= 29000) {
        const valid = this.voicedMs >= 256;
        this.reset();
        return valid ? 'end' : 'discard';
      }
    }
  }
}

/** Classify residual speech after playback is attenuated by ~30 dB.
 * Wait for echo/AEC settling. This is not guaranteed speaker separation.
 */
export class BargeProbe {
  private elapsed = 0;
  private voiced = 0;
  private gap = 0;
  reset() {
    this.elapsed = this.voiced = this.gap = 0;
  }
  feed(probability: number, rms: number, ms: number): 'accept' | 'reject' | undefined {
    this.elapsed += ms;
    if (this.elapsed <= 160) return;
    const speech = Number.isFinite(probability) && probability >= 0.65 && rms >= 0.0005;
    this.voiced = speech ? this.voiced + ms : Math.max(0, this.voiced - ms);
    this.gap = speech ? 0 : this.gap + ms;
    if (this.voiced >= 224) {
      this.reset();
      return 'accept';
    }
    if (this.gap >= 288 || this.elapsed >= 960) {
      this.reset();
      return 'reject';
    }
  }
}

/** Retained for diagnostic comparison; no full-ASR gate in playback. */
export function intentionalSpeech(text: string, assistant: string) {
  const words = (value: string) => value.toLowerCase().match(/[a-z0-9']+/g) || [];
  const user = words(text),
    output = words(assistant);
  if (!user.length || /^(thank you|thanks for watching|you|bye)[.!\s]*$/i.test(text.trim()))
    return false;
  const spoken = new Set(output);
  const overlap = user.filter(word => spoken.has(word)).length / user.length;
  if (output.length && overlap >= 0.75) return false;
  return user.length >= 2 || /^(stop|wait|no|pause|enough)$/i.test(user[0] || '');
}

export function normalizeSpeech(text: string) {
  return text
    .replace(/https?:\/\/\S+/g, 'link')
    .replace(/\*\*|__|[`#]/g, '')
    .replace(/\s*[—–]\s*/g, ', ')
    .replace(/\n\s*[-*]\s*/g, '. ')
    .replace(/\n+/g, '. ')
    .replace(/([.!?])[.!?]+/g, '$1')
    .replace(/\s+/g, ' ')
    .trim();
}

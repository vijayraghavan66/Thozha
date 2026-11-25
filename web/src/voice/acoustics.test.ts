import { describe, expect, it } from 'vitest';
import { BargeProbe, SpeechGate, intentionalSpeech, normalizeSpeech } from './acoustics';

describe('neural speech policy', () => {
  it('never starts from loud noise without speech probability', () => {
    for (const playback of [false, true]) {
      const gate = new SpeechGate();
      for (let i = 0; i < 1000; i++) expect(gate.feed(0.08, 0.7, 32, playback)).toBeUndefined();
    }
  });
  it('rejects short cough-like bursts even with high probability during playback', () => {
    const gate = new SpeechGate();
    for (let j = 0; j < 20; j++) {
      for (let i = 0; i < 8; i++) expect(gate.feed(0.98, 0.1, 32, true)).toBeUndefined();
      for (let i = 0; i < 20; i++) expect(gate.feed(0.01, 0.001, 32, true)).toBeUndefined();
    }
  });
  it('requires stricter onset for playback and preserves natural pauses', () => {
    const gate = new SpeechGate();
    for (let i = 0; i < 8; i++) expect(gate.feed(0.99, 0.1, 32, true)).toBeUndefined();
    expect(gate.feed(0.99, 0.1, 32, true)).toBe('start');
    for (let i = 0; i < 24; i++) expect(gate.feed(0.01, 0.001, 32, false)).toBeUndefined();
    gate.feed(0.8, 0.1, 32, false);
    for (let i = 0; i < 24; i++) expect(gate.feed(0.01, 0.001, 32, false)).toBeUndefined();
    expect(gate.feed(0.01, 0.001, 32, false)).toBe('end');
  });
  it('preserves quiet speech and discards too-short listening bursts', () => {
    const quiet = new SpeechGate();
    for (let i = 0; i < 4; i++) expect(quiet.feed(0.6, 0.0003, 32, false)).toBeUndefined();
    expect(quiet.feed(0.6, 0.0003, 32, false)).toBe('start');
    for (let i = 0; i < 8; i++) quiet.feed(0.6, 0.0003, 32, false);
    for (let i = 0; i < 24; i++) expect(quiet.feed(0.01, 0, 32, false)).toBeUndefined();
    expect(quiet.feed(0.01, 0, 32, false)).toBe('end');
    const short = new SpeechGate();
    for (let i = 0; i < 5; i++) short.feed(0.9, 0.1, 32, false);
    for (let i = 0; i < 24; i++) short.feed(0.01, 0, 32, false);
    expect(short.feed(0.01, 0, 32, false)).toBe('discard');
  });
  it('rejects assistant echo and empty recognition, accepts distinct intentional words', () => {
    expect(intentionalSpeech('I can help with that', 'Sure, I can help with that.')).toBe(false);
    expect(intentionalSpeech('Thank you.', '')).toBe(false);
    expect(intentionalSpeech('', '')).toBe(false);
    expect(intentionalSpeech('Wait, another question', 'Here is your answer.')).toBe(true);
    expect(intentionalSpeech('Stop', 'Here is your answer.')).toBe(true);
  });
  it('normalizes punctuation without fake breaths or SSML', () => {
    expect(normalizeSpeech('**Hello** — there!\nHow are you?')).toBe('Hello, there! How are you?');
  });
});

describe('duck-and-verify residual speech', () => {
  it('ignores settling echo then rejects silence without ASR', () => {
    const probe = new BargeProbe();
    for (let i = 0; i < 5; i++) expect(probe.feed(0.99, 0.1, 32)).toBeUndefined();
    for (let i = 0; i < 8; i++) expect(probe.feed(0.05, 0.001, 32)).toBeUndefined();
    expect(probe.feed(0.05, 0.001, 32)).toBe('reject');
  });
  it('accepts sustained speech after echo settling, without a transcription wait', () => {
    const probe = new BargeProbe();
    for (let i = 0; i < 11; i++) expect(probe.feed(0.9, 0.005, 32)).toBeUndefined();
    expect(probe.feed(0.9, 0.005, 32)).toBe('accept');
  });
  it('does not accept a short speech-like cough or loud non-speech', () => {
    const probe = new BargeProbe();
    for (let i = 0; i < 5; i++) probe.feed(0.1, 0.1, 32);
    for (let i = 0; i < 4; i++) expect(probe.feed(0.95, 0.1, 32)).toBeUndefined();
    let result;
    for (let i = 0; i < 9; i++) result = probe.feed(0.05, 0.5, 32);
    expect(result).toBe('reject');
  });
});

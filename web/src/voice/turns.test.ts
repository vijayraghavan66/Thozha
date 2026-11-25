import { describe, expect, it } from 'vitest';
import { TurnDetector, splitSpeech, wav16 } from './turns';

describe('local voice turn segmentation', () => {
  it('rejects clicks and requires sustained speech before ending after silence', () => {
    const vad = new TurnDetector();
    expect(vad.feed(0.2, 20, false)).toBeUndefined();
    expect(vad.feed(0.001, 20, false)).toBeUndefined();
    let result;
    for (let i = 0; i < 6; i++) result = vad.feed(0.1, 20, false);
    expect(result).toBe('start');
    for (let i = 0; i < 8; i++) vad.feed(0.1, 20, false);
    for (let i = 0; i < 30; i++) expect(vad.feed(0.001, 20, false)).toBeUndefined();
    expect(vad.feed(0.001, 20, false)).toBe('end');
    expect(vad.speaking).toBe(false);
  });
  it('uses a higher sustained threshold during speaker playback', () => {
    const vad = new TurnDetector();
    for (let i = 0; i < 50; i++) expect(vad.feed(0.02, 20, true)).toBeUndefined();
    let result;
    for (let i = 0; i < 9; i++) result = vad.feed(0.2, 20, true);
    expect(result).toBe('start');
    vad.reset();
    expect(vad.speaking).toBe(false);
  });
  it('bounds continuous speech even without a silent pause', () => {
    const vad = new TurnDetector();
    let ends = 0;
    for (let i = 0; i < 1500; i++) if (vad.feed(0.2, 20, false) === 'end') ends++;
    expect(ends).toBe(1);
  });
  it('splits complete sentences and retains partial speech', () => {
    expect(splitSpeech('Hello there. How are')).toEqual({
      sentences: ['Hello there.'],
      rest: 'How are',
    });
    expect(splitSpeech('How are you', true)).toEqual({ sentences: ['How are you'], rest: '' });
    expect(splitSpeech('```js\nsecret()\n``` Hello.', true).sentences.join(' ')).not.toContain(
      'secret'
    );
  });
  it('encodes bounded 16kHz PCM WAV from device sample rates', () => {
    const data = Uint8Array.from(atob(wav16(new Float32Array(48000).fill(0.5), 48000)), c =>
      c.charCodeAt(0)
    );
    const view = new DataView(data.buffer);
    expect(view.getUint32(24, true)).toBe(16000);
    expect(view.getUint32(40, true)).toBe(32000);
    expect(data.length).toBe(32044);
  });
});

import { afterEach, describe, expect, it, vi } from 'vitest';
import { CallSession } from './VoiceCall';
import type { WorkspaceApi } from '../api/client';

afterEach(() => {
  vi.restoreAllMocks();
  sessionStorage.clear();
});
describe('call lifecycle and cancellation ownership', () => {
  function fixture() {
    const cancel = vi.fn().mockResolvedValue({ status: 'cancelled' });
    const api = { cancel } as unknown as WorkspaceApi;
    const notify = vi.fn();
    const session = new CallSession(api, 'conversation', notify, vi.fn());
    // Tests deliberately exercise private lifetime ownership with inert audio resources.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return { session, cancel, notify, internal: session as unknown as Record<string, any> };
  }
  it('keeps the startup error in the authoritative ended snapshot', async () => {
    const { session, notify } = fixture();
    vi.stubGlobal(
      'AudioContext',
      class {
        constructor() {
          throw new Error('Audio permission unavailable');
        }
      }
    );
    try {
      await session.start();
      expect(session.snapshot.state).toBe('ended');
      expect(session.snapshot.message).toBe('Error: Audio permission unavailable');
      expect(notify.mock.calls.at(-1)?.[0]).toBe(session.snapshot);
    } finally {
      vi.unstubAllGlobals();
    }
  });
  it('ends capture, worklet and output on end exactly once', () => {
    const { session, internal, notify } = fixture();
    const track = { stop: vi.fn(), onended: vi.fn() };
    const disconnect = vi.fn(),
      close = vi.fn().mockResolvedValue(undefined),
      stop = vi.fn();
    internal.frames = [new Float32Array(512)];
    internal.preroll = [new Float32Array(512)];
    internal.media = { getTracks: () => [track] };
    internal.worklet = { port: { onmessage: vi.fn() }, disconnect };
    internal.source = { disconnect };
    internal.zero = { disconnect };
    internal.context = { close };
    internal.output = { stop };
    session.end();
    session.end();
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(track.onended).toBeNull();
    expect(internal.frames).toEqual([]);
    expect(internal.preroll).toEqual([]);
    expect(close).toHaveBeenCalledTimes(1);
    expect(stop).toHaveBeenCalledTimes(1);
    expect(disconnect).toHaveBeenCalledTimes(3);
    expect(notify.mock.calls.at(-1)?.[0].state).toBe('ended');
  });
  it('mutes actual capture tracks and clears pending audio frames', () => {
    const { session, internal } = fixture();
    const track = { enabled: true };
    internal.media = { getAudioTracks: () => [track] };
    internal.frames = [new Float32Array(10)];
    session.mute();
    expect(track.enabled).toBe(false);
    expect(internal.frames).toEqual([]);
    expect(session.snapshot.state).toBe('muted');
    session.mute();
    expect(track.enabled).toBe(true);
    expect(session.snapshot.state).toBe('listening');
  });
  it('interrupts output immediately and cancels only its own accepted response', () => {
    const { session, internal, cancel } = fixture();
    const stop = vi.fn();
    internal.output = { stop };
    internal.pending = { conversation: 'c', key: 'same-key', content: 'hello', response: 'r' };
    const epoch = internal.epoch;
    session.interrupt();
    expect(stop).toHaveBeenCalledTimes(1);
    expect(cancel).toHaveBeenCalledWith('c', 'r');
    expect(internal.epoch).toBeGreaterThan(epoch);
    expect(internal.pending).toBeUndefined();
    expect(session.snapshot.state).toBe('interrupted');
  });
  it('suppresses a late transcript after interruption', async () => {
    const { session, internal } = fixture();
    let finish!: (row: object) => void;
    internal.speech = vi.fn(
      () =>
        new Promise(resolve => {
          finish = resolve;
        })
    );
    const create = vi.fn();
    internal.api = { create };
    const response = internal.respond('encoded-wav');
    session.interrupt();
    finish({ text: 'late old transcript' });
    await response;
    expect(create).not.toHaveBeenCalled();
    expect(session.snapshot.user).toBe('');
  });
  it('cancels a residual-speech probe immediately on manual interrupt', () => {
    const { session, internal } = fixture();
    internal.verifyingBarge = internal.barging = true;
    session.interrupt();
    expect(internal.verifyingBarge).toBe(false);
    expect(internal.barging).toBe(false);
  });
  it('accepts residual speech without an ASR roundtrip and retains captured audio', () => {
    const { session, internal } = fixture();
    internal.context = {};
    internal.verifyingBarge = internal.barging = true;
    internal.probe = { feed: () => 'accept', reset: vi.fn() };
    internal.frames = [new Float32Array(512)];
    internal.speech = vi.fn();
    internal.frame(new Float32Array(512).fill(0.01), 0.95);
    expect(session.snapshot.state).toBe('interrupted');
    expect(internal.detector.speaking).toBe(true);
    expect(internal.frames).toHaveLength(2);
    expect(internal.speech).not.toHaveBeenCalled();
  });
  it('retains confirmed short interruptions through end-of-turn silence', () => {
    const { internal } = fixture();
    internal.context = {};
    internal.verifyingBarge = true;
    internal.probe = { feed: () => 'accept', reset: vi.fn() };
    internal.frames = [new Float32Array(512)];
    internal.frame(new Float32Array(512).fill(0.01), 0.9);
    for (let i = 0; i < 24; i++) expect(internal.detector.feed(0.01, 0, 32, false)).toBeUndefined();
    expect(internal.detector.feed(0.01, 0, 32, false)).toBe('end');
  });
  it('clears stale captured speech on manual interruption', () => {
    const { session, internal } = fixture();
    internal.frames = [new Float32Array(512)];
    internal.preroll = [new Float32Array(512)];
    internal.detector.speaking = true;
    session.interrupt();
    expect(internal.frames).toEqual([]);
    expect(internal.preroll).toEqual([]);
    expect(internal.detector.speaking).toBe(false);
  });
  it('rejects an echo probe without cancelling durable requests', () => {
    const { internal, cancel } = fixture();
    internal.context = {};
    internal.verifyingBarge = true;
    internal.probe = { feed: () => 'reject', reset: vi.fn() };
    internal.frame(new Float32Array(512), 0.05);
    expect(internal.verifyingBarge).toBe(false);
    expect(internal.frames).toEqual([]);
    expect(cancel).not.toHaveBeenCalled();
  });
  it('serializes interruption ASR and resumes a false alarm without new requests', async () => {
    const { internal, cancel } = fixture();
    let finish!: (row: object) => void;
    internal.speech = vi.fn(
      () =>
        new Promise(resolve => {
          finish = resolve;
        })
    );
    internal.candidate = true;
    internal.candidateText = 'Here is the current phrase';
    const play = vi.fn();
    internal.playback = { play };
    const first = internal.confirmInterruption('wav');
    await internal.confirmInterruption('same-wav');
    expect(internal.speech).toHaveBeenCalledTimes(1);
    finish({ text: 'the current phrase' });
    await first;
    expect(play).toHaveBeenCalledTimes(1);
    expect(cancel).not.toHaveBeenCalled();
    expect(internal.confirmingInterruption).toBe(false);
  });
  it('invalidates an endpoint across mute and unmute', () => {
    const { session, internal } = fixture();
    const revision = internal.speechRevision;
    session.mute();
    session.mute();
    expect(internal.speechRevision).toBeGreaterThan(revision);
  });
  it('does not resume an ended call after delayed interruption recognition', async () => {
    const { session, internal } = fixture();
    let finish!: (row: object) => void;
    internal.speech = vi.fn(
      () =>
        new Promise(resolve => {
          finish = resolve;
        })
    );
    internal.candidate = true;
    const play = vi.fn(),
      stop = vi.fn();
    internal.playback = { play };
    internal.output = { stop };
    const first = internal.confirmInterruption('wav');
    session.end();
    finish({ text: '' });
    await first;
    expect(play).not.toHaveBeenCalled();
    expect(stop).toHaveBeenCalledTimes(1);
  });
  it('retains response ownership when a chunk ends during interruption confirmation', async () => {
    vi.useFakeTimers();
    const { session, internal } = fixture();
    internal.pending = { conversation: 'c', key: 'key', content: 'question', response: 'r' };
    internal.busy = true;
    internal.candidate = true;
    const finished = internal.complete(internal.epoch);
    try {
      await vi.advanceTimersByTimeAsync(75);
      expect(internal.pending.response).toBe('r');
      expect(internal.busy).toBe(true);
      internal.candidate = false;
      await vi.advanceTimersByTimeAsync(25);
      await finished;
      expect(internal.pending).toBeUndefined();
      expect(session.snapshot.state).toBe('listening');
    } finally {
      vi.useRealTimers();
    }
  });
  it('invalidates semantic endpoint work on manual interruption', () => {
    const { session, internal } = fixture();
    const before = internal.speechRevision;
    session.interrupt();
    expect(internal.speechRevision).toBeGreaterThan(before);
  });
  it('retains an uncertain request key while reconciling cancellation', async () => {
    const { session, internal } = fixture();
    const pending = { conversation: 'c', key: 'durable-key', content: 'question' };
    internal.pending = pending;
    let finish!: (row: object) => void;
    const submit = vi.fn(
      () =>
        new Promise(resolve => {
          finish = resolve;
        })
    );
    const cancel = vi.fn().mockResolvedValue({ status: 'cancelled' });
    internal.api = { submit, cancel };
    session.interrupt();
    expect(submit).toHaveBeenCalledWith('c', { content: 'question' }, 'durable-key');
    expect(internal.pending.key).toBe('durable-key');
    expect(session.snapshot.state).toBe('disconnected');
    finish({ response_id: 'r' });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(cancel).toHaveBeenCalledWith('c', 'r');
    expect(internal.pending).toBeUndefined();
  });
});

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
  it('ends capture, worklet and output on end exactly once', () => {
    const { session, internal, notify } = fixture();
    const track = { stop: vi.fn(), onended: vi.fn() };
    const disconnect = vi.fn(),
      close = vi.fn().mockResolvedValue(undefined),
      stop = vi.fn();
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

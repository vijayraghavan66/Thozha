import { describe, expect, it, vi } from 'vitest';
import { ResumablePlayback } from './playback';

describe('false interruption recovery', () => {
  function fixture() {
    const sources: {
      start: ReturnType<typeof vi.fn>;
      stop: ReturnType<typeof vi.fn>;
      disconnect: ReturnType<typeof vi.fn>;
      onended?: () => void;
    }[] = [];
    const context = {
      currentTime: 0,
      createBufferSource: () => {
        const source = {
          start: vi.fn(),
          stop: vi.fn(),
          connect: vi.fn(),
          disconnect: vi.fn(),
          onended: undefined as (() => void) | undefined,
        };
        sources.push(source);
        return source;
      },
    };
    const player = new ResumablePlayback(
      context as unknown as AudioContext,
      { duration: 10 } as AudioBuffer,
      {} as AudioNode
    );
    return { player, context, sources };
  }
  it('resumes exactly after played samples rather than restarting the answer', async () => {
    const { player, context, sources } = fixture();
    let complete = false;
    void player.done.then(() => {
      complete = true;
    });
    player.play();
    context.currentTime = 2.5;
    player.pause();
    sources[0].onended?.();
    await Promise.resolve();
    expect(complete).toBe(false);
    player.play();
    expect(sources[1].start).toHaveBeenCalledWith(0, 2.5);
    sources[1].onended?.();
    await player.done;
    expect(complete).toBe(true);
  });
  it('never resumes after cancellation and unblocks queued playback', async () => {
    const { player, sources } = fixture();
    player.play();
    player.pause();
    player.stop();
    player.play();
    await player.done;
    expect(sources).toHaveLength(1);
  });
  it('supports multiple interruptions without cumulative offset drift', () => {
    const { player, context, sources } = fixture();
    player.play();
    context.currentTime = 2;
    player.pause();
    context.currentTime = 8;
    player.play();
    context.currentTime = 9;
    player.pause();
    player.play();
    expect(sources[2].start).toHaveBeenCalledWith(0, 3);
    player.stop();
  });
});

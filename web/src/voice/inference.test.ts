import { describe, it, expect, vi } from 'vitest';
import { orderedInference } from './inference';

describe('recurrent VAD inference ordering', () => {
  it('never overlaps inference and preserves arrival order', async () => {
    let active = 0,
      peak = 0;
    const seen: number[] = [];
    const runner = orderedInference(
      async frame => {
        peak = Math.max(peak, ++active);
        await new Promise(resolve => setTimeout(resolve, 2));
        seen.push(frame[0]);
        --active;
      },
      vi.fn(),
      vi.fn()
    );
    await Promise.all([1, 2, 3, 4, 5].map(n => runner.feed(new Float32Array([n]))));
    expect(peak).toBe(1);
    expect(seen).toEqual([1, 2, 3, 4, 5]);
  });
  it('reports overload instead of silently corrupting the utterance', async () => {
    const fail = vi.fn();
    const process = vi.fn().mockResolvedValue(undefined);
    const runner = orderedInference(process, vi.fn(), fail, 2);
    await Promise.all([1, 2, 3].map(n => runner.feed(new Float32Array([n]))));
    expect(fail).toHaveBeenCalledTimes(1);
    expect(process).not.toHaveBeenCalled();
  });
  it('does not process queued frames after teardown', async () => {
    const process = vi.fn().mockResolvedValue(undefined);
    const runner = orderedInference(process, vi.fn(), vi.fn());
    const pending = runner.feed(new Float32Array(512));
    runner.stop();
    await pending;
    expect(process).not.toHaveBeenCalled();
  });
});

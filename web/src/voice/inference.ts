/** Serialize recurrent inference. A backlog is visible and fatal, never silently dropped. */
export function orderedInference(
  process: (frame: Float32Array) => Promise<void>,
  report: (queue: number, inferenceMs: number, waitMs: number) => void,
  overload: () => void,
  limit = 64
) {
  let tail = Promise.resolve();
  let depth = 0;
  let stopped = false;
  return {
    stop() {
      stopped = true;
    },
    async feed(frame: Float32Array) {
      if (stopped) return;
      if (depth >= limit) {
        stopped = true;
        overload();
        return;
      }
      const queued = performance.now();
      ++depth;
      const task = tail.then(async () => {
        if (stopped) return;
        const started = performance.now();
        await process(frame);
        report(depth, performance.now() - started, started - queued);
      });
      tail = task.catch(() => {
        stopped = true;
        overload();
      });
      try {
        await task;
      } catch {
        // tail owns the visible fail-closed overload transition.
      } finally {
        --depth;
      }
    },
  };
}

import { useEffect, useRef } from 'react';

export function WaterSurface({
  reduced = false,
  quiet = false,
}: {
  reduced?: boolean;
  quiet?: boolean;
}) {
  const field = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const surface = field.current;
    const target = canvas.current;
    const host = surface?.parentElement;
    if (quiet || reduced || !surface || !target || !host) return;
    let active = true;
    let dispose: (() => void) | undefined;
    void import('../water/engine')
      .then(({ mountWater }) => {
        if (active) dispose = mountWater(surface, target, host);
      })
      .catch(error => {
        if (active)
          console.warn('Thozha water: could not load the renderer; using the still frame.', error);
      });
    return () => {
      active = false;
      dispose?.();
    };
  }, [quiet, reduced]);

  return (
    <div
      ref={field}
      aria-hidden="true"
      data-water="poster"
      data-motion="paused"
      className={`th-liquid-field absolute inset-0 pointer-events-none${quiet ? ' is-quiet' : ''}`}
    >
      {!quiet && (
        <canvas ref={canvas} width={1} height={1} className="absolute inset-0 h-full w-full" />
      )}
    </div>
  );
}

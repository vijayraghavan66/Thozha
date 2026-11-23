import { useEffect, useId, useRef } from 'react';

const rows = 21;
const columns = 48;
const depths = Array.from({ length: rows }, (_, index) => index / (rows - 1));
const lateral = Array.from({ length: columns + 1 }, (_, index) => index / columns);
type Ripple = { x: number; y: number; time: number };

function contour(depth: number, time: number, ripple?: Ripple, now = 0) {
  const base = 52 + depth ** 1.7 * 303;
  const depthScale = 0.45 + depth * 0.85;
  let path = '';
  for (const u of lateral) {
    const x = u * 920 - 10;
    const a = u * 13 + depth * 9 - time * 1.55;
    const b = u * 22 - depth * 12 + time * 1.03;
    const c = u * 35 + depth * 17 - time * 0.72;
    let y = base + depthScale * (25 * Math.sin(a) + 12 * Math.sin(b) + 5 * Math.sin(c));
    if (ripple) {
      const age = (now - ripple.time) / 1000;
      const distance = Math.hypot(x - ripple.x, (base - ripple.y) * 2);
      const front = (distance - age * 180) / 38;
      y += 13 * Math.sin(front * 2.3) * Math.exp(-front * front * 0.35 - age * 1.25);
    }
    path += `${u ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
  }
  return path;
}

const still = depths.map(depth => contour(depth, 1.4));

export function WaterSurface({
  reduced = false,
  quiet = false,
}: {
  reduced?: boolean;
  quiet?: boolean;
}) {
  const id = useId();
  const field = useRef<HTMLDivElement>(null);
  const artwork = useRef<SVGGElement>(null);

  useEffect(() => {
    const element = field.current;
    const host = element?.parentElement;
    if (
      !element ||
      !host ||
      !artwork.current ||
      quiet ||
      !window.matchMedia ||
      !window.IntersectionObserver
    )
      return;
    const paths = artwork.current.querySelectorAll('path');
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
    const economy =
      window.matchMedia('(update: slow), (prefers-reduced-data: reduce)').matches ||
      (navigator.hardwareConcurrency > 0 && navigator.hardwareConcurrency <= 4);
    let visible = false;
    let frame = 0;
    let previous = 0;
    let phase = 1.4;
    let ripple: Ripple | undefined;
    const draw = (now: number) => {
      if (!previous || now - previous >= (economy ? 62 : 30)) {
        phase += previous ? Math.min(now - previous, 100) / 1000 : 0;
        previous = now;
        if (ripple && now - ripple.time > 2400) ripple = undefined;
        paths.forEach((path, index) =>
          path.setAttribute('d', contour(depths[index], phase, ripple, now))
        );
      }
      frame = requestAnimationFrame(draw);
    };
    const sync = () => {
      cancelAnimationFrame(frame);
      previous = 0;
      if (visible && !reduced && !preference.matches && !document.hidden) {
        frame = requestAnimationFrame(draw);
      } else if (reduced || preference.matches) {
        ripple = undefined;
        paths.forEach((path, index) => path.setAttribute('d', still[index]));
      }
      element.dataset.motion =
        visible && !reduced && !preference.matches && !document.hidden ? 'running' : 'paused';
    };
    const observer = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      sync();
    });
    const disturb = (event: PointerEvent) => {
      if (economy || reduced || preference.matches || event.pointerType !== 'mouse') return;
      const time = performance.now();
      if (ripple && time - ripple.time < 950) return;
      const svg = artwork.current?.ownerSVGElement;
      const matrix = svg?.getScreenCTM();
      if (!matrix) return;
      const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
      ripple = { x: point.x, y: point.y, time };
    };
    observer.observe(element);
    preference.addEventListener('change', sync);
    document.addEventListener('visibilitychange', sync);
    host.addEventListener('pointermove', disturb);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      preference.removeEventListener('change', sync);
      document.removeEventListener('visibilitychange', sync);
      host.removeEventListener('pointermove', disturb);
    };
  }, [quiet, reduced]);

  return (
    <div
      ref={field}
      aria-hidden="true"
      className={`th-liquid-field absolute inset-0 pointer-events-none ${quiet ? 'is-quiet' : ''}`}
    >
      <svg
        viewBox="0 0 900 360"
        preserveAspectRatio="xMidYMid slice"
        className="h-full w-full"
        focusable="false"
      >
        <defs>
          <linearGradient id={id}>
            <stop offset="0" stopColor="#97999f" stopOpacity="0" />
            <stop offset=".29" stopColor="#97999f" stopOpacity=".1" />
            <stop offset=".57" stopColor="#d9dadc" stopOpacity=".65" />
            <stop offset=".76" stopColor="#f4f3ef" stopOpacity=".94" />
            <stop offset="1" stopColor="#a4a6ac" stopOpacity=".27" />
          </linearGradient>
        </defs>
        <g ref={artwork} fill="none" stroke={`url(#${id})`}>
          {still.map((path, index) => (
            <path
              key={index}
              data-water-contour={index}
              d={path}
              strokeWidth={index % 5 === 0 ? 1.8 : 0.85}
              opacity={(index % 5 === 0 ? 0.8 : 0.37) * (0.35 + (index / rows) * 0.7)}
            />
          ))}
        </g>
      </svg>
    </div>
  );
}

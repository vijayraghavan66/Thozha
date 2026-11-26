import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { mountSilver, type SilverRenderer } from '../silver/renderer';
import { motionAllowed, motionEase, type IdentityState } from './motion';

export function SilverIntelligence({
  state = 'idle',
  active = true,
  mode = 'detail',
}: {
  state?: IdentityState;
  active?: boolean;
  mode?: 'hero' | 'presence' | 'hidden' | 'detail';
}) {
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const renderer = useRef<SilverRenderer | null>(null);
  const latest = useRef({ state, active });
  const [generation, setGeneration] = useState(0);
  const gradient = useId();
  const previous = useRef<DOMRect | null>(null);
  useLayoutEffect(() => {
    const node = host.current!;
    const rect = node.getBoundingClientRect();
    const before = previous.current;
    previous.current = rect;
    if (!rect.width || !rect.height || !motionAllowed(node)) return;
    const animation = node.animate(
      [
        before?.width && before.height
          ? {
              transform: `translate(${before.left - rect.left}px,${before.top - rect.top}px) scale(${before.width / rect.width},${before.height / rect.height})`,
              opacity: 1,
            }
          : { transform: 'translateY(28px) scale(.8)', opacity: 0 },
        { transform: 'none', opacity: 1 },
      ],
      { duration: mode === 'hero' ? 680 : 440, easing: motionEase }
    );
    return () => animation.cancel();
  }, [mode]);
  useEffect(() => {
    const element = host.current!,
      target = canvas.current!;
    const restore = () => setGeneration(value => value + 1);
    target.addEventListener('webglcontextrestored', restore);
    const frame = requestAnimationFrame(() => {
      try {
        const start = performance.now();
        renderer.current = mountSilver(target, element);
        renderer.current.setState(latest.current.state);
        renderer.current.setActive(latest.current.active);
        performance.measure('th-silver-initialization', { start, end: performance.now() });
      } catch (error) {
        element.dataset.renderer = 'fallback';
        console.warn('Thozha silver: renderer unavailable; displaying the static identity.', error);
      }
    });
    return () => {
      cancelAnimationFrame(frame);
      target.removeEventListener('webglcontextrestored', restore);
      renderer.current?.dispose();
      renderer.current = null;
    };
  }, [generation]);
  useEffect(() => {
    latest.current = { state, active };
    renderer.current?.setState(state);
    renderer.current?.setActive(active);
  }, [state, active, generation]);
  return (
    <div
      ref={host}
      className="th-silver"
      data-state={state}
      data-renderer="fallback"
      aria-hidden="true"
    >
      <svg className="th-silver-poster" viewBox="0 0 420 260" fill="none">
        <defs>
          <linearGradient
            id={gradient}
            x1="120"
            y1="55"
            x2="280"
            y2="210"
            gradientUnits="userSpaceOnUse"
          >
            <stop stopColor="#f4f4f5" />
            <stop offset=".18" stopColor="#5e626a" />
            <stop offset=".4" stopColor="#15171c" />
            <stop offset=".6" stopColor="#c5c9d0" />
            <stop offset=".72" stopColor="#f6f7f9" />
            <stop offset="1" stopColor="#353941" />
          </linearGradient>
        </defs>
        <path
          d="M92 137C64 60 162 44 227 69C314 103 359 154 294 185C229 216 122 217 92 137Z"
          stroke={`url(#${gradient})`}
          strokeWidth="38"
        />
        <path d="M86 126C78 57 170 53 226 76" stroke="#e1e4e9" strokeWidth="1.5" />
      </svg>
      <canvas ref={canvas} className="th-silver-canvas" width="1" height="1" />
    </div>
  );
}

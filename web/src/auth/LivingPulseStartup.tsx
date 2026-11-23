import { useLayoutEffect, useRef, type RefObject } from 'react';

const primary = 'M17 44 C29 43 30 29 40 29 C50 29 51 47 63 40';

export function LivingPulseMark() {
  return (
    <svg aria-hidden="true" className="th-pulse-mark" viewBox="0 0 80 80" fill="none">
      <circle className="th-pulse-ring" cx="40" cy="40" r="37" pathLength="1" stroke="#62656e" />
      <path
        className="th-pulse-primary"
        d={primary}
        pathLength="1"
        stroke="#dce0e5"
        strokeWidth="2"
      />
      <path
        className="th-pulse-secondary"
        d="M19 50 C31 51 35 37 42 37 C51 37 53 54 62 48"
        pathLength="1"
        stroke="#a5abb4"
        strokeWidth="1.5"
      />
      <path
        className="th-pulse-highlight"
        d={primary}
        pathLength="1"
        stroke="#fff"
        strokeWidth="2.25"
      />
      <path
        className="th-pulse-highlight th-pulse-echo"
        d="M19 50 C31 51 35 37 42 37 C51 37 53 54 62 48"
        pathLength="1"
        stroke="#dce0e5"
        strokeWidth="1.5"
      />
    </svg>
  );
}

export function LivingPulseIntroduction({
  root,
  ready,
  dismiss,
}: {
  root: RefObject<HTMLDivElement | null>;
  ready: boolean;
  dismiss: () => void;
}) {
  const canvas = useRef<HTMLDivElement>(null);
  const mark = useRef<HTMLDivElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  const started = useRef<number | null>(null);

  useLayoutEffect(() => {
    const media = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    const skipIfHidden = () => {
      if (document.hidden || media?.matches) dismiss();
    };
    skipIfHidden();
    const skipOnKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' && event.key !== 'Tab' && !ready) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
      }
      dismiss();
    };
    document.addEventListener('pointerdown', dismiss, true);
    document.addEventListener('keydown', skipOnKey, true);
    document.addEventListener('visibilitychange', skipIfHidden);
    media?.addEventListener('change', skipIfHidden);
    window.addEventListener('resize', dismiss);
    window.visualViewport?.addEventListener('resize', dismiss);
    return () => {
      document.removeEventListener('pointerdown', dismiss, true);
      document.removeEventListener('keydown', skipOnKey, true);
      document.removeEventListener('visibilitychange', skipIfHidden);
      media?.removeEventListener('change', skipIfHidden);
      window.removeEventListener('resize', dismiss);
      window.visualViewport?.removeEventListener('resize', dismiss);
    };
  }, [dismiss, ready]);

  useLayoutEffect(() => {
    started.current ??= performance.now();
    if (!ready || !canvas.current || !mark.current || !backdrop.current || !root.current) return;
    const origin = mark.current;
    const shade = backdrop.current;
    const destination = Array.from(
      root.current.querySelectorAll<HTMLElement>('[data-pulse-slot]')
    ).find(element => element.getClientRects().length > 0);
    if (!destination || !origin.animate) {
      dismiss();
      return;
    }
    const from = origin.getBoundingClientRect();
    const to = destination.getBoundingClientRect();
    if (!from.width || !to.width) {
      dismiss();
      return;
    }

    // Measure once. The real workspace is already present and remains interactive beneath the overlay.
    const delay = Math.max(0, 570 - (performance.now() - started.current));
    const translateX = to.left + to.width / 2 - (from.left + from.width / 2);
    const translateY = to.top + to.height / 2 - (from.top + from.height / 2);
    const easing = 'cubic-bezier(0.22, 0.8, 0.27, 1)';
    destination.setAttribute('data-pulse-in-flight', '');
    root.current.setAttribute('data-intro-arriving', '');
    const content = root.current;
    content.style.setProperty('--th-intro-delay', `${delay + 85}ms`);
    const flight = origin.animate(
      [
        { transform: 'none' },
        {
          transform: `translate(${translateX}px, ${translateY}px) scale(${to.width / from.width})`,
        },
      ],
      { duration: 640, delay, easing, fill: 'both' }
    );
    const fade = shade.animate([{ opacity: 1 }, { opacity: 0 }], {
      duration: 490,
      delay: delay + 85,
      easing: 'ease-in-out',
      fill: 'both',
    });
    flight.onfinish = dismiss;
    return () => {
      flight.onfinish = null;
      flight.cancel();
      fade.cancel();
      destination.removeAttribute('data-pulse-in-flight');
      content.removeAttribute('data-intro-arriving');
      content.style.removeProperty('--th-intro-delay');
    };
  }, [ready, dismiss, root]);

  return (
    <div
      ref={canvas}
      className="th-startup th-startup-animate"
      data-awaiting-session={!ready}
      aria-hidden="true"
    >
      <div ref={backdrop} className="th-startup-backdrop" />
      <div ref={mark} className="th-pulse-flight">
        <div className="th-pulse-wave" />
        <LivingPulseMark />
      </div>
    </div>
  );
}

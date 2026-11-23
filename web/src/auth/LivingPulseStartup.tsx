import { useEffect, useState } from 'react';

const primary = 'M17 44 C29 43 30 29 40 29 C50 29 51 47 63 40';

export function LivingPulseStartup({ animateOnMount }: { animateOnMount: boolean }) {
  const [animate, setAnimate] = useState(
    () => animateOnMount && document.visibilityState === 'visible'
  );
  useEffect(() => {
    if (!animate) return;
    const settle = () => {
      if (document.visibilityState !== 'visible') setAnimate(false);
    };
    document.addEventListener('visibilitychange', settle);
    return () => document.removeEventListener('visibilitychange', settle);
  }, [animate]);

  return (
    <main className={`th-startup${animate ? ' th-startup-animate' : ''}`}>
      <div className="th-startup-content">
        <svg aria-hidden="true" className="th-pulse-mark" viewBox="0 0 80 80" fill="none">
          <circle className="th-pulse-ring" cx="40" cy="40" r="37" stroke="#4c4e55" />
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
        </svg>
        <p className="th-startup-brand">Thozha</p>
        <p role="status" className="th-startup-status">
          Checking browser session…
        </p>
      </div>
    </main>
  );
}

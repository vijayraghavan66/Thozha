import { useEffect, useId, useLayoutEffect, useRef, type ReactNode } from 'react';
import { muted, outline } from './styles';

export function Panel({ children, cls = '' }: { children: ReactNode; cls?: string }) {
  return (
    <section
      className={`rounded-[20px] border border-white/[.09] bg-[#161719]/85 shadow-[0_18px_60px_rgba(0,0,0,.18)] ${cls}`}
    >
      {children}
    </section>
  );
}

export function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="inline-block rounded-full border border-white/10 bg-white/[.06] px-2.5 py-1 text-[10px] text-[#c4c5cb]">
      {children}
    </span>
  );
}

export function Line({ title, desc, flag }: { title: string; desc: string; flag?: string }) {
  return (
    <div className="flex items-center gap-3 border-b border-white/[.07] py-4 last:border-none">
      <div
        aria-hidden="true"
        className="grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-white/[.09] bg-white/[.045] text-xl text-[#c9cad0]"
      >
        ◌
      </div>
      <div className="min-w-0 flex-1">
        <p className="break-words text-[13px] font-medium text-[#ececf0]">{title}</p>
        <p className="mt-1 text-[11px] text-[#a4a5af]">{desc}</p>
      </div>
      {flag && <Chip>{flag}</Chip>}
    </div>
  );
}

export function Head({
  label,
  title,
  desc,
  action,
}: {
  label: string;
  title: string;
  desc: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
      <div>
        <p className="mb-2 text-[10px] font-semibold tracking-[.23em] text-[#a4a5af]">{label}</p>
        <h1 className="text-[34px] font-semibold tracking-[-.055em] text-[#f5f5f6]">{title}</h1>
        <p className="mt-2 text-[13px] text-[#a4a5af]">{desc}</p>
      </div>
      {action}
    </div>
  );
}

export function Stat({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <Panel cls="min-w-0 p-5">
      <p className="text-[10px] font-medium tracking-[.15em] text-[#a4a5af]">{label}</p>
      <p className="mt-3 break-words text-[28px] font-medium tracking-tight text-white">{value}</p>
      <p className="mt-2 text-[11px] text-[#a4a5af]">{detail}</p>
    </Panel>
  );
}

export function Row({ main, sub, right }: { main: string; sub: string; right?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 py-4 last:border-none">
      <div className="min-w-0 break-words">
        <b className="text-xs">{main}</b>
        <p className="mt-2 text-[11px] text-[#a4a5af]">{sub}</p>
      </div>
      {right && <div className="text-xs text-[#c7c8ce]">{right}</div>}
    </div>
  );
}

export function Unavailable({
  title,
  reason,
  children,
}: {
  title: string;
  reason: string;
  children?: ReactNode;
}) {
  return (
    <Panel cls="p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <h2 className="font-semibold">{title}</h2>
        <Chip>UNAVAILABLE IN WEB v1</Chip>
      </div>
      <p className={`mt-3 ${muted}`}>{reason}</p>
      {children && <div className="mt-4">{children}</div>}
    </Panel>
  );
}

export function Notice({
  children,
  retry,
  error = false,
}: {
  children: ReactNode;
  retry?: () => void;
  error?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-white/15 bg-[#25262c] px-4 py-3 text-xs leading-5 text-[#e8e9ee]">
      <div role={error ? 'alert' : 'status'}>{children}</div>
      {retry && (
        <button type="button" className={outline} onClick={retry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Loading({ label }: { label: string }) {
  return (
    <div role="status" className="space-y-3 p-4 text-xs text-[#a4a5af]">
      <p>{label}</p>
      <div aria-hidden="true" className="h-3 w-2/3 rounded bg-white/10" />
      <div aria-hidden="true" className="h-3 w-1/2 rounded bg-white/[.07]" />
    </div>
  );
}

export function Crest() {
  return (
    <span
      aria-hidden="true"
      className="grid h-10 w-10 shrink-0 place-items-center rounded-2xl border border-white/20 bg-gradient-to-br from-[#63646b] via-[#292a30] to-[#111215] text-[24px] text-white shadow-[inset_0_1px_1px_rgba(255,255,255,.4)]"
    >
      ◈
    </span>
  );
}

export function Water() {
  const id = useId().replace(/:/g, '');
  const field = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = field.current;
    const surface = element?.parentElement;
    if (!element || !surface || !window.matchMedia?.('(pointer: fine)').matches) return;
    let bounds: DOMRect | undefined;
    let frame = 0;
    const enter = () => {
      bounds = surface.getBoundingClientRect();
    };
    const move = (event: PointerEvent) => {
      if (
        !bounds ||
        surface.closest('.th-stop-motion') ||
        window.matchMedia('(prefers-reduced-motion: reduce)').matches
      )
        return;
      cancelAnimationFrame(frame);
      const x = (event.clientX - bounds.left) / bounds.width - 0.5;
      const y = (event.clientY - bounds.top) / bounds.height - 0.5;
      frame = requestAnimationFrame(() => {
        element.style.setProperty('--liquid-x', `${(x * 9).toFixed(1)}px`);
        element.style.setProperty('--liquid-y', `${(y * 6).toFixed(1)}px`);
      });
    };
    const leave = () => {
      bounds = undefined;
      cancelAnimationFrame(frame);
      element.style.removeProperty('--liquid-x');
      element.style.removeProperty('--liquid-y');
    };
    surface.addEventListener('pointerenter', enter);
    surface.addEventListener('pointermove', move);
    surface.addEventListener('pointerleave', leave);
    return () => {
      leave();
      surface.removeEventListener('pointerenter', enter);
      surface.removeEventListener('pointermove', move);
      surface.removeEventListener('pointerleave', leave);
    };
  }, []);
  return (
    <div
      ref={field}
      aria-hidden="true"
      className="th-liquid-field pointer-events-none absolute inset-0 overflow-hidden"
    >
      <svg
        className="h-full w-full"
        viewBox="0 0 900 360"
        preserveAspectRatio="xMidYMid slice"
        focusable="false"
      >
        <defs>
          <linearGradient id={`${id}-depth`} x1="0" y1="0" x2=".28" y2="1">
            <stop stopColor="#74767e" stopOpacity=".04" />
            <stop offset=".43" stopColor="#9b9da7" stopOpacity=".26" />
            <stop offset=".55" stopColor="#e3e5e9" stopOpacity=".12" />
            <stop offset="1" stopColor="#101115" stopOpacity=".05" />
          </linearGradient>
          <linearGradient id={`${id}-pearl`} x1="0" y1="0" x2="0" y2="1">
            <stop stopColor="#f5f6f7" stopOpacity=".42" />
            <stop offset=".15" stopColor="#c2c4cd" stopOpacity=".09" />
            <stop offset=".78" stopColor="#9b9da7" stopOpacity=".16" />
            <stop offset="1" stopColor="#f3f4f6" stopOpacity=".03" />
          </linearGradient>
          <linearGradient id={`${id}-light`} x1="0" y1="0" x2="1" y2="0">
            <stop stopColor="#fff" stopOpacity="0" />
            <stop offset=".25" stopColor="#fff" stopOpacity=".08" />
            <stop offset=".53" stopColor="#fff" stopOpacity=".42" />
            <stop offset=".79" stopColor="#fff" stopOpacity=".06" />
            <stop offset="1" stopColor="#fff" stopOpacity="0" />
          </linearGradient>
        </defs>
        <g className="th-water">
          <path
            d="M-120 138 C30 205 138 61 296 123 S553 249 724 117 S968 82 1060 131 L1060 400 -120 400Z"
            fill={`url(#${id}-depth)`}
          />
          <path
            d="M-120 138 C30 205 138 61 296 123 S553 249 724 117 S968 82 1060 131"
            fill="none"
            stroke={`url(#${id}-light)`}
            strokeWidth="1.5"
          />
          <path
            d="M-80 169 C80 242 157 93 316 147 S563 267 744 140 S975 98 1050 155"
            fill="none"
            stroke="#f0f1f3"
            strokeOpacity=".13"
            strokeWidth=".8"
          />
        </g>
        <g className="th-water th-water-inner">
          <path
            d="M-110 226 C76 136 160 299 347 209 S549 132 714 234 S976 288 1040 210 L1040 400 -110 400Z"
            fill={`url(#${id}-pearl)`}
          />
          <path
            d="M-110 226 C76 136 160 299 347 209 S549 132 714 234 S976 288 1040 210"
            fill="none"
            stroke={`url(#${id}-light)`}
            strokeWidth="2"
          />
          <path
            d="M-80 251 C76 169 158 324 350 239 S550 157 728 263 S971 309 1040 236"
            fill="none"
            stroke="#e9ebef"
            strokeOpacity=".11"
          />
        </g>
        <g className="th-liquid-glint">
          <path
            d="M92 310 C219 214 277 317 421 239 S650 188 796 260 S974 302 1080 228"
            fill="none"
            stroke={`url(#${id}-light)`}
            strokeWidth="14"
            strokeOpacity=".27"
          />
          <path
            d="M171 75 C309 121 364 56 493 103 S761 137 941 72"
            fill="none"
            stroke={`url(#${id}-light)`}
            strokeWidth="2"
            strokeOpacity=".38"
          />
        </g>
      </svg>
    </div>
  );
}

export function Modal({
  title,
  children,
  close,
  cls = '',
  locked = false,
}: {
  title: string;
  children: ReactNode;
  close: () => void;
  cls?: string;
  locked?: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const headingId = useId();
  useLayoutEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const trigger = document.activeElement;
    element.showModal();
    element.querySelector<HTMLElement>('[data-dialog-focus]')?.focus();
    return () => {
      element.close();
      // Restore only when the trigger survived and navigation did not move focus elsewhere.
      queueMicrotask(() => {
        if (
          !element.isConnected &&
          trigger instanceof HTMLElement &&
          trigger.isConnected &&
          document.activeElement === document.body
        )
          trigger.focus({ preventScroll: true });
      });
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      aria-labelledby={headingId}
      className={`m-auto max-h-[90dvh] w-[calc(100%-2rem)] max-w-[460px] overflow-auto rounded-2xl border border-white/20 bg-[#1c1d22] p-4 text-[#ececf0] shadow-2xl backdrop:bg-black/80 ${cls}`}
      onCancel={event => {
        event.preventDefault();
        if (!locked) close();
      }}
    >
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 id={headingId} className="text-sm font-semibold">
          {title}
        </h2>
        <button
          type="button"
          className="rounded-lg px-3 py-2 text-[#c7c8ce] hover:bg-white/10"
          aria-label={`Close ${title}`}
          disabled={locked}
          onClick={close}
        >
          ✕
        </button>
      </div>
      {children}
    </dialog>
  );
}

import { useId, useLayoutEffect, useRef, type ReactNode } from 'react';
import { muted, outline } from './styles';

export function Panel({ children, cls = '' }: { children: ReactNode; cls?: string }) {
  return (
    <section
      className={`th-panel rounded-[20px] border border-white/[.09] bg-[#161719]/85 shadow-[0_18px_60px_rgba(0,0,0,.18)] ${cls}`}
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
    <div className="th-page-heading mb-8 flex flex-wrap items-end justify-between gap-4">
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

export function Modal({
  title,
  children,
  close,
  cls = '',
  locked = false,
  id,
}: {
  title: string;
  children: ReactNode;
  close: () => void;
  cls?: string;
  locked?: boolean;
  id?: string;
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
      id={id}
      aria-labelledby={headingId}
      className={`m-auto max-h-[90dvh] w-[calc(100%-2rem)] max-w-[460px] overflow-auto rounded-2xl border border-white/20 bg-[#1c1d22] p-4 text-[#ececf0] shadow-2xl backdrop:bg-black/80 ${cls}`}
      onClick={event => {
        if (event.target === event.currentTarget && !locked) close();
      }}
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

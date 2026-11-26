import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { ComponentPropsWithoutRef, ReactNode } from 'react';

export function LiquidCard({ className = '', ...props }: ComponentPropsWithoutRef<'button'>) {
  const light = useRef<HTMLSpanElement>(null);
  const bounds = useRef<DOMRect>(null);
  return (
    <button
      {...props}
      type={props.type ?? 'button'}
      className={`th-liquid-card ${className}`}
      onPointerEnter={event => {
        props.onPointerEnter?.(event);
        if (
          event.pointerType === 'mouse' &&
          !event.currentTarget.closest('.th-stop-motion') &&
          !window.matchMedia('(prefers-reduced-motion: reduce)').matches
        )
          bounds.current = event.currentTarget.getBoundingClientRect();
      }}
      onPointerLeave={event => {
        props.onPointerLeave?.(event);
        bounds.current = null;
      }}
      onPointerMove={event => {
        props.onPointerMove?.(event);
        const rect = bounds.current;
        if (!rect || event.currentTarget.closest('.th-stop-motion')) return;
        light.current?.style.setProperty(
          'transform',
          `translate3d(${event.clientX - rect.left}px, ${event.clientY - rect.top}px, 0)`
        );
      }}
    >
      <span ref={light} className="th-card-light" aria-hidden="true" />
      {props.children}
    </button>
  );
}

export function AnchoredSurface({
  open,
  id,
  children,
  className = '',
}: {
  open: boolean;
  id: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      id={id}
      data-open={open}
      inert={!open}
      aria-hidden={!open}
      className={`th-anchored-surface ${className}`}
    >
      <div className="th-anchored-content">{children}</div>
    </div>
  );
}

export function NotificationPanel({
  open,
  onOpenChange,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
}) {
  const id = useId();
  const anchor = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const title = useRef<HTMLHeadingElement>(null);
  useLayoutEffect(() => {
    if (open) title.current?.focus({ preventScroll: true });
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !anchor.current?.contains(event.target)) {
        if (anchor.current?.contains(document.activeElement))
          trigger.current?.focus({ preventScroll: true });
        onOpenChange(false);
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      event.preventDefault();
      trigger.current?.focus({ preventScroll: true });
      onOpenChange(false);
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [open, onOpenChange]);

  return (
    <div
      ref={anchor}
      className="relative h-10 w-10 shrink-0"
      onBlur={event => {
        if (
          event.relatedTarget instanceof Node &&
          !event.currentTarget.contains(event.relatedTarget)
        )
          onOpenChange(false);
      }}
    >
      <button
        ref={trigger}
        type="button"
        className="th-header-action relative z-30 grid size-10 place-items-center rounded-xl text-[#dddde0]"
        aria-label={open ? 'Close notifications' : 'Notifications'}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => {
          if (open) trigger.current?.focus({ preventScroll: true });
          onOpenChange(!open);
        }}
      >
        <svg
          viewBox="0 0 24 24"
          width="19"
          height="19"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
          className="th-bell"
          data-open={open}
        >
          <path d="M18 9a6 6 0 0 0-12 0c0 5-2 6-2 7h16c0-1-2-2-2-7M10 20h4" />
        </svg>
      </button>
      <AnchoredSurface id={id} open={open} className="th-notification-surface">
        <section aria-labelledby={`${id}-title`} className="px-5 pb-5 pt-12">
          <h2
            ref={title}
            id={`${id}-title`}
            tabIndex={-1}
            className="mb-3 text-base font-semibold outline-none"
          >
            Notifications
          </h2>
          {children}
        </section>
      </AnchoredSurface>
    </div>
  );
}

// The browser retargets the same surface from its current position if navigation changes mid-flight.
export function TravelingSurface({
  container,
  selection,
}: {
  container: React.RefObject<HTMLElement | null>;
  selection: string;
}) {
  const [geometry, setGeometry] = useState<{ top: number; height: number } | null>(null);
  useEffect(() => {
    const element = container.current;
    const active = element?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!element || !active) return;
    const measure = () => {
      const top = active.getBoundingClientRect().top - element.getBoundingClientRect().top;
      const height = active.offsetHeight;
      setGeometry(previous =>
        previous?.top === top && previous.height === height ? previous : { top, height }
      );
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    observer.observe(active);
    return () => observer.disconnect();
  }, [container, selection]);
  return (
    geometry && (
      <span
        aria-hidden="true"
        data-testid="navigation-highlight"
        className="th-nav-highlight absolute left-0 right-0 z-0 rounded-xl border border-white/[.1]"
        style={{ height: geometry.height, transform: `translate3d(0, ${geometry.top}px, 0)` }}
      />
    )
  );
}

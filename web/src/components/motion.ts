import { useLayoutEffect, type RefObject } from 'react';

export const motionTiming = {
  feedback: 110,
  control: 180,
  panel: 280,
  page: 320,
  reveal: 560,
  exit: 160,
} as const;
export const motionEase = 'cubic-bezier(.22,.8,.24,1)';
export type IdentityState =
  | 'idle'
  | 'thinking'
  | 'executing'
  | 'approval'
  | 'completed'
  | 'failed'
  | 'cancelled';

export function motionAllowed(element: Element | null) {
  return (
    !document.hidden &&
    !matchMedia('(prefers-reduced-motion: reduce)').matches &&
    !element?.closest('.th-stop-motion')
  );
}

// Keep scene contents mounted; animate the container, not streamed text or React state.
export function useSceneArrival(
  element: RefObject<HTMLElement | null>,
  scene: string | null,
  active = true
) {
  useLayoutEffect(() => {
    const node = element.current;
    if (!active || !node?.animate || !motionAllowed(node)) return;
    const animation = node.animate(
      [
        { opacity: 0.4, translate: '0 7px' },
        { opacity: 1, translate: '0 0' },
      ],
      { duration: motionTiming.page, easing: motionEase }
    );
    return () => animation.cancel();
  }, [element, scene, active]);
}

export function taskIdentity(state?: string): IdentityState {
  switch (state) {
    case 'CREATED':
    case 'PLANNING':
    case 'RUNNING':
    case 'VERIFYING':
      return 'executing';
    case 'WAITING_APPROVAL':
      return 'approval';
    case 'COMPLETED':
      return 'completed';
    case 'FAILED':
      return 'failed';
    case 'CANCELLED':
      return 'cancelled';
    default:
      return 'idle';
  }
}

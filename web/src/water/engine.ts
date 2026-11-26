import { qualityLimits, ripplePoint, waterBuffer, WaterMotion, type WaterQuality } from './motion';
import { createWaterRenderer } from './renderer';

export function mountWater(surface: HTMLElement, canvas: HTMLCanvasElement, input: HTMLElement) {
  if (!window.IntersectionObserver || !window.ResizeObserver || !window.matchMedia) return;
  const reduction = matchMedia('(prefers-reduced-motion: reduce)');
  const conserve = matchMedia('(update: slow), (prefers-reduced-data: reduce)');
  const pointer = matchMedia('(hover: hover) and (pointer: fine)');
  const motion = new WaterMotion();
  let renderer: ReturnType<typeof createWaterRenderer>;
  let visible = false;
  let lost = false;
  let unsupported = false;
  let disposed = false;
  let throttled = false;
  let width = 0;
  let height = 0;
  let frame: number | undefined;
  let previousTick = 0;
  let previousDraw = 0;
  let frameCount = 0;
  let slowFrames = 0;
  let nextDraw = 0;
  let disturbance = -Infinity;
  let latest: { x: number; y: number; press: boolean } | undefined;
  let rest = 0;
  let quality: WaterQuality = 'balanced';
  let bufferWidth = 0;
  let bufferHeight = 0;
  let rendered = false;

  function stop() {
    if (frame !== undefined) cancelAnimationFrame(frame);
    frame = undefined;
    previousTick = previousDraw = nextDraw = frameCount = slowFrames = 0;
    latest = undefined;
    rest = 0;
    surface.dataset.motion = 'paused';
  }

  function showPoster() {
    surface.dataset.water = 'poster';
    delete surface.dataset.quality;
    rendered = false;
  }

  function release() {
    renderer?.dispose();
    renderer = undefined;
    bufferWidth = bufferHeight = 0;
    canvas.width = canvas.height = 1;
    showPoster();
  }

  function allowed() {
    return (
      !disposed &&
      !lost &&
      !unsupported &&
      !reduction.matches &&
      visible &&
      !document.hidden &&
      width >= 2 &&
      height >= 2
    );
  }

  function qualityForHost(): WaterQuality {
    return throttled ||
      conserve.matches ||
      width < 560 ||
      (navigator.hardwareConcurrency > 0 && navigator.hardwareConcurrency <= 4)
      ? 'economy'
      : 'balanced';
  }

  function applyPointer(now: number) {
    if (!latest) return;
    const { x, y, press } = latest;
    latest = undefined;
    const box = surface.getBoundingClientRect();
    const u = (x - box.left) / box.width;
    const v = (y - box.top) / box.height;
    if (u < 0 || u > 1 || v < 0 || v > 1) return;
    rest = (u - 0.5) * 0.52;
    if (now - disturbance < (press ? 110 : 360)) return;
    motion.disturb(...ripplePoint(u, v, width / height), press ? 0.015 : 0.0085);
    disturbance = now;
  }

  function tick(now: number) {
    frame = undefined;
    if (!allowed()) return;
    const nextQuality = qualityForHost();
    if (quality !== nextQuality) {
      quality = nextQuality;
      frameCount = slowFrames = 0;
      surface.dataset.quality = quality;
    }
    if (previousTick) {
      frameCount++;
      if (now - previousTick > (quality === 'balanced' ? 28 : 43)) slowFrames++;
      if (frameCount >= 60) {
        if (slowFrames >= 20) {
          if (quality === 'economy') {
            unsupported = true;
            stop();
            release();
            return;
          }
          throttled = true;
        }
        frameCount = slowFrames = 0;
      }
    }
    previousTick = now;
    if (now + 0.6 >= nextDraw) {
      try {
        renderer ??= createWaterRenderer(canvas);
        if (!renderer) {
          unsupported = true;
          stop();
          showPoster();
          return;
        }
        const [w, h] = waterBuffer(width, height, window.devicePixelRatio || 1, quality);
        if (w !== bufferWidth || h !== bufferHeight) {
          renderer.resize(w, h);
          bufferWidth = w;
          bufferHeight = h;
        }
        applyPointer(now);
        motion.step(previousDraw ? (now - previousDraw) / 1000 : 0, rest);
        renderer.draw(motion);
        if (!rendered) {
          surface.dataset.water = 'webgl';
          surface.dataset.quality = quality;
          rendered = true;
        }
      } catch (error) {
        unsupported = true;
        console.warn('Thozha water: falling back to the still frame.', error);
        stop();
        release();
        return;
      }
      previousDraw = now;
      const interval = 1000 / qualityLimits[quality].fps;
      nextDraw = nextDraw && now - nextDraw < interval ? nextDraw + interval : now + interval;
    }
    frame = requestAnimationFrame(tick);
  }

  function sync() {
    if (!allowed()) {
      stop();
      if (reduction.matches && renderer) release();
    } else if (frame === undefined) {
      surface.dataset.motion = 'running';
      frame = requestAnimationFrame(tick);
    }
  }

  const intersections = new IntersectionObserver(
    ([entry]) => {
      visible = entry.isIntersecting && entry.intersectionRatio >= 0.01;
      sync();
    },
    { threshold: 0.01 }
  );
  const sizes = new ResizeObserver(([entry]) => {
    ({ width, height } = entry.contentRect);
    sync();
  });
  const move = (event: PointerEvent) => {
    if (
      frame === undefined ||
      quality !== 'balanced' ||
      !pointer.matches ||
      (event.pointerType !== 'mouse' && event.pointerType !== 'pen')
    )
      return;
    const press = event.type === 'pointerdown';
    if (press && event.target instanceof Element && event.target.closest('a, button, input'))
      return;
    if (latest?.press && !press) return;
    latest = { x: event.clientX, y: event.clientY, press };
  };
  const leave = () => {
    latest = undefined;
    rest = 0;
  };
  const onLost = (event: Event) => {
    event.preventDefault();
    lost = true;
    stop();
    renderer = undefined;
    bufferWidth = bufferHeight = 0;
    showPoster();
  };
  const onRestored = () => {
    lost = false;
    unsupported = false;
    sync();
  };
  sizes.observe(surface);
  intersections.observe(surface);
  reduction.addEventListener('change', sync);
  document.addEventListener('visibilitychange', sync);
  input.addEventListener('pointermove', move, { passive: true });
  input.addEventListener('pointerdown', move, { passive: true });
  input.addEventListener('pointerleave', leave, { passive: true });
  canvas.addEventListener('webglcontextlost', onLost);
  canvas.addEventListener('webglcontextrestored', onRestored);
  return () => {
    disposed = true;
    stop();
    release();
    sizes.disconnect();
    intersections.disconnect();
    reduction.removeEventListener('change', sync);
    document.removeEventListener('visibilitychange', sync);
    input.removeEventListener('pointermove', move);
    input.removeEventListener('pointerdown', move);
    input.removeEventListener('pointerleave', leave);
    canvas.removeEventListener('webglcontextlost', onLost);
    canvas.removeEventListener('webglcontextrestored', onRestored);
  };
}

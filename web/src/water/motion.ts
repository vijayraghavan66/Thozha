// Adapted from Jarvis Water Lab's simulation.mjs at 2dcf244 (SurfaceMotion).
const lifetime = 4;
const maxRipples = 4;

export type WaterQuality = 'balanced' | 'economy';

export const qualityLimits = {
  balanced: { fps: 30, pixels: 260_000, dpr: 1.25 },
  economy: { fps: 24, pixels: 96_000, dpr: 1 },
} as const;

export function waterBuffer(width: number, height: number, dpr: number, quality: WaterQuality) {
  const limits = qualityLimits[quality];
  const scale = Math.min(
    dpr,
    limits.dpr,
    Math.sqrt(limits.pixels / (width * height)),
    1536 / Math.max(width, height)
  );
  return [Math.max(1, Math.floor(width * scale)), Math.max(1, Math.floor(height * scale))] as const;
}

export function ripplePoint(x: number, y: number, aspect: number) {
  // The same oblique projection as water.frag.glsl, with y measured from the top.
  const depth = 1 / (0.55 + y * 0.45);
  return [(x - 0.5) * aspect * depth, (1 - y) * 1.25 * depth] as const;
}

export class WaterMotion {
  time = 1.65;
  lean = 0;
  private velocity = 0;
  private next = 0;
  // Four packed vec4s: projected x/z, age, strength. Zero strength is inactive.
  readonly ripples = new Float32Array(maxRipples * 4);

  disturb(x: number, z: number, strength: number) {
    this.ripples.set([x, z, 0, strength], this.next * 4);
    this.next = (this.next + 1) % maxRipples;
  }

  step(delta: number, rest = 0) {
    const dt = Math.min(Math.max(delta, 0), 0.04);
    this.time += dt;
    for (let i = 2; i < this.ripples.length; i += 4) {
      if (!this.ripples[i + 1]) continue;
      this.ripples[i] += dt;
      if (this.ripples[i] >= lifetime) this.ripples[i + 1] = 0;
    }
    const steps = Math.ceil(dt / (1 / 240));
    if (!steps) return;
    const h = dt / steps;
    for (let i = 0; i < steps; i++) {
      const acceleration = (rest - this.lean) * 12 - this.velocity * 4.7;
      this.velocity += acceleration * h;
      this.lean += this.velocity * h;
    }
  }
}

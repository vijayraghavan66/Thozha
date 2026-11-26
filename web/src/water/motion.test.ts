import { expect, it } from 'vitest';
import { WaterMotion, waterBuffer } from './motion';

it('lets the pointer spring settle after release without jumping on a long frame', () => {
  const motion = new WaterMotion();
  const clamped = new WaterMotion();
  motion.step(60, 0.26);
  clamped.step(0.04, 0.26);
  expect(motion.lean).toBe(clamped.lean);
  for (let i = 0; i < 90; i++) motion.step(1 / 30, 0.26);
  expect(motion.lean).toBeCloseTo(0.26);
  for (let i = 0; i < 120; i++) motion.step(1 / 30);
  expect(motion.lean).toBeCloseTo(0, 3);
});

it('packs only recent ripples and clears them after the propagation window', () => {
  const motion = new WaterMotion();
  for (let i = 1; i <= 8; i++) motion.disturb(i, 0, 0.01);
  const origins = Array.from(motion.ripples).filter((_, i) => i % 4 === 0);
  expect(origins).toEqual([5, 6, 7, 8]);
  motion.step(0.02);
  expect(motion.ripples[2]).toBeCloseTo(0.02);
  for (let i = 0; i < 125; i++) motion.step(1 / 30);
  expect([3, 7, 11, 15].every(slot => motion.ripples[slot] === 0)).toBe(true);
});

it('preserves aspect ratio while bounding the Retina and economy backing buffers', () => {
  const [width, height] = waterBuffer(2700, 450, 3, 'balanced');
  expect(width * height).toBeLessThanOrEqual(260_000);
  expect(width / height).toBeCloseTo(6, 1);
  const [mobileWidth, mobileHeight] = waterBuffer(390, 560, 2, 'economy');
  expect(mobileWidth * mobileHeight).toBeLessThanOrEqual(96_000);
  expect(mobileWidth / mobileHeight).toBeCloseTo(390 / 560, 2);
});

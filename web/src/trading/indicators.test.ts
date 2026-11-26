import { expect, it } from 'vitest';
import { ema, macd, rsi, signals, sma } from './indicators';
import type { Candle } from './marketData';

it('calculates SMA and EMA with warm-up values', () => {
  expect(sma([1, 2, 3, 4], 3)).toEqual([null, null, 2, 3]);
  const average = ema([1, 2, 3, 4], 3);
  expect(average.slice(0, 2)).toEqual([null, null]);
  expect(average[2]).toBe(2);
  expect(average[3]).toBe(3);
});

it('calculates RSI, MACD and exposes warm-up transparently', () => {
  expect(rsi([1, 2, 3], 2)).toEqual([null, null, 100]);
  const flat = Array.from({ length: 40 }, () => 10);
  expect(macd(flat).line.at(-1)).toBe(0);
  const candles: Candle[] = flat.map((close, index) => ({
    time: index + 1,
    open: close,
    high: close,
    low: close,
    close,
    volume: 100,
  }));
  expect(signals(candles).every(signal => signal.bias === 'Neutral')).toBe(true);
  expect(signals(candles.slice(0, 5))).toSatisfy(rows =>
    rows.some((row: ReturnType<typeof signals>[number]) => row.value.includes('Warming up'))
  );
});

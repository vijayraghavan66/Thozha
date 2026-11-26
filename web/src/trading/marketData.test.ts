import { expect, it } from 'vitest';
import { demoInstruments, demoProvider, missingMarketData } from './marketData';

it('does not fabricate a provider when the contract is missing', () => {
  const unavailable = missingMarketData(demoInstruments[0], '1d');
  expect(unavailable.available).toBe(false);
  expect(unavailable.candles).toEqual([]);
  expect(unavailable.availabilityReason).toContain('No versioned market-data endpoint');
});

it('returns a clearly labeled deterministic synthetic series only on explicit request', async () => {
  const one = await demoProvider.candles(demoInstruments[0], '15m');
  const two = await demoProvider.candles(demoInstruments[0], '15m');
  expect(one.mode).toBe('demo');
  expect(one.source).toContain('synthetic');
  expect(one.candles).toEqual(two.candles);
  expect(
    one.candles.every(candle => candle.high >= candle.open && candle.high >= candle.close)
  ).toBe(true);
});

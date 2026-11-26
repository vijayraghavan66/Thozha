import { expect, it } from 'vitest';
import type { MarketSeries } from './marketData';
import {
  cancelOrder,
  freshLedger,
  loadLedger,
  refreshLimitOrders,
  replayLedger,
  saveLedger,
  submitPaperOrder,
  startingCash,
} from './paperLedger';

const market: MarketSeries = {
  instrument: { symbol: 'NIFTYBEES', name: 'Demo', exchange: 'NSE', currency: 'INR' },
  interval: '1d',
  candles: [{ time: 100, open: 100, high: 105, low: 95, close: 100, volume: 1 }],
  source: 'Unit test demo',
  mode: 'demo',
  timezone: 'Asia/Kolkata',
  updatedAt: '2026-01-01T00:00:00Z',
  available: true,
};

it('reconciles cash, FIFO realized P&L and short-sale validation', () => {
  const bought = submitPaperOrder(
    freshLedger(),
    {
      instrument: 'NIFTYBEES',
      type: 'Market',
      side: 'Buy',
      quantity: 5,
    },
    market
  );
  const sold = submitPaperOrder(
    bought,
    {
      instrument: 'NIFTYBEES',
      type: 'Market',
      side: 'Sell',
      quantity: 5,
    },
    {
      ...market,
      candles: [{ ...market.candles[0], close: 110 }],
    }
  );
  const ledger = replayLedger(sold);
  expect(ledger.cash).toBe(startingCash + 5 * 1_000 - 1_000);
  expect(ledger.realized).toBe(5 * 1_000 - 1_000);
  expect(ledger.positions).toEqual([]);
  expect(() =>
    submitPaperOrder(
      freshLedger(),
      {
        instrument: 'NIFTYBEES',
        type: 'Market',
        side: 'Sell',
        quantity: 1,
      },
      market
    )
  ).toThrow('Not enough paper holdings');
});

it('keeps limit orders open until explicitly cancelled', () => {
  const funded = submitPaperOrder(
    freshLedger(),
    {
      instrument: 'NIFTYBEES',
      type: 'Market',
      side: 'Buy',
      quantity: 2,
    },
    market
  );
  const pending = submitPaperOrder(
    funded,
    {
      instrument: 'NIFTYBEES',
      type: 'Limit',
      side: 'Sell',
      quantity: 2,
      limit: 120,
    },
    market
  );
  const open = pending.orders.at(-1)!;
  expect(open.status).toBe('Open');
  expect(cancelOrder(pending, open.id).orders.at(-1)!.status).toBe('Cancelled');
});

it('fills matching limits from a supplied market snapshot and persists the ledger', () => {
  const values = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: key => values.get(key) ?? null,
    key: index => [...values.keys()][index] ?? null,
    removeItem: key => {
      values.delete(key);
    },
    setItem: (key, value) => {
      values.set(key, value);
    },
  };
  const funded = submitPaperOrder(
    freshLedger(),
    {
      instrument: 'NIFTYBEES',
      type: 'Market',
      side: 'Buy',
      quantity: 2,
    },
    market
  );
  const pending = submitPaperOrder(
    funded,
    {
      instrument: 'NIFTYBEES',
      type: 'Limit',
      side: 'Sell',
      quantity: 2,
      limit: 110,
    },
    market
  );
  const filled = refreshLimitOrders(pending, {
    ...market,
    candles: [{ ...market.candles[0], close: 112, time: 200 }],
  });
  expect(filled.orders.at(-1)).toMatchObject({
    status: 'Filled',
    fillPrice: 11_200,
    fillTime: 200,
  });
  saveLedger(storage, filled);
  expect(loadLedger(storage)).toEqual(filled);
});

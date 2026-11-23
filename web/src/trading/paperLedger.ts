import { z } from 'zod';
import type { MarketSeries } from './marketData';

export const startingCash = 10_000_000;
export const paperFee = 500;
export const storageKey = 'thozha-web.paper-ledger.v2';
const symbols = ['NIFTYBEES', 'GOLDBEES', 'ITBEES'] as const;
export type Instrument = (typeof symbols)[number];
export const instruments = symbols;
const order = z.object({
  id: z.uuid(),
  instrument: z.enum(symbols),
  side: z.enum(['Buy', 'Sell']),
  type: z.enum(['Market', 'Limit']),
  quantity: z.number().int().min(1).max(10_000),
  limit: z.number().positive().optional(),
  submittedAt: z.iso.datetime(),
  status: z.enum(['Open', 'Filled', 'Cancelled']),
  filledAt: z.iso.datetime().optional(),
  fillPrice: z.number().positive().optional(),
  fillTime: z.number().int().positive().optional(),
  source: z.string(),
  fees: z.number().int().nonnegative(),
});
const schema = z.object({
  version: z.literal(2),
  startingCash: z.literal(startingCash),
  orders: z.array(order).max(2_000),
});
export type PaperOrder = z.infer<typeof order>;
export type PaperLedger = z.infer<typeof schema>;
export type Ticket = Pick<PaperOrder, 'instrument' | 'side' | 'type' | 'quantity' | 'limit'>;
export type Position = {
  instrument: Instrument;
  quantity: number;
  averageCost: number;
  realized: number;
  mark: number;
  unrealized: number;
  invested: number;
  markAvailable: boolean;
};
const inr = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' });
export const formatInr = (paise: number) => inr.format(paise / 100);
export const freshLedger = (): PaperLedger => ({ version: 2, startingCash, orders: [] });

function currentPrice(series: MarketSeries, symbol: Instrument) {
  if (series.instrument.symbol !== symbol) throw new Error('Select data for the order instrument.');
  const candle = series.candles.at(-1);
  if (!series.available || !candle) throw new Error('No usable market snapshot is available.');
  if (series.mode === 'demo') return { paise: Math.round(candle.close * 100), candle };
  const age = Date.now() - candle.time * 1000;
  if (age > 15 * 60_000)
    throw new Error(
      'The latest source candle is stale. Refresh market data before placing an order.'
    );
  return { paise: Math.round(candle.close * 100), candle };
}

export function replayLedger(ledger: PaperLedger, marks: Partial<Record<Instrument, number>> = {}) {
  let cash = ledger.startingCash;
  let realized = 0;
  const lots: Record<Instrument, Array<{ quantity: number; price: number }>> = {
    NIFTYBEES: [],
    GOLDBEES: [],
    ITBEES: [],
  };
  const entries: Array<{ order: PaperOrder; equity: number; cash: number; pnl: number }> = [];
  for (const item of ledger.orders) {
    if (item.status !== 'Filled' || item.fillPrice == null) continue;
    const fillPrice = item.fillPrice;
    const gross = fillPrice * item.quantity;
    cash += (item.side === 'Sell' ? gross : -gross) - item.fees;
    if (item.side === 'Buy')
      lots[item.instrument].push({
        quantity: item.quantity,
        price: fillPrice + item.fees / item.quantity,
      });
    else {
      let remaining = item.quantity;
      for (const lot of lots[item.instrument]) {
        const used = Math.min(remaining, lot.quantity);
        realized += (fillPrice - lot.price - item.fees / item.quantity) * used;
        lot.quantity -= used;
        remaining -= used;
        if (!remaining) break;
      }
      if (remaining) throw new Error('The saved paper ledger contains an invalid short sale.');
      lots[item.instrument] = lots[item.instrument].filter(lot => lot.quantity);
    }
    if (cash < 0) throw new Error('The saved paper ledger contains an overdrawn buy.');
    entries.push({
      order: item,
      cash,
      equity:
        cash +
        instruments.reduce(
          (total, symbol) =>
            total +
            lots[symbol].reduce((quantity, lot) => quantity + lot.quantity, 0) *
              (marks[symbol] ?? fillPrice),
          0
        ),
      pnl: realized - (item.side === 'Sell' ? item.fees : 0),
    });
  }
  const positions = instruments.flatMap<Position>(instrument => {
    const quantity = lots[instrument].reduce((total, lot) => total + lot.quantity, 0);
    if (!quantity) return [];
    const invested = lots[instrument].reduce((total, lot) => total + lot.quantity * lot.price, 0);
    const averageCost = Math.round(invested / quantity);
    const mark = marks[instrument] ?? averageCost;
    return [
      {
        instrument,
        quantity,
        averageCost,
        realized,
        mark,
        unrealized: (mark - averageCost) * quantity,
        invested,
        markAvailable: marks[instrument] !== undefined,
      },
    ];
  });
  const marketValue = positions.reduce(
    (total, position) => total + position.quantity * position.mark,
    0
  );
  return {
    cash,
    positions,
    realized,
    marketValue,
    invested: positions.reduce((total, position) => total + position.invested, 0),
    unrealized: positions.reduce((total, position) => total + position.unrealized, 0),
    equity: cash + marketValue,
    entries,
    fullyMarked: positions.every(position => position.markAvailable),
  };
}

function fill(item: PaperOrder, price: number, timestamp: number, source: string): PaperOrder {
  return {
    ...item,
    status: 'Filled',
    fillPrice: price,
    fillTime: timestamp,
    filledAt: new Date(timestamp * 1000).toISOString(),
    source,
  };
}

function validateOrder(ledger: PaperLedger, ticket: Ticket, price: number) {
  if (ticket.quantity < 1 || ticket.quantity > 10_000 || !Number.isInteger(ticket.quantity))
    throw new Error('Quantity must be a whole number between 1 and 10,000.');
  if (ticket.type === 'Limit' && (!ticket.limit || ticket.limit <= 0))
    throw new Error('Enter a positive limit price.');
  const candidate = replayLedger(ledger);
  if (ticket.side === 'Buy' && ticket.quantity * price + paperFee > candidate.cash)
    throw new Error('Insufficient virtual cash, including the ₹5 paper fee.');
  if (
    ticket.side === 'Sell' &&
    (candidate.positions.find(position => position.instrument === ticket.instrument)?.quantity ??
      0) < ticket.quantity
  )
    throw new Error('Not enough paper holdings to sell. Short selling is not supported.');
}

export function submitPaperOrder(ledger: PaperLedger, ticket: Ticket, series: MarketSeries) {
  if (ledger.orders.length >= 2_000)
    throw new Error('The local paper ledger is full. Reset the sandbox to place more orders.');
  const { paise, candle } = currentPrice(series, ticket.instrument);
  const executionPrice = ticket.type === 'Market' ? paise : Math.round((ticket.limit ?? 0) * 100);
  validateOrder(ledger, ticket, executionPrice);
  const item: PaperOrder = {
    ...ticket,
    id: crypto.randomUUID(),
    submittedAt: new Date().toISOString(),
    status: 'Open',
    limit: ticket.type === 'Limit' ? ticket.limit : undefined,
    source: series.source,
    fees: paperFee,
  };
  const shouldFill =
    item.type === 'Market' ||
    (item.side === 'Buy' && paise <= executionPrice) ||
    (item.side === 'Sell' && paise >= executionPrice);
  const next = shouldFill ? fill(item, paise, candle.time, series.source) : item;
  const result = { ...ledger, orders: [...ledger.orders, next] };
  replayLedger(result);
  return result;
}

export function refreshLimitOrders(ledger: PaperLedger, series: MarketSeries): PaperLedger {
  const { paise, candle } = currentPrice(series, series.instrument.symbol as Instrument);
  let changed = false;
  const orders = ledger.orders.map(item => {
    if (
      item.status !== 'Open' ||
      item.type !== 'Limit' ||
      item.instrument !== series.instrument.symbol
    )
      return item;
    if (
      (item.side === 'Buy' && paise <= Math.round(item.limit! * 100)) ||
      (item.side === 'Sell' && paise >= Math.round(item.limit! * 100))
    ) {
      changed = true;
      return fill(item, paise, candle.time, series.source);
    }
    return item;
  });
  return changed ? { ...ledger, orders } : ledger;
}

export function cancelOrder(ledger: PaperLedger, id: string) {
  const item = ledger.orders.find(order => order.id === id);
  if (!item || item.status !== 'Open')
    throw new Error('Only an open paper order can be cancelled.');
  return {
    ...ledger,
    orders: ledger.orders.map(order =>
      order.id === id ? { ...order, status: 'Cancelled' as const } : order
    ),
  };
}

export function loadLedger(storage: Storage): PaperLedger {
  const serialized = storage.getItem(storageKey);
  const ledger = serialized === null ? freshLedger() : schema.parse(JSON.parse(serialized));
  replayLedger(ledger);
  return ledger;
}

export function saveLedger(storage: Storage, ledger: PaperLedger) {
  storage.setItem(storageKey, JSON.stringify(schema.parse(ledger)));
}

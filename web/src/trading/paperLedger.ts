import { z } from 'zod';

export const paperQuotes = { NIFTYBEES: 32_000, GOLDBEES: 10_700, ITBEES: 4_700 } as const;
export const instruments = ['NIFTYBEES', 'GOLDBEES', 'ITBEES'] as const;
export type Instrument = (typeof instruments)[number];
export const paperFee = 500;
export const startingCash = 10_000_000;
export const maxFills = 500;
export const storageKey = 'thozha-web.paper-fills.v1';
const ticket = z.object({
  instrument: z.enum(instruments),
  side: z.enum(['Buy', 'Sell']),
  quantity: z.number().int().min(1).max(500),
});
const fillSchema = ticket.extend({ id: z.uuid(), time: z.iso.datetime() });
const historySchema = z.array(fillSchema).max(maxFills);
export type Ticket = z.infer<typeof ticket>;
export type Fill = z.infer<typeof fillSchema>;
const inr = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' });
export const formatInr = (paise: number) => inr.format(paise / 100);

export function replayFills(fills: Fill[]) {
  let cash = startingCash;
  const units: Record<Instrument, number> = { NIFTYBEES: 0, ITBEES: 0, GOLDBEES: 0 };
  const entries: { fill: Fill; equity: number; delta: number; cash: number }[] = [];
  for (const fill of fills) {
    const gross = paperQuotes[fill.instrument] * fill.quantity;
    const delta = (fill.side === 'Buy' ? -gross : gross) - paperFee;
    if (fill.side === 'Buy' && cash + delta < 0) throw new Error('Insufficient virtual cash.');
    if (fill.side === 'Sell' && units[fill.instrument] < fill.quantity)
      throw new Error('Not enough paper holdings to sell.');
    cash += delta;
    units[fill.instrument] += fill.side === 'Buy' ? fill.quantity : -fill.quantity;
    const equity =
      cash + instruments.reduce((total, symbol) => total + units[symbol] * paperQuotes[symbol], 0);
    entries.push({ fill, equity, delta, cash });
  }
  return { cash, units, entries, equity: entries.at(-1)?.equity ?? startingCash };
}

export function makeFill(input: Ticket, fills: Fill[]): Fill {
  if (fills.length >= maxFills)
    throw new Error('The local ledger is full. Reset the sandbox to place more orders.');
  const parsed = ticket.safeParse(input);
  if (!parsed.success)
    throw new Error('Select an instrument and enter a whole number between 1 and 500.');
  const fill = { ...parsed.data, id: crypto.randomUUID(), time: new Date().toISOString() };
  replayFills([...fills, fill]);
  return fill;
}

export function loadFills(storage: Storage) {
  const serialized = storage.getItem(storageKey);
  const fills = serialized === null ? [] : historySchema.parse(JSON.parse(serialized));
  replayFills(fills);
  return fills;
}

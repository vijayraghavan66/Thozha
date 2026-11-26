export type MarketInterval = '1d' | '1h' | '15m';
export type Candle = {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};
export type MarketInstrument = {
  symbol: string;
  name: string;
  exchange: 'NSE' | 'BSE';
  currency: 'INR';
};
export type MarketSeries = {
  instrument: MarketInstrument;
  interval: MarketInterval;
  candles: Candle[];
  source: string;
  mode: 'demo' | 'historical' | 'delayed' | 'live';
  timezone: string;
  updatedAt: string;
  available: boolean;
  availabilityReason?: string;
};

/**
 * Backend proposal for Codex: GET /v1/web/market-data/instruments and
 * GET /v1/web/market-data/candles?symbol=&interval=&exchange=. No frontend
 * request is made until those routes are present in a versioned contract.
 */
export interface MarketDataProvider {
  instruments(): readonly MarketInstrument[];
  candles(
    instrument: MarketInstrument,
    interval: MarketInterval,
    signal?: AbortSignal
  ): Promise<MarketSeries>;
}

export const demoInstruments: MarketInstrument[] = [
  { symbol: 'NIFTYBEES', name: 'Nippon India ETF Nifty 50 BeES', exchange: 'NSE', currency: 'INR' },
  { symbol: 'GOLDBEES', name: 'Nippon India ETF Gold BeES', exchange: 'NSE', currency: 'INR' },
  { symbol: 'ITBEES', name: 'Nippon India ETF Nifty IT BeES', exchange: 'NSE', currency: 'INR' },
];

function hash(input: string) {
  let value = 2166136261;
  for (const character of input) value = Math.imul(value ^ character.charCodeAt(0), 16777619);
  return value >>> 0;
}

function syntheticCandles(symbol: string, interval: MarketInterval): Candle[] {
  const seed = hash(`${symbol}:${interval}`);
  const count = 120;
  const intervalSeconds = interval === '1d' ? 86_400 : interval === '1h' ? 3_600 : 900;
  const end = Math.floor(Date.now() / (intervalSeconds * 1000)) * intervalSeconds;
  let random = seed || 1;
  let close = 400 + (seed % 600);
  return Array.from({ length: count }, (_, index) => {
    random = (Math.imul(random, 1664525) + 1013904223) >>> 0;
    const drift = (random / 0xffffffff - 0.495) * 8;
    const open = close;
    close = Math.max(1, open + drift);
    const spread = 1 + ((random >>> 8) % 700) / 100;
    const volume = 20_000 + ((random >>> 12) % 180_000);
    return {
      time: end - (count - 1 - index) * intervalSeconds,
      open: Number(open.toFixed(2)),
      high: Number((Math.max(open, close) + spread).toFixed(2)),
      low: Number(Math.max(0.01, Math.min(open, close) - spread).toFixed(2)),
      close: Number(close.toFixed(2)),
      volume,
    };
  });
}

export class ExplicitDemoProvider implements MarketDataProvider {
  instruments() {
    return demoInstruments;
  }

  async candles(
    instrument: MarketInstrument,
    interval: MarketInterval,
    signal?: AbortSignal
  ): Promise<MarketSeries> {
    if (signal?.aborted) throw signal.reason;
    return {
      instrument,
      interval,
      candles: syntheticCandles(instrument.symbol, interval),
      source: 'Browser synthetic generator',
      mode: 'demo',
      timezone: 'Asia/Kolkata',
      updatedAt: new Date().toISOString(),
      available: true,
    };
  }
}

export const demoProvider = new ExplicitDemoProvider();

export function missingMarketData(
  instrument: MarketInstrument,
  interval: MarketInterval
): MarketSeries {
  return {
    instrument,
    interval,
    candles: [],
    source: 'Web API v1',
    mode: 'demo',
    timezone: 'Asia/Kolkata',
    updatedAt: new Date(0).toISOString(),
    available: false,
    availabilityReason:
      'No versioned market-data endpoint is present in this contract. Select synthetic demo data to inspect the terminal.',
  };
}

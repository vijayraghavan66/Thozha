import DatabaseConstructor from 'better-sqlite3';
import path from 'path';
import type { InstrumentId, Quote } from '../types';

// ============================================================================
// Market Data Provider
// Integrates with MoneyPilot database for real Indian equity/ETF prices
// ============================================================================

export interface MarketDataConfig {
  moneyPilotDbPath: string;
  stalenessThresholdMs: number; // Max age for 'live' data
  delayedThresholdMs: number; // Max age for 'delayed' data
}

export interface MarketDataResult {
  quote: Quote;
  source: string;
  fetchedAt: Date;
  providerTime: Date;
  freshness: 'live' | 'delayed' | 'historical' | 'fixture';
  marketHours: 'open' | 'closed' | 'pre-market' | 'post-market';
}

export interface Bar {
  symbol: string;
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  source: string;
  status: 'provisional' | 'final';
}

// NSE market hours (IST)
const NSE_MARKET_OPEN = 9 * 60 + 15; // 9:15 AM
const NSE_MARKET_CLOSE = 15 * 60 + 30; // 3:30 PM

export class MarketDataProvider {
  private db: DatabaseConstructor.Database;
  private config: MarketDataConfig;

  constructor(config: MarketDataConfig) {
    this.config = config;
    this.db = new DatabaseConstructor(config.moneyPilotDbPath, { readonly: true });
  }

  /**
   * Get current quote for an instrument from MoneyPilot database
   */
  getQuote(instrument: InstrumentId): MarketDataResult | null {
    const nseSymbol = this.toNseSymbol(instrument);

    // Get latest quote
    const quote = this.db
      .prepare(
        `
      SELECT symbol, price, day_high, day_low, volume, prev_close, provider_time, fetched_at, source
      FROM quotes
      WHERE symbol = ?
      ORDER BY fetched_at DESC
      LIMIT 1
    `
      )
      .get(nseSymbol) as any;

    if (!quote) {
      return null;
    }

    const fetchedAt = new Date(quote.fetched_at);
    const providerTime = new Date(quote.provider_time);
    const now = new Date();
    const ageMs = now.getTime() - fetchedAt.getTime();

    // Determine freshness
    let freshness: 'live' | 'delayed' | 'historical' | 'fixture';
    if (ageMs <= this.config.stalenessThresholdMs) {
      freshness = 'live';
    } else if (ageMs <= this.config.delayedThresholdMs) {
      freshness = 'delayed';
    } else {
      freshness = 'historical';
    }

    // Determine market hours
    const marketHours = this.getMarketHours(now);

    // Build quote object - ensure timestamp is a Date
    const result: Quote = {
      instrument,
      timestamp: providerTime,
      bid: quote.price * 0.9995, // Approximate bid/ask spread
      ask: quote.price * 1.0005,
      last: quote.price,
      volume: quote.volume || 0,
      open: quote.prev_close || quote.price,
      high: quote.day_high || quote.price,
      low: quote.day_low || quote.price,
      close: quote.price,
      freshness,
      source: quote.source,
    };

    return {
      quote: result,
      source: quote.source,
      fetchedAt,
      providerTime,
      freshness,
      marketHours,
    };
  }

  /**
   * Get historical bars for backtesting
   */
  getBars(instrument: InstrumentId, startDate: Date, endDate: Date): Bar[] {
    const nseSymbol = this.toNseSymbol(instrument);

    const rows = this.db
      .prepare(
        `
      SELECT symbol, date, open, high, low, close, volume, source, status
      FROM bars
      WHERE symbol = ? AND date BETWEEN ? AND ?
      ORDER BY date ASC
    `
      )
      .all(nseSymbol, startDate.toISOString().split('T')[0], endDate.toISOString().split('T')[0]) as any[];

    return rows.map((row) => ({
      symbol: row.symbol,
      date: row.date,
      open: row.open,
      high: row.high,
      low: row.low,
      close: row.close,
      volume: row.volume,
      source: row.source,
      status: row.status,
    }));
  }

  /**
   * Get latest available bar date
   */
  getLatestBarDate(instrument: InstrumentId): string | null {
    const nseSymbol = this.toNseSymbol(instrument);
    const row = this.db
      .prepare('SELECT MAX(date) as max_date FROM bars WHERE symbol = ?')
      .get(nseSymbol) as any;
    return row?.max_date || null;
  }

  /**
   * Check if market is currently open
   */
  isMarketOpen(date: Date = new Date()): boolean {
    return this.getMarketHours(date) === 'open';
  }

  /**
   * Get current market hours status
   */
  getMarketHours(date: Date = new Date()): 'open' | 'closed' | 'pre-market' | 'post-market' {
    const istDate = new Date(date.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const minutes = istDate.getHours() * 60 + istDate.getMinutes();
    const day = istDate.getDay();

    // Weekend check
    if (day === 0 || day === 6) {
      return 'closed';
    }

    if (minutes >= NSE_MARKET_OPEN && minutes <= NSE_MARKET_CLOSE) {
      return 'open';
    } else if (minutes < NSE_MARKET_OPEN) {
      return 'pre-market';
    } else {
      return 'post-market';
    }
  }

  /**
   * Convert InstrumentId to MoneyPilot symbol format
   */
  private toNseSymbol(instrument: InstrumentId): string {
    if (instrument.symbol.startsWith('^')) {
      return instrument.symbol; // Index symbols like ^NSEI
    }
    if (instrument.symbol.endsWith('.NS')) {
      return instrument.symbol; // Already in Yahoo format
    }
    return `${instrument.symbol}.NS`;
  }

  /**
   * Check data staleness and return appropriate freshness
   */
  checkStaleness(quote: MarketDataResult): 'fresh' | 'stale' | 'very-stale' {
    const ageMs = Date.now() - quote.fetchedAt.getTime();
    if (ageMs <= this.config.stalenessThresholdMs) return 'fresh';
    if (ageMs <= this.config.delayedThresholdMs) return 'stale';
    return 'very-stale';
  }

  /**
   * Close the database connection
   */
  close(): void {
    this.db.close();
  }
}

// ============================================================================
// Fixture Market Data Provider (for testing)
// ============================================================================

export class FixtureMarketDataProvider {
  private fixtures = new Map<string, Quote[]>();

  addFixture(instrument: InstrumentId, quote: Quote): void {
    const key = this.key(instrument);
    if (!this.fixtures.has(key)) {
      this.fixtures.set(key, []);
    }
    this.fixtures.get(key)!.push(quote);
  }

  getQuote(instrument: InstrumentId): MarketDataResult | null {
    const key = this.key(instrument);
    const quotes = this.fixtures.get(key);
    if (!quotes || quotes.length === 0) return null;

    const quote = quotes[quotes.length - 1];
    return {
      quote,
      source: 'fixture',
      fetchedAt: new Date(),
      providerTime: quote.timestamp instanceof Date ? quote.timestamp : new Date(quote.timestamp),
      freshness: 'fixture',
      marketHours: 'open',
    };
  }

  private key(instrument: InstrumentId): string {
    return `${instrument.exchange}:${instrument.symbol}:${instrument.instrumentType}`;
  }
}

export default MarketDataProvider;

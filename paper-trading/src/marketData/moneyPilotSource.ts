import DatabaseConstructor from 'better-sqlite3';

// ============================================================================
// MoneyPilot Read-Only Data Source
// Connects to the existing MoneyPilot SQLite database (read-only mode)
// Provides verified quotes, bars, and market state information
// ============================================================================

export interface MoneyPilotQuoteRow {
  symbol: string;
  price: number;
  day_high: number;
  day_low: number;
  volume: number;
  prev_close: number;
  provider_time: string;
  fetched_at: string;
  source: string;
}

export interface MoneyPilotBarRow {
  symbol: string;
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  source: string;
  status: 'provisional' | 'final';
  provider_time: string | null;
  observed_at: string;
  verification: string;
}

export interface MoneyPilotInstrumentRow {
  symbol: string;
  nse_symbol: string;
  name: string;
  kind: 'EQUITY' | 'INDEX';
  active: number;
  exchange: string;
  isin: string | null;
  price_band: string | null;
}

export interface MoneyPilotHolidayRow {
  date: string;
  description: string;
  source: string;
}

export class MoneyPilotSource {
  private db: DatabaseConstructor.Database | null = null;
  private dbPath: string;

  constructor(dbPath: string) {
    this.dbPath = dbPath;
  }

  /**
   * Open the database in read-only mode
   */
  connect(): void {
    if (this.db) return;
    // Skip connection for in-memory databases (testing)
    if (this.dbPath === ':memory:') {
      return;
    }
    this.db = new DatabaseConstructor(this.dbPath, {
      readonly: true,
      fileMustExist: true,
    });
  }

  /**
   * Close the database connection
   */
  disconnect(): void {
    if (this.db) {
      this.db.close();
      this.db = null;
    }
  }

  /**
   * Get all active instruments
   */
  getInstruments(): MoneyPilotInstrumentRow[] {
    this.ensureConnected();
    const stmt = this.db!.prepare(`
      SELECT symbol, nse_symbol, name, kind, active, exchange, isin, price_band
      FROM instruments
      WHERE active = 1
      ORDER BY symbol
    `);
    return stmt.all() as MoneyPilotInstrumentRow[];
  }

  /**
   * Get latest quote for a symbol
   */
  getLatestQuote(symbol: string): MoneyPilotQuoteRow | null {
    this.ensureConnected();
    const stmt = this.db!.prepare(`
      SELECT symbol, price, day_high, day_low, volume, prev_close, provider_time, fetched_at, source
      FROM quotes
      WHERE symbol = ?
      ORDER BY fetched_at DESC
      LIMIT 1
    `);
    const row = stmt.get(symbol) as MoneyPilotQuoteRow | undefined;
    return row ?? null;
  }

  /**
   * Get bars for a symbol within a date range
   */
  getBars(symbol: string, startDate: string, endDate: string): MoneyPilotBarRow[] {
    this.ensureConnected();
    const stmt = this.db!.prepare(`
      SELECT symbol, date, open, high, low, close, volume, source, status, provider_time, observed_at, verification
      FROM bars
      WHERE symbol = ? AND date >= ? AND date <= ?
      ORDER BY date ASC
    `);
    return stmt.all(symbol, startDate, endDate) as MoneyPilotBarRow[];
  }

  /**
   * Get latest bar date for a symbol
   */
  getLatestBarDate(symbol: string): string | null {
    this.ensureConnected();
    const stmt = this.db!.prepare(`
      SELECT date FROM bars WHERE symbol = ? ORDER BY date DESC LIMIT 1
    `);
    const row = stmt.get(symbol) as { date: string } | undefined;
    return row?.date ?? null;
  }

  /**
   * Get all holidays
   */
  getHolidays(): MoneyPilotHolidayRow[] {
    this.ensureConnected();
    const stmt = this.db!.prepare(`
      SELECT date, description, source FROM holidays ORDER BY date ASC
    `);
    return stmt.all() as MoneyPilotHolidayRow[];
  }

  /**
   * Check if a specific date is a holiday
   */
  isHoliday(date: string): boolean {
    this.ensureConnected();
    const stmt = this.db!.prepare(`SELECT 1 FROM holidays WHERE date = ?`);
    const row = stmt.get(date) as { 1: number } | undefined;
    return row !== undefined;
  }

  /**
   * Get database metadata
   */
  getMeta(key: string): string | null {
    this.ensureConnected();
    const stmt = this.db!.prepare(`SELECT value FROM meta WHERE key = ?`);
    const row = stmt.get(key) as { value: string } | undefined;
    return row?.value ?? null;
  }

  /**
   * Check if the database is reachable
   */
  isAvailable(): boolean {
    try {
      this.connect();
      // Test query
      const result = this.db!.prepare('SELECT 1 as test').get() as { test: number } | undefined;
      return result?.test === 1;
    } catch {
      return false;
    }
  }

  private ensureConnected(): void {
    if (this.dbPath === ':memory:') {
      throw new Error('In-memory database not available for MoneyPilotSource');
    }
    if (!this.db) {
      throw new Error('MoneyPilotSource not connected. Call connect() first.');
    }
  }
}

/**
 * Create a MoneyPilot source instance with default path
 */
export function createMoneyPilotSource(dbPath?: string): MoneyPilotSource {
  const defaultPath =
    '/Users/vijay-25589/Documents/InvestmentProject/MoneyPilot/data/money_pilot.sqlite3';
  const source = new MoneyPilotSource(dbPath ?? defaultPath);
  source.connect();
  return source;
}

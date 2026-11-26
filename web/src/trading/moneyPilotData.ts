import type { MarketInstrument, MarketSeries, MarketInterval, Candle } from './marketData';

// ============================================================================
// MoneyPilot Market Data Provider for Web
// Connects to MoneyPilot SQLite database via backend API
// ============================================================================

export const moneyPilotInstruments: MarketInstrument[] = [
  { symbol: 'RELIANCE', name: 'Reliance Industries Ltd', exchange: 'NSE', currency: 'INR' },
  { symbol: 'TCS', name: 'Tata Consultancy Services Ltd', exchange: 'NSE', currency: 'INR' },
  { symbol: 'INFY', name: 'Infosys Ltd', exchange: 'NSE', currency: 'INR' },
  { symbol: 'HDFCBANK', name: 'HDFC Bank Ltd', exchange: 'NSE', currency: 'INR' },
  { symbol: 'ICICIBANK', name: 'ICICI Bank Ltd', exchange: 'NSE', currency: 'INR' },
  { symbol: 'ITC', name: 'ITC Ltd', exchange: 'NSE', currency: 'INR' },
  { symbol: 'SBIN', name: 'State Bank of India', exchange: 'NSE', currency: 'INR' },
  { symbol: 'LT', name: 'Larsen & Toubro Ltd', exchange: 'NSE', currency: 'INR' },
];

export interface MoneyPilotQuote {
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

export interface MoneyPilotBar {
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

/**
 * MoneyPilot data provider that fetches from the local SQLite database
 * via a lightweight backend service or direct file access
 */
export class MoneyPilotProvider {
  private baseUrl: string;

  constructor(baseUrl: string = '/api/market-data') {
    this.baseUrl = baseUrl;
  }

  async getQuote(symbol: string): Promise<MoneyPilotQuote | null> {
    try {
      const response = await fetch(`${this.baseUrl}/quote?symbol=${symbol}.NS`);
      if (!response.ok) return null;
      return await response.json();
    } catch {
      return null;
    }
  }

  async getBars(symbol: string, startDate: string, endDate: string): Promise<MoneyPilotBar[]> {
    try {
      const response = await fetch(
        `${this.baseUrl}/bars?symbol=${symbol}.NS&start=${startDate}&end=${endDate}`
      );
      if (!response.ok) return [];
      return await response.json();
    } catch {
      return [];
    }
  }

  async getLatestBarDate(symbol: string): Promise<string | null> {
    try {
      const response = await fetch(`${this.baseUrl}/latest-bar?symbol=${symbol}.NS`);
      if (!response.ok) return null;
      const data = await response.json();
      return data.date;
    } catch {
      return null;
    }
  }

  /**
   * Check if market is currently open (NSE hours)
   */
  isMarketOpen(): boolean {
    const now = new Date();
    const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const minutes = ist.getHours() * 60 + ist.getMinutes();
    const day = ist.getDay();

    // Weekend check
    if (day === 0 || day === 6) return false;

    // NSE: 9:15 AM to 3:30 PM IST
    return minutes >= 555 && minutes <= 930; // 9:15 = 555, 15:30 = 930
  }

  /**
   * Get market status
   */
  getMarketStatus(): 'open' | 'closed' | 'pre-market' | 'post-market' {
    const now = new Date();
    const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const minutes = ist.getHours() * 60 + ist.getMinutes();
    const day = ist.getDay();

    if (day === 0 || day === 6) return 'closed';
    if (minutes >= 555 && minutes <= 930) return 'open';
    if (minutes < 555) return 'pre-market';
    return 'post-market';
  }

  /**
   * Convert MoneyPilot bars to candle format for charts
   */
  barsToCandles(bars: MoneyPilotBar[]): Candle[] {
    return bars.map(bar => ({
      time: new Date(bar.date).getTime() / 1000,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
      volume: bar.volume,
    }));
  }

  /**
   * Get historical data as MarketSeries
   */
  async getHistoricalSeries(
    instrument: MarketInstrument,
    interval: MarketInterval,
    days: number = 30
  ): Promise<MarketSeries> {
    const endDate = new Date();
    const startDate = new Date();
    startDate.setDate(startDate.getDate() - days);

    const bars = await this.getBars(
      instrument.symbol,
      startDate.toISOString().split('T')[0],
      endDate.toISOString().split('T')[0]
    );

    if (bars.length === 0) {
      return {
        instrument,
        interval,
        candles: [],
        source: 'MoneyPilot',
        mode: 'historical',
        timezone: 'Asia/Kolkata',
        updatedAt: new Date().toISOString(),
        available: false,
        availabilityReason: 'No historical data available for this instrument',
      };
    }

    return {
      instrument,
      interval,
      candles: this.barsToCandles(bars),
      source: `MoneyPilot (${bars[0].source})`,
      mode: 'historical',
      timezone: 'Asia/Kolkata',
      updatedAt: new Date().toISOString(),
      available: true,
    };
  }

  /**
   * Get live quote as MarketSeries (single candle)
   */
  async getLiveSeries(instrument: MarketInstrument): Promise<MarketSeries> {
    const quote = await this.getQuote(instrument.symbol);

    if (!quote) {
      return {
        instrument,
        interval: '1d',
        candles: [],
        source: 'MoneyPilot',
        mode: 'live',
        timezone: 'Asia/Kolkata',
        updatedAt: new Date().toISOString(),
        available: false,
        availabilityReason: 'No live quote available',
      };
    }

    const fetchedAt = new Date(quote.fetched_at);
    const ageMs = Date.now() - fetchedAt.getTime();
    const isLive = ageMs < 60000; // 1 minute
    const isDelayed = ageMs < 900000; // 15 minutes

    const candle: Candle = {
      time: new Date(quote.provider_time).getTime() / 1000,
      open: quote.prev_close,
      high: quote.day_high,
      low: quote.day_low,
      close: quote.price,
      volume: quote.volume,
    };

    return {
      instrument,
      interval: '1d',
      candles: [candle],
      source: `MoneyPilot (${quote.source})`,
      mode: isLive ? 'live' : isDelayed ? 'delayed' : 'historical',
      timezone: 'Asia/Kolkata',
      updatedAt: quote.fetched_at,
      available: true,
    };
  }
}

export const moneyPilotProvider = new MoneyPilotProvider();

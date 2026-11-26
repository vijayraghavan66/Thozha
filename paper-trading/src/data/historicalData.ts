import { readFileSync, writeFileSync, existsSync } from 'fs';
import { parse } from 'csv-parse/sync';

// ============================================================================
// Historical Data Loader
// Loads CSV candlestick data for backtesting and replay
// ============================================================================

export interface Candle {
  timestamp: Date;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface HistoricalDataProvider {
  symbol: string;
  exchange: string;
  loadFromFile(filePath: string): Candle[];
  validate(candles: Candle[]): boolean;
}

export class NSEHistoricalProvider implements HistoricalDataProvider {
  constructor(public symbol: string, public exchange: 'NSE' | 'BSE' = 'NSE') {}

  loadFromFile(filePath: string): Candle[] {
    if (!existsSync(filePath)) {
      throw new Error(`Historical data file not found: ${filePath}`);
    }

    const csvContent = readFileSync(filePath, 'utf-8');
    const records = parse(csvContent, {
      columns: true,
      skip_empty_lines: true,
      trim: true,
    });

    const candles: Candle[] = [];

    for (const record of records) {
      // Expected columns: Date, Open, High, Low, Close, Volume
      const date = new Date(record.Date);
      const open = parseFloat(record.Open);
      const high = parseFloat(record.High);
      const low = parseFloat(record.Low);
      const close = parseFloat(record.Close);
      const volume = parseInt(record.Volume, 10);

      if (isNaN(open) || isNaN(high) || isNaN(low) || isNaN(close) || isNaN(volume)) {
        console.warn(`Invalid candle at ${date.toISOString()}, skipping`);
        continue;
      }

      candles.push({
        timestamp: date,
        open,
        high,
        low,
        close,
        volume,
      });
    }

    // Sort by timestamp ascending
    candles.sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());

    return candles;
  }

  validate(candles: Candle[]): boolean {
    if (candles.length === 0) {
      return false;
    }

    // Check for monotonic timestamps
    for (let i = 1; i < candles.length; i++) {
      if (candles[i].timestamp <= candles[i - 1].timestamp) {
        console.warn('Non-monotonic timestamps detected');
        return false;
      }
    }

    // Check OHLC logic
    for (const candle of candles) {
      if (candle.low > candle.high) {
        console.warn(`Invalid OHLC at ${candle.timestamp.toISOString()}`);
        return false;
      }
      if (candle.open < candle.low || candle.open > candle.high) {
        console.warn(`Open outside range at ${candle.timestamp.toISOString()}`);
        return false;
      }
      if (candle.close < candle.low || candle.close > candle.high) {
        console.warn(`Close outside range at ${candle.timestamp.toISOString()}`);
        return false;
      }
    }

    return true;
  }
}

// ============================================================================
// Backtest Engine
// Runs deterministic historical replay
// ============================================================================

export interface BacktestConfig {
  initialCash: number;
  commissionRate: number;
  slippageBps: number;
  minTick: number;
}

export interface BacktestTrade {
  timestamp: Date;
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  commission: number;
  taxes: number;
  pnl?: number; // For sell trades
}

export interface BacktestResult {
  totalReturn: number;
  maxDrawdown: number;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  equityCurve: Array<{ timestamp: Date; value: number }>;
  trades: BacktestTrade[];
  dailyReturns: Array<{ date: Date; return: number }>;
}

export class BacktestEngine {
  constructor(private config: BacktestConfig) {}

  /**
   * Run backtest with a simple moving average crossover strategy
   */
  run(
    candles: Candle[],
    shortPeriod: number,
    longPeriod: number
  ): BacktestResult {
    let cash = this.config.initialCash;
    let shares = 0;
    let avgCost = 0;

    const equityCurve: BacktestResult['equityCurve'] = [];
    const trades: BacktestTrade[] = [];
    const dailyReturns: BacktestResult['dailyReturns'] = [];

    let maxEquity = this.config.initialCash;
    let maxDrawdown = 0;

    // Calculate moving averages
    const shortMA: (number | null)[] = [];
    const longMA: (number | null)[] = [];

    for (let i = 0; i < candles.length; i++) {
      const close = candles[i].close;

      // Short MA
      if (i >= shortPeriod - 1) {
        let sum = 0;
        for (let j = 0; j < shortPeriod; j++) {
          sum += candles[i - j].close;
        }
        shortMA[i] = sum / shortPeriod;
      } else {
        shortMA[i] = null;
      }

      // Long MA
      if (i >= longPeriod - 1) {
        let sum = 0;
        for (let j = 0; j < longPeriod; j++) {
          sum += candles[i - j].close;
        }
        longMA[i] = sum / longPeriod;
      } else {
        longMA[i] = null;
      }
    }

    // Generate signals and execute trades
    for (let i = longPeriod; i < candles.length; i++) {
      const candle = candles[i];
      const short = shortMA[i];
      const long = longMA[i];

      if (short === null || long === null) continue;

      const prevShort = shortMA[i - 1];
      const prevLong = longMA[i - 1];

      if (prevShort === null || prevLong === null) continue;

      // Crossover: short crosses above long -> BUY
      if (prevShort <= prevLong && short > long && shares === 0) {
        // Execute buy
        const slippage = candle.close * (this.config.slippageBps / 10000);
        const buyPrice = candle.close + slippage;
        const maxShares = Math.floor((cash * 0.95) / buyPrice); // 95% of cash

        if (maxShares > 0) {
          const commission = buyPrice * maxShares * this.config.commissionRate;
          const taxes = buyPrice * maxShares * 0.0001; // STT
          const totalCost = buyPrice * maxShares + commission + taxes;

          cash -= totalCost;
          shares = maxShares;
          avgCost = buyPrice;

          trades.push({
            timestamp: candle.timestamp,
            symbol: 'TEST',
            side: 'BUY',
            quantity: maxShares,
            price: buyPrice,
            commission,
            taxes,
          });
        }
      }

      // Crossover: short crosses below long -> SELL
      else if (prevShort >= prevLong && short < long && shares > 0) {
        // Execute sell
        const slippage = candle.close * (this.config.slippageBps / 10000);
        const sellPrice = candle.close - slippage;
        const grossValue = sellPrice * shares;
        const commission = grossValue * this.config.commissionRate;
        const taxes = grossValue * 0.0001; // STT
        const netValue = grossValue - commission - taxes;

        const pnl = (sellPrice - avgCost) * shares;

        cash += netValue;
        shares = 0;
        avgCost = 0;

        trades.push({
          timestamp: candle.timestamp,
          symbol: 'TEST',
          side: 'SELL',
          quantity: shares,
          price: sellPrice,
          commission,
          taxes,
          pnl,
        });
      }

      // Calculate equity
      const equity = cash + shares * candle.close;
      equityCurve.push({ timestamp: candle.timestamp, value: equity });

      // Track max drawdown
      if (equity > maxEquity) {
        maxEquity = equity;
      }
      const drawdown = (maxEquity - equity) / maxEquity;
      if (drawdown > maxDrawdown) {
        maxDrawdown = drawdown;
      }

      // Calculate daily return
      if (equityCurve.length > 1) {
        const prevEquity = equityCurve[equityCurve.length - 2].value;
        const dailyReturn = (equity - prevEquity) / prevEquity;
        dailyReturns.push({ date: candle.timestamp, return: dailyReturn });
      }
    }

    // Calculate metrics
    const totalReturn = ((equityCurve[equityCurve.length - 1]?.value ?? this.config.initialCash) - this.config.initialCash) / this.config.initialCash;
    const winningTrades = trades.filter((t) => t.side === 'SELL' && t.pnl && t.pnl > 0).length;
    const losingTrades = trades.filter((t) => t.side === 'SELL' && t.pnl && t.pnl <= 0).length;

    return {
      totalReturn,
      maxDrawdown,
      totalTrades: trades.length,
      winningTrades,
      losingTrades,
      equityCurve,
      trades,
      dailyReturns,
    };
  }
}

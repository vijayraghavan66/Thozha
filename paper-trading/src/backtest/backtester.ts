import { v4 as uuidv4 } from 'uuid';
import type { Database } from '../db/sqlAdapter';
import { BacktestRepository, StrategyRepository, PortfolioRepository } from '../db/repositories';
import type { Strategy, BacktestConfig, BacktestResult, InstrumentId, Quote } from '../types';
import { VirtualTradingEngine } from '../engine/virtualEngine';
import { MarketDataProvider, Bar } from '../marketData/provider';

// ============================================================================
// Historical Backtester
// Runs strategy simulations against historical market data
// ============================================================================

export interface BacktesterConfig {
  moneyPilotDbPath: string;
  initialCash: number;
  commissionRate: number;
  slippageBps: number;
}

export interface BacktestTrade {
  timestamp: Date;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  commission: number;
  symbol: string;
}

export interface EquityPoint {
  timestamp: string;
  value: number;
}

export class Backtester {
  private marketData: MarketDataProvider;
  private backtestRepo: BacktestRepository;
  private strategyRepo: StrategyRepository;
  private portfolioRepo: PortfolioRepository;

  constructor(
    private db: Database,
    private config: BacktesterConfig
  ) {
    this.marketData = new MarketDataProvider({
      moneyPilotDbPath: config.moneyPilotDbPath,
      stalenessThresholdMs: 365 * 24 * 60 * 60 * 1000, // Accept any age for backtest
      delayedThresholdMs: 365 * 24 * 60 * 60 * 1000,
    });
    this.backtestRepo = new BacktestRepository(db);
    this.strategyRepo = new StrategyRepository(db);
    this.portfolioRepo = new PortfolioRepository(db);
  }

  /**
   * Run a backtest for a strategy
   */
  async runBacktest(
    strategyId: string,
    instrument: InstrumentId,
    startDate: Date,
    endDate: Date
  ): Promise<BacktestResult> {
    const strategy = this.strategyRepo.findById(strategyId);
    if (!strategy) {
      throw new Error(`Strategy ${strategyId} not found`);
    }

    // Create backtest config
    const config: BacktestConfig = {
      configId: uuidv4(),
      strategyId,
      startDate: startDate.toISOString(),
      endDate: endDate.toISOString(),
      initialCash: this.config.initialCash,
      commissionRate: this.config.commissionRate,
      slippageBps: this.config.slippageBps,
      dataSources: ['money_pilot'],
    };
    this.backtestRepo.saveConfig(config);

    // Fetch historical bars
    const bars = this.marketData.getBars(instrument, startDate, endDate);
    if (bars.length === 0) {
      throw new Error(`No historical data found for ${instrument.symbol} between ${startDate} and ${endDate}`);
    }

    // Run simulation
    const result = await this.simulateStrategy(strategy, instrument, bars, config);

    // Save result
    this.backtestRepo.saveResult(result);

    return result;
  }

  /**
   * Simulate strategy execution over historical bars
   */
  private async simulateStrategy(
    strategy: Strategy,
    instrument: InstrumentId,
    bars: Bar[],
    config: BacktestConfig
  ): Promise<BacktestResult> {
    const startTime = new Date();
    const trades: BacktestTrade[] = [];
    const equityCurve: EquityPoint[] = [];

    let cash = config.initialCash;
    let position = 0;
    let entryPrice = 0;

    // Simple moving average crossover strategy
    const shortPeriod = (strategy.parameters.shortPeriod as number) || 10;
    const longPeriod = (strategy.parameters.longPeriod as number) || 30;

    for (let i = longPeriod; i < bars.length; i++) {
      const bar = bars[i];
      const timestamp = new Date(bar.date);

      // Calculate moving averages
      const shortSMA = this.calculateSMA(bars.slice(i - shortPeriod, i), shortPeriod);
      const longSMA = this.calculateSMA(bars.slice(i - longPeriod, i), longPeriod);
      const prevShortSMA = this.calculateSMA(bars.slice(i - shortPeriod - 1, i - 1), shortPeriod);
      const prevLongSMA = this.calculateSMA(bars.slice(i - longPeriod - 1, i - 1), longPeriod);

      // Generate signals
      if (prevShortSMA <= prevLongSMA && shortSMA > longSMA && position <= 0) {
        // Golden cross - BUY
        const quantity = Math.floor(cash / bar.close / 100) * 100; // Round to lot size
        if (quantity > 0) {
          const cost = quantity * bar.close;
          const commission = Math.round(cost * config.commissionRate);
          cash -= cost + commission;
          position += quantity;
          entryPrice = bar.close;

          trades.push({
            timestamp,
            side: 'BUY',
            quantity,
            price: bar.close,
            commission,
            symbol: instrument.symbol,
          });
        }
      } else if (prevShortSMA >= prevLongSMA && shortSMA < longSMA && position > 0) {
        // Death cross - SELL
        const proceeds = position * bar.close;
        const commission = Math.round(proceeds * config.commissionRate);
        cash += proceeds - commission;

        trades.push({
          timestamp,
          side: 'SELL',
          quantity: position,
          price: bar.close,
          commission,
          symbol: instrument.symbol,
        });

        position = 0;
        entryPrice = 0;
      }

      // Record equity
      const equity = cash + position * bar.close;
      equityCurve.push({
        timestamp: timestamp.toISOString(),
        value: equity,
      });
    }

    // Close any remaining position at final bar
    if (position > 0) {
      const finalBar = bars[bars.length - 1];
      const proceeds = position * finalBar.close;
      const commission = Math.round(proceeds * config.commissionRate);
      cash += proceeds - commission;

      trades.push({
        timestamp: new Date(finalBar.date),
        side: 'SELL',
        quantity: position,
        price: finalBar.close,
        commission,
        symbol: instrument.symbol,
      });
    }

    // Calculate metrics
    const finalEquity = cash;
    const totalReturn = ((finalEquity - config.initialCash) / config.initialCash) * 100;
    const maxDrawdown = this.calculateMaxDrawdown(equityCurve);
    const winningTrades = trades.filter((t, i) => {
      if (t.side === 'BUY') return false;
      const buyTrade = trades.slice(0, i).reverse().find((bt) => bt.side === 'BUY');
      return buyTrade && t.price > buyTrade.price;
    }).length;
    const losingTrades = trades.filter((t) => t.side === 'SELL').length - winningTrades;

    const result: BacktestResult = {
      resultId: uuidv4(),
      configId: config.configId,
      totalReturn,
      maxDrawdown,
      totalTrades: trades.length,
      winningTrades,
      losingTrades,
      equityCurve,
      trades: trades as unknown[],
      startedAt: startTime.toISOString(),
      completedAt: new Date().toISOString(),
    };

    return result;
  }

  /**
   * Calculate simple moving average
   */
  private calculateSMA(bars: Bar[], period: number): number {
    if (bars.length === 0) return 0;
    const sum = bars.reduce((acc, bar) => acc + bar.close, 0);
    return sum / bars.length;
  }

  /**
   * Calculate maximum drawdown
   */
  private calculateMaxDrawdown(equityCurve: EquityPoint[]): number {
    let maxDrawdown = 0;
    let peak = -Infinity;

    for (const point of equityCurve) {
      if (point.value > peak) {
        peak = point.value;
      }
      const drawdown = ((peak - point.value) / peak) * 100;
      if (drawdown > maxDrawdown) {
        maxDrawdown = drawdown;
      }
    }

    return maxDrawdown;
  }

  /**
   * Close market data connection
   */
  close(): void {
    this.marketData.close();
  }
}

export default Backtester;

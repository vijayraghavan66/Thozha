import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import Database from '../src/db/sqlAdapter';
import { initDatabase } from '../src/types';
import { Backtester } from '../src/backtest/backtester';
import { StrategyRepository, PortfolioRepository } from '../src/db/repositories';
import { v4 as uuidv4 } from 'uuid';

describe('Historical Backtester', () => {
  const moneyPilotPath = '/Users/vijay-25589/Documents/InvestmentProject/MoneyPilot/data/money_pilot.sqlite3';
  let db: Database;
  let backtester: Backtester;
  let portfolioId: string;
  let strategyId: string;

  beforeEach(async () => {
    db = Database.createMemory();
    initDatabase(db);
    backtester = new Backtester(db, {
      moneyPilotDbPath: moneyPilotPath,
      initialCash: 1000000, // ₹10,000
      commissionRate: 0.001,
      slippageBps: 10,
    });

    // Create portfolio
    const now = new Date();
    const portfolioRepo = new PortfolioRepository(db);
    portfolioId = uuidv4();
    portfolioRepo.create({
      portfolioId,
      name: 'Backtest Portfolio',
      baseCurrency: 'INR',
      initialCash: 1000000,
      currentCash: 1000000,
      totalEquity: 1000000,
      status: 'ACTIVE',
      createdAt: now,
      updatedAt: now,
    });

    // Create strategy
    const strategyRepo = new StrategyRepository(db);
    strategyId = uuidv4();
    strategyRepo.create({
      strategyId,
      portfolioId,
      name: 'SMA Crossover',
      parameters: { shortPeriod: 5, longPeriod: 20 },
      status: 'DRAFT',
      riskLimits: {},
      createdAt: now,
      updatedAt: now,
    });
  });

  afterEach(() => {
    backtester.close();
  });

  it('should run backtest on RELIANCE historical data', async () => {
    const instrument = {
      exchange: 'NSE' as const,
      symbol: 'RELIANCE',
      instrumentType: 'EQUITY' as const,
    };

    const endDate = new Date('2026-11-25');
    const startDate = new Date('2026-10-01');

    const result = await backtester.runBacktest(strategyId, instrument, startDate, endDate);

    expect(result).toBeDefined();
    expect(result.configId).toBeTruthy();
    expect(typeof result.totalReturn).toBe('number');
    expect(typeof result.maxDrawdown).toBe('number');
    expect(result.totalTrades).toBeGreaterThanOrEqual(0);
    expect(result.equityCurve.length).toBeGreaterThan(0);
    expect(result.startedAt).toBeTruthy();
    expect(result.completedAt).toBeTruthy();

    console.log(`Backtest Result for RELIANCE (${startDate.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}):`);
    console.log(`  Total Return: ${result.totalReturn.toFixed(2)}%`);
    console.log(`  Max Drawdown: ${result.maxDrawdown.toFixed(2)}%`);
    console.log(`  Total Trades: ${result.totalTrades}`);
    console.log(`  Winning Trades: ${result.winningTrades}`);
    console.log(`  Losing Trades: ${result.losingTrades}`);
  }, 30000);

  it('should run backtest on TCS historical data', async () => {
    const instrument = {
      exchange: 'NSE' as const,
      symbol: 'TCS',
      instrumentType: 'EQUITY' as const,
    };

    const endDate = new Date('2026-11-25');
    const startDate = new Date('2026-10-01');

    const result = await backtester.runBacktest(strategyId, instrument, startDate, endDate);

    expect(result).toBeDefined();
    expect(result.totalTrades).toBeGreaterThanOrEqual(0);

    console.log(`Backtest Result for TCS (${startDate.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}):`);
    console.log(`  Total Return: ${result.totalReturn.toFixed(2)}%`);
    console.log(`  Total Trades: ${result.totalTrades}`);
  }, 30000);
});

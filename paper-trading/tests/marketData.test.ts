import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import Database from '../src/db/sqlAdapter';
import { initDatabase } from '../src/types';
import { PortfolioRepository } from '../src/db/repositories';
import { VirtualTradingEngine } from '../src/engine/virtualEngine';
import { MarketDataProvider, FixtureMarketDataProvider } from '../src/marketData/provider';
import { v4 as uuidv4 } from 'uuid';
import path from 'path';

describe('Market Data Provider', () => {
  describe('MoneyPilot Integration', () => {
    const moneyPilotPath = '/Users/vijay-25589/Documents/InvestmentProject/MoneyPilot/data/money_pilot.sqlite3';
    let provider: MarketDataProvider;

    beforeEach(() => {
      provider = new MarketDataProvider({
        moneyPilotDbPath: moneyPilotPath,
        stalenessThresholdMs: 5 * 60 * 1000, // 5 minutes
        delayedThresholdMs: 15 * 60 * 1000, // 15 minutes
      });
    });

    afterEach(() => {
      provider.close();
    });

    it('should fetch real quote for RELIANCE', () => {
      const instrument = {
        exchange: 'NSE' as const,
        symbol: 'RELIANCE',
        instrumentType: 'EQUITY' as const,
      };

      const result = provider.getQuote(instrument);

      expect(result).not.toBeNull();
      if (result) {
        expect(result.quote.last).toBeGreaterThan(0);
        expect(result.quote.symbol).toBeUndefined(); // Quote doesn't have symbol property
        expect(result.source).toBeTruthy();
        expect(result.providerTime).toBeInstanceOf(Date);
        expect(['live', 'delayed', 'historical']).toContain(result.freshness);
        expect(['open', 'closed', 'pre-market', 'post-market']).toContain(result.marketHours);
      }
    });

    it('should fetch real quote for TCS', () => {
      const instrument = {
        exchange: 'NSE' as const,
        symbol: 'TCS',
        instrumentType: 'EQUITY' as const,
      };

      const result = provider.getQuote(instrument);

      expect(result).not.toBeNull();
      if (result) {
        expect(result.quote.last).toBeGreaterThan(0);
        expect(result.freshness).toBeTruthy();
      }
    });

    it('should fetch historical bars for backtesting', () => {
      const instrument = {
        exchange: 'NSE' as const,
        symbol: 'RELIANCE',
        instrumentType: 'EQUITY' as const,
      };

      const endDate = new Date('2026-11-25');
      const startDate = new Date('2026-11-20');

      const bars = provider.getBars(instrument, startDate, endDate);

      expect(bars.length).toBeGreaterThan(0);
      expect(bars[0].close).toBeGreaterThan(0);
      expect(bars[0].source).toBeTruthy();
    });

    it('should correctly identify market hours', () => {
      // Market closed (Sunday)
      const sunday = new Date('2026-11-22T10:00:00+05:30');
      expect(provider.getMarketHours(sunday)).toBe('closed');

      // Market open (Monday 10:00 AM IST)
      const monday = new Date('2026-11-23T10:00:00+05:30');
      expect(provider.getMarketHours(monday)).toBe('open');

      // Pre-market (Monday 9:00 AM IST)
      const preMarket = new Date('2026-11-23T09:00:00+05:30');
      expect(provider.getMarketHours(preMarket)).toBe('pre-market');

      // Post-market (Monday 4:00 PM IST)
      const postMarket = new Date('2026-11-23T16:00:00+05:30');
      expect(provider.getMarketHours(postMarket)).toBe('post-market');
    });
  });

  describe('Fixture Provider', () => {
    it('should return fixture quotes', () => {
      const fixtureProvider = new FixtureMarketDataProvider();
      const instrument = {
        exchange: 'NSE' as const,
        symbol: 'TEST',
        instrumentType: 'EQUITY' as const,
      };

      fixtureProvider.addFixture(instrument, {
        instrument,
        timestamp: new Date(),
        bid: 100,
        ask: 100.1,
        last: 100.05,
        volume: 1000,
        open: 99,
        high: 101,
        low: 98,
        close: 100.05,
        freshness: 'fixture',
        source: 'test',
      });

      const result = fixtureProvider.getQuote(instrument);
      expect(result).not.toBeNull();
      expect(result?.freshness).toBe('fixture');
      expect(result?.quote.last).toBe(100.05);
    });
  });
});

describe('Engine with Market Data Safeguards', () => {
  let db: Database;
  let engine: VirtualTradingEngine;
  let portfolioId: string;

  beforeEach(async () => {
    db = Database.createMemory();
    initDatabase(db);
    engine = new VirtualTradingEngine(db, {
      commissionRate: 0.001,
      slippageBps: 10,
      minTick: 0.05,
      stalenessThresholdMs: 60_000,
      requireMarketHours: false,
    });

    const now = new Date();
    const portfolioRepo = new PortfolioRepository(db);
    portfolioId = uuidv4();
    portfolioRepo.create({
      portfolioId,
      name: 'Test Portfolio',
      baseCurrency: 'INR',
      initialCash: 50000000,
      currentCash: 50000000,
      totalEquity: 50000000,
      status: 'ACTIVE',
      createdAt: now,
      updatedAt: now,
    });
  });

  it('should reject order with stale quote', () => {
    const instrument = {
      exchange: 'NSE' as const,
      symbol: 'RELIANCE',
      instrumentType: 'EQUITY' as const,
    };

    const orderResult = engine.submitOrder(
      portfolioId,
      instrument,
      'BUY' as const,
      'MARKET' as const,
      100,
      undefined,
      uuidv4()
    );

    if ('order' in orderResult) {
      const staleQuote = {
        instrument,
        timestamp: new Date(Date.now() - 5 * 60 * 1000), // 5 minutes old
        bid: 2500,
        ask: 2500.1,
        last: 2500.05,
        volume: 10000,
        open: 2490,
        high: 2510,
        low: 2485,
        close: 2500.05,
        freshness: 'delayed' as const,
        source: 'test',
      };

      const result = engine.executeOrder(orderResult.order.orderId, staleQuote);
      expect('notFilled' in result).toBe(true);
      if ('notFilled' in result) {
        expect(result.notFilled.code).toBe('STALE_QUOTE');
      }
    }
  });

  it('should reject order with historical quote', () => {
    const instrument = {
      exchange: 'NSE' as const,
      symbol: 'RELIANCE',
      instrumentType: 'EQUITY' as const,
    };

    const orderResult = engine.submitOrder(
      portfolioId,
      instrument,
      'BUY' as const,
      'MARKET' as const,
      100,
      undefined,
      uuidv4()
    );

    if ('order' in orderResult) {
      const historicalQuote = {
        instrument,
        timestamp: new Date('2026-11-20'),
        bid: 2500,
        ask: 2500.1,
        last: 2500.05,
        volume: 10000,
        open: 2490,
        high: 2510,
        low: 2485,
        close: 2500.05,
        freshness: 'historical' as const,
        source: 'test',
      };

      const result = engine.executeOrder(orderResult.order.orderId, historicalQuote);
      expect('notFilled' in result).toBe(true);
      if ('notFilled' in result) {
        expect(result.notFilled.code).toBe('STALE_QUOTE');
      }
    }
  });

  it('should accept order with fixture quote', () => {
    const instrument = {
      exchange: 'NSE' as const,
      symbol: 'RELIANCE',
      instrumentType: 'EQUITY' as const,
    };

    const orderResult = engine.submitOrder(
      portfolioId,
      instrument,
      'BUY' as const,
      'MARKET' as const,
      100,
      undefined,
      uuidv4()
    );

    if ('order' in orderResult) {
      const fixtureQuote = {
        instrument,
        timestamp: new Date(),
        bid: 2500,
        ask: 2500.1,
        last: 2500.05,
        volume: 10000,
        open: 2490,
        high: 2510,
        low: 2485,
        close: 2500.05,
        freshness: 'fixture' as const,
        source: 'test',
      };

      const result = engine.executeOrder(orderResult.order.orderId, fixtureQuote);
      expect('filled' in result || 'partiallyFilled' in result).toBe(true);
    }
  });
});

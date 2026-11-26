import { describe, it, expect, beforeEach } from 'vitest';
import Database from '../src/db/sqlAdapter';
import { initDatabase } from '../src/types';
import { PortfolioRepository, OrderRepository, FillRepository, PositionRepository, CashTransactionRepository } from '../src/db/repositories';
import { VirtualTradingEngine } from '../src/engine/virtualEngine';
import { v4 as uuidv4 } from 'uuid';

describe('Virtual Trading Engine', () => {
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
    });

    // Create a test portfolio
    const now = new Date();
    const portfolioRepo = new PortfolioRepository(db);
    portfolioId = uuidv4();
    portfolioRepo.create({
      portfolioId,
      name: 'Test Portfolio',
      baseCurrency: 'INR',
      initialCash: 50000000, // ₹5,00,000 in paise (enough for 100 shares @ ~2500)
      currentCash: 50000000,
      totalEquity: 50000000,
      status: 'ACTIVE',
      createdAt: now,
      updatedAt: now,
    });
  });

  describe('Order Submission', () => {
    it('should accept a valid market buy order', () => {
      const instrument = {
        exchange: 'NSE' as const,
        symbol: 'RELIANCE',
        instrumentType: 'EQUITY' as const,
      };

      const result = engine.submitOrder(
        portfolioId,
        instrument,
        'BUY' as const,
        'MARKET' as const,
        100,
        undefined,
        uuidv4()
      );

      expect('order' in result).toBe(true);
      if ('order' in result) {
        expect(result.order.quantity).toBe(100);
        expect(result.order.side).toBe('BUY');
        expect(result.order.status).toBe('PENDING');
      }
    });

    it('should reject duplicate order submissions', () => {
      const idempotencyKey = uuidv4();
      const instrument = {
        exchange: 'NSE' as const,
        symbol: 'RELIANCE',
        instrumentType: 'EQUITY' as const,
      };

      engine.submitOrder(portfolioId, instrument, 'BUY' as const, 'MARKET' as const, 100, undefined, idempotencyKey);

      const result = engine.submitOrder(
        portfolioId,
        instrument,
        'BUY' as const,
        'MARKET' as const,
        100,
        undefined,
        idempotencyKey
      );

      expect('rejected' in result).toBe(true);
      if ('rejected' in result) {
        expect(result.rejected.code).toBe('DUPLICATE_ORDER');
      }
    });

    it('should reject order with zero quantity', () => {
      const instrument = {
        exchange: 'NSE' as const,
        symbol: 'RELIANCE',
        instrumentType: 'EQUITY' as const,
      };

      const result = engine.submitOrder(portfolioId, instrument, 'BUY' as const, 'MARKET' as const, 0);

      expect('rejected' in result).toBe(true);
      if ('rejected' in result) {
        expect(result.rejected.code).toBe('INVALID_QUANTITY');
      }
    });

    it('should reject limit order without limit price', () => {
      const instrument = {
        exchange: 'NSE' as const,
        symbol: 'RELIANCE',
        instrumentType: 'EQUITY' as const,
      };

      const result = engine.submitOrder(portfolioId, instrument, 'BUY' as const, 'LIMIT' as const, 100);

      expect('rejected' in result).toBe(true);
      if ('rejected' in result) {
        expect(result.rejected.code).toBe('INVALID_LIMIT_PRICE');
      }
    });
  });

  describe('Order Execution', () => {
    it('should execute a market buy order', () => {
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
        const orderId = orderResult.order.orderId;

        const quote = {
          instrument,
          timestamp: new Date().toISOString(),
          bid: 2500,
          ask: 2500.1,
          last: 2500.05,
          volume: 10000,
          open: 2490,
          high: 2510,
          low: 2485,
          close: 2500.05,
          freshness: 'live' as const,
          source: 'test',
        };

        const result = engine.executeOrder(orderId, quote);

        expect('filled' in result || 'partiallyFilled' in result).toBe(true);
        
        const fill = 'filled' in result ? result.filled : result.partiallyFilled;
        expect(fill.filled).toBe(true);
        expect(fill.fills.length).toBe(1);
        expect(fill.fills[0].quantity).toBe(100);
        expect(fill.fills[0].fillPrice).toBeGreaterThan(2500); // Includes slippage
      }
    });

    it('should reject buy order with insufficient cash', () => {
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
        10000, // Large quantity
        undefined,
        uuidv4()
      );

      if ('order' in orderResult) {
        const orderId = orderResult.order.orderId;

        const quote = {
          instrument,
          timestamp: new Date().toISOString(),
          bid: 2500,
          ask: 2500.1,
          last: 2500.05,
          volume: 10000,
          open: 2490,
          high: 2510,
          low: 2485,
          close: 2500.05,
          freshness: 'live' as const,
          source: 'test',
        };

        const result = engine.executeOrder(orderId, quote);

        expect('notFilled' in result).toBe(true);
        if ('notFilled' in result) {
          expect(result.notFilled.code).toBe('INSUFFICIENT_CASH');
        }
      }
    });
  });

  describe('Position Accounting', () => {
    it('should update position after a buy fill', () => {
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
        const orderId = orderResult.order.orderId;

        const quote = {
          instrument,
          timestamp: new Date().toISOString(),
          bid: 2500,
          ask: 2500.1,
          last: 2500.05,
          volume: 10000,
          open: 2490,
          high: 2510,
          low: 2485,
          close: 2500.05,
          freshness: 'live' as const,
          source: 'test',
        };

        engine.executeOrder(orderId, quote);

        const positionRepo = new PositionRepository(db);
        const position = positionRepo.findByPortfolioAndInstrument(portfolioId, instrument);

        expect(position).not.toBeNull();
        if (position) {
          expect(position.quantity).toBe(100);
          expect(position.averageCost).toBeGreaterThan(2500); // Includes slippage
        }
      }
    });

    it('should calculate realized P&L on sell', () => {
      const instrument = {
        exchange: 'NSE' as const,
        symbol: 'RELIANCE',
        instrumentType: 'EQUITY' as const,
      };

      // Buy 100 shares @ ~2500
      const buyOrder = engine.submitOrder(
        portfolioId,
        instrument,
        'BUY' as const,
        'MARKET' as const,
        100,
        undefined,
        uuidv4()
      );

      if ('order' in buyOrder) {
        const buyQuote = {
          instrument,
          timestamp: new Date().toISOString(),
          bid: 2500,
          ask: 2500.1,
          last: 2500.05,
          volume: 10000,
          open: 2490,
          high: 2510,
          low: 2485,
          close: 2500.05,
          freshness: 'live' as const,
          source: 'test',
        };

        engine.executeOrder(buyOrder.order.orderId, buyQuote);

        // Sell 100 shares @ 2600
        const sellOrder = engine.submitOrder(
          portfolioId,
          instrument,
          'SELL' as const,
          'MARKET' as const,
          100,
          undefined,
          uuidv4()
        );

        if ('order' in sellOrder) {
          const sellQuote = {
            instrument,
            timestamp: new Date().toISOString(),
            bid: 2600,
            ask: 2600.1,
            last: 2600.05,
            volume: 10000,
            open: 2590,
            high: 2610,
            low: 2585,
            close: 2600.05,
            freshness: 'live' as const,
            source: 'test',
          };

          engine.executeOrder(sellOrder.order.orderId, sellQuote);

          const positionRepo = new PositionRepository(db);
          const position = positionRepo.findByPortfolioAndInstrument(portfolioId, instrument);

          expect(position).not.toBeNull();
          if (position) {
            expect(position.quantity).toBe(0);
            expect(position.realizedPnl).toBeGreaterThan(0); // Profit from buy low, sell high
          }
        }
      }
    });
  });

  describe('Order Cancellation', () => {
    it('should cancel a pending order', () => {
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
        const orderId = orderResult.order.orderId;

        const cancelResult = engine.cancelOrder(orderId);

        expect('canceled' in cancelResult).toBe(true);
        if ('canceled' in cancelResult) {
          expect(cancelResult.canceled.status).toBe('CANCELED');
          expect(cancelResult.canceled.canceledAt).toBeDefined();
        }
      }
    });

    it('should not cancel a filled order', () => {
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
        const orderId = orderResult.order.orderId;

        const quote = {
          instrument,
          timestamp: new Date().toISOString(),
          bid: 2500,
          ask: 2500.1,
          last: 2500.05,
          volume: 10000,
          open: 2490,
          high: 2510,
          low: 2485,
          close: 2500.05,
          freshness: 'live' as const,
          source: 'test',
        };

        engine.executeOrder(orderId, quote);

        const cancelResult = engine.cancelOrder(orderId);

        expect('notCancellable' in cancelResult).toBe(true);
        if ('notCancellable' in cancelResult) {
          expect(cancelResult.notCancellable.code).toBe('ORDER_ALREADY_COMPLETED');
        }
      }
    });
  });

  describe('Cash Ledger', () => {
    it('should update cash balance after a fill', () => {
      const instrument = {
        exchange: 'NSE' as const,
        symbol: 'RELIANCE',
        instrumentType: 'EQUITY' as const,
      };

      const initialCash = 50000000; // ₹5,00,000

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
        const orderId = orderResult.order.orderId;

        const quote = {
          instrument,
          timestamp: new Date().toISOString(),
          bid: 2500,
          ask: 2500.1,
          last: 2500.05,
          volume: 10000,
          open: 2490,
          high: 2510,
          low: 2485,
          close: 2500.05,
          freshness: 'live' as const,
          source: 'test',
        };

        engine.executeOrder(orderId, quote);

        const portfolioRepo = new PortfolioRepository(db);
        const portfolio = portfolioRepo.findById(portfolioId);

        expect(portfolio).not.toBeNull();
        if (portfolio) {
          expect(portfolio.currentCash).toBeLessThan(initialCash);
          expect(initialCash - portfolio.currentCash).toBeGreaterThan(250000); // ~₹250000 for 100 shares
        }
      }
    });
  });
});

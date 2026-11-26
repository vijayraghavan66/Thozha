import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { createServer, type ServerInstance } from '../src/server';
import { createMoneyPilotSource } from '../src/marketData/moneyPilotSource';
import Database from '../src/db/sqlAdapter';
import { initDatabase } from '../src/types';
import { v4 as uuidv4 } from 'uuid';
import path from 'path';
import fs from 'fs';

const REAL_MONEYPILOT_DB = '/Users/vijay-25589/Documents/InvestmentProject/MoneyPilot/data/money_pilot.sqlite3';
const TEST_DB_PATH = path.join(process.cwd(), 'tests', 'test_paper_trading.sqlite3');

describe('Paper Trading API Server', () => {
  let server: ServerInstance;
  let db: Database;
  let baseUrl: string;

  beforeEach(async () => {
    // Clean up test database
    if (fs.existsSync(TEST_DB_PATH)) {
      fs.unlinkSync(TEST_DB_PATH);
    }
    db = Database.create(TEST_DB_PATH);
    initDatabase(db);
    server = await createServer(db, ':memory:', 'test-api-key');
    baseUrl = `http://127.0.0.1:${server.port}`;
  });

  afterEach(async () => {
    await server.close();
    if (fs.existsSync(TEST_DB_PATH)) {
      fs.unlinkSync(TEST_DB_PATH);
    }
  });

  describe('Authentication', () => {
    it('should reject requests without API key', async () => {
      const response = await fetch(`${baseUrl}/api/paper/portfolio`);
      expect(response.status).toBe(401);
    });

    it('should reject requests with wrong API key', async () => {
      const response = await fetch(`${baseUrl}/api/paper/portfolio`, {
        headers: { 'x-api-key': 'wrong-key' },
      });
      expect(response.status).toBe(401);
    });

    it('should accept requests with correct API key', async () => {
      const response = await fetch(`${baseUrl}/api/paper/portfolio`, {
        headers: { 'x-api-key': 'test-api-key' },
      });
      expect(response.status).toBe(200);
    });
  });

  describe('Paper Trading Endpoints', () => {
    it('should create and return default portfolio', async () => {
      const response = await fetch(`${baseUrl}/api/paper/portfolio`, {
        headers: { 'x-api-key': 'test-api-key' },
      });
      const data = await response.json();
      expect(response.status).toBe(200);
      expect(data.portfolio).toBeDefined();
      expect(data.portfolio.portfolioId).toBeDefined();
      expect(data.portfolio.currentCash).toBe(10_000_000);
      expect(data.portfolio.status).toBe('ACTIVE');
    });

    it('should submit and execute an order', async () => {
      // Get portfolio
      const portfolioResponse = await fetch(`${baseUrl}/api/paper/portfolio`, {
        headers: { 'x-api-key': 'test-api-key' },
      });
      const { portfolio } = await portfolioResponse.json();

      // Submit order
      const orderResponse = await fetch(`${baseUrl}/api/paper/orders`, {
        method: 'POST',
        headers: {
          'x-api-key': 'test-api-key',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          portfolioId: portfolio.portfolioId,
          instrument: { exchange: 'NSE', symbol: 'RELIANCE', instrumentType: 'EQUITY' },
          side: 'BUY',
          orderType: 'MARKET',
          quantity: 10,
        }),
      });
      expect(orderResponse.status).toBe(201);
      const { order } = await orderResponse.json();

      // Execute order with live quote
      const quote = {
        instrument: { exchange: 'NSE', symbol: 'RELIANCE', instrumentType: 'EQUITY' },
        timestamp: new Date().toISOString(),
        bid: 2500,
        ask: 2500.1,
        last: 2500.05,
        volume: 10000,
        open: 2490,
        high: 2510,
        low: 2485,
        close: 2500.05,
        freshness: 'live',
        source: 'test',
      };

      const executeResponse = await fetch(`${baseUrl}/api/paper/orders/${order.orderId}/execute`, {
        method: 'POST',
        headers: {
          'x-api-key': 'test-api-key',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ quote }),
      });
      expect(executeResponse.status).toBe(200);

      // Verify position
      const positionsResponse = await fetch(
        `${baseUrl}/api/paper/positions?portfolioId=${portfolio.portfolioId}`,
        { headers: { 'x-api-key': 'test-api-key' } }
      );
      const { positions } = await positionsResponse.json();
      expect(positions.length).toBeGreaterThan(0);
      expect(positions[0].instrument.symbol).toBe('RELIANCE');
      expect(positions[0].quantity).toBe(10);

      // Verify cash reduced
      const updatedPortfolioResponse = await fetch(`${baseUrl}/api/paper/portfolio`, {
        headers: { 'x-api-key': 'test-api-key' },
      });
      const { portfolio: updatedPortfolio } = await updatedPortfolioResponse.json();
      expect(updatedPortfolio.currentCash).toBeLessThan(10_000_000);
    });

    it('should reject duplicate orders with idempotency', async () => {
      const portfolioResponse = await fetch(`${baseUrl}/api/paper/portfolio`, {
        headers: { 'x-api-key': 'test-api-key' },
      });
      const { portfolio } = await portfolioResponse.json();

      const key = uuidv4();
      const order1Response = await fetch(`${baseUrl}/api/paper/orders`, {
        method: 'POST',
        headers: {
          'x-api-key': 'test-api-key',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          portfolioId: portfolio.portfolioId,
          instrument: { exchange: 'NSE', symbol: 'TCS', instrumentType: 'EQUITY' },
          side: 'BUY',
          orderType: 'MARKET',
          quantity: 5,
          idempotencyKey: key,
        }),
      });
      expect(order1Response.status).toBe(201);

      const order2Response = await fetch(`${baseUrl}/api/paper/orders`, {
        method: 'POST',
        headers: {
          'x-api-key': 'test-api-key',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          portfolioId: portfolio.portfolioId,
          instrument: { exchange: 'NSE', symbol: 'TCS', instrumentType: 'EQUITY' },
          side: 'BUY',
          orderType: 'MARKET',
          quantity: 5,
          idempotencyKey: key,
        }),
      });
      expect(order2Response.status).toBe(422);
      const error = await order2Response.json();
      expect(error.code).toBe('DUPLICATE_ORDER');
    });

    it('should cancel a pending order', async () => {
      const portfolioResponse = await fetch(`${baseUrl}/api/paper/portfolio`, {
        headers: { 'x-api-key': 'test-api-key' },
      });
      const { portfolio } = await portfolioResponse.json();

      const orderResponse = await fetch(`${baseUrl}/api/paper/orders`, {
        method: 'POST',
        headers: {
          'x-api-key': 'test-api-key',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          portfolioId: portfolio.portfolioId,
          instrument: { exchange: 'NSE', symbol: 'INFY', instrumentType: 'EQUITY' },
          side: 'BUY',
          orderType: 'LIMIT',
          quantity: 5,
          limitPrice: 1000,
        }),
      });
      const { order } = await orderResponse.json();

      const cancelResponse = await fetch(`${baseUrl}/api/paper/orders/${order.orderId}/cancel`, {
        method: 'POST',
        headers: { 'x-api-key': 'test-api-key' },
      });
      expect(cancelResponse.status).toBe(200);
      const { order: canceled } = await cancelResponse.json();
      expect(canceled.status).toBe('CANCELED');
    });
  });

  describe('Persistence', () => {
    it('should persist state across server restart', async () => {
      // Create portfolio and execute trade
      const portfolioResponse = await fetch(`${baseUrl}/api/paper/portfolio`, {
        headers: { 'x-api-key': 'test-api-key' },
      });
      const { portfolio } = await portfolioResponse.json();

      const orderResponse = await fetch(`${baseUrl}/api/paper/orders`, {
        method: 'POST',
        headers: {
          'x-api-key': 'test-api-key',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          portfolioId: portfolio.portfolioId,
          instrument: { exchange: 'NSE', symbol: 'SBIN', instrumentType: 'EQUITY' },
          side: 'BUY',
          orderType: 'MARKET',
          quantity: 5,
        }),
      });
      const { order } = await orderResponse.json();

      const quote = {
        instrument: { exchange: 'NSE', symbol: 'SBIN', instrumentType: 'EQUITY' },
        timestamp: new Date().toISOString(),
        bid: 600,
        ask: 600.1,
        last: 600.05,
        volume: 5000,
        open: 590,
        high: 610,
        low: 585,
        close: 600.05,
        freshness: 'live',
        source: 'test',
      };

      await fetch(`${baseUrl}/api/paper/orders/${order.orderId}/execute`, {
        method: 'POST',
        headers: {
          'x-api-key': 'test-api-key',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ quote }),
      });

      // Close server and database; redirect afterEach's idempotent close to the
      // restart server so this test is the sole owner of the original shutdown.
      await server.close();
      server.closedByTest = true;

      // Reopen with same database file
      const newDb = Database.create(TEST_DB_PATH);
      const newServer = await createServer(newDb, ':memory:', 'test-api-key');
      server = newServer;

      // Verify portfolio persists
      const newBaseUrl = `http://127.0.0.1:${newServer.port}`;
      const newPortfolioResponse = await fetch(`${newBaseUrl}/api/paper/portfolio`, {
        headers: { 'x-api-key': 'test-api-key' },
      });
      expect(newPortfolioResponse.status).toBe(200);
      const { portfolio: persistedPortfolio } = await newPortfolioResponse.json();
      expect(persistedPortfolio.portfolioId).toBe(portfolio.portfolioId);
      expect(persistedPortfolio.currentCash).toBeLessThan(10_000_000);

      // Verify position persists
      const positionsResponse = await fetch(
        `${newBaseUrl}/api/paper/positions?portfolioId=${portfolio.portfolioId}`,
        { headers: { 'x-api-key': 'test-api-key' } }
      );
      const { positions } = await positionsResponse.json();
      expect(positions.length).toBeGreaterThan(0);
      expect(positions[0].instrument.symbol).toBe('SBIN');
      expect(positions[0].quantity).toBe(5);

      // afterEach closes the restart server (already assigned to `server`).
    });
  });

  describe('MoneyPilot Integration', () => {
    it('should return real instruments from MoneyPilot', async () => {
      const source = createMoneyPilotSource(REAL_MONEYPILOT_DB);
      const instruments = source.getInstruments();

      expect(instruments.length).toBeGreaterThan(0);
      expect(instruments.some(i => i.symbol === 'RELIANCE.NS')).toBe(true);
      expect(instruments.some(i => i.kind === 'EQUITY')).toBe(true);
      source.disconnect();
    });

    it('should return real quotes with timestamps', async () => {
      const source = createMoneyPilotSource(REAL_MONEYPILOT_DB);
      const quote = source.getLatestQuote('RELIANCE.NS');

      expect(quote).not.toBeNull();
      expect(quote?.price).toBeGreaterThan(0);
      expect(quote?.provider_time).toMatch(/^\d{4}-\d{2}-\d{2}T/);
      expect(quote?.source).toBe('yahoo_chart');
      expect(quote?.fetched_at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
      source.disconnect();
    });

    it('should return real bars with dates', async () => {
      const source = createMoneyPilotSource(REAL_MONEYPILOT_DB);
      const bars = source.getBars('RELIANCE.NS', '2026-11-01', '2026-11-26');

      expect(bars.length).toBeGreaterThan(0);
      expect(bars[0].date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      expect(bars[0].open).toBeGreaterThan(0);
      expect(bars[0].close).toBeGreaterThan(0);
      expect(bars[0].volume).toBeGreaterThanOrEqual(0);
      source.disconnect();
    });

    it('should return the real holiday calendar in a valid shape (may be empty upstream)', async () => {
      // The MoneyPilot holiday table is currently empty upstream. The API must
      // still return a well-formed array without fabricating dates.
      const source = createMoneyPilotSource(REAL_MONEYPILOT_DB);
      const holidays = source.getHolidays();

      expect(Array.isArray(holidays)).toBe(true);
      for (const h of holidays) {
        expect(h.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      }
      source.disconnect();
    });

    it('should check holidays against a seeded fixture calendar', async () => {
      // Seed a temporary copy of the MoneyPilot DB with known holidays so the
      // isHoliday/getHolidays logic is verified against nonempty data without
      // mutating the real upstream database.
      const fixtureDb = path.join(process.cwd(), 'tests', 'fixture_holidays.sqlite3');
      if (fs.existsSync(fixtureDb)) {
        fs.unlinkSync(fixtureDb);
      }
      // Use the SQLite backup API so the copy is consistent even though the
      // upstream database is in WAL mode (a raw file copy misses WAL pages).
      const backupSource = new (await import('better-sqlite3')).default(REAL_MONEYPILOT_DB, { readonly: true, fileMustExist: true });
      await backupSource.backup(fixtureDb);
      backupSource.close();
      const writable = new (await import('better-sqlite3')).default(fixtureDb);
      writable.exec("DELETE FROM holidays; INSERT INTO holidays (date, description, source, added_at) VALUES ('2026-11-04', 'Diwali', 'fixture', datetime('now')), ('2026-12-25', 'Christmas', 'fixture', datetime('now'));");
      writable.close();

      const source = createMoneyPilotSource(fixtureDb);
      expect(source.isHoliday('2026-11-04')).toBe(true); // seeded Diwali
      expect(source.isHoliday('2026-11-05')).toBe(false);
      const holidays = source.getHolidays();
      expect(holidays.some(h => h.date === '2026-12-25')).toBe(true);
      source.disconnect();
      fs.unlinkSync(fixtureDb);
    });
  });
});

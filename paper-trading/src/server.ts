import express, { type Request, type Response, type NextFunction } from 'express';
import cors from 'cors';
import path from 'path';
import fs from 'fs';
import crypto from 'crypto';
import type { Server } from 'http';
import { createMoneyPilotSource, type MoneyPilotSource } from './marketData/moneyPilotSource';
import Database from './db/sqlAdapter';
import { initDatabase } from './types';
import {
  PortfolioRepository,
  OrderRepository,
  FillRepository,
  PositionRepository,
  CashTransactionRepository,
} from './db/repositories';
import { VirtualTradingEngine } from './engine/virtualEngine';
import { RiskEngine } from './risk/riskEngine';
import type { Quote, InstrumentId, OrderSide, OrderType } from './types';

// ============================================================================
// Thozha Paper Trading Backend API
// Isolated, authenticated server connecting MoneyPilot market data
// to the persistent paper-trading SQLite engine
// ============================================================================

export interface ServerInstance {
  app: express.Express;
  port: number;
  close: () => Promise<void>;
}

export async function createServer(db: Database, moneyPilotPath: string, apiKey: string): Promise<ServerInstance> {
  const engine = new VirtualTradingEngine(db, {
    commissionRate: 0.001,
    slippageBps: 10,
    minTick: 0.05,
  });
  const riskEngine = new RiskEngine(db);
  const portfolioRepo = new PortfolioRepository(db);
  const orderRepo = new OrderRepository(db);
  const positionRepo = new PositionRepository(db);
  const cashTxRepo = new CashTransactionRepository(db);

  let moneyPilot: MoneyPilotSource | null = null;
  try {
    moneyPilot = createMoneyPilotSource(moneyPilotPath);
  } catch (err) {
    console.error('MoneyPilot database not available:', err);
  }

  const app = express();
  app.use(cors({ origin: 'http://127.0.0.1:5174', credentials: true }));
  app.use(express.json());

  function requireApiKey(req: Request, res: Response, next: NextFunction): void {
    const key = req.headers['x-api-key'];
    if (key !== apiKey) {
      res.status(401).json({ error: 'Unauthorized', code: 'INVALID_API_KEY' });
      return;
    }
    next();
  }

  // Health check
  app.get('/health', (_req: Request, res: Response) => {
    res.json({ status: 'ok', timestamp: new Date().toISOString() });
  });

  // Market data endpoints
  app.get('/api/market-data/status', requireApiKey, (_req: Request, res: Response) => {
    if (!moneyPilot) {
      res.status(503).json({ error: 'MoneyPilot database unavailable', code: 'MONEYPILOT_UNAVAILABLE' });
      return;
    }
    const isOpen = checkMarketOpen(moneyPilot);
    res.json({
      status: isOpen ? 'open' : 'closed',
      timestamp: new Date().toISOString(),
      source: 'moneypilot',
    });
  });

  app.get('/api/market-data/instruments', requireApiKey, (_req: Request, res: Response) => {
    if (!moneyPilot) {
      res.status(503).json({ error: 'MoneyPilot database unavailable', code: 'MONEYPILOT_UNAVAILABLE' });
      return;
    }
    const instruments = moneyPilot.getInstruments();
    res.json({ instruments: instruments.filter(i => i.kind === 'EQUITY' || i.kind === 'INDEX') });
  });

  app.get('/api/market-data/quote', requireApiKey, (req: Request, res: Response) => {
    if (!moneyPilot) {
      res.status(503).json({ error: 'MoneyPilot database unavailable', code: 'MONEYPILOT_UNAVAILABLE' });
      return;
    }
    const symbol = req.query.symbol as string;
    if (!symbol) {
      res.status(400).json({ error: 'Symbol required', code: 'MISSING_SYMBOL' });
      return;
    }
    const quote = moneyPilot.getLatestQuote(symbol);
    if (!quote) {
      res.status(404).json({ error: 'Quote not found', code: 'QUOTE_NOT_FOUND' });
      return;
    }
    res.json(quote);
  });

  app.get('/api/market-data/bars', requireApiKey, (req: Request, res: Response) => {
    if (!moneyPilot) {
      res.status(503).json({ error: 'MoneyPilot database unavailable', code: 'MONEYPILOT_UNAVAILABLE' });
      return;
    }
    const { symbol, start, end } = req.query as { symbol?: string; start?: string; end?: string };
    if (!symbol || !start || !end) {
      res.status(400).json({ error: 'Symbol, start, and end required', code: 'MISSING_PARAMS' });
      return;
    }
    const bars = moneyPilot.getBars(symbol, start, end);
    res.json({ bars });
  });

  app.get('/api/market-data/latest-bar', requireApiKey, (req: Request, res: Response) => {
    if (!moneyPilot) {
      res.status(503).json({ error: 'MoneyPilot database unavailable', code: 'MONEYPILOT_UNAVAILABLE' });
      return;
    }
    const symbol = req.query.symbol as string;
    if (!symbol) {
      res.status(400).json({ error: 'Symbol required', code: 'MISSING_SYMBOL' });
      return;
    }
    const date = moneyPilot.getLatestBarDate(symbol);
    res.json({ date });
  });

  app.get('/api/market-data/holidays', requireApiKey, (_req: Request, res: Response) => {
    if (!moneyPilot) {
      res.status(503).json({ error: 'MoneyPilot database unavailable', code: 'MONEYPILOT_UNAVAILABLE' });
      return;
    }
    const holidays = moneyPilot.getHolidays();
    res.json({ holidays });
  });

  // Paper trading endpoints
  app.get('/api/paper/portfolio', requireApiKey, (_req: Request, res: Response) => {
    const portfolios = portfolioRepo.listByStatus('ACTIVE');
    if (portfolios.length === 0) {
      // Create default portfolio
      const now = new Date();
      const portfolio = portfolioRepo.create({
        portfolioId: crypto.randomUUID(),
        name: 'Default Paper Portfolio',
        baseCurrency: 'INR',
        initialCash: 10_000_000, // ₹10,00,000 in paise
        currentCash: 10_000_000,
        totalEquity: 10_000_000,
        status: 'ACTIVE',
        createdAt: now,
        updatedAt: now,
      });
      res.json({ portfolio });
      return;
    }
    res.json({ portfolio: portfolios[0] });
  });

  app.get('/api/paper/positions', requireApiKey, (req: Request, res: Response) => {
    const portfolioId = req.query.portfolioId as string;
    if (!portfolioId) {
      res.status(400).json({ error: 'portfolioId required', code: 'MISSING_PORTFOLIO_ID' });
      return;
    }
    const positions = positionRepo.findByPortfolio(portfolioId);
    res.json({ positions });
  });

  app.get('/api/paper/orders', requireApiKey, (req: Request, res: Response) => {
    const portfolioId = req.query.portfolioId as string;
    if (!portfolioId) {
      res.status(400).json({ error: 'portfolioId required', code: 'MISSING_PORTFOLIO_ID' });
      return;
    }
    const orders = orderRepo.findByStatus(portfolioId, 'PENDING')
      .concat(orderRepo.findByStatus(portfolioId, 'SUBMITTED'))
      .concat(orderRepo.findByStatus(portfolioId, 'FILLED'))
      .concat(orderRepo.findByStatus(portfolioId, 'CANCELED'));
    res.json({ orders });
  });

  app.post('/api/paper/orders', requireApiKey, (req: Request, res: Response) => {
    const { portfolioId, instrument, side, orderType, quantity, limitPrice, idempotencyKey } = req.body as {
      portfolioId: string;
      instrument: InstrumentId;
      side: OrderSide;
      orderType: OrderType;
      quantity: number;
      limitPrice?: number;
      idempotencyKey?: string;
    };

    if (!portfolioId || !instrument || !side || !orderType || !quantity) {
      res.status(400).json({ error: 'Missing required fields', code: 'MISSING_FIELDS' });
      return;
    }

    const result = engine.submitOrder(portfolioId, instrument, side, orderType, quantity, limitPrice, idempotencyKey);
    if ('rejected' in result) {
      res.status(422).json({ error: result.rejected.reason, code: result.rejected.code });
      return;
    }

    res.status(201).json({ order: result.order });
  });

  app.post('/api/paper/orders/:orderId/execute', requireApiKey, (req: Request, res: Response) => {
    const orderId = req.params.orderId as string;
    const { quote } = req.body as { quote: Quote };

    if (!quote) {
      res.status(400).json({ error: 'Quote required', code: 'MISSING_QUOTE' });
      return;
    }

    // Risk check
    const order = orderRepo.findById(orderId);
    if (!order) {
      res.status(404).json({ error: 'Order not found', code: 'ORDER_NOT_FOUND' });
      return;
    }
    const portfolio = portfolioRepo.findById(order.portfolioId);
    const positions = positionRepo.findByPortfolio(order.portfolioId);
    const riskResult = riskEngine.checkOrder(order, portfolio!, positions, quote, null, new Date());

    if (!riskResult.passed) {
      riskResult.violations.forEach(v => {
        riskEngine.recordViolation(order.portfolioId, orderId, v.rule, v.message, v.current, v.limit);
      });
      res.status(422).json({ error: 'Risk check failed', code: 'RISK_CHECK_FAILED', violations: riskResult.violations });
      return;
    }

    const result = engine.executeOrder(orderId, quote);
    if ('notFilled' in result) {
      res.status(422).json({ error: result.notFilled.reason, code: result.notFilled.code });
      return;
    }

    res.json({ fill: 'filled' in result ? result.filled : result.partiallyFilled });
  });

  app.post('/api/paper/orders/:orderId/cancel', requireApiKey, (req: Request, res: Response) => {
    const orderId = req.params.orderId as string;
    const result = engine.cancelOrder(orderId);
    if ('notCancellable' in result) {
      res.status(422).json({ error: result.notCancellable.reason, code: result.notCancellable.code });
      return;
    }
    res.json({ order: result.canceled });
  });

  app.get('/api/paper/ledger', requireApiKey, (req: Request, res: Response) => {
    const portfolioId = req.query.portfolioId as string;
    if (!portfolioId) {
      res.status(400).json({ error: 'portfolioId required', code: 'MISSING_PORTFOLIO_ID' });
      return;
    }
    const transactions = cashTxRepo.findByPortfolio(portfolioId);
    res.json({ transactions });
  });

  // Market hours check
  function checkMarketOpen(source: MoneyPilotSource): boolean {
    const now = new Date();
    const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const dateStr = ist.toISOString().split('T')[0];
    const minutes = ist.getHours() * 60 + ist.getMinutes();
    const day = ist.getDay();

    // Weekend check
    if (day === 0 || day === 6) return false;

    // Holiday check
    if (source.isHoliday(dateStr)) return false;

    // NSE: 9:15 AM to 3:30 PM IST
    return minutes >= 555 && minutes <= 930;
  }

  // Start server
  return new Promise((resolve, reject) => {
    const server = app.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      // Idempotent close: repeated calls reuse the same promise.
      let closePromise: Promise<void> | null = null;
      resolve({
        app,
        port,
        close: () => {
          if (!closePromise) {
            closePromise = new Promise<void>((resolveClose) => {
              server.close(() => {
                db.close();
                resolveClose();
              });
            });
          }
          return closePromise;
        },
      });
    });
    server.on('error', reject);
  });
}

// Production startup
if (import.meta.url === `file://${process.argv[1]}`) {
  const PAPER_DB_PATH = process.env.PAPER_DB_PATH ?? path.join(process.cwd(), 'data', 'paper_trading.sqlite3');
  const MONEYPILOT_DB_PATH = process.env.MONEYPILOT_DB_PATH ?? '/Users/vijay-25589/Documents/InvestmentProject/MoneyPilot/data/money_pilot.sqlite3';
  const PAPER_API_KEY = process.env.PAPER_API_KEY ?? crypto.randomBytes(32).toString('hex');
  const PORT = parseInt(process.env.PAPER_PORT ?? '5175', 10);

  const dbDir = path.dirname(PAPER_DB_PATH);
  if (!fs.existsSync(dbDir)) fs.mkdirSync(dbDir, { recursive: true });
  const db = Database.create(PAPER_DB_PATH);
  initDatabase(db);

  createServer(db, MONEYPILOT_DB_PATH, PAPER_API_KEY)
    .then(({ app, close }) => {
      app.listen(PORT, '127.0.0.1', () => {
        console.log(`Paper Trading API server listening on http://127.0.0.1:${PORT}`);
        console.log(`Paper DB: ${PAPER_DB_PATH}`);
        console.log(`MoneyPilot DB: ${MONEYPILOT_DB_PATH}`);
        console.log(`API Key: ${PAPER_API_KEY.substring(0, 8)}...`);
      });

      process.on('SIGTERM', async () => {
        console.log('SIGTERM received, closing database...');
        await close();
        process.exit(0);
      });
      process.on('SIGINT', async () => {
        console.log('SIGINT received, closing database...');
        await close();
        process.exit(0);
      });
    })
    .catch((err) => {
      console.error('Failed to start server:', err);
      process.exit(1);
    });
}

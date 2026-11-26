import { z } from 'zod';
import type { Database } from './db/sqlAdapter';

// ============================================================================
// Core Domain Types
// ============================================================================

export const currencySchema = z.object({
  minorUnits: z.number().int().nonnegative(), // INR in paise (100 paise = 1 INR)
});
export type Currency = z.infer<typeof currencySchema>;

export const moneySchema = z.object({
  amount: z.number().int(), // Can be negative for short positions
  currency: z.enum(['INR']),
  minorUnits: z.number().int(),
});
export type Money = z.infer<typeof moneySchema>;

export const instrumentIdSchema = z.object({
  exchange: z.enum(['NSE', 'BSE']),
  symbol: z.string().min(1).max(50),
  instrumentType: z.enum(['EQUITY', 'ETF', 'INDEX']),
});
export type InstrumentId = z.infer<typeof instrumentIdSchema>;

export const quoteSchema = z.object({
  instrument: instrumentIdSchema,
  timestamp: z.coerce.date(),
  bid: z.number().positive(),
  ask: z.number().positive(),
  last: z.number().positive(),
  volume: z.number().int().nonnegative(),
  open: z.number().positive(),
  high: z.number().positive(),
  low: z.number().positive(),
  close: z.number().positive(),
  freshness: z.enum(['live', 'delayed', 'historical']),
  source: z.string(),
});
export type Quote = z.infer<typeof quoteSchema>;

export const orderSideSchema = z.enum(['BUY', 'SELL']);
export type OrderSide = z.infer<typeof orderSideSchema>;

export const orderTypeSchema = z.enum(['MARKET', 'LIMIT']);
export type OrderType = z.infer<typeof orderTypeSchema>;

export const orderStatusSchema = z.enum([
  'PENDING',
  'SUBMITTED',
  'PARTIALLY_FILLED',
  'FILLED',
  'CANCELED',
  'REJECTED',
  'EXPIRED',
]);
export type OrderStatus = z.infer<typeof orderStatusSchema>;

export const orderSchema = z.object({
  orderId: z.string().uuid(),
  portfolioId: z.string().uuid(),
  instrument: instrumentIdSchema,
  side: orderSideSchema,
  orderType: orderTypeSchema,
  quantity: z.number().int().positive(),
  limitPrice: z.number().positive().optional(),
  status: orderStatusSchema,
  filledQuantity: z.number().int().nonnegative(),
  averageFillPrice: z.number().positive().optional(),
  createdAt: z.coerce.date(),
  updatedAt: z.coerce.date(),
  submittedAt: z.coerce.date().optional(),
  filledAt: z.coerce.date().optional(),
  canceledAt: z.coerce.date().optional(),
  rejectedReason: z.string().optional(),
  idempotencyKey: z.string(),
  timeInForce: z.enum(['DAY', 'GTC']).default('DAY'),
  expireAt: z.coerce.date().optional(),
});
export type Order = z.infer<typeof orderSchema>;

export const fillSchema = z.object({
  fillId: z.string().uuid(),
  orderId: z.string().uuid(),
  instrument: instrumentIdSchema,
  side: orderSideSchema,
  quantity: z.number().int().positive(),
  fillPrice: z.number().positive(),
  commission: currencySchema,
  taxes: currencySchema,
  timestamp: z.coerce.date(),
  exchangeFillId: z.string().optional(),
});
export type Fill = z.infer<typeof fillSchema>;

export const positionSchema = z.object({
  portfolioId: z.string().uuid(),
  instrument: instrumentIdSchema,
  quantity: z.number().int(), // Positive for long, negative for short
  averageCost: z.number().positive(), // Per-share cost basis
  marketValue: z.number(), // Current market value
  unrealizedPnl: z.number(), // In minor units (paise)
  realizedPnl: z.number(), // In minor units (paise)
  lastUpdated: z.coerce.date(),
});
export type Position = z.infer<typeof positionSchema>;

export const cashTransactionSchema = z.object({
  transactionId: z.string().uuid(),
  portfolioId: z.string().uuid(),
  type: z.enum(['DEPOSIT', 'WITHDRAWAL', 'DIVIDEND', 'COMMISSION', 'TAX', 'FILL']),
  amount: z.number().int(), // Can be negative
  balanceAfter: z.number().int(),
  timestamp: z.coerce.date(),
  description: z.string(),
  referenceId: z.string().optional(),
});
export type CashTransaction = z.infer<typeof cashTransactionSchema>;

export const portfolioSchema = z.object({
  portfolioId: z.string().uuid(),
  name: z.string().max(100),
  baseCurrency: z.enum(['INR']),
  initialCash: z.number().int(),
  currentCash: z.number().int(),
  totalEquity: z.number(),
  createdAt: z.coerce.date(),
  updatedAt: z.coerce.date(),
  status: z.enum(['ACTIVE', 'PAUSED', 'CLOSED']),
});
export type Portfolio = z.infer<typeof portfolioSchema>;

// ============================================================================
// Risk Limits
// ============================================================================

export const riskLimitsSchema = z.object({
  maxPositionSize: z.number().int().positive().optional(), // Max shares per instrument
  maxPortfolioExposure: z.number().int().min(0).max(100).optional(), // Percentage
  maxDailyLoss: z.number().int().optional(), // In minor units
  maxOrdersPerDay: z.number().int().positive().optional(),
  maxStrategyAllocation: z.number().int().min(0).max(100).optional(), // Percentage
  minUnallocatedCash: z.number().int().optional(), // In minor units
  dataStalenessSeconds: z.number().int().positive().optional(),
  maxDrawdown: z.number().int().min(0).max(100).optional(), // Percentage
  stopOnDrawdown: z.boolean().default(false),
});
export type RiskLimits = z.infer<typeof riskLimitsSchema>;

export const riskCheckResultSchema = z.object({
  passed: z.boolean(),
  violations: z.array(
    z.object({
      rule: z.string(),
      message: z.string(),
      current: z.unknown(),
      limit: z.unknown(),
    })
  ),
});
export type RiskCheckResult = z.infer<typeof riskCheckResultSchema>;

// ============================================================================
// Strategy Lifecycle
// ============================================================================

export const strategyStatusSchema = z.enum([
  'DRAFT',
  'BACKTESTED',
  'ENABLED',
  'RUNNING',
  'PAUSED',
  'STOPPED',
  'FAILED',
]);
export type StrategyStatus = z.infer<typeof strategyStatusSchema>;

export const strategySchema = z.object({
  strategyId: z.string().uuid(),
  portfolioId: z.string().uuid(),
  name: z.string().max(100),
  parameters: z.record(z.string(), z.unknown()),
  status: strategyStatusSchema,
  riskLimits: riskLimitsSchema,
  createdAt: z.coerce.date(),
  updatedAt: z.coerce.date(),
  startedAt: z.coerce.date().optional(),
  pausedAt: z.coerce.date().optional(),
  stoppedAt: z.coerce.date().optional(),
  failedAt: z.coerce.date().optional(),
  failureReason: z.string().optional(),
});
export type Strategy = z.infer<typeof strategySchema>;

export const signalSchema = z.object({
  signalId: z.string().uuid(),
  strategyId: z.string().uuid(),
  instrument: instrumentIdSchema,
  side: orderSideSchema,
  quantity: z.number().int().positive(),
  reason: z.string(),
  timestamp: z.coerce.date(),
  confidence: z.number().min(0).max(1).optional(),
});
export type Signal = z.infer<typeof signalSchema>;

// ============================================================================
// Backtest Configuration
// ============================================================================

export const backtestConfigSchema = z.object({
  configId: z.string().uuid(),
  strategyId: z.string().uuid(),
  startDate: z.string().datetime(),
  endDate: z.string().datetime(),
  initialCash: z.number().int(),
  commissionRate: z.number().min(0).max(1), // As decimal (e.g., 0.001 = 0.1%)
  slippageBps: z.number().min(0), // Basis points
  dataSources: z.array(z.string()),
});
export type BacktestConfig = z.infer<typeof backtestConfigSchema>;

export const backtestResultSchema = z.object({
  resultId: z.string().uuid(),
  configId: z.string().uuid(),
  totalReturn: z.number(), // Percentage
  annualizedReturn: z.number().optional(),
  maxDrawdown: z.number(), // Percentage
  sharpeRatio: z.number().optional(),
  winRate: z.number().min(0).max(1).optional(),
  profitFactor: z.number().optional(),
  totalTrades: z.number().int().nonnegative(),
  winningTrades: z.number().int().nonnegative(),
  losingTrades: z.number().int().nonnegative(),
  averageWin: z.number().optional(),
  averageLoss: z.number().optional(),
  equityCurve: z.array(z.object({ timestamp: z.iso.datetime(), value: z.number() })),
  trades: z.array(z.unknown()),
  startedAt: z.iso.datetime(),
  completedAt: z.iso.datetime(),
});
export type BacktestResult = z.infer<typeof backtestResultSchema>;

// ============================================================================
// Database Schema
// ============================================================================

export function initDatabase(db: Database) {
  const sql = `
    CREATE TABLE IF NOT EXISTS portfolios (
      portfolio_id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      base_currency TEXT NOT NULL DEFAULT 'INR',
      initial_cash INTEGER NOT NULL,
      current_cash INTEGER NOT NULL,
      total_equity REAL NOT NULL,
      status TEXT NOT NULL DEFAULT 'ACTIVE',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS positions (
      portfolio_id TEXT NOT NULL,
      exchange TEXT NOT NULL,
      symbol TEXT NOT NULL,
      instrument_type TEXT NOT NULL,
      quantity INTEGER NOT NULL,
      average_cost REAL NOT NULL,
      market_value REAL NOT NULL,
      unrealized_pnl INTEGER NOT NULL,
      realized_pnl INTEGER NOT NULL,
      last_updated TEXT NOT NULL,
      PRIMARY KEY (portfolio_id, exchange, symbol, instrument_type)
    );

    CREATE TABLE IF NOT EXISTS orders (
      order_id TEXT PRIMARY KEY,
      portfolio_id TEXT NOT NULL,
      exchange TEXT NOT NULL,
      symbol TEXT NOT NULL,
      instrument_type TEXT NOT NULL,
      side TEXT NOT NULL,
      order_type TEXT NOT NULL,
      quantity INTEGER NOT NULL,
      limit_price REAL,
      status TEXT NOT NULL,
      filled_quantity INTEGER NOT NULL DEFAULT 0,
      average_fill_price REAL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      submitted_at TEXT,
      filled_at TEXT,
      canceled_at TEXT,
      rejected_reason TEXT,
      idempotency_key TEXT UNIQUE NOT NULL,
      time_in_force TEXT NOT NULL DEFAULT 'DAY',
      expire_at TEXT,
      FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id)
    );

    CREATE INDEX IF NOT EXISTS idx_orders_portfolio ON orders(portfolio_id);
    CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
    CREATE INDEX IF NOT EXISTS idx_orders_idempotency ON orders(idempotency_key);

    CREATE TABLE IF NOT EXISTS fills (
      fill_id TEXT PRIMARY KEY,
      order_id TEXT NOT NULL,
      exchange TEXT NOT NULL,
      symbol TEXT NOT NULL,
      instrument_type TEXT NOT NULL,
      side TEXT NOT NULL,
      quantity INTEGER NOT NULL,
      fill_price REAL NOT NULL,
      commission INTEGER NOT NULL,
      taxes INTEGER NOT NULL,
      timestamp TEXT NOT NULL,
      exchange_fill_id TEXT,
      FOREIGN KEY (order_id) REFERENCES orders(order_id)
    );

    CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
    CREATE INDEX IF NOT EXISTS idx_fills_timestamp ON fills(timestamp);

    CREATE TABLE IF NOT EXISTS cash_transactions (
      transaction_id TEXT PRIMARY KEY,
      portfolio_id TEXT NOT NULL,
      type TEXT NOT NULL,
      amount INTEGER NOT NULL,
      balance_after INTEGER NOT NULL,
      timestamp TEXT NOT NULL,
      description TEXT NOT NULL,
      reference_id TEXT,
      FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id)
    );

    CREATE INDEX IF NOT EXISTS idx_cash_portfolio ON cash_transactions(portfolio_id);
    CREATE INDEX IF NOT EXISTS idx_cash_timestamp ON cash_transactions(timestamp);

    CREATE TABLE IF NOT EXISTS strategies (
      strategy_id TEXT PRIMARY KEY,
      portfolio_id TEXT NOT NULL,
      name TEXT NOT NULL,
      parameters TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'DRAFT',
      risk_limits TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      started_at TEXT,
      paused_at TEXT,
      stopped_at TEXT,
      failed_at TEXT,
      failure_reason TEXT,
      FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id)
    );

    CREATE INDEX IF NOT EXISTS idx_strategies_portfolio ON strategies(portfolio_id);
    CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status);

    CREATE TABLE IF NOT EXISTS signals (
      signal_id TEXT PRIMARY KEY,
      strategy_id TEXT NOT NULL,
      exchange TEXT NOT NULL,
      symbol TEXT NOT NULL,
      instrument_type TEXT NOT NULL,
      side TEXT NOT NULL,
      quantity INTEGER NOT NULL,
      reason TEXT NOT NULL,
      timestamp TEXT NOT NULL,
      confidence REAL,
      FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id)
    );

    CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_id);
    CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);

    CREATE TABLE IF NOT EXISTS backtest_configs (
      config_id TEXT PRIMARY KEY,
      strategy_id TEXT NOT NULL,
      start_date TEXT NOT NULL,
      end_date TEXT NOT NULL,
      initial_cash INTEGER NOT NULL,
      commission_rate REAL NOT NULL,
      slippage_bps REAL NOT NULL,
      data_sources TEXT NOT NULL,
      FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id)
    );

    CREATE TABLE IF NOT EXISTS backtest_results (
      result_id TEXT PRIMARY KEY,
      config_id TEXT NOT NULL,
      total_return REAL NOT NULL,
      annualized_return REAL,
      max_drawdown REAL NOT NULL,
      sharpe_ratio REAL,
      win_rate REAL,
      profit_factor REAL,
      total_trades INTEGER NOT NULL,
      winning_trades INTEGER NOT NULL,
      losing_trades INTEGER NOT NULL,
      average_win REAL,
      average_loss REAL,
      equity_curve TEXT NOT NULL,
      trades TEXT NOT NULL,
      started_at TEXT NOT NULL,
      completed_at TEXT NOT NULL,
      FOREIGN KEY (config_id) REFERENCES backtest_configs(config_id)
    );

    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
      snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
      portfolio_id TEXT NOT NULL,
      timestamp TEXT NOT NULL,
      total_equity REAL NOT NULL,
      cash_balance INTEGER NOT NULL,
      exposure REAL NOT NULL,
      daily_pnl INTEGER NOT NULL,
      total_pnl INTEGER NOT NULL,
      FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id)
    );

    CREATE INDEX IF NOT EXISTS idx_snapshots_portfolio ON portfolio_snapshots(portfolio_id);
    CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON portfolio_snapshots(timestamp);

    CREATE TABLE IF NOT EXISTS risk_violations (
      violation_id INTEGER PRIMARY KEY AUTOINCREMENT,
      strategy_id TEXT NOT NULL,
      order_id TEXT,
      rule TEXT NOT NULL,
      message TEXT NOT NULL,
      current_value TEXT,
      limit_value TEXT,
      timestamp TEXT NOT NULL,
      FOREIGN KEY (strategy_id) REFERENCES strategies(strategy_id),
      FOREIGN KEY (order_id) REFERENCES orders(order_id)
    );

    CREATE INDEX IF NOT EXISTS idx_violations_strategy ON risk_violations(strategy_id);
    CREATE INDEX IF NOT EXISTS idx_violations_timestamp ON risk_violations(timestamp);
  `;
  db.exec(sql);
}

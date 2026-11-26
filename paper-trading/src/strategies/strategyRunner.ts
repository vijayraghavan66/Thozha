import { v4 as uuidv4 } from 'uuid';
import { z } from 'zod';
import type { Database } from 'better-sqlite3';
import { StrategyRepository, SignalRepository } from '../db/repositories';
import { RiskEngine } from '../risk/riskEngine';
import { VirtualTradingEngine } from '../engine/virtualEngine';
import type {
  Strategy,
  Signal,
  Order,
  Quote,
  Portfolio,
  Position,
  StrategyStatus,
  BacktestConfig,
  BacktestResult,
} from '../types';
import { backtestConfigSchema, backtestResultSchema } from '../types';

// ============================================================================
// Strategy Runner
// Manages strategy lifecycle, signal generation, and autonomous execution
// ============================================================================

export interface StrategyRunnerConfig {
  checkIntervalMs: number;
  maxSignalsPerCycle: number;
}

export class StrategyRunner {
  private strategyRepo: StrategyRepository;
  private signalRepo: SignalRepository;
  private riskEngine: RiskEngine;
  private tradingEngine: VirtualTradingEngine;
  private running: boolean = false;
  private intervalId: NodeJS.Timeout | null = null;

  constructor(
    private db: Database,
    private config: StrategyRunnerConfig
  ) {
    this.strategyRepo = new StrategyRepository(db);
    this.signalRepo = new SignalRepository(db);
    this.riskEngine = new RiskEngine(db);
    this.tradingEngine = new VirtualTradingEngine(db, {
      commissionRate: 0.001,
      slippageBps: 10,
      minTick: 0.05,
    });
  }

  /**
   * Start the autonomous execution loop
   */
  start(): void {
    if (this.running) {
      return;
    }

    this.running = true;
    this.intervalId = setInterval(() => this.cycle(), this.config.checkIntervalMs);
  }

  /**
   * Stop the execution loop
   */
  stop(): void {
    this.running = false;
    if (this.intervalId) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }
  }

  /**
   * One execution cycle
   */
  private cycle(): void {
    if (!this.running) {
      return;
    }

    try {
      // Get all running strategies
      const strategies = this.strategyRepo.listByStatus('RUNNING');

      for (const strategy of strategies) {
        this.processStrategy(strategy);
      }
    } catch (error) {
      console.error('Strategy cycle error:', error);
    }
  }

  /**
   * Process a single strategy
   */
  private processStrategy(strategy: Strategy): void {
    // Generate signals based on strategy logic
    const signals = this.generateSignals(strategy);

    for (const signal of signals) {
      this.processSignal(signal, strategy);
    }
  }

  /**
   * Generate trading signals for a strategy
   */
  private generateSignals(strategy: Strategy): Signal[] {
    const signals: Signal[] = [];

    // Simple moving average crossover example
    if (strategy.parameters.movingAveragePeriod) {
      const period = strategy.parameters.movingAveragePeriod as number;
      // Would fetch market data and calculate signals here
      // For now, we just log that the strategy ran
      console.log(`Strategy ${strategy.name} ran with period ${period}`);
    }

    return signals;
  }

  /**
   * Process a signal and potentially create an order
   */
  private processSignal(signal: Signal, strategy: Strategy): void {
    // Get portfolio
    const portfolioId = strategy.portfolioId;
    // Would fetch portfolio, positions, and current quote here

    // Check risk limits
    // const riskResult = this.riskEngine.checkOrder(...);
    // if (!riskResult.passed) {
    //   this.recordRiskViolation(strategy, signal, riskResult);
    //   return;
    // }

    // Submit order
    // const order = this.tradingEngine.submitOrder(...);
  }

  /**
   * Create a new strategy
   */
  createStrategy(
    portfolioId: string,
    name: string,
    parameters: Record<string, unknown>,
    riskLimits: Partial<Strategy['riskLimits']> = {}
  ): Strategy {
    const now = new Date();
    const strategy: Strategy = {
      strategyId: uuidv4(),
      portfolioId,
      name,
      parameters,
      status: 'DRAFT',
      riskLimits: {
        maxPositionSize: 10000,
        maxPortfolioExposure: 95,
        maxDailyLoss: 500000,
        maxOrdersPerDay: 50,
        maxStrategyAllocation: 100,
        minUnallocatedCash: 100000,
        dataStalenessSeconds: 300,
        maxDrawdown: 20,
        stopOnDrawdown: false,
        ...riskLimits,
      },
      createdAt: now,
      updatedAt: now,
    };

    this.strategyRepo.create(strategy);
    return strategy;
  }

  /**
   * Enable a strategy for execution
   */
  enableStrategy(strategyId: string): { enabled: Strategy } | { notFound: true } | { alreadyEnabled: true } {
    const strategy = this.strategyRepo.findById(strategyId);
    if (!strategy) {
      return { notFound: true };
    }

    if (strategy.status === 'ENABLED' || strategy.status === 'RUNNING') {
      return { alreadyEnabled: true };
    }

    const updated = {
      ...strategy,
      status: 'ENABLED' as const,
      startedAt: new Date(),
      updatedAt: new Date(),
    };
    this.strategyRepo.update(updated);
    return { enabled: updated };
  }

  /**
   * Pause a running strategy
   */
  pauseStrategy(strategyId: string): { paused: Strategy } | { notFound: true } | { notRunning: true } {
    const strategy = this.strategyRepo.findById(strategyId);
    if (!strategy) {
      return { notFound: true };
    }

    if (strategy.status !== 'RUNNING' && strategy.status !== 'ENABLED') {
      return { notRunning: true };
    }

    const updated = {
      ...strategy,
      status: 'PAUSED' as const,
      pausedAt: new Date(),
      updatedAt: new Date(),
    };
    this.strategyRepo.update(updated);
    return { paused: updated };
  }

  /**
   * Stop a strategy permanently
   */
  stopStrategy(strategyId: string): { stopped: Strategy } | { notFound: true } {
    const strategy = this.strategyRepo.findById(strategyId);
    if (!strategy) {
      return { notFound: true };
    }

    const updated = {
      ...strategy,
      status: 'STOPPED' as const,
      stoppedAt: new Date(),
      updatedAt: new Date(),
    };
    this.strategyRepo.update(updated);
    return { stopped: updated };
  }

  /**
   * Mark a strategy as failed
   */
  failStrategy(strategyId: string, reason: string): { failed: Strategy } | { notFound: true } {
    const strategy = this.strategyRepo.findById(strategyId);
    if (!strategy) {
      return { notFound: true };
    }

    const updated = {
      ...strategy,
      status: 'FAILED' as const,
      failedAt: new Date(),
      failureReason: reason,
      updatedAt: new Date(),
    };
    this.strategyRepo.update(updated);
    return { failed: updated };
  }

  /**
   * Run a backtest for a strategy
   */
  runBacktest(
    strategyId: string,
    startDate: Date,
    endDate: Date,
    initialCash: number,
    commissionRate: number,
    slippageBps: number,
    dataSources: string[]
  ): BacktestResult {
    const config: BacktestConfig = {
      configId: uuidv4(),
      strategyId,
      startDate,
      endDate,
      initialCash,
      commissionRate,
      slippageBps,
      dataSources,
    };

    // Save config
    this.strategyRepo.db.exec(`
      INSERT INTO backtest_configs (
        config_id, strategy_id, start_date, end_date, initial_cash,
        commission_rate, slippage_bps, data_sources
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `.replace(/\?/g, () => '?')); // Placeholder - would use proper bindings

    // Would run actual backtest logic here
    const result: BacktestResult = {
      resultId: uuidv4(),
      configId: config.configId,
      totalReturn: 0,
      maxDrawdown: 0,
      totalTrades: 0,
      winningTrades: 0,
      losingTrades: 0,
      equityCurve: [],
      trades: [],
      startedAt: new Date(),
      completedAt: new Date(),
    };

    return result;
  }

  /**
   * Get strategy signals
   */
  getSignals(strategyId: string): Signal[] {
    return this.signalRepo.findByStrategy(strategyId);
  }

  /**
   * List strategies by status
   */
  listByStatus(status: StrategyStatus): Strategy[] {
    const stmt = this.strategyRepo.db.prepare('SELECT * FROM strategies WHERE status = ?');
    const rows = stmt.all(status) as any[];
    return rows.map((row) => ({
      ...row,
      parameters: JSON.parse(row.parameters),
      riskLimits: JSON.parse(row.risk_limits),
      createdAt: new Date(row.created_at),
      updatedAt: new Date(row.updated_at),
      startedAt: row.started_at ? new Date(row.started_at) : undefined,
      pausedAt: row.paused_at ? new Date(row.paused_at) : undefined,
      stoppedAt: row.stopped_at ? new Date(row.stopped_at) : undefined,
      failedAt: row.failed_at ? new Date(row.failed_at) : undefined,
    }));
  }
}

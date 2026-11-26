import { v4 as uuidv4 } from 'uuid';
import type { Database } from '../db/sqlAdapter';
import { RiskViolationRepository } from '../db/repositories';
import type {
  RiskLimits,
  RiskCheckResult,
  Order,
  Quote,
  Portfolio,
  Position,
  Strategy,
} from '../types';

// ============================================================================
// Risk Engine
// Deterministic risk checks that run server-side before order execution
// ============================================================================

export class RiskEngine {
  private violationRepo: RiskViolationRepository;

  constructor(private db: Database) {
    this.violationRepo = new RiskViolationRepository(db);
  }

  /**
   * Check an order against all risk limits
   */
  checkOrder(
    order: Order,
    portfolio: Portfolio,
    positions: Position[],
    quote: Quote,
    strategy: Strategy | null,
    todayDate: Date
  ): RiskCheckResult {
    const violations: RiskCheckResult['violations'] = [];
    const limits = strategy?.riskLimits ?? this.getDefaultLimits();

    // 1. Data staleness check
    const staleness = this.checkDataStaleness(quote, limits.dataStalenessSeconds);
    if (!staleness.passed) {
      violations.push(...staleness.violations);
    }

    // 2. Position size limit
    const positionCheck = this.checkPositionSize(order, positions, portfolio, limits.maxPositionSize);
    if (!positionCheck.passed) {
      violations.push(...positionCheck.violations);
    }

    // 3. Portfolio exposure limit
    const exposureCheck = this.checkPortfolioExposure(order, portfolio, positions, limits.maxPortfolioExposure);
    if (!exposureCheck.passed) {
      violations.push(...exposureCheck.violations);
    }

    // 4. Daily loss limit
    const dailyLossCheck = this.checkDailyLoss(portfolio, todayDate, limits.maxDailyLoss);
    if (!dailyLossCheck.passed) {
      violations.push(...dailyLossCheck.violations);
    }

    // 5. Orders per day limit
    const ordersPerDayCheck = this.checkOrdersPerDay(portfolio.portfolioId, todayDate);
    if (!ordersPerDayCheck.passed) {
      violations.push(...ordersPerDayCheck.violations);
    }

    // 6. Strategy allocation limit
    if (strategy) {
      const strategyCheck = this.checkStrategyAllocation(strategy, portfolio, positions, limits.maxStrategyAllocation);
      if (!strategyCheck.passed) {
        violations.push(...strategyCheck.violations);
      }
    }

    // 7. Minimum unallocated cash
    const minCashCheck = this.checkMinUnallocatedCash(portfolio, limits.minUnallocatedCash);
    if (!minCashCheck.passed) {
      violations.push(...minCashCheck.violations);
    }

    // 8. Drawdown stop
    const drawdownCheck = this.checkDrawdown(portfolio, limits.maxDrawdown, limits.stopOnDrawdown);
    if (!drawdownCheck.passed) {
      violations.push(...drawdownCheck.violations);
    }

    return {
      passed: violations.length === 0,
      violations,
    };
  }

  /**
   * Record a risk violation
   */
  recordViolation(
    strategyId: string,
    orderId: string | undefined,
    rule: string,
    message: string,
    currentValue: unknown,
    limitValue: unknown
  ): void {
    this.violationRepo.create({
      strategyId,
      orderId,
      rule,
      message,
      currentValue,
      limitValue,
      timestamp: new Date(),
    });
  }

  /**
   * Check data staleness
   */
  private checkDataStaleness(quote: Quote, maxStalenessSeconds: number | undefined): RiskCheckResult {
    if (!maxStalenessSeconds) {
      return { passed: true, violations: [] };
    }

    const now = new Date();
    const quoteTime = new Date(quote.timestamp);
    const stalenessSeconds = (now.getTime() - quoteTime.getTime()) / 1000;

    if (stalenessSeconds > maxStalenessSeconds) {
      return {
        passed: false,
        violations: [
          {
            rule: 'DATA_STALENESS',
            message: `Market data is ${stalenessSeconds.toFixed(0)} seconds old (limit: ${maxStalenessSeconds}s)`,
            current: stalenessSeconds,
            limit: maxStalenessSeconds,
          },
        ],
      };
    }

    return { passed: true, violations: [] };
  }

  /**
   * Check position size limit
   */
  private checkPositionSize(
    order: Order,
    positions: Position[],
    portfolio: Portfolio,
    maxPositionSize: number | undefined
  ): RiskCheckResult {
    if (!maxPositionSize) {
      return { passed: true, violations: [] };
    }

    const existingPosition = positions.find(
      (p) =>
        p.instrument.exchange === order.instrument.exchange &&
        p.instrument.symbol === order.instrument.symbol &&
        p.instrument.instrumentType === order.instrument.instrumentType
    );

    const currentQty = existingPosition?.quantity ?? 0;
    const newQty = order.side === 'BUY' ? currentQty + order.quantity : Math.abs(currentQty - order.quantity);

    if (newQty > maxPositionSize) {
      return {
        passed: false,
        violations: [
          {
            rule: 'MAX_POSITION_SIZE',
            message: `Position would be ${newQty} shares (limit: ${maxPositionSize})`,
            current: newQty,
            limit: maxPositionSize,
          },
        ],
      };
    }

    return { passed: true, violations: [] };
  }

  /**
   * Check portfolio exposure limit
   */
  private checkPortfolioExposure(
    order: Order,
    portfolio: Portfolio,
    positions: Position[],
    maxExposurePercent: number | undefined
  ): RiskCheckResult {
    if (!maxExposurePercent) {
      return { passed: true, violations: [] };
    }

    // Calculate current exposure (sum of position market values)
    let currentExposure = 0;
    for (const pos of positions) {
      if (pos.quantity > 0) {
        currentExposure += pos.marketValue;
      }
    }

    // Add order value if buy
    if (order.side === 'BUY' && order.orderType === 'MARKET') {
      currentExposure += order.quantity * 100; // Rough estimate
    }

    const exposurePercent = (currentExposure / portfolio.totalEquity) * 100;

    if (exposurePercent > maxExposurePercent) {
      return {
        passed: false,
        violations: [
          {
            rule: 'MAX_PORTFOLIO_EXPOSURE',
            message: `Exposure would be ${exposurePercent.toFixed(1)}% (limit: ${maxExposurePercent}%)`,
            current: exposurePercent,
            limit: maxExposurePercent,
          },
        ],
      };
    }

    return { passed: true, violations: [] };
  }

  /**
   * Check daily loss limit
   */
  private checkDailyLoss(
    portfolio: Portfolio,
    today: Date,
    maxDailyLoss: number | undefined
  ): RiskCheckResult {
    if (!maxDailyLoss) {
      return { passed: true, violations: [] };
    }

    // Calculate daily P&L (simplified - would need cash transaction history)
    // For now, we check if portfolio is already down
    const dailyPnl = 0; // Placeholder - would query cash_transactions

    if (dailyPnl < -maxDailyLoss) {
      return {
        passed: false,
        violations: [
          {
            rule: 'MAX_DAILY_LOSS',
            message: `Daily loss of ₹${(dailyPnl / 100).toFixed(2)} exceeds limit of ₹${(maxDailyLoss / 100).toFixed(2)}`,
            current: dailyPnl,
            limit: -maxDailyLoss,
          },
        ],
      };
    }

    return { passed: true, violations: [] };
  }

  /**
   * Check orders per day limit
   */
  private checkOrdersPerDay(portfolioId: string, today: Date): RiskCheckResult {
    const maxOrdersPerDay = 50; // Default limit
    const { FillRepository } = require('../db/repositories') as typeof import('../db/repositories');
    const fillRepo = new FillRepository(this.db);
    const orderCount = fillRepo.countByDate(portfolioId, today);

    if (orderCount >= maxOrdersPerDay) {
      return {
        passed: false,
        violations: [
          {
            rule: 'MAX_ORDERS_PER_DAY',
            message: `Order count for today is ${orderCount} (limit: ${maxOrdersPerDay})`,
            current: orderCount,
            limit: maxOrdersPerDay,
          },
        ],
      };
    }

    return { passed: true, violations: [] };
  }

  /**
   * Check strategy allocation limit
   */
  private checkStrategyAllocation(
    strategy: Strategy,
    portfolio: Portfolio,
    positions: Position[],
    maxAllocationPercent: number | undefined
  ): RiskCheckResult {
    if (!maxAllocationPercent) {
      return { passed: true, violations: [] };
    }

    // Calculate strategy's current allocation (simplified - would track per-strategy positions)
    const strategyAllocation = 0; // Placeholder

    if (strategyAllocation > maxAllocationPercent) {
      return {
        passed: false,
        violations: [
          {
            rule: 'MAX_STRATEGY_ALLOCATION',
            message: `Strategy allocation is ${strategyAllocation.toFixed(1)}% (limit: ${maxAllocationPercent}%)`,
            current: strategyAllocation,
            limit: maxAllocationPercent,
          },
        ],
      };
    }

    return { passed: true, violations: [] };
  }

  /**
   * Check minimum unallocated cash
   */
  private checkMinUnallocatedCash(
    portfolio: Portfolio,
    minCash: number | undefined
  ): RiskCheckResult {
    if (!minCash) {
      return { passed: true, violations: [] };
    }

    if (portfolio.currentCash < minCash) {
      return {
        passed: false,
        violations: [
          {
            rule: 'MIN_UNALLOCATED_CASH',
            message: `Cash balance ₹${(portfolio.currentCash / 100).toFixed(2)} below minimum of ₹${(minCash / 100).toFixed(2)}`,
            current: portfolio.currentCash,
            limit: minCash,
          },
        ],
      };
    }

    return { passed: true, violations: [] };
  }

  /**
   * Check drawdown limit
   */
  private checkDrawdown(
    portfolio: Portfolio,
    maxDrawdownPercent: number | undefined,
    stopOnDrawdown: boolean
  ): RiskCheckResult {
    if (!maxDrawdownPercent || !stopOnDrawdown) {
      return { passed: true, violations: [] };
    }

    // Calculate drawdown (simplified - would need equity curve history)
    const currentDrawdown = 0; // Placeholder

    if (currentDrawdown > maxDrawdownPercent) {
      return {
        passed: false,
        violations: [
          {
            rule: 'MAX_DRAWDOWN',
            message: `Drawdown of ${currentDrawdown.toFixed(1)}% exceeds limit of ${maxDrawdownPercent}%`,
            current: currentDrawdown,
            limit: maxDrawdownPercent,
          },
        ],
      };
    }

    return { passed: true, violations: [] };
  }

  /**
   * Get default risk limits
   */
  private getDefaultLimits(): RiskLimits {
    return {
      maxPositionSize: 10000,
      maxPortfolioExposure: 95,
      maxDailyLoss: 500000, // ₹5000 in paise
      maxOrdersPerDay: 50,
      maxStrategyAllocation: 100,
      minUnallocatedCash: 100000, // ₹1000 in paise
      dataStalenessSeconds: 300, // 5 minutes
      maxDrawdown: 20,
      stopOnDrawdown: false,
    };
  }
}

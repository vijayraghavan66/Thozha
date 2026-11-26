import { v4 as uuidv4 } from 'uuid';
import type { Database } from 'better-sqlite3';
import {
  OrderRepository,
  FillRepository,
  PositionRepository,
  CashTransactionRepository,
  PortfolioRepository,
} from '../db/repositories';
import type {
  Order,
  Fill,
  Position,
  CashTransaction,
  Portfolio,
  Quote,
  InstrumentId,
  OrderSide,
  OrderType,
  Currency,
} from '../types';
import { currencySchema } from '../types';

// ============================================================================
// Virtual Trading Engine
// Handles deterministic simulated order execution with fees, slippage, and accounting
// ============================================================================

export interface EngineConfig {
  commissionRate: number; // As decimal (e.g., 0.001 = 0.1%)
  slippageBps: number; // Basis points (e.g., 10 = 0.1%)
  minTick: number; // Minimum price increment
}

export interface FillResult {
  filled: boolean;
  fills: Fill[];
  remainingQuantity: number;
  averageFillPrice: number;
  totalCommission: Currency;
  totalTaxes: Currency;
}

export interface RejectionReason {
  reason: string;
  code: string;
}

export class VirtualTradingEngine {
  private orderRepo: OrderRepository;
  private fillRepo: FillRepository;
  private positionRepo: PositionRepository;
  private cashRepo: CashTransactionRepository;
  private portfolioRepo: PortfolioRepository;

  constructor(
    private db: Database,
    private config: EngineConfig
  ) {
    this.orderRepo = new OrderRepository(db);
    this.fillRepo = new FillRepository(db);
    this.positionRepo = new PositionRepository(db);
    this.cashRepo = new CashTransactionRepository(db);
    this.portfolioRepo = new PortfolioRepository(db);
  }

  /**
   * Submit a new order with idempotency guarantee
   */
  submitOrder(
    portfolioId: string,
    instrument: InstrumentId,
    side: OrderSide,
    orderType: OrderType,
    quantity: number,
    limitPrice?: number,
    idempotencyKey?: string
  ): { order: Order } | { rejected: RejectionReason } {
    const finalKey = idempotencyKey ?? uuidv4();

    // Check for duplicate submission
    const existing = this.orderRepo.findByPortfolioAndIdempotency(portfolioId, finalKey);
    if (existing) {
      return { rejected: { reason: 'Duplicate order submission', code: 'DUPLICATE_ORDER' } };
    }

    const portfolio = this.portfolioRepo.findById(portfolioId);
    if (!portfolio) {
      return { rejected: { reason: 'Portfolio not found', code: 'PORTFOLIO_NOT_FOUND' } };
    }

    if (portfolio.status !== 'ACTIVE') {
      return { rejected: { reason: 'Portfolio is not active', code: 'PORTFOLIO_INACTIVE' } };
    }

    // Validate quantity
    if (quantity <= 0) {
      return { rejected: { reason: 'Quantity must be positive', code: 'INVALID_QUANTITY' } };
    }

    // Validate limit price for limit orders
    if (orderType === 'LIMIT' && (!limitPrice || limitPrice <= 0)) {
      return { rejected: { reason: 'Limit price required for limit orders', code: 'INVALID_LIMIT_PRICE' } };
    }

    const orderId = uuidv4();
    const now = new Date();

    const order: Order = {
      orderId,
      portfolioId,
      instrument,
      side,
      orderType,
      quantity,
      limitPrice,
      status: 'PENDING',
      filledQuantity: 0,
      createdAt: now,
      updatedAt: now,
      idempotencyKey: finalKey,
      timeInForce: 'DAY',
    };

    this.orderRepo.create(order);

    return { order };
  }

  /**
   * Execute an order against a market quote
   */
  executeOrder(orderId: string, quote: Quote): { filled: FillResult } | { partiallyFilled: FillResult } | { notFilled: RejectionReason } {
    const order = this.orderRepo.findById(orderId);
    if (!order) {
      return { notFilled: { reason: 'Order not found', code: 'ORDER_NOT_FOUND' } };
    }

    if (order.status === 'FILLED' || order.status === 'CANCELED' || order.status === 'REJECTED' || order.status === 'EXPIRED') {
      return { notFilled: { reason: 'Order already completed', code: 'ORDER_ALREADY_COMPLETED' } };
    }

    if (order.status !== 'PENDING' && order.status !== 'SUBMITTED') {
      return { notFilled: { reason: 'Order not in executable state', code: 'INVALID_ORDER_STATE' } };
    }

    // Check instrument match
    if (
      order.instrument.exchange !== quote.instrument.exchange ||
      order.instrument.symbol !== quote.instrument.symbol ||
      order.instrument.instrumentType !== quote.instrument.instrumentType
    ) {
      return { notFilled: { reason: 'Instrument mismatch', code: 'INSTRUMENT_MISMATCH' } };
    }

    // Calculate execution price with slippage
    let executionPrice = this.calculateExecutionPrice(order, quote);
    if (!executionPrice) {
      return { notFilled: { reason: 'Order conditions not met', code: 'CONDITION_NOT_MET' } };
    }

    // Round to tick size
    executionPrice = this.applyTickSize(executionPrice);

    const fillQty = order.quantity - order.filledQuantity;
    const portfolio = this.portfolioRepo.findById(order.portfolioId)!;

    // Calculate costs
    const grossValue = executionPrice * fillQty;
    const commission = Math.round(grossValue * this.config.commissionRate * 100); // Convert to paise
    const taxes = Math.round(grossValue * 0.0001 * 100); // Simplified STT at 0.01%

    const totalCost = grossValue + (commission + taxes) / 100; // Convert paise back to INR

    // Validate cash for buys
    if (order.side === 'BUY') {
      if (portfolio.currentCash < Math.floor(totalCost * 100)) {
        return { notFilled: { reason: 'Insufficient cash', code: 'INSUFFICIENT_CASH' } };
      }
    }

    // Validate holdings for sells
    if (order.side === 'SELL') {
      const position = this.positionRepo.findByPortfolioAndInstrument(order.portfolioId, order.instrument);
      const availableQty = position ? Math.max(0, position.quantity) : 0;
      if (availableQty < fillQty) {
        return { notFilled: { reason: 'Insufficient holdings', code: 'INSUFFICIENT_HOLDINGS' } };
      }
    }

    // Execute the fill
    const now = new Date();
    const fillId = uuidv4();
    const exchangeFillId = `EX-${uuidv4().replace(/-/g, '').slice(0, 12)}`;

    const fill: Fill = {
      fillId,
      orderId,
      instrument: order.instrument,
      side: order.side,
      quantity: fillQty,
      fillPrice: executionPrice,
      commission: { minorUnits: commission },
      taxes: { minorUnits: taxes },
      timestamp: now,
      exchangeFillId,
    };

    this.fillRepo.create(fill);

    // Update order status
    const updatedOrder = {
      ...order,
      filledQuantity: order.quantity,
      averageFillPrice: executionPrice,
      status: 'FILLED' as const,
      filledAt: now,
      updatedAt: now,
    };
    this.orderRepo.update(updatedOrder);

    // Update position
    this.updatePosition(order.portfolioId, order.instrument, order.side, fillQty, executionPrice);

    // Update cash
    const cashChange = order.side === 'BUY' ? -Math.floor(totalCost * 100) : Math.floor((grossValue - commission / 100 - taxes / 100) * 100);
    const cashTx: CashTransaction = {
      transactionId: uuidv4(),
      portfolioId: order.portfolioId,
      type: 'FILL',
      amount: cashChange,
      balanceAfter: portfolio.currentCash + cashChange,
      timestamp: now,
      description: `${order.side} ${fillQty} ${order.instrument.symbol} @ ₹${executionPrice.toFixed(2)}`,
      referenceId: orderId,
    };
    this.cashRepo.create(cashTx);

    // Update portfolio cash
    portfolio.currentCash += cashChange;
    portfolio.updatedAt = now;
    this.portfolioRepo.update(portfolio);

    return {
      filled: {
        filled: true,
        fills: [fill],
        remainingQuantity: 0,
        averageFillPrice: executionPrice,
        totalCommission: { minorUnits: commission },
        totalTaxes: { minorUnits: taxes },
      },
    };
  }

  /**
   * Cancel an order
   */
  cancelOrder(orderId: string): { canceled: Order } | { notCancellable: RejectionReason } {
    const order = this.orderRepo.findById(orderId);
    if (!order) {
      return { notCancellable: { reason: 'Order not found', code: 'ORDER_NOT_FOUND' } };
    }

    if (order.status === 'FILLED' || order.status === 'CANCELED' || order.status === 'REJECTED' || order.status === 'EXPIRED') {
      return { notCancellable: { reason: 'Order already completed', code: 'ORDER_ALREADY_COMPLETED' } };
    }

    if (order.status === 'PARTIALLY_FILLED') {
      return { notCancellable: { reason: 'Partially filled orders cannot be fully canceled', code: 'PARTIALLY_FILLED' } };
    }

    const updatedOrder = {
      ...order,
      status: 'CANCELED' as const,
      canceledAt: new Date(),
      updatedAt: new Date(),
    };
    this.orderRepo.update(updatedOrder);

    return { canceled: updatedOrder };
  }

  /**
   * Update position after a fill
   */
  private updatePosition(
    portfolioId: string,
    instrument: InstrumentId,
    side: OrderSide,
    quantity: number,
    fillPrice: number
  ): void {
    const existing = this.positionRepo.findByPortfolioAndInstrument(portfolioId, instrument);
    const now = new Date();

    if (side === 'BUY') {
      if (existing) {
        // Update average cost
        const totalCost = existing.averageCost * existing.quantity + fillPrice * quantity;
        const newQty = existing.quantity + quantity;
        const newAvgCost = totalCost / newQty;

        const position: Position = {
          ...existing,
          quantity: newQty,
          averageCost: newAvgCost,
          lastUpdated: now,
        };
        this.positionRepo.upsert(position);
      } else {
        const position: Position = {
          portfolioId,
          instrument,
          quantity,
          averageCost: fillPrice,
          marketValue: fillPrice * quantity,
          unrealizedPnl: 0,
          realizedPnl: 0,
          lastUpdated: now,
        };
        this.positionRepo.upsert(position);
      }
    } else {
      // SELL
      if (existing && existing.quantity > 0) {
        const sellQty = Math.min(existing.quantity, quantity);
        const realizedPnl = (fillPrice - existing.averageCost) * sellQty * 100; // Convert to paise

        const newQty = existing.quantity - sellQty;

        if (newQty <= 0) {
          // Position closed
          const updated: Position = {
            ...existing,
            quantity: 0,
            averageCost: 0,
            marketValue: 0,
            unrealizedPnl: 0,
            realizedPnl: existing.realizedPnl + realizedPnl,
            lastUpdated: now,
          };
          this.positionRepo.upsert(updated);
        } else {
          const updated: Position = {
            ...existing,
            quantity: newQty,
            marketValue: existing.averageCost * newQty,
            unrealizedPnl: 0,
            realizedPnl: existing.realizedPnl + realizedPnl,
            lastUpdated: now,
          };
          this.positionRepo.upsert(updated);
        }
      }
    }
  }

  /**
   * Calculate execution price with slippage
   */
  private calculateExecutionPrice(order: Order, quote: Quote): number | null {
    const midPrice = (quote.bid + quote.ask) / 2;

    if (order.orderType === 'MARKET') {
      // Market order: execute at worst side + slippage
      const slippage = midPrice * (this.config.slippageBps / 10000);
      if (order.side === 'BUY') {
        return quote.ask + slippage;
      } else {
        return quote.bid - slippage;
      }
    } else {
      // Limit order: check if quote crosses limit
      if (order.side === 'BUY' && quote.ask <= (order.limitPrice || Infinity)) {
        return Math.min(order.limitPrice || midPrice, quote.ask);
      } else if (order.side === 'SELL' && quote.bid >= (order.limitPrice || 0)) {
        return Math.max(order.limitPrice || midPrice, quote.bid);
      }
      return null;
    }
  }

  /**
   * Apply tick size rounding
   */
  private applyTickSize(price: number): number {
    return Math.round(price / this.config.minTick) * this.config.minTick;
  }

  /**
   * Reconcile portfolio after restart (check for incomplete orders)
   */
  reconcilePortfolio(portfolioId: string): { incompleteOrders: Order[]; recoveredFills: Fill[] } {
    const incomplete = this.orderRepo.findByStatus(portfolioId, 'PENDING');
    const submitted = this.orderRepo.findByStatus(portfolioId, 'SUBMITTED');
    const incompleteOrders = [...incomplete, ...submitted];

    const fills: Fill[] = [];
    for (const order of incompleteOrders) {
      const orderFills = this.fillRepo.findByOrderId(order.orderId);
      fills.push(...orderFills);

      // If filled but order not updated, fix it
      if (order.filledQuantity > 0 && order.status !== 'FILLED') {
        const updated = {
          ...order,
          status: order.filledQuantity === order.quantity ? 'FILLED' : 'PARTIALLY_FILLED',
          filledAt: order.fills?.[order.fills.length - 1]?.timestamp,
          updatedAt: new Date(),
        };
        this.orderRepo.update(updated);
      }
    }

    return { incompleteOrders, recoveredFills: fills };
  }
}

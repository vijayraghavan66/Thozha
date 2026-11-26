// ============================================================================
// Paper Trading Engine API Client
// Connects the web UI to the authoritative persistent paper-trading engine
// ============================================================================

const API_BASE = '/api/paper';
const API_KEY_HEADER = 'x-api-key';

let apiKey: string | null = null;

export function setApiKey(key: string) {
  apiKey = key;
}

function getHeaders(): HeadersInit {
  if (!apiKey) {
    throw new Error('Paper trading API key not configured');
  }
  return {
    'Content-Type': 'application/json',
    [API_KEY_HEADER]: apiKey,
  };
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...getHeaders(),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: 'Unknown error', code: 'UNKNOWN' }));
    throw new PaperEngineError(error.error, error.code, response.status);
  }
  return response.json() as Promise<T>;
}

export class PaperEngineError extends Error {
  constructor(
    message: string,
    public code: string,
    public status: number
  ) {
    super(message);
    this.name = 'PaperEngineError';
  }
}

// Types
export interface PaperPortfolio {
  portfolioId: string;
  name: string;
  baseCurrency: 'INR';
  initialCash: number;
  currentCash: number;
  totalEquity: number;
  status: 'ACTIVE' | 'PAUSED' | 'CLOSED';
  createdAt: string;
  updatedAt: string;
}

export interface PaperPosition {
  portfolioId: string;
  instrument: {
    exchange: 'NSE' | 'BSE';
    symbol: string;
    instrumentType: 'EQUITY' | 'ETF' | 'INDEX';
  };
  quantity: number;
  averageCost: number;
  marketValue: number;
  unrealizedPnl: number;
  realizedPnl: number;
  lastUpdated: string;
}

export interface PaperOrder {
  orderId: string;
  portfolioId: string;
  instrument: {
    exchange: 'NSE' | 'BSE';
    symbol: string;
    instrumentType: 'EQUITY' | 'ETF' | 'INDEX';
  };
  side: 'BUY' | 'SELL';
  orderType: 'MARKET' | 'LIMIT';
  quantity: number;
  limitPrice?: number;
  status: 'PENDING' | 'SUBMITTED' | 'PARTIALLY_FILLED' | 'FILLED' | 'CANCELED' | 'REJECTED' | 'EXPIRED';
  filledQuantity: number;
  averageFillPrice?: number;
  createdAt: string;
  updatedAt: string;
  submittedAt?: string;
  filledAt?: string;
  canceledAt?: string;
  rejectedReason?: string;
  idempotencyKey: string;
  timeInForce: 'DAY' | 'GTC';
  expireAt?: string;
}

export interface PaperQuote {
  instrument: {
    exchange: 'NSE' | 'BSE';
    symbol: string;
    instrumentType: 'EQUITY' | 'ETF' | 'INDEX';
  };
  timestamp: string;
  bid: number;
  ask: number;
  last: number;
  volume: number;
  open: number;
  high: number;
  low: number;
  close: number;
  freshness: 'live' | 'delayed' | 'historical';
  source: string;
}

export interface PaperCashTransaction {
  transactionId: string;
  portfolioId: string;
  type: 'DEPOSIT' | 'WITHDRAWAL' | 'DIVIDEND' | 'COMMISSION' | 'TAX' | 'FILL';
  amount: number;
  balanceAfter: number;
  timestamp: string;
  description: string;
  referenceId?: string;
}

export interface RiskViolation {
  rule: string;
  message: string;
  current: unknown;
  limit: unknown;
}

// API Functions
export async function getPortfolio(): Promise<{ portfolio: PaperPortfolio }> {
  return request('/portfolio');
}

export async function getPositions(portfolioId: string): Promise<{ positions: PaperPosition[] }> {
  return request(`/positions?portfolioId=${encodeURIComponent(portfolioId)}`);
}

export async function getOrders(portfolioId: string): Promise<{ orders: PaperOrder[] }> {
  return request(`/orders?portfolioId=${encodeURIComponent(portfolioId)}`);
}

export async function submitOrder(
  portfolioId: string,
  instrument: { exchange: 'NSE' | 'BSE'; symbol: string; instrumentType: 'EQUITY' | 'ETF' | 'INDEX' },
  side: 'BUY' | 'SELL',
  orderType: 'MARKET' | 'LIMIT',
  quantity: number,
  limitPrice?: number,
  idempotencyKey?: string
): Promise<{ order: PaperOrder }> {
  return request('/orders', {
    method: 'POST',
    body: JSON.stringify({
      portfolioId,
      instrument,
      side,
      orderType,
      quantity,
      limitPrice,
      idempotencyKey: idempotencyKey ?? crypto.randomUUID(),
    }),
  });
}

export async function executeOrder(
  orderId: string,
  quote: PaperQuote
): Promise<{ fill: { filled: boolean; fills: unknown[] } }> {
  return request(`/orders/${encodeURIComponent(orderId)}/execute`, {
    method: 'POST',
    body: JSON.stringify({ quote }),
  });
}

export async function cancelOrder(orderId: string): Promise<{ order: PaperOrder }> {
  return request(`/orders/${encodeURIComponent(orderId)}/cancel`, { method: 'POST' });
}

export async function getLedger(portfolioId: string): Promise<{ transactions: PaperCashTransaction[] }> {
  return request(`/ledger?portfolioId=${encodeURIComponent(portfolioId)}`);
}

export async function getMarketStatus(): Promise<{ status: 'open' | 'closed'; timestamp: string }> {
  return request('/market-data/status');
}

export async function getInstruments(): Promise<{ instruments: unknown[] }> {
  return request('/market-data/instruments');
}

export async function getQuote(symbol: string): Promise<PaperQuote | null> {
  try {
    return await request(`/market-data/quote?symbol=${encodeURIComponent(symbol)}`);
  } catch (e) {
    if (e instanceof PaperEngineError && e.status === 404) return null;
    throw e;
  }
}

export async function getBars(
  symbol: string,
  startDate: string,
  endDate: string
): Promise<{ bars: unknown[] }> {
  return request(
    `/market-data/bars?symbol=${encodeURIComponent(symbol)}&start=${encodeURIComponent(startDate)}&end=${encodeURIComponent(endDate)}`
  );
}

export async function getHolidays(): Promise<{ holidays: unknown[] }> {
  return request('/market-data/holidays');
}

# Thozha Paper Trading Module

## Overview
A deterministic, simulation-only autonomous paper-trading engine for NSE/BSE equities and ETFs.

## Safety Boundary
- **PAPER TRADING ONLY** - No real-money order submission
- No broker API integration for order placement
- All accounts, orders, positions, cash balances, and P&L are virtual
- Market data adapters are separated from the simulator

## Architecture
```
paper-trading/
├── src/
│   ├── engine/           # Virtual trading engine
│   ├── risk/             # Risk management
│   ├── strategies/       # Strategy runner
│   ├── data/             # Market data adapters
│   └── db/               # Persistent accounting
├── tests/
│   └── fixtures/         # Historical datasets
└── docs/                 # API contracts
```

## Current Status
**BLOCKED**: No live market data provider configured.
**IMPLEMENTED**: Historical replay, deterministic execution, persistent ledger.

See `SAHAA_PAPER_TRADING_PROGRESS.md` for detailed status.

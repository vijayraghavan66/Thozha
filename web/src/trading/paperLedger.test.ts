import { expect, it } from 'vitest';
import { makeFill, replayFills, startingCash } from './paperLedger';

it('charges paper fees, reconciles cash and positions, and prevents short sales', () => {
  const buy = makeFill({ instrument: 'NIFTYBEES', quantity: 5, side: 'Buy' }, []);
  const sell = makeFill({ instrument: 'NIFTYBEES', quantity: 5, side: 'Sell' }, [buy]);
  const ledger = replayFills([buy, sell]);
  expect(ledger.units.NIFTYBEES).toBe(0);
  expect(ledger.equity).toBe(startingCash - 1000);
  expect(ledger.cash).toBe(ledger.equity);
  expect(() => makeFill({ instrument: 'NIFTYBEES', quantity: 6, side: 'Sell' }, [buy])).toThrow(
    'Not enough paper holdings'
  );
  expect(() => makeFill({ instrument: 'NIFTYBEES', quantity: 500, side: 'Buy' }, [])).toThrow(
    'Insufficient virtual cash'
  );
});

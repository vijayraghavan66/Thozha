import { expect, test } from 'vitest';
import { resolveIdentity } from './motion';

test('a completed reply cannot mask a new supervisor task or approval', () => {
  expect(resolveIdentity('completed', 'executing')).toBe('executing');
  expect(resolveIdentity('completed', 'approval')).toBe('approval');
  expect(resolveIdentity('completed', 'idle')).toBe('completed');
});

test('active chat remains expressive, without masking a connection failure', () => {
  expect(resolveIdentity('thinking', 'executing')).toBe('thinking');
  expect(resolveIdentity('thinking', 'failed')).toBe('failed');
});

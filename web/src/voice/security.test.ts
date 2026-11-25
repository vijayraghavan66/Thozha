// @vitest-environment node
import { describe, expect, it } from 'vitest';
import { validVoiceRequest, equalSecret } from '../../voice/plugin';

describe('local audio boundary', () => {
  const headers = {
    host: '127.0.0.1:5176',
    origin: 'http://127.0.0.1:5176',
    'content-type': 'application/json',
    'sec-fetch-site': 'same-origin',
  };
  it('requires same-origin JSON and rejects foreign sites and DNS rebinding', () => {
    expect(validVoiceRequest({ headers, method: 'POST' })).toBe(true);
    expect(
      validVoiceRequest({ headers: { ...headers, origin: 'https://evil.invalid' }, method: 'POST' })
    ).toBe(false);
    expect(
      validVoiceRequest({ headers: { ...headers, host: 'evil.invalid' }, method: 'POST' })
    ).toBe(false);
    expect(
      validVoiceRequest({ headers: { ...headers, 'sec-fetch-site': 'cross-site' }, method: 'POST' })
    ).toBe(false);
    expect(validVoiceRequest({ headers, method: 'GET' })).toBe(false);
    expect(
      validVoiceRequest({ headers: { ...headers, 'content-type': 'text/plain' }, method: 'POST' })
    ).toBe(false);
  });
  it('does not accept missing, unequal or partial CSRF values', () => {
    expect(equalSecret('', '')).toBe(false);
    expect(equalSecret('token', 'token')).toBe(true);
    expect(equalSecret('token', 'toke')).toBe(false);
    expect(equalSecret('token', 'other')).toBe(false);
  });
});

import { describe, expect, it, vi } from 'vitest';
import { createWebApi } from './client';
import { devicePlatformInventory, inventoryUnavailable, presenceLabel } from './devicePlatform';
import { WebApiError } from './errors';

const id = 'a'.repeat(32);
const observed_at = '2026-11-25T12:00:00Z';
const inventory = {
  contract_version: 1,
  observed_at,
  stale_after_seconds: 30,
  devices: [],
  agents: [],
};

describe('device platform contract', () => {
  it('uses the existing authenticated same-origin session without companion credentials', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(inventory));
    const api = createWebApi(fetcher).authorized('csrf', vi.fn());
    await expect(api.devicePlatformInventory()).resolves.toEqual(inventory);
    expect(fetcher).toHaveBeenCalledWith(
      '/v1/web/device-platform/inventory',
      expect.objectContaining({
        credentials: 'same-origin',
        mode: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
      })
    );
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).has('Authorization')).toBe(false);
  });
  it('does not turn missing or unauthorized APIs into an empty inventory', async () => {
    for (const status of [401, 403, 404, 501, 503]) {
      const unauthorized = vi.fn();
      const api = createWebApi(
        vi.fn<typeof fetch>().mockResolvedValue(Response.json({}, { status }))
      ).authorized('csrf', unauthorized);
      await expect(api.devicePlatformInventory()).rejects.toMatchObject({ status });
      expect(unauthorized).toHaveBeenCalledTimes(status === 401 ? 1 : 0);
      expect(inventoryUnavailable(new WebApiError('http', status))).toBe(
        [404, 501].includes(status)
      );
    }
  });
  it('rejects malformed, duplicate and unversioned inventory', () => {
    const device = {
      device_id: id,
      platform: 'android',
      os_version: null,
      app_version: null,
      presence: { state: 'online', last_seen_at: observed_at },
      capabilities: [],
    };
    expect(devicePlatformInventory.safeParse({ ...inventory, devices: [device] }).success).toBe(
      true
    );
    expect(
      devicePlatformInventory.safeParse({ ...inventory, devices: [device, device] }).success
    ).toBe(false);
    expect(devicePlatformInventory.safeParse({ ...inventory, contract_version: 2 }).success).toBe(
      false
    );
    expect(
      devicePlatformInventory.safeParse({
        ...inventory,
        devices: [{ ...device, capabilities: [{ id: 'shell' }] }],
      }).success
    ).toBe(false);
  });
  it('never presents stale, future or disconnected reports as online', () => {
    const presence = { state: 'online' as const, last_seen_at: observed_at };
    const now = Date.parse(observed_at);
    expect(presenceLabel(presence, inventory, true, now)).toBe('Online');
    expect(presenceLabel(presence, inventory, true, now + 31_000)).toContain('stale');
    expect(presenceLabel(presence, inventory, true, now - 31_000)).toContain('stale');
    expect(presenceLabel(presence, inventory, false, now)).toContain('offline');
  });
});

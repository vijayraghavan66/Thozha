import { z } from 'zod';
import type { JsonRequest } from './client';
import { WebApiError } from './errors';

// Proposed additive contract. Never infer these facts from pairing or device names.
const identifier = z.string().regex(/^[0-9a-f]{32}$/);
const timestamp = z.iso.datetime({ offset: true });
export const platform = z.enum(['macos', 'android', 'windows', 'linux', 'ios', 'unknown']);
const capability = z.object({
  id: z.string().min(1).max(100),
  label: z.string().min(1).max(120),
  supported: z.boolean(),
  authorization: z.enum(['allowed', 'approval_required', 'denied', 'unknown']),
});
const presence = z.object({
  state: z.enum(['online', 'offline', 'unknown']),
  last_seen_at: timestamp.nullable(),
});
const facts = z.object({
  device_id: identifier,
  platform,
  os_version: z.string().max(100).nullable(),
  app_version: z.string().max(100).nullable(),
  presence,
  capabilities: z.array(capability).max(200),
});
const agent = z.object({
  agent_id: identifier,
  device_id: identifier.nullable(),
  name: z.string().min(1).max(120),
  runtime: z.string().min(1).max(100),
  platform,
  version: z.string().max(100).nullable(),
  lifecycle: z.enum(['enrolling', 'active', 'suspended', 'revoked', 'incompatible']),
  presence,
  capabilities: z.array(capability).max(200),
});
export const devicePlatformInventory = z
  .object({
    contract_version: z.literal(1),
    observed_at: timestamp,
    stale_after_seconds: z.number().int().min(1).max(300),
    devices: z.array(facts).max(1000),
    agents: z.array(agent).max(1000),
  })
  .superRefine((value, context) => {
    for (const [key, ids] of [
      ['devices', value.devices.map(item => item.device_id)],
      ['agents', value.agents.map(item => item.agent_id)],
    ] as const) {
      if (new Set(ids).size !== ids.length)
        context.addIssue({ code: 'custom', message: 'Duplicate inventory identity', path: [key] });
    }
  });
export type DevicePlatformInventory = z.infer<typeof devicePlatformInventory>;
export type DeviceFacts = z.infer<typeof facts>;
export type ExecutionAgent = z.infer<typeof agent>;
export type Capability = z.infer<typeof capability>;

export function createDevicePlatformApi(request: JsonRequest) {
  return {
    devicePlatformInventory: (signal?: AbortSignal) =>
      request('/v1/web/device-platform/inventory', 200, devicePlatformInventory, { signal }),
  };
}

export function inventoryUnavailable(error: unknown) {
  return error instanceof WebApiError && [404, 501].includes(error.status ?? 0);
}

export function presenceLabel(
  value: DeviceFacts['presence'],
  inventory: Pick<DevicePlatformInventory, 'observed_at' | 'stale_after_seconds'>,
  connected: boolean,
  now = Date.now()
) {
  if (!connected) return 'Unknown · browser offline';
  const age = now - Date.parse(inventory.observed_at);
  if (age > inventory.stale_after_seconds * 1000 || age < -30_000)
    return 'Unknown · report is stale';
  return value.state === 'online' ? 'Online' : value.state === 'offline' ? 'Offline' : 'Unknown';
}

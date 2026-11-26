import { z } from 'zod';
import type { Exchange, JsonRequest } from './client';
import { WebApiError } from './errors';
import type { components, paths } from './generated';
import { matchesSha256 } from './integrity';
import { readEventStream } from './sse';

type Schemas = components['schemas'];
export type TaskInput = Schemas['WebTaskCreate'];
export type TaskScope = TaskInput['capability_scope'][number];
export type ApprovalInput = Schemas['WebApprovalDecision'];
export type AutomationInput = Schemas['WebAutomationCreate'];
export type AutomationPatch = Schemas['WebAutomationUpdate'];
export type AutomationAction = 'pause' | 'enable' | 'delete' | 'manual_trigger';

const id = z.string().regex(/^[0-9a-f]{32}$/);
const digest = z.string().regex(/^[0-9a-f]{64}$/);
const date = z.iso.datetime({ offset: true });
const sequence = z.number().int().nonnegative();
const object = z.record(z.string(), z.unknown());
const componentReadiness = z.object({
  state: z.enum(['ready', 'unavailable', 'unknown']),
  reason: z.string().optional(),
});
const readiness = z.object({
  status: z.enum(['ready', 'degraded']),
  components: z.object({
    authentication: z.object({
      browser_session: z.literal('authenticated'),
      codex_sdk: z.enum([
        'not_reported',
        'missing',
        'credential_present_unverified',
        'verified_this_process',
        'unavailable',
      ]),
    }),
    device_mapping: componentReadiness.extend({
      active_device_count: z.number().int().nonnegative(),
    }),
    engine: componentReadiness.extend({ check: z.enum(['preflight', 'runtime']).optional() }),
    supervisor_worker: componentReadiness,
    automation_scheduler: componentReadiness,
  }),
}) satisfies z.ZodType<Schemas['WebSupervisorReadiness']>;
export type SupervisorReadiness = z.infer<typeof readiness>;
export const taskState = z.enum([
  'CREATED',
  'PLANNING',
  'RUNNING',
  'VERIFYING',
  'WAITING_APPROVAL',
  'PAUSED',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
]);
const task = z.object({
  task_id: id,
  state: taskState,
  summary: z.string(),
  result_text: z.string().nullish(),
  result_digest: digest.nullish(),
  idempotent_replay: z.boolean(),
  error_code: z.string().nullish(),
}) satisfies z.ZodType<Schemas['WebTaskResult']>;
// b47142d5 returns cancellation facts, not a WebTaskResult.
const cancellation = z.object({
  task_id: id,
  state: taskState,
  cancelled: z.boolean(),
  action_may_have_started: z.boolean(),
});
const event = z.object({
  seq: sequence.positive(),
  event_type: z.string().regex(/^[A-Za-z0-9_.-]{1,100}$/),
  created_at: date,
  payload: object,
}) satisfies z.ZodType<Schemas['WebTaskEvent']>;
const eventPage = z.object({
  task_id: id,
  events: z.array(event),
  next_seq: sequence,
}) satisfies z.ZodType<Schemas['WebTaskEventPage']>;
const approval = z.object({
  id,
  task_id: id,
  operation: z.string(),
  requester_device_id: id,
  target_resource: z.string(),
  canonical_arguments: object.optional(),
  operation_digest: digest,
  reason: z.string().optional(),
  status: z.string(),
  created_at: date,
  expires_at: date,
}) satisfies z.ZodType<Schemas['Approval']>;
const decision = z.object({
  status: z.string(),
  executed: z.boolean().optional(),
  success: z.boolean().optional(),
  execution_state: z.string().optional(),
});
const device = z.object({
  device_id: id,
  device_name: z.string(),
  created_at: date,
  expires_at: date,
  status: z.literal('active'),
}) satisfies z.ZodType<Schemas['WebDevice']>;
const fingerprint = z.string().regex(/^sha256:[0-9a-f]{64}$/);
const pairingQrPayload = z
  .object({
    protocol: z.literal('openjarvis-pairing-v2'),
    server_url: z.string().url(),
    gateway_address: z.string().regex(/^(?:\d{1,3}\.){3}\d{1,3}$/),
    gateway_port: z.enum(['8443', '8444']),
    loopback_url: z.string().url().optional(),
    gateway_certificate_sha256: fingerprint,
    server_fingerprint: fingerprint,
    server_public_key: z.string().min(1),
    invitation_id: id,
    challenge: z.string().min(1),
    expires_at: date,
    server_signature: z.string().min(1),
  })
  .strict() satisfies z.ZodType<Schemas['PairingQrPayloadV2']>;
const pairingInvitation = z
  .object({
    protocol: z.literal('openjarvis-pairing-v2'),
    server_url: z.string().url(),
    gateway_address: z.string().regex(/^(?:\d{1,3}\.){3}\d{1,3}$/),
    gateway_port: z.enum(['8443', '8444']),
    loopback_url: z.string().url().optional(),
    gateway_certificate_sha256: fingerprint,
    server_fingerprint: fingerprint,
    server_public_key: z.string().min(1),
    invitation_id: id,
    challenge: z.string().min(1),
    expires_at: date,
    server_signature: z.string().min(1),
    qr_payload: pairingQrPayload,
  })
  .strict() satisfies z.ZodType<Schemas['WebPairingInvitationResponse']>;
const pairingRequest = z.object({
  id,
  status: z.literal('PENDING_CONFIRMATION'),
  device_name: z.string().max(80),
  device_fingerprint: z.string().min(1),
  requested_at: date.nullable().optional(),
  expires_at: date,
}) satisfies z.ZodType<Schemas['WebPairingRequest']>;
const pairingRequestList = z.object({
  requests: z.array(pairingRequest),
  count: z.number().int().nonnegative(),
}) satisfies z.ZodType<Schemas['WebPairingRequestList']>;
const pairingDecisionResult = z.object({
  invitation_id: id,
  status: z.enum(['APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED']),
}) satisfies z.ZodType<Schemas['WebPairingDecisionResult']>;
const pairingInvitationStatus = z.object({
  invitation_id: id,
  status: z.enum([
    'INVITED',
    'PENDING_CONFIRMATION',
    'APPROVED',
    'REJECTED',
    'EXPIRED',
    'CANCELLED',
    'TOKEN_ISSUED',
  ]),
}) satisfies z.ZodType<Schemas['WebPairingInvitationStatus']>;
const revoke = z.object({
  status: z.enum(['revoked', 'already_revoked']),
  device_id: id,
  notification_token_cleanup: z.enum(['none', 'revoked']),
}) satisfies z.ZodType<Schemas['WebDeviceRevokeResult']>;
const maxZipBytes = 5 * 1024 * 1024;
const artifact = z.object({
  artifact_id: digest,
  task_id: id,
  file_name: z.string(),
  size_bytes: z.number().int().positive().max(maxZipBytes),
  archive_sha256: digest,
  manifest_sha256: digest,
  files: z.array(object),
  download_path: z.string(),
}) satisfies z.ZodType<Schemas['Artifact']>;
const scheduleId = z.string().regex(/^[0-9a-f]{16}$/);
const automation = z.object({
  id: scheduleId,
  prompt: z.string(),
  schedule_type: z.enum(['once', 'interval', 'cron']),
  schedule_value: z.string(),
  timezone: z.string().nullish(),
  status: z.enum(['active', 'paused', 'completed', 'cancelled']),
  next_run: date.nullish(),
  last_run: date.nullish(),
  last_supervisor_task_id: id.nullable(),
}) satisfies z.ZodType<Schemas['WebAutomation']>;
const capabilities = z.object({
  status: z.literal('available'),
  operations: z.record(z.string(), z.boolean()),
  unavailable_reasons: z.record(z.string(), z.string()),
}) satisfies z.ZodType<Schemas['WebAutomationCapabilities']>;
const automationDetail = z.object({
  automation,
  latest_supervisor_task: object.nullable(),
}) satisfies z.ZodType<Schemas['WebAutomationDetail']>;
const automationRun = z.object({
  started_at: date,
  finished_at: date.nullable(),
  success: z.boolean(),
  state: z.string(),
  supervisor_task_id: id.nullish(),
  result: z.string(),
  error: z.string(),
  supervisor_task: object.nullable(),
}) satisfies z.ZodType<Schemas['WebAutomationRun']>;
// The runtime acknowledges mutations with a receipt; refresh detail for persisted state.
const automationReceipt = z.object({
  status: z.string(),
  supervisor_task_id: id.nullish(),
  state: z.string().optional(),
});

export type SupervisorTask = z.infer<typeof task>;
export type SupervisorEvent = z.infer<typeof event>;
export type PairedDevice = z.infer<typeof device>;
export type PairingInvitation = z.infer<typeof pairingInvitation>;
export type PairingInvitationStatus = z.infer<typeof pairingInvitationStatus>;
export type PairingRequest = z.infer<typeof pairingRequest>;
export type PairingDecision = Schemas['WebPairingDecision']['decision'];
export type SupervisorArtifact = z.infer<typeof artifact>;
export type WebAutomation = z.infer<typeof automation>;
export type AutomationCapabilities = z.infer<typeof capabilities>;

const tasksPath: keyof paths = '/v1/web/tasks';
const pendingPath: keyof paths = '/v1/web/approvals/pending';
const approvalsPath = '/v1/web/approvals';
const devicesPath: keyof paths = '/v1/web/devices';
const pairingInvitationsPath: keyof paths = '/v1/web/devices/pairing-invitations';
const pairingRequestsPath: keyof paths = '/v1/web/devices/pairing-requests';
const automationsPath: keyof paths = '/v1/web/automations';
const authorityPath: keyof paths = '/v1/web/automations/capabilities';
const taskPath = (taskId: string) => `${tasksPath}/${encodeURIComponent(taskId)}`;
const approvalPath = (approvalId: string) => `${approvalsPath}/${encodeURIComponent(approvalId)}`;
const automationPath = (automationId: string) =>
  `${automationsPath}/${encodeURIComponent(automationId)}`;

function sameId<T>(result: T, actual: string, expected: string): T {
  if (actual !== expected) throw new WebApiError('protocol');
  return result;
}

async function verifiedZip(response: Response, item: SupervisorArtifact, signal: AbortSignal) {
  const length = response.headers.get('content-length');
  if (
    !response.body ||
    response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !==
      'application/zip' ||
    response.headers.get('x-content-sha256') !== item.archive_sha256 ||
    (length !== null && Number(length) !== item.size_bytes)
  )
    throw new WebApiError('integrity');
  const bytes = new Uint8Array(item.size_bytes);
  const reader = response.body.getReader();
  let count = 0;
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      if (count + chunk.value.length > bytes.length) throw new WebApiError('integrity');
      bytes.set(chunk.value, count);
      count += chunk.value.length;
    }
  } catch (error) {
    if (signal.aborted) throw signal.reason;
    throw error instanceof WebApiError ? error : new WebApiError('connection');
  } finally {
    void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  if (count !== bytes.length || !(await matchesSha256(bytes, item.archive_sha256)))
    throw new WebApiError('integrity');
  return new Blob([bytes], { type: 'application/zip' });
}

export function createControlApi(request: JsonRequest, exchange: Exchange, csrf: string) {
  const write = { method: 'POST', body: {}, csrf } as const;
  return {
    supervisorReadiness: (signal?: AbortSignal) =>
      request('/v1/web/supervisor/readiness' satisfies keyof paths, 200, readiness, { signal }),
    listTasks: (state: 'active' | 'completed' | 'all', limit: number, signal?: AbortSignal) =>
      request(
        `${tasksPath}?state=${state}&limit=${limit}`,
        200,
        z.object({ tasks: z.array(task) }),
        { signal }
      ),
    submitTask: (body: TaskInput) => request(tasksPath, [200, 202], task, { ...write, body }),
    getTask: (taskId: string, signal?: AbortSignal) =>
      request(taskPath(taskId), 200, task, { signal }).then(data =>
        sameId(data, data.task_id, taskId)
      ),
    cancelTask: (taskId: string) =>
      request(`${taskPath(taskId)}/cancel`, 200, cancellation, write).then(data =>
        sameId(data, data.task_id, taskId)
      ),
    getTaskEvents: async (taskId: string, after: number, signal?: AbortSignal) => {
      const data = await request(
        `${taskPath(taskId)}/events?after_seq=${after}&limit=200`,
        200,
        eventPage,
        { signal }
      );
      let cursor = after;
      for (const item of data.events) {
        if (item.seq <= cursor) throw new WebApiError('protocol');
        cursor = item.seq;
      }
      if (data.next_seq !== cursor) throw new WebApiError('protocol');
      return sameId(data, data.task_id, taskId);
    },
    streamTaskEvents: (
      taskId: string,
      after: number,
      signal: AbortSignal,
      onOpen: () => void,
      onEvent: (item: SupervisorEvent) => void
    ) =>
      exchange(
        `${taskPath(taskId)}/events/stream?after_seq=${after}`,
        200,
        { signal, cursor: after, stream: true },
        async (response, streamSignal) => {
          if (
            !response.body ||
            !response.headers.get('content-type')?.toLowerCase().startsWith('text/event-stream')
          )
            throw new WebApiError('protocol');
          onOpen();
          let cursor = after;
          try {
            await readEventStream(
              response.body,
              frame => {
                const parsed = event.safeParse(frame.data);
                const item = parsed.data;
                if (!item || frame.id !== String(item.seq) || frame.event !== item.event_type)
                  throw new WebApiError('protocol');
                if (item.seq <= cursor) return;
                cursor = item.seq;
                onEvent(item);
              },
              30_000
            );
          } catch (error) {
            if (streamSignal.aborted) throw streamSignal.reason;
            if (error instanceof WebApiError) throw error;
            throw new WebApiError(error instanceof SyntaxError ? 'protocol' : 'connection');
          }
        }
      ),
    listApprovals: (signal?: AbortSignal) =>
      request(pendingPath, 200, z.object({ approvals: z.array(approval), count: sequence }), {
        signal,
      }),
    getApproval: (approvalId: string, signal?: AbortSignal) =>
      request(approvalPath(approvalId), 200, approval, { signal }).then(data =>
        sameId(data, data.id, approvalId)
      ),
    decideApproval: (approvalId: string, body: ApprovalInput) =>
      request(`${approvalPath(approvalId)}/decision`, 200, decision, { ...write, body }),
    listDevices: (signal?: AbortSignal) =>
      request(devicesPath, 200, z.object({ devices: z.array(device), count: sequence }), {
        signal,
      }),
    getDevice: (deviceId: string, signal?: AbortSignal) =>
      request(`${devicesPath}/${encodeURIComponent(deviceId)}`, 200, device, { signal }).then(
        data => sameId(data, data.device_id, deviceId)
      ),
    revokeDevice: (deviceId: string) =>
      request(`${devicesPath}/${encodeURIComponent(deviceId)}/revoke`, 200, revoke, {
        ...write,
        body: { confirm: true },
      }).then(data => sameId(data, data.device_id, deviceId)),
    createPairingInvitation: () =>
      request(pairingInvitationsPath, 200, pairingInvitation, {
        ...write,
        body: { transport: 'wifi' } satisfies Schemas['WebPairingInvitationCreate'],
      }),
    getPairingInvitationStatus: (invitationId: string, signal?: AbortSignal) =>
      request(
        `${pairingInvitationsPath}/${encodeURIComponent(invitationId)}`,
        200,
        pairingInvitationStatus,
        { signal }
      ).then(data => sameId(data, data.invitation_id, invitationId)),
    cancelPairingInvitation: (invitationId: string) =>
      request(
        `${pairingInvitationsPath}/${encodeURIComponent(invitationId)}/cancel`,
        200,
        pairingDecisionResult,
        { ...write, body: {} satisfies Schemas['WebPairingCancelRequest'] }
      ).then(data => sameId(data, data.invitation_id, invitationId)),
    listPairingRequests: (limit = 50, signal?: AbortSignal) =>
      request(
        `${pairingRequestsPath}?${new URLSearchParams({ limit: String(limit) })}`,
        200,
        pairingRequestList,
        { signal }
      ),
    decidePairingRequest: (invitationId: string, decision: PairingDecision) =>
      request(
        `${pairingRequestsPath}/${encodeURIComponent(invitationId)}/decision`,
        200,
        pairingDecisionResult,
        { ...write, body: { decision } satisfies Schemas['WebPairingDecision'] }
      ).then(data => sameId(data, data.invitation_id, invitationId)),
    listArtifacts: async (taskId: string, signal?: AbortSignal) => {
      const data = await request(
        `${taskPath(taskId)}/artifacts`,
        200,
        z.object({
          task_id: id,
          artifacts: z.array(artifact),
        }),
        { signal }
      );
      if (data.artifacts.some(item => item.task_id !== taskId)) throw new WebApiError('protocol');
      return sameId(data, data.task_id, taskId);
    },
    downloadArtifact: (item: SupervisorArtifact, signal?: AbortSignal) =>
      exchange(
        `${taskPath(item.task_id)}/artifacts/${encodeURIComponent(item.artifact_id)}/download`,
        200,
        { signal, accept: 'application/zip' },
        (response, downloadSignal) => verifiedZip(response, item, downloadSignal)
      ),
    automationCapabilities: (signal?: AbortSignal) =>
      request(authorityPath, 200, capabilities, { signal }),
    listAutomations: (signal?: AbortSignal) =>
      request(automationsPath, 200, z.object({ items: z.array(automation) }), { signal }),
    getAutomation: (automationId: string, signal?: AbortSignal) =>
      request(automationPath(automationId), 200, automationDetail, { signal }).then(data =>
        sameId(data, data.automation.id, automationId)
      ),
    automationHistory: (automationId: string, signal?: AbortSignal) =>
      request(
        `${automationPath(automationId)}/history`,
        200,
        z.object({ items: z.array(automationRun) }),
        { signal }
      ),
    createAutomation: (body: AutomationInput, key: string) =>
      request(automationsPath, 202, z.object({ status: z.literal('created'), automation }), {
        ...write,
        body,
        key,
      }),
    updateAutomation: (automationId: string, body: AutomationPatch) =>
      request(automationPath(automationId), 200, automationReceipt, {
        ...write,
        method: 'PATCH',
        body,
      }),
    actOnAutomation: (automationId: string, action: AutomationAction, key?: string) => {
      const path = automationPath(automationId);
      return request(
        action === 'delete' ? path : `${path}/${action === 'manual_trigger' ? 'trigger' : action}`,
        200,
        automationReceipt,
        { ...write, method: action === 'delete' ? 'DELETE' : 'POST', key }
      );
    },
  };
}

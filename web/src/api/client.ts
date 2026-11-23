import { z } from 'zod';
import type { components, paths } from './generated';
import type { WebConversationSseEvent } from '../../../contracts/web-api/v1';
import { readConversationStream } from './sse';

type Schemas = components['schemas'];
export type LoginInput = Schemas['WebLoginRequest'];
export type ConversationInput = Schemas['WebConversationCreate'];
export type MemoryInput = Schemas['WebMemoryCreate'];
const dateTime = z.iso.datetime({ offset: true });

const sessionContract = z.object({
  user_id: z.string().min(1),
  expires_at: z.number().int().positive(),
  csrf_token: z.string().min(1),
}) satisfies z.ZodType<Schemas['WebSession']>;
const conversationContract = z.object({
  id: z.uuid(),
  title: z.string(),
  created_at: dateTime,
  updated_at: dateTime,
  archived_at: dateTime.nullable(),
}) satisfies z.ZodType<Schemas['WebConversation']>;
const responseStatus = z.enum(['queued', 'running', 'completed', 'failed', 'cancelled']);
const messageContract = z.object({
  id: z.uuid(),
  conversation_id: z.uuid(),
  role: z.enum(['user', 'assistant']),
  content: z.string(),
  status: z.union([z.literal('accepted'), responseStatus]),
  response_to: z.uuid().nullable(),
  error_code: z.string().nullable(),
  created_at: dateTime,
  updated_at: dateTime,
}) satisfies z.ZodType<Schemas['WebMessage']>;
const eventContract = z.object({
  sequence: z.number().int(),
  message_id: z.uuid(),
  created_at: dateTime,
  event_type: z.enum([
    'accepted',
    'queued',
    'started',
    'completed',
    'failed',
    'cancelled',
    'cancellation_requested',
  ]),
  details: z.record(z.string(), z.unknown()),
}) satisfies z.ZodType<Schemas['WebConversationEvent']>;
const detailContract = conversationContract.extend({
  messages: z.array(messageContract),
  events: z.array(eventContract),
}) satisfies z.ZodType<Schemas['WebConversationDetail']>;
const acceptedContract = z.object({
  conversation_id: z.uuid(),
  message_id: z.uuid(),
  response_id: z.uuid(),
  status: responseStatus,
  replayed: z.boolean(),
}) satisfies z.ZodType<Schemas['WebMessageAccepted']>;
const cancelledContract = z.object({
  conversation_id: z.uuid(),
  message_id: z.uuid(),
  status: z.union([responseStatus, z.literal('cancellation_requested')]),
}) satisfies z.ZodType<Schemas['WebMessageCancelResult']>;
const memoryContract = z.object({
  id: z.string().regex(/^[0-9a-f]{32}$/),
  title: z.string(),
  content: z.string(),
  tags: z.array(z.string()),
  created_at: dateTime,
  updated_at: dateTime,
}) satisfies z.ZodType<Schemas['WebMemory']>;
const memoryList = z.object({
  items: z.array(memoryContract),
  search_mode: z.literal('substring'),
}) satisfies z.ZodType<Schemas['WebMemoryList']>;
const memoryCreated = z.object({
  memory: memoryContract,
  replayed: z.boolean(),
}) satisfies z.ZodType<Schemas['WebMemoryCreateResult']>;

export type WebSession = z.infer<typeof sessionContract>;
export type Conversation = z.infer<typeof conversationContract>;
export type ConversationDetail = z.infer<typeof detailContract>;
export type WebMessage = z.infer<typeof messageContract>;
export type ConversationEvent = z.infer<typeof eventContract>;
export type WebMemory = z.infer<typeof memoryContract>;

export class WebApiError extends Error {
  constructor(
    readonly kind: 'http' | 'connection' | 'timeout' | 'protocol',
    readonly status?: number
  ) {
    super(kind === 'http' ? `Browser API returned HTTP ${status}` : `Browser API ${kind} error`);
    this.name = 'WebApiError';
  }
}

export function isUnauthorized(error: unknown) {
  return error instanceof WebApiError && error.status === 401;
}

export function mayHaveBeenAccepted(error: unknown) {
  return error instanceof WebApiError && (error.kind !== 'http' || (error.status ?? 0) >= 500);
}

export function errorText(error: unknown, action = 'complete this request'): string {
  if (!(error instanceof WebApiError)) return `Could not ${action}. Please try again.`;
  if (error.kind === 'protocol')
    return 'The server returned an unexpected response. Check the Web v1 proxy and backend version.';
  if (error.kind === 'timeout') return `The server took too long to ${action}.`;
  if (error.kind === 'connection')
    return 'The browser could not reach the workspace API. Check your connection and retry.';
  switch (error.status) {
    case 401:
      return 'Your Web session is no longer valid. Sign in again.';
    case 403:
      return 'The server rejected the request. Check your session and the configured Web origin.';
    case 404:
      return 'This resource is no longer available, or the Web route is not mounted.';
    case 409:
      return 'The request conflicts with server state. Refresh the saved data before continuing.';
    case 413:
      return 'The browser memory storage limit was reached. Delete an entry before saving another.';
    case 422:
      return 'The server did not accept the input. Review it and try again.';
    case 429:
      return 'Too many attempts. Wait a few minutes before trying again.';
    case 503:
      return 'The Web API is unavailable. Confirm browser authentication is configured on the backend.';
    default:
      return `Could not ${action} (HTTP ${error.status}). Please try again.`;
  }
}

const loginPath: keyof paths = '/v1/web/session/login';
const sessionPath: keyof paths = '/v1/web/session';
const logoutPath: keyof paths = '/v1/web/session/logout';
const conversationsPath: keyof paths = '/v1/web/conversations';
const memoryPath: keyof paths = '/v1/web/memory';
const memorySearchPath: keyof paths = '/v1/web/memory/search';
const detailPath = (id: string) => `${conversationsPath}/${encodeURIComponent(id)}`;
const requestTimeout = 15_000;
type Options = {
  method?: 'POST' | 'DELETE';
  body?: object;
  csrf?: string;
  key?: string;
  signal?: AbortSignal;
  stream?: boolean;
};

export function createWebApi(fetcher: typeof fetch = globalThis.fetch.bind(globalThis)) {
  async function exchange<T>(
    path: string,
    expectedStatus: number,
    options: Options,
    consume: (response: Response, signal: AbortSignal) => Promise<T>
  ): Promise<T> {
    const { signal, method = 'GET', body, csrf, key, stream = false } = options;
    const headers = new Headers({ Accept: stream ? 'text/event-stream' : 'application/json' });
    if (body !== undefined) headers.set('Content-Type', 'application/json');
    if (csrf) headers.set('X-Thozha-CSRF', csrf);
    if (key) headers.set('Idempotency-Key', key);
    const controller = new AbortController();
    const abort = () => controller.abort(signal?.reason);
    if (signal?.aborted) abort();
    signal?.addEventListener('abort', abort, { once: true });
    const timeout = setTimeout(() => controller.abort(new WebApiError('timeout')), requestTimeout);
    try {
      const response = await fetcher(path, {
        method,
        headers,
        signal: controller.signal,
        credentials: 'same-origin',
        mode: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        body: body === undefined ? undefined : JSON.stringify(body),
      }).catch(() => {
        throw controller.signal.aborted ? controller.signal.reason : new WebApiError('connection');
      });
      if (!response.ok) throw new WebApiError('http', response.status);
      if (response.status !== expectedStatus) throw new WebApiError('protocol');
      if (stream) clearTimeout(timeout);
      return await consume(response, controller.signal);
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener('abort', abort);
    }
  }

  function request<S extends z.ZodType>(
    path: string,
    expectedStatus: number,
    schema: S,
    options: Options = {}
  ): Promise<z.output<S>> {
    return exchange(path, expectedStatus, options, async (response, signal) => {
      if (schema instanceof z.ZodVoid) return schema.parse(undefined);
      if (!response.headers.get('content-type')?.toLowerCase().startsWith('application/json')) {
        throw new WebApiError('protocol');
      }
      const json: unknown = await response.json().catch(() => {
        throw signal.aborted ? signal.reason : new WebApiError('protocol');
      });
      const parsed = schema.safeParse(json);
      if (!parsed.success) throw new WebApiError('protocol');
      return parsed.data;
    });
  }

  return {
    login: (body: LoginInput, signal?: AbortSignal) =>
      request(loginPath, 200, sessionContract, { method: 'POST', body, signal }),
    session: (signal?: AbortSignal) => request(sessionPath, 200, sessionContract, { signal }),
    authorized(csrf: string, onUnauthorized: () => void) {
      async function auth<T>(operation: Promise<T>) {
        try {
          return await operation;
        } catch (error) {
          if (isUnauthorized(error)) onUnauthorized();
          throw error;
        }
      }
      const mutation = { method: 'POST', csrf, body: {} } as const;
      return {
        logout: () => request(logoutPath, 204, z.void(), mutation),
        list: (signal?: AbortSignal) =>
          auth(
            request(
              `${conversationsPath}?include_archived=true`,
              200,
              z.object({ items: z.array(conversationContract) }),
              { signal }
            )
          ),
        create: (body: ConversationInput) =>
          auth(request(conversationsPath, 201, conversationContract, { ...mutation, body })),
        get: (id: string, signal?: AbortSignal) =>
          auth(request(detailPath(id), 200, detailContract, { signal })),
        archive: (id: string) =>
          auth(request(`${detailPath(id)}/archive`, 200, z.void(), mutation)),
        delete: (id: string) =>
          auth(request(detailPath(id), 204, z.void(), { ...mutation, method: 'DELETE' })),
        submit: (id: string, body: Schemas['WebMessageCreate'], key: string) =>
          auth(
            request(`${detailPath(id)}/messages`, 202, acceptedContract, { ...mutation, body, key })
          ),
        stream: (
          id: string,
          body: Schemas['WebMessageCreate'],
          key: string,
          signal: AbortSignal,
          onEvent: (event: WebConversationSseEvent) => void
        ) =>
          auth(
            exchange(
              `${detailPath(id)}/messages/stream`,
              200,
              { ...mutation, body, key, signal, stream: true },
              async (response, streamSignal) => {
                if (
                  !response.body ||
                  !response.headers
                    .get('content-type')
                    ?.toLowerCase()
                    .startsWith('text/event-stream')
                ) {
                  throw new WebApiError('protocol');
                }
                await readConversationStream(response.body, id, onEvent).catch(() => {
                  throw streamSignal.aborted ? streamSignal.reason : new WebApiError('connection');
                });
              }
            )
          ),
        cancel: (id: string, responseId: string) =>
          auth(
            request(
              `${detailPath(id)}/messages/${encodeURIComponent(responseId)}/cancel`,
              200,
              cancelledContract,
              mutation
            )
          ),
        listMemories: (limit: number, offset: number, signal?: AbortSignal) =>
          auth(
            request(
              `${memoryPath}?${new URLSearchParams({ limit: String(limit), offset: String(offset) })}`,
              200,
              memoryList,
              { signal }
            )
          ),
        searchMemories: (query: string, limit: number, signal?: AbortSignal) =>
          auth(
            request(
              `${memorySearchPath}?${new URLSearchParams({ q: query, limit: String(limit) })}`,
              200,
              memoryList,
              { signal }
            )
          ),
        getMemory: (id: string, signal?: AbortSignal) =>
          auth(request(`${memoryPath}/${encodeURIComponent(id)}`, 200, memoryContract, { signal })),
        createMemory: (body: MemoryInput, key: string) =>
          auth(request(memoryPath, 201, memoryCreated, { ...mutation, body, key })),
        deleteMemory: (id: string) =>
          auth(
            request(`${memoryPath}/${encodeURIComponent(id)}`, 204, z.void(), {
              ...mutation,
              method: 'DELETE',
            })
          ),
      };
    },
  };
}

export type WebApi = ReturnType<typeof createWebApi>;
export type WorkspaceApi = ReturnType<WebApi['authorized']>;
